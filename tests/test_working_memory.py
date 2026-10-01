"""Working Memory 与语义无进展检测测试（Agent Engineering V0.2 §3/§5，离线）。

覆盖：repair 渲染取证账本、反思的假设合并、reflect→取证→repair 路由、
连续语义无进展的确定性短路。
"""

from __future__ import annotations

from devfix.graph import build_repair_graph, invoke_config
from devfix.models import (
    ContextRequest,
    Diagnosis,
    DiagnosisContext,
    DiagnosisDecision,
    Evidence,
    Hypothesis,
    NextAction,
    Patch,
    PatchEdit,
    ReflectionResult,
    VerificationLevel,
    VerificationResult,
    VerificationStatus,
)
from devfix.nodes.reflection import _merge_hypotheses, reflection_node
from devfix.nodes.repair import render_repair_message
from tests.sample_logs import JUNIT4_NPE_ERROR
from tests.test_loop_graph import (
    NEW_LINE_1,
    OLD_LINE,
    SERVICE,
    FakeDiagnoser,
)

ACQUIRED = Evidence(
    id="EV-013", kind="source_range",
    source="tool:read_file#src/main/java/org/apache/commons/csv/Lexer.java",
    file_path="src/main/java/org/apache/commons/csv/Lexer.java",
    line_start=100, line_end=200,
    content="    public long getBytesRead() { return bytesRead; }",
)


# ------------------------------------------------------------------ repair 渲染
def _bundle() -> DiagnosisContext:
    from devfix.models import DiagnosisContext
    from devfix.parsing import parse_maven_log

    ctx, triage = parse_maven_log(JUNIT4_NPE_ERROR)
    return DiagnosisContext(failure=ctx, triage=triage)


class TestRepairEvidenceRendering:
    def test_acquired_evidence_rendered(self) -> None:
        diagnosis = Diagnosis(summary="s", root_cause="r", confidence=0.9)
        bundle = _bundle().model_copy(update={"snippets": []})  # 只看取证账本的渲染
        msg = render_repair_message(diagnosis, bundle, evidence=[ACQUIRED])
        assert "【取证证据" in msg
        assert "getBytesRead" in msg
        assert "Lexer.java" in msg

    def test_initial_evidence_not_duplicated(self) -> None:
        """initial 来源的证据与 bundle.snippets 内容重复，不重复渲染。"""
        diagnosis = Diagnosis(summary="s", root_cause="r", confidence=0.9)
        initial = [
            Evidence(id="EV-001", kind="source_range", source="initial:failed_test", content="x"),
        ]
        msg = render_repair_message(diagnosis, _bundle(), evidence=initial)
        assert "【取证证据" not in msg


# ------------------------------------------------------------------ 假设合并
class TestMergeHypotheses:
    def test_same_statement_updated(self) -> None:
        current = [Hypothesis(statement="H1", status="POSSIBLE")]
        merged = _merge_hypotheses(current, [Hypothesis(statement="H1", status="REJECTED")])
        assert len(merged) == 1
        assert merged[0].status == "REJECTED"

    def test_new_statement_appended(self) -> None:
        merged = _merge_hypotheses(
            [Hypothesis(statement="H1", status="SUPPORTED")],
            [Hypothesis(statement="H2", status="POSSIBLE")],
        )
        assert [h.statement for h in merged] == ["H1", "H2"]


class TestReflectionHypothesisMerge:
    def test_node_merges_updates_into_state(self) -> None:
        diagnosis = Diagnosis(summary="s", root_cause="r", confidence=0.9)
        patch = Patch(patch_id="P", edits=[PatchEdit(file=SERVICE, search="a", replace="b")])
        state = {
            "diagnosis": diagnosis,
            "patch": patch,
            "verification": VerificationResult(
                verification_id="VR", level=VerificationLevel.FULL,
                status=VerificationStatus.FAIL,
            ),
            "attempt": 1,
            "hypotheses": [Hypothesis(statement="H1", status="POSSIBLE")],
        }

        class FakeReflector:
            def reflect(self, diagnosis, patch, verification=None, previous_patches=None,
                        verdict=None, hypotheses=None, evidence=None):
                return ReflectionResult(
                    failure_reason="验证失败",
                    next_action=NextAction.REPAIR_AGAIN,
                    hypothesis_updates=[Hypothesis(statement="H1", status="REJECTED")],
                )

        out = reflection_node(state, FakeReflector())
        assert out["hypotheses"][0].status == "REJECTED"


# ------------------------------------------------------------------ 反思取证路由
class ScriptedPatcher:
    """记录收到的 evidence，验证 reflect 取证结果进入了修复上下文。"""

    def __init__(self, patches: list[Patch]) -> None:
        self._patches = patches
        self.evidence_seen: list[list[Evidence]] = []

    def propose(self, diagnosis, bundle, attempt=1, previous_attempts=None,
                feedback=None, evidence=None) -> Patch:
        self.evidence_seen.append(list(evidence or []))
        return self._patches[min(attempt - 1, len(self._patches) - 1)]


class ScriptedReflector:
    def __init__(self, results: list[ReflectionResult]) -> None:
        self._results = results
        self.calls = 0

    def reflect(self, diagnosis, patch, verification=None, previous_patches=None,
                verdict=None, hypotheses=None, evidence=None):
        result = self._results[min(self.calls, len(self._results) - 1)]
        self.calls += 1
        return result


class AlwaysFailVerifier:
    def verify(self, failure, workspace) -> list[VerificationResult]:
        return [VerificationResult(
            verification_id="VR", level=VerificationLevel.FULL,
            status=VerificationStatus.FAIL, exit_code=1,
            new_errors=["仍失败"],
        )]


def _patch(line: str = NEW_LINE_1) -> Patch:
    return Patch(
        patch_id="P1",
        edits=[PatchEdit(file=SERVICE, search=OLD_LINE, replace=line)],
        reason="r", expected_effect="e",
    )


def test_reflect_acquires_then_repairs(git_repo, tmp_path) -> None:
    """反思发起取证请求 → acquire_reflect 执行 → repair 的 evidence 里有结果。"""
    patcher = ScriptedPatcher([_patch(), _patch()])
    reflector = ScriptedReflector([
        ReflectionResult(
            failure_reason="需要查看 OrderService 的实现",
            next_action=NextAction.REPAIR_AGAIN,
            context_requests=[ContextRequest(
                tool="read_file", args={"path": SERVICE}, reason="确认实现",
            )],
        ),
        ReflectionResult(failure_reason="仍未修复", next_action=NextAction.STOP),
    ])
    graph = build_repair_graph(
        diagnoser=FakeDiagnoser(), patcher=patcher, reflector=reflector,
        verifier=AlwaysFailVerifier(),
    )
    final = graph.invoke(
        {
            "repo": str(git_repo), "log_text": JUNIT4_NPE_ERROR, "base": "HEAD~1",
            "workspace_dir": str(tmp_path / "ws"), "max_attempts": 3,
        },
        invoke_config(3),
    )
    # 第一次修复调用时账本里还没有取证证据（诊断 legacy 路径无 tool: 证据）
    assert not any(e.source.startswith("tool:") for e in patcher.evidence_seen[0])
    # 第二次修复调用时取证证据已入账
    assert any(
        e.source.startswith("tool:read_file") and "greet" in e.content
        for e in patcher.evidence_seen[1]
    )
    assert final["attempt"] == 2


# ------------------------------------------------------------------ 语义无进展
class EmptyPatchPatcher:
    """永远返回 cannot_fix 空补丁（语义无进展的典型来源）。"""

    def __init__(self) -> None:
        self.calls = 0

    def propose(self, diagnosis, bundle, attempt=1, previous_attempts=None,
                feedback=None, evidence=None) -> Patch:
        self.calls += 1
        return Patch(patch_id=f"P{self.calls}", edits=[], reason="需要查看源码",
                     cannot_fix_reason="无法在约束内修复")


class ScriptedReflector2:
    def reflect(self, diagnosis, patch, verification=None, previous_patches=None,
                verdict=None, hypotheses=None, evidence=None):
        return ReflectionResult(failure_reason="模型自述无法修复", next_action=NextAction.REPAIR_AGAIN)


def test_semantic_no_progress_short_circuits_loop(git_repo, tmp_path) -> None:
    """连续 2 次语义无进展 → 确定性短路（不再烧满 max_attempts=3）。"""
    patcher = EmptyPatchPatcher()
    graph = build_repair_graph(
        diagnoser=FakeDiagnoser(), patcher=patcher,
        reflector=ScriptedReflector2(), verifier=AlwaysFailVerifier(),
    )
    final = graph.invoke({
        "repo": str(git_repo), "log_text": JUNIT4_NPE_ERROR, "base": "HEAD~1",
        "workspace_dir": str(tmp_path / "ws"), "max_attempts": 3,
    })
    assert final["attempt"] == 2  # 第 3 次不再尝试
    assert "语义无进展" in final["stop_reason"]
    assert patcher.calls == 2


# ------------------------------------------------------------------ 无因停止无效
def _insufficient_no_reasons() -> DiagnosisDecision:
    """模型声称证据不足但说不出缺什么——伪停止。"""
    return DiagnosisDecision(
        status="INSUFFICIENT_EVIDENCE",
        diagnosis=Diagnosis(summary="s", root_cause="r", confidence=0.9),
        unresolved_questions=[],
    )


class PassVerifier:
    def verify(self, failure, workspace) -> list[VerificationResult]:
        return [VerificationResult(
            verification_id="VR", level=VerificationLevel.FULL,
            status=VerificationStatus.PASS,
        )]


class TestUnjustifiedStop:
    def test_unjustified_stop_falls_to_repair(self, git_repo, tmp_path) -> None:
        """无因停止不被承认：INSUFFICIENT 且无 unresolved_questions →
        不写 stop_reason、不标记不足 → 转入修复（policy 仍按置信度把关）。"""
        patcher = ScriptedPatcher([_patch()])
        reflector = ScriptedReflector([
            ReflectionResult(failure_reason="r", next_action=NextAction.REPAIR_AGAIN),
        ])

        class DecidingDiagnoser:
            def decide(self, bundle, evidence, hypotheses, *, allow_tools,
                       rounds_left, tool_calls_left, round_no):
                return _insufficient_no_reasons()

        graph = build_repair_graph(
            diagnoser=DecidingDiagnoser(), patcher=patcher,
            reflector=reflector, verifier=PassVerifier(),
        )
        final = graph.invoke(
            {
                "repo": str(git_repo), "log_text": JUNIT4_NPE_ERROR,
                "base": "HEAD~1", "workspace_dir": str(tmp_path / "ws"),
                "max_attempts": 3,
            },
            invoke_config(3),
        )
        assert not final.get("stop_reason")
        assert final["diagnosis"].insufficient_evidence is False
        assert final["attempt"] == 1
        assert final["verification"].passed  # 修复真实发生并验证通过
        assert final["decision"].status == "INSUFFICIENT_EVIDENCE"  # 审计保留
