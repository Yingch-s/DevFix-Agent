"""Diagnosis 图编排与消息渲染测试（FakeDiagnoser 注入，离线可跑）。"""

from __future__ import annotations

import os

import pytest

from devfix.graph import build_diagnosis_graph
from devfix.models import Diagnosis, DiagnosisContext
from devfix.nodes.diagnosis import (
    diagnosis_node,
    render_user_message,
)
from devfix.parsing import parse_maven_log
from tests.sample_logs import JUNIT4_NPE_ERROR


class FakeDiagnoser:
    """记录收到的 bundle，返回固定诊断。"""

    def __init__(self, diagnosis: Diagnosis | None = None, fail: Exception | None = None):
        self.seen: DiagnosisContext | None = None
        self._diagnosis = diagnosis or Diagnosis(
            summary="测试摘要", root_cause="user 为空未处理", confidence=0.8
        )
        self._fail = fail

    def diagnose(self, bundle: DiagnosisContext) -> Diagnosis:
        self.seen = bundle
        if self._fail:
            raise self._fail
        return self._diagnosis


class TestGraphFlow:
    def test_full_flow(self, git_repo) -> None:
        fake = FakeDiagnoser()
        graph = build_diagnosis_graph(fake)
        final = graph.invoke({
            "repo": str(git_repo),
            "log_text": JUNIT4_NPE_ERROR,
            "base": "HEAD~1",
        })
        assert final["triage"].failure_type.value == "UNIT_TEST_FAILURE"
        assert final["diagnosis"].root_cause == "user 为空未处理"
        # Fake 确实收到了完整的上下文
        assert fake.seen is not None
        assert fake.seen.failure.key_error == "NullPointerException"
        assert fake.seen.git_diff
        assert fake.seen.snippets

    def test_diagnoser_error_goes_to_state(self, git_repo) -> None:
        fake = FakeDiagnoser(fail=RuntimeError("LLM 超时"))
        graph = build_diagnosis_graph(fake)
        final = graph.invoke({
            "repo": str(git_repo), "log_text": JUNIT4_NPE_ERROR, "base": "HEAD~1",
        })
        assert "diagnosis 失败" in final["error"]
        assert "LLM 超时" in final["error"]


class TestDiagnosisNode:
    def test_missing_bundle(self) -> None:
        out = diagnosis_node({}, diagnoser=FakeDiagnoser())
        assert "缺少 context_bundle" in out["error"]


class TestRenderMessage:
    def test_contains_key_evidence(self, git_repo) -> None:
        fake = FakeDiagnoser()
        graph = build_diagnosis_graph(fake)
        graph.invoke({
            "repo": str(git_repo), "log_text": JUNIT4_NPE_ERROR, "base": "HEAD~1",
        })
        msg = render_user_message(fake.seen)
        assert "NullPointerException" in msg
        assert "shouldRejectDeletedUser" in msg
        assert "OrderService.java" in msg
        assert "diff --git" in msg            # git diff 已注入
        assert "stack_trace" in msg           # 片段来源标注


@pytest.mark.llm
@pytest.mark.skipif(
    os.environ.get("DEVFIX_RUN_LLM_TESTS") != "1",
    reason="真实 LLM 调用：需设置 DEEPSEEK_API_KEY 与 DEVFIX_RUN_LLM_TESTS=1",
)
class TestLLMIntegration:
    """真实 LLM 集成测试（默认跳过，手动开启：$env:DEVFIX_RUN_LLM_TESTS=1）。"""

    def test_real_diagnosis(self, git_repo) -> None:
        from devfix.config import load_config
        from devfix.llm.provider import get_chat_model
        from devfix.nodes.diagnosis import LLMDiagnoser

        diagnoser = LLMDiagnoser(get_chat_model(load_config().llm))
        failure, triage = parse_maven_log(JUNIT4_NPE_ERROR)
        from devfix.nodes.context_collector import collect_context

        bundle = collect_context(failure, triage, repo=git_repo, base="HEAD~1")
        d = diagnoser.diagnose(bundle)
        assert d.summary
        assert d.root_cause
        assert d.observations, "真实诊断应产出观察证据"
