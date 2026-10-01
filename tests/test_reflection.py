"""Reflection 节点测试：新证据提取、诊断修订、停止条件（FakeReflector 注入）。"""

from __future__ import annotations

import os

import pytest

from devfix.models import (
    Diagnosis,
    NextAction,
    Patch,
    PatchEdit,
    ReflectionResult,
    VerificationLevel,
    VerificationResult,
    VerificationStatus,
)
from devfix.nodes.reflection import reflection_node, render_reflection_message
from devfix.nodes.verification import verification_node

SERVICE = "src/main/java/com/example/OrderService.java"


def _patch(reason: str = "在 NPE 处加 null 检查") -> Patch:
    return Patch(
        edits=[PatchEdit(file=SERVICE, search="a", replace="b")],
        reason=reason,
        expected_effect="抛出 OrderRejectedException",
        diff="--- a/x\n+++ b/x\n+if (user == null) { throw new OrderRejectedException(); }",
    )


def _failed_verification() -> VerificationResult:
    return VerificationResult(
        verification_id="VR-L1",
        level=VerificationLevel.FOCUSED,
        status=VerificationStatus.FAIL,
        command="mvn -Dtest=T#m test",
        exit_code=1,
        new_errors=["被拒绝的订单不应落库 expected: <true> but was: <false>"],
        log_tail="[ERROR] OrderServiceTest.shouldRejectDeletedUser:31 被拒绝的订单不应落库",
    )


class FakeReflector:
    def __init__(self, result: ReflectionResult | None = None, fail: Exception | None = None):
        self.result = result or ReflectionResult(
            previous_diagnosis_valid=True,
            failure_reason="补丁在 save() 之后才校验，订单已落库",
            new_evidence=["断言显示订单仍被保存"],
            should_update_diagnosis=True,
            updated_root_cause="校验必须发生在 save() 之前",
            next_action=NextAction.REPAIR_AGAIN,
            confidence=0.85,
        )
        self.fail = fail
        self.seen: tuple | None = None

    def reflect(
        self, diagnosis, patch, verification=None, previous_patches=None, verdict=None,
        hypotheses=None, evidence=None,
    ) -> ReflectionResult:
        self.seen = (diagnosis, patch, verification, previous_patches, verdict)
        if self.fail:
            raise self.fail
        return self.result


def _state(**extra) -> dict:
    state = {
        "diagnosis": Diagnosis(summary="s", root_cause="旧根因：null 未处理", confidence=0.9),
        "patch": _patch(),
        "verification": _failed_verification(),
        "attempt": 1,
    }
    state.update(extra)
    return state


class TestReflectionNode:
    def test_reflection_recorded(self) -> None:
        out = reflection_node(_state(), FakeReflector())
        assert out["reflection"].failure_reason.startswith("补丁在 save() 之后")
        assert out["reflection"].new_evidence == ["断言显示订单仍被保存"]

    def test_diagnosis_updated_when_requested(self) -> None:
        out = reflection_node(_state(), FakeReflector())
        assert out["diagnosis"].root_cause == "校验必须发生在 save() 之前"

    def test_diagnosis_kept_when_not_requested(self) -> None:
        reflector = FakeReflector(ReflectionResult(
            previous_diagnosis_valid=True,
            failure_reason="只是补丁不够精确",
            should_update_diagnosis=False,
            next_action=NextAction.REPAIR_AGAIN,
        ))
        out = reflection_node(_state(), reflector)
        assert "diagnosis" not in out  # 不覆盖

    def test_stop_action_sets_stop_reason(self) -> None:
        reflector = FakeReflector(ReflectionResult(
            previous_diagnosis_valid=True,
            failure_reason="必须修改测试才能通过，超出约束",
            next_action=NextAction.STOP,
        ))
        out = reflection_node(_state(), reflector)
        assert "stop_reason" in out
        assert "超出约束" in out["stop_reason"]

    def test_failed_attempt_recorded(self) -> None:
        out = reflection_node(_state(), FakeReflector())
        attempt = out["attempts"][0]
        assert attempt.attempt == 1
        assert attempt.verification is not None
        assert attempt.reflection is not None
        assert out["attempt_history"][0].startswith("第1次验证失败")

    def test_llm_exception_goes_to_error(self) -> None:
        out = reflection_node(_state(), FakeReflector(fail=RuntimeError("超时")))
        assert "反思失败" in out["error"]

    def test_missing_inputs(self) -> None:
        out = reflection_node({}, FakeReflector())
        assert "缺少" in out["error"]


class TestPromptRendering:
    def test_contains_failure_evidence(self) -> None:
        msg = render_reflection_message(
            Diagnosis(summary="s", root_cause="根因 A", confidence=0.9),
            _patch(),
            _failed_verification(),
            previous_patches=[_patch("第一次的改法")],
        )
        assert "根因 A" in msg
        assert "被拒绝的订单不应落库" in msg          # 新证据进入提示
        assert "第一次的改法" in msg                  # 历史补丁防重复
        assert "shouldRejectDeletedUser" in msg


class TestVerificationNodeAttemptRecording:
    def test_pass_appends_attempt(self, git_repo, git_worktree) -> None:
        class PassVerifier:
            def verify(self, failure, workspace):
                return [VerificationResult(
                    level=VerificationLevel.FULL,
                    status=VerificationStatus.PASS,
                    exit_code=0,
                )]

        from devfix.parsing import parse_maven_log
        from tests.sample_logs import JUNIT4_NPE_ERROR

        failure, _ = parse_maven_log(JUNIT4_NPE_ERROR)
        out = verification_node(
            {
                "failure": failure,
                "workspace": str(git_worktree),
                "patch": _patch(),
                "attempt": 2,
            },
            PassVerifier(),
        )
        assert out["verification"].passed
        assert out["attempts"][0].attempt == 2

    def test_fail_still_appends_attempt(self, git_worktree) -> None:
        """失败也要记录：循环可能在此终止（达上限/主动停止），
        最后一轮失败必须留在历史里供 Repair Report 使用。"""

        class FailVerifier:
            def verify(self, failure, workspace):
                return [_failed_verification()]

        from devfix.parsing import parse_maven_log
        from tests.sample_logs import JUNIT4_NPE_ERROR

        failure, _ = parse_maven_log(JUNIT4_NPE_ERROR)
        out = verification_node(
            {"failure": failure, "workspace": str(git_worktree)}, FailVerifier()
        )
        assert not out["verification"].passed
        assert len(out["attempts"]) == 1
        assert out["attempts"][0].reflection is None  # 反思信息随后补全


@pytest.mark.llm
@pytest.mark.skipif(
    os.environ.get("DEVFIX_RUN_LLM_TESTS") != "1",
    reason="真实 LLM 调用：需设置 DEEPSEEK_API_KEY 与 DEVFIX_RUN_LLM_TESTS=1",
)
class TestLLMReflectorIntegration:
    def test_real_reflection(self) -> None:
        from devfix.config import load_config
        from devfix.llm.provider import get_chat_model
        from devfix.nodes.reflection import LLMReflector

        reflector = LLMReflector(get_chat_model(load_config().llm))
        result = reflector.reflect(
            Diagnosis(
                summary="createOrder 对已删除用户抛 NPE",
                root_cause="findActiveById 返回 null 未处理",
                confidence=0.95,
            ),
            Patch(
                edits=[PatchEdit(file=SERVICE, search="a", replace="b")],
                reason="在 NPE 处加 null 检查并抛 OrderRejectedException",
                diff=(
                    "--- a/OrderService.java\n+++ b/OrderService.java\n"
                    "@@\n     Order order = new Order(userId);\n"
                    "     orderRepository.save(order);\n"
                    "+    if (user == null) { throw new OrderRejectedException(\"x\"); }\n"
                    "     order.setBuyerEmail(user.getEmail());\n"
                ),
            ),
            _failed_verification(),
        )
        assert result.failure_reason
        assert result.new_evidence, "反思必须给出新证据"
