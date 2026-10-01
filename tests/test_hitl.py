"""Human-in-the-Loop 门控测试（Agent Engineering V0.2 §6，离线）。

覆盖：headless 自动拒绝（benchmark 零改动）、interrupt 暂停与 payload、
人工批准覆盖策略应用补丁进验证、人工拒绝、空补丁批准按拒绝处理。
"""

from __future__ import annotations

import subprocess

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from devfix.graph import build_repair_graph
from devfix.models import (
    Patch,
    PatchEdit,
    VerificationLevel,
    VerificationResult,
    VerificationStatus,
)
from tests.sample_logs import JUNIT4_NPE_ERROR
from tests.test_loop_graph import FakeDiagnoser

TEST_FILE = "src/test/java/com/example/OrderServiceTest.java"


class ScriptedPatcher:
    def __init__(self, patches: list[Patch]) -> None:
        self._patches = patches
        self.calls = 0

    def propose(self, diagnosis, bundle, attempt=1, previous_attempts=None,
                feedback=None, evidence=None) -> Patch:
        patch = self._patches[min(self.calls, len(self._patches) - 1)]
        self.calls += 1
        return patch


class ScriptedReflector:
    def reflect(self, diagnosis, patch, verification=None, previous_patches=None,
                verdict=None, hypotheses=None, evidence=None):
        from devfix.models import NextAction, ReflectionResult

        return ReflectionResult(failure_reason="r", next_action=NextAction.REPAIR_AGAIN)


class PassVerifier:
    def verify(self, failure, workspace) -> list[VerificationResult]:
        return [VerificationResult(
            verification_id="VR", level=VerificationLevel.FULL,
            status=VerificationStatus.PASS,
        )]


class AlwaysFailVerifier:
    def verify(self, failure, workspace) -> list[VerificationResult]:
        return [VerificationResult(
            verification_id="VR", level=VerificationLevel.FULL,
            status=VerificationStatus.FAIL, exit_code=1, new_errors=["仍失败"],
        )]


def _patch(file: str, search: str, replace: str) -> Patch:
    return Patch(
        patch_id="P1",
        edits=[PatchEdit(file=file, search=search, replace=replace)],
        reason="r", expected_effect="e",
    )


def _add_test_file(git_repo) -> None:
    """提交一个测试文件到仓库（worktree 需要它存在才能应用补丁）。"""
    path = git_repo / TEST_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "package com.example;\n\nclass OrderServiceTest { void t() {} }\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "test file"],
        cwd=git_repo, check=True, capture_output=True,
    )


def _graph(**kwargs):
    return build_repair_graph(
        diagnoser=FakeDiagnoser(),
        patcher=kwargs["patcher"],
        reflector=ScriptedReflector(),
        verifier=kwargs["verifier"],
        checkpointer=kwargs.get("checkpointer"),
    )


THREAD = "hitl-test"  # 恢复必须使用与首次 invoke 相同的 thread_id


def _invoke(graph, git_repo, tmp_path, hitl: bool, max_attempts: int = 1):
    return graph.invoke(
        {
            "repo": str(git_repo), "log_text": JUNIT4_NPE_ERROR, "base": "HEAD~1",
            "workspace_dir": str(tmp_path / "ws"), "max_attempts": max_attempts,
            "hitl_enabled": hitl,
        },
        {"configurable": {"thread_id": THREAD}, "recursion_limit": 40},
    )


def _resume(graph, action: str):
    return graph.invoke(
        Command(resume={"action": action}),
        {"configurable": {"thread_id": THREAD}},
    )


class TestHeadlessAutoReject:
    def test_no_progress_auto_rejected_without_checkpointer(self, git_repo, tmp_path) -> None:
        """benchmark/headless：无 checkpointer、hitl 关闭——连续语义无进展
        自动拒绝，评测管道行为与 A2 之前一致（零改动）。"""
        empty = Patch(patch_id="P", edits=[], reason="r", cannot_fix_reason="无法修复")
        graph = _graph(patcher=ScriptedPatcher([empty]), verifier=PassVerifier())
        final = _invoke(graph, git_repo, tmp_path, hitl=False, max_attempts=3)
        assert final["human_decision"] == "auto_rejected"
        assert "语义无进展" in final["stop_reason"]
        assert "__interrupt__" not in final  # 从不暂停


class TestHitlInterrupt:
    def test_manual_review_at_max_attempts_pauses_for_human(self, git_repo, tmp_path) -> None:
        """改测试文件的补丁被策略拒绝 + 尝试耗尽 → interrupt，payload 完整。"""
        _add_test_file(git_repo)
        # 模型给出改测试的补丁（策略 MANUAL_REVIEW）——正是需要人工裁决的场景
        patcher = ScriptedPatcher([_patch(TEST_FILE, "void t() {}", "void t() { int x = 1; }")])
        graph = _graph(
            patcher=patcher, verifier=PassVerifier(),
            checkpointer=MemorySaver(),
        )
        final = _invoke(graph, git_repo, tmp_path, hitl=True, max_attempts=1)
        interrupts = final.get("__interrupt__")
        assert interrupts, "策略拒绝 + 尝试耗尽必须暂停等待人工"
        payload = interrupts[0].value
        assert payload["gate_reason"]
        assert payload["patch_files"] == [TEST_FILE]
        assert payload["cannot_fix"] is False
        assert payload["patch_diff"]  # 人能看到要批准的 diff

    def test_human_approve_overrides_policy_and_verifies(self, git_repo, tmp_path) -> None:
        """人工批准 → 覆盖策略应用补丁 → 进入验证 → FIXED。"""
        _add_test_file(git_repo)
        patcher = ScriptedPatcher([_patch(TEST_FILE, "void t() {}", "void t() { int x = 1; }")])
        graph = _graph(
            patcher=patcher, verifier=PassVerifier(),
            checkpointer=MemorySaver(),
        )
        final = _invoke(graph, git_repo, tmp_path, hitl=True, max_attempts=1)
        assert final.get("__interrupt__")
        final = _resume(graph, "approve")
        assert final["human_decision"] == "approved"
        assert final["verification"].passed
        assert final["patch"].diff  # 补丁真实应用了（人工覆盖策略）

    def test_human_reject_ends_run(self, git_repo, tmp_path) -> None:
        _add_test_file(git_repo)
        patcher = ScriptedPatcher([_patch(TEST_FILE, "void t() {}", "void t() { int x = 1; }")])
        graph = _graph(
            patcher=patcher, verifier=PassVerifier(),
            checkpointer=MemorySaver(),
        )
        final = _invoke(graph, git_repo, tmp_path, hitl=True, max_attempts=1)
        assert final.get("__interrupt__")
        final = _resume(graph, "reject")
        assert final["human_decision"] == "rejected"
        assert "人工拒绝" in final["stop_reason"]

    def test_approving_empty_patch_coerced_to_reject(self, git_repo, tmp_path) -> None:
        """空补丁（cannot_fix）没有可应用内容——批准也按拒绝处理。"""
        empty = Patch(patch_id="P", edits=[], reason="r", cannot_fix_reason="无法修复")
        graph = _graph(patcher=ScriptedPatcher([empty]), verifier=PassVerifier(), checkpointer=MemorySaver())
        final = _invoke(graph, git_repo, tmp_path, hitl=True, max_attempts=1)
        assert final.get("__interrupt__")
        payload = final["__interrupt__"][0].value
        assert payload["cannot_fix"] is True
        final = _resume(graph, "approve")
        assert final["human_decision"] == "rejected"
        assert "空补丁" in final["stop_reason"]
