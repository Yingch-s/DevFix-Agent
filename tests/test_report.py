"""Repair Report 测试：状态判定、各分区渲染、落盘产物。"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from devfix.models import (
    Diagnosis,
    FailedTest,
    FailureContext,
    FailureType,
    NextAction,
    Patch,
    PatchEdit,
    PolicyVerdict,
    ReflectionResult,
    RepairAttempt,
    RepairDecision,
    TriageResult,
    VerificationLevel,
    VerificationResult,
    VerificationStatus,
)
from devfix.report import (
    STATUS_DIAGNOSED_ONLY,
    STATUS_FAILED,
    STATUS_FIXED,
    STATUS_STOPPED,
    build_report,
    final_status,
    new_run_id,
    render_markdown,
    save_report,
)

PATCH = Patch(
    patch_id="PATCH-001",
    edits=[PatchEdit(file="src/main/java/com/example/OrderService.java",
                     search="a", replace="b")],
    reason="恢复 null 守卫",
    expected_effect="已删除用户抛 OrderRejectedException",
    diff="--- a/OrderService.java\n+++ b/OrderService.java\n+if (user == null) { throw new OrderRejectedException(); }",
)

PASS_VERIFICATION = VerificationResult(
    verification_id="VR-L3",
    level=VerificationLevel.FULL,
    status=VerificationStatus.PASS,
    command="mvn -B test",
    exit_code=0,
    duration_ms=2300,
)

FAIL_VERIFICATION = VerificationResult(
    verification_id="VR-L2",
    level=VerificationLevel.RELATED,
    status=VerificationStatus.FAIL,
    command="mvn -B -Dtest=OrderServiceTest test",
    exit_code=1,
    failed_tests=[FailedTest(
        class_name="com.example.order.OrderServiceTest",
        method_name="shouldNotPersistRejectedOrder",
        kind="FAILURE",
        error_type="org.opentest4j.AssertionFailedError",
        message="被拒绝的订单不应落库 expected: <true> but was: <false>",
    )],
)


def _base_state(**extra) -> dict:
    state = {
        "repo": ".",
        "base": "HEAD~1",
        "failure": FailureContext(
            key_error="AssertionFailedError",
            failed_tests=[FailedTest(
                class_name="com.example.order.OrderServiceTest",
                method_name="shouldRejectDeletedUser",
                error_type="org.opentest4j.AssertionFailedError",
                message="expected OrderRejectedException but was NullPointerException",
            )],
        ),
        "triage": TriageResult(
            failure_type=FailureType.UNIT_TEST_FAILURE,
            confidence=0.9,
            key_error="AssertionFailedError",
            reason="解析到 1 个失败测试",
        ),
        "diagnosis": Diagnosis(
            summary="NPE 取代了业务异常",
            root_cause="校验被删除且副作用先于校验",
            confidence=0.95,
            observations=["diff 删除了判空分支"],
            inference="findActiveById 返回 null，未处理即解引用",
            evidence=["diff 删除行", "异常栈"],
            related_files=["src/main/java/com/example/OrderService.java"],
        ),
    }
    state.update(extra)
    return state


class TestFinalStatus:
    def test_fixed(self) -> None:
        assert final_status(_base_state(verification=PASS_VERIFICATION, attempt=1)) == STATUS_FIXED

    def test_failed_to_fix(self) -> None:
        assert final_status(
            _base_state(verification=FAIL_VERIFICATION, attempt=3)
        ) == STATUS_FAILED

    def test_stopped_wins_over_failure(self) -> None:
        state = _base_state(
            verification=FAIL_VERIFICATION, attempt=1, stop_reason="证据不足，主动停止"
        )
        assert final_status(state) == STATUS_STOPPED

    def test_diagnosed_only(self) -> None:
        assert final_status(_base_state()) == STATUS_DIAGNOSED_ONLY


class TestRunId:
    def test_format(self) -> None:
        rid = new_run_id(datetime(2026, 9, 30, 21, 15, 30, tzinfo=UTC))
        assert rid == "RF-20260930-211530"


class TestMarkdownRendering:
    def test_fixed_report_sections(self) -> None:
        state = _base_state(
            verification=PASS_VERIFICATION,
            verification_results=[PASS_VERIFICATION],
            attempt=1,
            attempts=[RepairAttempt(
                attempt=1, patch=PATCH,
                verdict=PolicyVerdict(decision=RepairDecision.AUTO_REPAIR, reasons=["通过"]),
                verification=PASS_VERIFICATION, verifications=[PASS_VERIFICATION],
            )],
        )
        md = render_markdown(build_report(state, "RF-TEST"))
        assert "# DevFix Repair Report" in md
        assert "✅ FIXED" in md
        for section in ("## Run", "## Failure", "## Diagnosis", "## Repair",
                        "## Verification", "## Result"):
            assert section in md
        assert "恢复 null 守卫" in md
        assert "```diff" in md                       # 补丁 diff 可复制
        assert "FULL：PASS" in md

    def test_failed_report_shows_reflection_history(self) -> None:
        reflection = ReflectionResult(
            previous_diagnosis_valid=False,
            failure_reason="校验仍在 save() 之后",
            new_evidence=["被拒绝的订单不应落库 expected: <true> but was: <false>"],
            should_update_diagnosis=True,
            updated_root_cause="校验必须发生在副作用之前",
            next_action=NextAction.REPAIR_AGAIN,
        )
        state = _base_state(
            verification=FAIL_VERIFICATION,
            verification_results=[FAIL_VERIFICATION],
            attempt=1,
            attempts=[RepairAttempt(
                attempt=1, patch=PATCH,
                verdict=PolicyVerdict(decision=RepairDecision.AUTO_REPAIR),
                verification=FAIL_VERIFICATION, verifications=[FAIL_VERIFICATION],
                reflection=reflection,
            )],
        )
        md = render_markdown(build_report(state, "RF-TEST"))
        assert "❌ FAILED_TO_FIX" in md
        assert "## Attempt History（反思迭代记录）" in md
        assert "校验必须发生在副作用之前" in md
        assert "REPAIR_AGAIN" in md

    def test_stopped_report_shows_reason(self) -> None:
        state = _base_state(stop_reason="证据不足：置信度 0.3 < 0.5")
        md = render_markdown(build_report(state, "RF-TEST"))
        assert "🟡 STOPPED" in md
        assert "证据不足" in md


class TestSaveReport:
    def test_writes_markdown_and_json(self, tmp_path) -> None:
        state = _base_state(
            verification=PASS_VERIFICATION,
            verification_results=[PASS_VERIFICATION],
            attempt=1,
            attempts=[RepairAttempt(attempt=1, patch=PATCH, verification=PASS_VERIFICATION)],
        )
        out_dir, report = save_report(state, output_root=tmp_path, run_id="RF-20260930-211530")
        assert out_dir == tmp_path / "RF-20260930-211530"
        assert (out_dir / "report.md").exists()
        data = json.loads((out_dir / "run.json").read_text(encoding="utf-8"))
        assert data["run_id"] == "RF-20260930-211530"
        assert data["final_status"] == STATUS_FIXED
        assert data["diagnosis"]["root_cause"].startswith("校验被删除")
        assert report.attempts[0].patch is not None

    def test_survives_missing_repo(self, tmp_path) -> None:
        """仓库信息不可得时报告仍要生成（不能因为查 git 失败而丢报告）。"""
        state = _base_state(repo=str(tmp_path / "nonexistent"))
        out_dir, report = save_report(state, output_root=tmp_path / "out", run_id="RF-X")
        assert out_dir.exists()
        assert report.final_status == STATUS_DIAGNOSED_ONLY
