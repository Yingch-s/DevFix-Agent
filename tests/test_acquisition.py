"""Context Acquisition Loop 测试（Agent Engineering V0.2 §1~§2，离线）。

覆盖：工具执行器（成功/失败反馈/预算拒绝）、acquire 节点预算计数、
诊断循环（取证→再诊断→READY）、预算耗尽的受限最终决策、
最终决策仍 NEED_MORE 的确定性诚实停止。
"""

from __future__ import annotations

from devfix.graph import build_diagnosis_graph
from devfix.models import (
    ContextRequest,
    Diagnosis,
    DiagnosisDecision,
    Evidence,
)
from devfix.nodes.acquisition import (
    MAX_TOOL_CALLS,
    acquire_node,
    execute_request,
)
from tests.sample_logs import JUNIT4_NPE_ERROR

SERVICE = "src/main/java/com/example/OrderService.java"


def _request(tool: str, **args) -> ContextRequest:
    return ContextRequest(tool=tool, args=args, reason="测试请求", hypothesis="H")


# ------------------------------------------------------------------ 工具执行器
class TestExecuteRequest:
    def test_read_file_returns_evidence(self, git_repo) -> None:
        ev = execute_request(_request("read_file", path=SERVICE, start=1), git_repo, "HEAD~1", [])
        assert ev.kind == "source_range"
        assert ev.file_path == SERVICE
        assert "greet" in ev.content
        assert ev.id == "EV-001"

    def test_missing_file_becomes_feedback_not_exception(self, git_repo) -> None:
        ev = execute_request(_request("read_file", path="nope/missing.java"), git_repo, None, [])
        assert ev.kind == "tool_result"
        assert "[取证未成功]" in ev.content
        assert "nope/missing.java" in ev.content  # 错误信息可行动

    def test_unknown_tool_lists_available(self, git_repo) -> None:
        ev = execute_request(_request("run_shell", cmd="rm -rf /"), git_repo, None, [])
        assert "未知工具" in ev.content
        assert "read_file" in ev.content

    def test_search_no_match_is_actionable_feedback(self, git_repo) -> None:
        ev = execute_request(_request("search_code", query="zzzNonexistentZZZ"), git_repo, None, [])
        assert "[取证未成功]" in ev.content
        assert "换更短" in ev.content

    def test_budget_denial(self, git_repo) -> None:
        full = [
            Evidence(id=f"EV-{i:03d}", kind="source_range", source="test", content="x" * 30_000)
            for i in range(1, 5)  # 4 × 30k = 120k，恰好占满预算
        ]
        ev = execute_request(_request("read_file", path=SERVICE), git_repo, None, full)
        assert "证据预算已满" in ev.content

    def test_evidence_id_sequencing(self, git_repo) -> None:
        existing = [Evidence(id="EV-001", kind="git_diff", source="test", content="d")]
        ev = execute_request(_request("read_file", path=SERVICE), git_repo, None, existing)
        assert ev.id == "EV-002"


# ------------------------------------------------------------------ acquire 节点
class TestAcquireNode:
    def test_executes_requests_and_updates_counters(self, git_repo) -> None:
        out = acquire_node({
            "repo": str(git_repo),
            "base": "HEAD~1",
            "evidence": [],
            "context_requests": [_request("read_file", path=SERVICE)],
            "acquire_rounds": 0,
            "tool_calls_used": 0,
        })
        assert out["tool_calls_used"] == 1
        assert out["acquire_rounds"] == 1
        assert out["context_requests"] == []  # 请求已消费
        assert any("greet" in e.content for e in out["evidence"])
        assert out["inspected"] == [f"read_file:{SERVICE}"]

    def test_tool_call_cap_enforced_deterministically(self, git_repo) -> None:
        out = acquire_node({
            "repo": str(git_repo),
            "evidence": [],
            "context_requests": [_request("read_file", path=SERVICE) for _ in range(MAX_TOOL_CALLS + 2)],
            "acquire_rounds": 0,
            "tool_calls_used": 0,
        })
        assert out["tool_calls_used"] == MAX_TOOL_CALLS
        denied = [e for e in out["evidence"] if "已达上限" in e.content]
        assert len(denied) == 2


# ------------------------------------------------------------------ 诊断循环
class ScriptedDecider:
    """按脚本返回决策序列，记录每次调用的关键入参。"""

    def __init__(self, script: list[DiagnosisDecision]):
        self.script = list(script)
        self.calls: list[dict] = []

    def decide(self, bundle, evidence, hypotheses, *, allow_tools, rounds_left, tool_calls_left, round_no):
        self.calls.append({
            "evidence": list(evidence),
            "hypotheses": list(hypotheses),
            "allow_tools": allow_tools,
            "round_no": round_no,
        })
        return self.script.pop(0)


def _need_more(*requests: ContextRequest) -> DiagnosisDecision:
    return DiagnosisDecision(
        status="NEED_MORE_CONTEXT",
        hypotheses=[{"statement": "H1: greet 语义变化", "supporting": [], "status": "POSSIBLE"}],
        context_requests=list(requests),
    )


def _ready() -> DiagnosisDecision:
    return DiagnosisDecision(
        status="DIAGNOSIS_READY",
        diagnosis=Diagnosis(summary="s", root_cause="r", confidence=0.9),
    )


def _insufficient() -> DiagnosisDecision:
    return DiagnosisDecision(status="INSUFFICIENT_EVIDENCE", unresolved_questions=["Lexer 字节逻辑"])


class TestAcquisitionLoop:
    def _invoke(self, git_repo, decider: ScriptedDecider) -> dict:
        graph = build_diagnosis_graph(decider)
        return graph.invoke(
            {"repo": str(git_repo), "log_text": JUNIT4_NPE_ERROR, "base": "HEAD~1"},
            {"recursion_limit": 40},
        )

    def test_acquire_then_ready(self, git_repo) -> None:
        decider = ScriptedDecider([
            _need_more(_request("read_file", path=SERVICE)),
            _ready(),
        ])
        final = self._invoke(git_repo, decider)
        # 第一轮 NEED_MORE → 取证 → 第二轮 READY
        assert [c["round_no"] for c in decider.calls] == [1, 2]
        assert decider.calls[0]["allow_tools"] is True
        # 第二轮的账本包含工具取回的证据
        second = decider.calls[1]["evidence"]
        assert any(e.kind == "source_range" and "greet" in e.content for e in second)
        assert final["diagnosis"].root_cause == "r"
        assert final["acquire_rounds"] == 1

    def test_budget_exhaustion_forces_final_decision(self, git_repo) -> None:
        """3 轮预算耗尽 → 受限最终决策（禁用工具）→ INSUFFICIENT → 诚实停止。"""
        req = _request("read_file", path=SERVICE)
        decider = ScriptedDecider([
            _need_more(req), _need_more(req), _need_more(req),
            _insufficient(),  # 最终决策
        ])
        final = self._invoke(git_repo, decider)
        allow_flags = [c["allow_tools"] for c in decider.calls]
        assert allow_flags == [True, True, True, False]  # 第 4 轮为受限最终决策
        assert final["decision"].status == "INSUFFICIENT_EVIDENCE"
        assert "证据不足" in final["stop_reason"]
        assert final["diagnosis"].insufficient_evidence is True

    def test_final_disobedience_without_reasons_falls_through(self, git_repo) -> None:
        """最终决策仍输出 NEED_MORE_CONTEXT 且未说明缺什么 → 按"无因停止无效"
        处理（决策保留 INSUFFICIENT 供审计，不写 stop_reason、不标记不足）。
        诊断子图中 prepare_workspace 映射为 END（修复行为由全图测试覆盖）。"""
        req = _request("read_file", path=SERVICE)
        decider = ScriptedDecider([
            _need_more(req), _need_more(req), _need_more(req),
            _need_more(req),  # 不服从最终决策约束
        ])
        final = self._invoke(git_repo, decider)
        assert final["decision"].status == "INSUFFICIENT_EVIDENCE"  # 审计保留
        assert final["diagnosis"].insufficient_evidence is False  # 不标记证据不足
        assert not final.get("stop_reason")
        # 没有无限循环：总共 4 轮诊断
        assert len(decider.calls) == 4

    def test_insufficient_from_first_round_ends_immediately(self, git_repo) -> None:
        decider = ScriptedDecider([_insufficient()])
        final = self._invoke(git_repo, decider)
        assert final["stop_reason"]
        assert final["diagnosis"].insufficient_evidence is True
        assert len(decider.calls) == 1  # 不空跑 repair/worktree

