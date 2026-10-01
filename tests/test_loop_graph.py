"""Repair Loop 图测试：修复 → 验证 → 反思 → 再修复 的完整编排。

用脚本化的 Fake 组件驱动，离线验证循环逻辑、尝试计数、
历史上传（避免重复改法）、以及各类终止条件。
"""

from __future__ import annotations

from pathlib import Path

from devfix.graph import build_repair_graph
from devfix.models import (
    Diagnosis,
    DiagnosisContext,
    NextAction,
    Patch,
    PatchEdit,
    ReflectionResult,
    VerificationLevel,
    VerificationResult,
    VerificationStatus,
)
from tests.sample_logs import JUNIT4_NPE_ERROR

SERVICE = "src/main/java/com/example/OrderService.java"
OLD_LINE = '    public String greet(String name) { return "hi " + name; }'
NEW_LINE_1 = '    public String greet(String name) { return "first fix " + name; }'
NEW_LINE_2 = '    public String greet(String name) { return "second fix " + name; }'


# ---------------------------------------------------------------------- Fakes
class FakeDiagnoser:
    def diagnose(self, bundle: DiagnosisContext) -> Diagnosis:
        return Diagnosis(summary="NPE", root_cause="user 为 null", confidence=0.9)


class ScriptedPatcher:
    """按轮次返回预置补丁，并记录收到的 attempt / previous_attempts。"""

    def __init__(self, patches: list[Patch]) -> None:
        self._patches = patches
        self.calls: list[tuple[int, list[str]]] = []

    def propose(
        self, diagnosis, bundle, attempt=1, previous_attempts=None, feedback=None,
        evidence=None,
    ) -> Patch:
        self.calls.append((attempt, list(previous_attempts or [])))
        return self._patches[min(attempt - 1, len(self._patches) - 1)]


class ScriptedReflector:
    def __init__(self, result: ReflectionResult | None = None) -> None:
        self._result = result or ReflectionResult(
            previous_diagnosis_valid=True,
            failure_reason="补丁只解决了异常类型，未满足其余约束",
            new_evidence=["订单仍被落库"],
            should_update_diagnosis=True,
            updated_root_cause="校验必须发生在副作用之前",
            next_action=NextAction.REPAIR_AGAIN,
        )
        self.calls = 0

    def reflect(
        self, diagnosis, patch, verification=None, previous_patches=None, verdict=None,
        hypotheses=None, evidence=None,
    ):
        self.calls += 1
        return self._result


class RecordingReflector:
    """记录每次反思收到的输入（验证结果 or 策略结论）。"""

    def __init__(self, result: ReflectionResult | None = None) -> None:
        self._result = result or ReflectionResult(
            previous_diagnosis_valid=True,
            failure_reason="补丁被策略拒绝（改了测试代码）",
            new_evidence=["策略禁止修改 src/test/**"],
            next_action=NextAction.REPAIR_AGAIN,
        )
        self.calls: list[dict] = []

    def reflect(self, diagnosis, patch, verification=None, previous_patches=None, verdict=None, hypotheses=None, evidence=None):
        self.calls.append({"verification": verification, "verdict": verdict})
        return self._result


class ScriptedVerifier:
    """按调用次数依次返回预置结果。"""

    def __init__(self, statuses: list[VerificationStatus]) -> None:
        self._statuses = statuses
        self.calls = 0

    def verify(self, failure, workspace: Path) -> list[VerificationResult]:
        idx = min(self.calls, len(self._statuses) - 1)
        status = self._statuses[idx]
        self.calls += 1
        payload = {} if status is VerificationStatus.PASS else {
            "exit_code": 1,
            "new_errors": ["被拒绝的订单不应落库 expected: <true> but was: <false>"],
        }
        return [VerificationResult(
            verification_id=f"VR-{self.calls}",
            level=VerificationLevel.FULL,
            status=status,
            command="mvn -B test",
            **payload,
        )]


def _patch(line: str, reason: str) -> Patch:
    return Patch(
        patch_id=f"PATCH-{abs(hash(reason)) % 1000:03d}",
        edits=[PatchEdit(file=SERVICE, search=OLD_LINE, replace=line)],
        reason=reason,
        expected_effect="greet 返回新前缀",
    )


def _run(git_repo, tmp_path, *, patches, statuses, reflector=None, max_attempts=3):
    graph = build_repair_graph(
        diagnoser=FakeDiagnoser(),
        patcher=ScriptedPatcher(patches),
        reflector=reflector or ScriptedReflector(),
        verifier=ScriptedVerifier(statuses),
    )
    return graph.invoke({
        "repo": str(git_repo),
        "log_text": JUNIT4_NPE_ERROR,
        "base": "HEAD~1",
        "workspace_dir": str(tmp_path / "ws"),
        "max_attempts": max_attempts,
    })


# ---------------------------------------------------------------------- 测试
class TestSuccessOnFirstAttempt:
    def test_single_attempt(self, git_repo, tmp_path) -> None:
        final = _run(
            git_repo, tmp_path,
            patches=[_patch(NEW_LINE_1, "第一次就修对")],
            statuses=[VerificationStatus.PASS],
        )
        assert final["verification"].passed
        assert final["attempt"] == 1
        assert len(final["attempts"]) == 1
        assert final["attempts"][0].reflection is None  # 成功即无需反思


class TestReflectionLoop:
    def test_fail_then_pass(self, git_repo, tmp_path) -> None:
        patcher = ScriptedPatcher([
            _patch(NEW_LINE_1, "第一次补丁"),
            _patch(NEW_LINE_2, "第二次补丁"),
        ])
        reflector = ScriptedReflector()
        graph = build_repair_graph(
            diagnoser=FakeDiagnoser(),
            patcher=patcher,
            reflector=reflector,
            verifier=ScriptedVerifier([VerificationStatus.FAIL, VerificationStatus.PASS]),
        )
        final = graph.invoke({
            "repo": str(git_repo), "log_text": JUNIT4_NPE_ERROR, "base": "HEAD~1",
            "workspace_dir": str(tmp_path / "ws"), "max_attempts": 3,
        })

        # 循环确实跑了两轮
        assert final["attempt"] == 2
        assert final["verification"].passed
        assert reflector.calls == 1

        # 第一轮失败被完整记录（补丁 + 验证 + 反思）
        failed = final["attempts"][0]
        assert failed.attempt == 1
        assert failed.verification is not None and not failed.verification.passed
        assert failed.reflection is not None
        # 第二轮成功记录
        assert final["attempts"][1].attempt == 2
        assert final["attempts"][1].verification.passed

        # 历史被回传给 Patcher（防止重复改法）
        assert patcher.calls[0][0] == 1 and patcher.calls[0][1] == []
        assert patcher.calls[1][0] == 2
        assert patcher.calls[1][1], "第二轮必须收到历史尝试"

        # 诊断被反思修订，第二轮补丁基于新根因
        assert final["diagnosis"].root_cause == "校验必须发生在副作用之前"

        # 第二次补丁真的写进了隔离工作区
        content = (Path(final["workspace"]) / SERVICE).read_text(encoding="utf-8")
        assert "second fix" in content


class TestPolicyRejectionPath:
    """补丁被策略拒绝时：不跑验证、把拒绝原因交给反思、允许一轮改正机会。"""

    def test_rejected_patch_goes_to_reflection_with_reason(self, git_repo, tmp_path) -> None:
        test_file = "src/test/java/com/example/OrderServiceTest.java"
        bad = Patch(
            edits=[PatchEdit(file=test_file, search="a", replace="b")],
            reason="直接改测试断言",
        )
        good = _patch(NEW_LINE_1, "改为修生产代码")
        patcher = ScriptedPatcher([bad, good])

        reflector = RecordingReflector()
        verifier = ScriptedVerifier([VerificationStatus.PASS])
        graph = build_repair_graph(
            diagnoser=FakeDiagnoser(), patcher=patcher,
            reflector=reflector, verifier=verifier,
        )
        final = graph.invoke({
            "repo": str(git_repo), "log_text": JUNIT4_NPE_ERROR, "base": "HEAD~1",
            "workspace_dir": str(tmp_path / "ws"), "max_attempts": 3,
        })

        assert final["attempt"] == 2
        assert final["verification"].passed
        # 被拒那轮没有跑验证（什么都没改，验证只会产生误导证据）
        assert verifier.calls == 1
        # 反思收到的是策略结论，不是验证结果
        assert reflector.calls[0]["verdict"] is not None
        assert reflector.calls[0]["verification"] is None
        assert any(
            "禁止自动修改的区域" in r and "src/test" in r
            for r in reflector.calls[0]["verdict"].reasons
        )
        # 两次尝试都留在历史里
        assert len(final["attempts"]) == 2

    def test_insufficient_evidence_stops_immediately(self, git_repo, tmp_path) -> None:
        class LowEvidenceDiagnoser:
            def diagnose(self, bundle):
                return Diagnosis(
                    summary="说不清", root_cause="证据不足",
                    confidence=0.3, insufficient_evidence=True,
                )

        reflector = RecordingReflector()
        graph = build_repair_graph(
            diagnoser=LowEvidenceDiagnoser(),
            patcher=ScriptedPatcher([_patch(NEW_LINE_1, "p")]),
            reflector=reflector,
            verifier=ScriptedVerifier([VerificationStatus.PASS]),
        )
        final = graph.invoke({
            "repo": str(git_repo), "log_text": JUNIT4_NPE_ERROR, "base": "HEAD~1",
            "workspace_dir": str(tmp_path / "ws"), "max_attempts": 3,
        })

        assert final["attempt"] == 1
        assert not reflector.calls            # 不该反思，直接停止
        assert "verification" not in final    # 也不该验证
        assert "证据不足" in final["stop_reason"]


class TestStopConditions:
    def test_stops_at_max_attempts(self, git_repo, tmp_path) -> None:
        final = _run(
            git_repo, tmp_path,
            patches=[_patch(NEW_LINE_1, "反复失败的改法")],
            statuses=[VerificationStatus.FAIL],
            max_attempts=3,
        )
        assert final["attempt"] == 3
        assert not final["verification"].passed
        assert len(final["attempts"]) == 3  # 三次失败都被记录

    def test_reflection_stop_ends_loop(self, git_repo, tmp_path) -> None:
        reflector = ScriptedReflector(ReflectionResult(
            previous_diagnosis_valid=True,
            failure_reason="必须修改测试才能通过，超出约束",
            next_action=NextAction.STOP,
        ))
        final = _run(
            git_repo, tmp_path,
            patches=[_patch(NEW_LINE_1, "第一次补丁")],
            statuses=[VerificationStatus.FAIL],
            reflector=reflector,
            max_attempts=3,
        )
        assert final["attempt"] == 1          # 一轮后主动停止
        assert reflector.calls == 1
        assert "超出约束" in final["stop_reason"]

    def test_timeout_counts_as_failure(self, git_repo, tmp_path) -> None:
        final = _run(
            git_repo, tmp_path,
            patches=[_patch(NEW_LINE_1, "p")],
            statuses=[VerificationStatus.TIMEOUT],
            max_attempts=1,
        )
        assert not final["verification"].passed
        assert final["verification"].status is VerificationStatus.TIMEOUT
        assert final["attempt"] == 1
