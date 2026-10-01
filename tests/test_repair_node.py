"""Repair 节点测试：生成 → 策略 → 应用/回滚的完整编排（FakePatcher 注入）。"""

from __future__ import annotations

import os

import pytest

from devfix.models import (
    Diagnosis,
    Patch,
    PatchEdit,
    RepairDecision,
)
from devfix.nodes.repair import render_repair_message, repair_node
from devfix.parsing import parse_maven_log
from devfix.repair import RepairPolicy
from tests.sample_logs import JUNIT4_NPE_ERROR

SERVICE = "src/main/java/com/example/OrderService.java"
OLD_LINE = '    public String greet(String name) { return "hi " + name; }'
NEW_LINE = '    public String greet(String name) { return "hello " + name; }'


class FakePatcher:
    def __init__(self, patch: Patch | None = None, fail: Exception | None = None):
        self.patch = patch
        self.fail = fail
        self.calls: list[tuple[int, list[str] | None]] = []

    def propose(
        self, diagnosis, bundle, attempt=1, previous_attempts=None, feedback=None,
        evidence=None,
    ) -> Patch:
        self.calls.append((attempt, previous_attempts))
        if self.fail:
            raise self.fail
        assert self.patch is not None
        return self.patch


def _state(git_repo, git_worktree, **extra) -> dict:
    failure, triage = parse_maven_log(JUNIT4_NPE_ERROR)
    from devfix.nodes.context_collector import collect_context

    bundle = collect_context(failure, triage, repo=git_repo, base="HEAD~1")
    state = {
        "context_bundle": bundle,
        "diagnosis": Diagnosis(summary="s", root_cause="r", confidence=0.9),
        "workspace": str(git_worktree),
    }
    state.update(extra)
    return state


def _good_patch() -> Patch:
    return Patch(
        edits=[PatchEdit(file=SERVICE, search=OLD_LINE, replace=NEW_LINE)],
        reason="恢复原始返回语义",
        expected_effect="greet 返回 hello 前缀",
    )


class TestRepairNodeHappyPath:
    def test_patch_applied_and_diff_attached(self, git_repo, git_worktree) -> None:
        out = repair_node(_state(git_repo, git_worktree), FakePatcher(_good_patch()))
        assert out["policy_verdict"].decision is RepairDecision.AUTO_REPAIR
        assert out["patch"].changed_files == [SERVICE]
        assert "hello " in out["patch"].diff
        # 修改确实落在隔离工作区
        assert NEW_LINE in (git_worktree / SERVICE).read_text(encoding="utf-8")
        # 主仓库不受影响
        assert OLD_LINE in (git_repo / SERVICE).read_text(encoding="utf-8")


class TestRepairNodeGuards:
    def test_patcher_exception_goes_to_error(self, git_repo, git_worktree) -> None:
        out = repair_node(
            _state(git_repo, git_worktree),
            FakePatcher(fail=RuntimeError("LLM 超时")),
        )
        assert "补丁生成失败" in out["error"]

    def test_missing_workspace(self, git_repo) -> None:
        state = _state(git_repo, git_repo)
        state.pop("workspace")
        out = repair_node(state, FakePatcher(_good_patch()))
        assert "缺少 workspace" in out["error"]

    def test_forbidden_file_not_applied(self, git_repo, git_worktree) -> None:
        target = git_worktree / "src/test/java/com/example/OrderServiceTest.java"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("package com.example;\n\nclass OrderServiceTest { }\n", encoding="utf-8")
        before = target.read_text(encoding="utf-8")

        patch = Patch(edits=[PatchEdit(
            file="src/test/java/com/example/OrderServiceTest.java",
            search="class OrderServiceTest { }", replace="class OrderServiceTest { /* hacked */ }",
        )])
        out = repair_node(_state(git_repo, git_worktree), FakePatcher(patch))
        assert out["policy_verdict"].decision is RepairDecision.MANUAL_REVIEW
        assert out["patch"].changed_files == []          # 未应用
        assert target.read_text(encoding="utf-8") == before  # 文件未被写入

    def test_post_check_failure_rolls_back(self, git_repo, git_worktree) -> None:
        """应用后复核不过（diff 过大）→ 工作区必须回滚。"""
        before = (git_worktree / SERVICE).read_text(encoding="utf-8")
        big_patch = Patch(edits=[PatchEdit(
            file=SERVICE,
            full_content="package com.example;\n\npublic class OrderService {\n"
            + "\n".join(f"    // 填充行 {i}" for i in range(120))
            + "\n}\n",
        )])
        out = repair_node(
            _state(git_repo, git_worktree),
            FakePatcher(big_patch),
            RepairPolicy(max_diff_lines=50),
        )
        assert out["policy_verdict"].decision is RepairDecision.MANUAL_REVIEW
        assert (git_worktree / SERVICE).read_text(encoding="utf-8") == before  # 已回滚

    def test_apply_failure_reported_not_crash(self, git_repo, git_worktree) -> None:
        bad = Patch(edits=[PatchEdit(file=SERVICE, search="不存在的片段", replace="x")])
        out = repair_node(_state(git_repo, git_worktree), FakePatcher(bad))
        assert out["policy_verdict"].decision is RepairDecision.MANUAL_REVIEW
        assert "补丁应用失败" in out["policy_verdict"].reasons[0]


class RetryPatcher:
    """按调用顺序返回不同补丁，并记录每次收到的 feedback。"""

    def __init__(self, patches: list[Patch]) -> None:
        self._patches = patches
        self.calls: list[dict] = []

    def propose(
        self, diagnosis, bundle, attempt=1, previous_attempts=None, feedback=None,
        evidence=None,
    ) -> Patch:
        self.calls.append({"attempt": attempt, "feedback": feedback})
        return self._patches[min(len(self.calls) - 1, len(self._patches) - 1)]


class TestInnerRetryOnApplyFailure:
    """search 匹配失败属于工具级错误：喂回文件真实内容后立即重试，
    不应消耗外层尝试次数、也不该让外层反思来兜。"""

    def test_retry_recovers_without_extra_attempt(self, git_repo, git_worktree) -> None:
        bad = Patch(edits=[PatchEdit(
            file=SERVICE, search="Order order = new Order(userId, user.getEmail());",
            replace="x",
        )], reason="凭印象编造的 search")
        good = _good_patch()
        patcher = RetryPatcher([bad, good])

        out = repair_node(_state(git_repo, git_worktree), patcher)

        # 第二次生成成功并落地
        assert out["policy_verdict"].decision is RepairDecision.AUTO_REPAIR
        assert out["patch"].changed_files == [SERVICE]
        assert out["attempt"] == 1                      # 仍是第一次尝试
        assert len(patcher.calls) == 2
        # 重试时收到了工具错误，且其中包含文件真实内容（模型可照抄）
        feedback = patcher.calls[1]["feedback"]
        assert feedback is not None
        assert "未找到匹配" in feedback
        assert "greet" in feedback
        assert patcher.calls[0]["feedback"] is None     # 首次无 feedback

    def test_exhausted_retries_report_manual_review(self, git_repo, git_worktree) -> None:
        bad = Patch(edits=[PatchEdit(file=SERVICE, search="永不匹配", replace="x")])
        patcher = RetryPatcher([bad])
        out = repair_node(_state(git_repo, git_worktree), patcher)
        assert out["policy_verdict"].decision is RepairDecision.MANUAL_REVIEW
        assert "已重试" in out["policy_verdict"].reasons[0]
        assert len(patcher.calls) == 3                  # 1 次 + 2 次重试


class TestPromptRendering:
    def test_message_contains_diagnosis_and_snippets(self, git_repo, git_worktree) -> None:
        failure, triage = parse_maven_log(JUNIT4_NPE_ERROR)
        from devfix.nodes.context_collector import collect_context

        bundle = collect_context(failure, triage, repo=git_repo, base="HEAD~1")
        d = Diagnosis(summary="NPE", root_cause="user 为 null", confidence=0.9)
        msg = render_repair_message(d, bundle, attempt=2, previous_attempts=["第1次：抛 IllegalStateException 失败"])
        assert "user 为 null" in msg
        assert SERVICE in msg
        assert "第2次" in msg or "第 2 次" in msg
        assert "避免重复" in msg


@pytest.mark.llm
@pytest.mark.skipif(
    os.environ.get("DEVFIX_RUN_LLM_TESTS") != "1",
    reason="真实 LLM 调用：需设置 DEEPSEEK_API_KEY 与 DEVFIX_RUN_LLM_TESTS=1",
)
class TestLLMPatcherIntegration:
    def test_real_patch_generation(self, git_repo) -> None:
        from devfix.config import load_config
        from devfix.llm.provider import get_chat_model
        from devfix.nodes.context_collector import collect_context
        from devfix.nodes.repair import LLMPatcher

        failure, triage = parse_maven_log(JUNIT4_NPE_ERROR)
        bundle = collect_context(failure, triage, repo=git_repo, base="HEAD~1")
        diagnosis = Diagnosis(
            summary="findActiveById 语义变化导致 null",
            root_cause="查询改为 findActiveById 后返回 null，调用方未处理",
            confidence=0.9,
        )
        patch = LLMPatcher(get_chat_model(load_config().llm)).propose(diagnosis, bundle)
        assert patch.edits or patch.cannot_fix_reason, "必须给出补丁或说明无法修复"
