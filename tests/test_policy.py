"""RepairPolicy 测试：安全红线（设计文档 14 节）。"""

from __future__ import annotations

from devfix.models import (
    Diagnosis,
    Patch,
    PatchEdit,
    RepairDecision,
)
from devfix.repair import RepairPolicy

GOOD_FILE = "src/main/java/com/example/OrderService.java"


def _diagnosis(confidence: float = 0.9, insufficient: bool = False) -> Diagnosis:
    return Diagnosis(
        summary="s", root_cause="NPE because null user",
        confidence=confidence, insufficient_evidence=insufficient,
    )


def _patch(*files: str, cannot_fix: str = "") -> Patch:
    edits = [PatchEdit(file=f, search="a", replace="b") for f in files]
    return Patch(edits=edits, cannot_fix_reason=cannot_fix)


def _diff(add_lines: int = 2, extra: str = "") -> str:
    body = "\n".join(f"+line {i}" for i in range(add_lines))
    return f"diff --git a/x b/x\n--- a/x\n+++ b/x\n@@\n{body}\n{extra}"


class TestPreChecks:
    def test_happy_path_auto_repair(self) -> None:
        v = RepairPolicy().evaluate(_diagnosis(), _patch(GOOD_FILE))
        assert v.decision is RepairDecision.AUTO_REPAIR

    def test_cannot_fix_reason_goes_manual(self) -> None:
        v = RepairPolicy().evaluate(_diagnosis(), _patch(cannot_fix="需要修改测试，被约束禁止"))
        assert v.decision is RepairDecision.MANUAL_REVIEW
        assert "无法在约束内安全修复" in v.reasons[0]

    def test_empty_patch_goes_manual(self) -> None:
        v = RepairPolicy().evaluate(_diagnosis(), _patch())
        assert v.decision is RepairDecision.MANUAL_REVIEW
        assert "空补丁" in v.reasons[0]

    def test_insufficient_evidence(self) -> None:
        v = RepairPolicy().evaluate(_diagnosis(insufficient=True), _patch(GOOD_FILE))
        assert v.decision is RepairDecision.INSUFFICIENT_EVIDENCE

    def test_low_confidence(self) -> None:
        v = RepairPolicy().evaluate(_diagnosis(confidence=0.3), _patch(GOOD_FILE))
        assert v.decision is RepairDecision.INSUFFICIENT_EVIDENCE


class TestFileScope:
    def test_test_file_rejected(self) -> None:
        v = RepairPolicy().evaluate(
            _diagnosis(), _patch("src/test/java/com/example/OrderServiceTest.java")
        )
        assert v.decision is RepairDecision.MANUAL_REVIEW
        assert "禁止自动修改的区域" in v.reasons[0]

    def test_ci_config_rejected(self) -> None:
        v = RepairPolicy().evaluate(_diagnosis(), _patch(".github/workflows/ci.yml"))
        assert v.decision is RepairDecision.MANUAL_REVIEW

    def test_outside_whitelist_rejected(self) -> None:
        v = RepairPolicy().evaluate(_diagnosis(), _patch("docs/README.md"))
        assert v.decision is RepairDecision.MANUAL_REVIEW
        assert "白名单之外" in v.reasons[0]

    def test_resources_and_pom_allowed(self) -> None:
        v = RepairPolicy().evaluate(
            _diagnosis(),
            _patch("src/main/resources/application.yml", "pom.xml"),
        )
        assert v.decision is RepairDecision.AUTO_REPAIR

    def test_too_many_files(self) -> None:
        files = [f"src/main/java/com/example/C{i}.java" for i in range(4)]
        v = RepairPolicy(max_files=3).evaluate(_diagnosis(), _patch(*files))
        assert v.decision is RepairDecision.MANUAL_REVIEW
        assert "超过上限" in v.reasons[0]

    def test_equivalent_path_forms_accepted(self) -> None:
        """路径书写形式不应影响判定（回退斜杠 / 前导 ./ / 前导 /）。"""
        for form in (
            "src\\main\\java\\com\\example\\OrderService.java",
            "./src/main/java/com/example/OrderService.java",
            "/src/main/java/com/example/OrderService.java",
        ):
            v = RepairPolicy().evaluate(_diagnosis(), _patch(form))
            assert v.decision is RepairDecision.AUTO_REPAIR, form

    def test_forbidden_path_forms_still_rejected(self) -> None:
        for form in (
            "./src/test/java/com/example/T.java",
            "src\\test\\java\\com\\example\\T.java",
        ):
            v = RepairPolicy().evaluate(_diagnosis(), _patch(form))
            assert v.decision is RepairDecision.MANUAL_REVIEW, form

    def test_path_traversal_bypass_rejected(self) -> None:
        """含 .. 的路径字符串能命中白名单前缀、绕开测试保护——必须拦下。

        "src/main/java/../src/test/java/X.java" 在文件系统层面落在
        src/test 下（PatchTool 会 resolve 后接受），若策略按原始字符串
        匹配，"src/main/java/" 前缀恰好命中白名单 → Agent 可改测试。
        """
        bypass = "src/main/java/../src/test/java/com/example/T.java"
        v = RepairPolicy().evaluate(_diagnosis(), _patch(bypass))
        assert v.decision is RepairDecision.MANUAL_REVIEW


class TestPostChecks:
    def test_small_diff_passes(self) -> None:
        v = RepairPolicy().evaluate_applied(_patch(GOOD_FILE).model_copy(update={"diff": _diff(5)}))
        assert v.decision is RepairDecision.AUTO_REPAIR

    def test_huge_diff_rejected(self) -> None:
        p = _patch(GOOD_FILE).model_copy(update={"diff": _diff(150)})
        v = RepairPolicy().evaluate_applied(p)
        assert v.decision is RepairDecision.MANUAL_REVIEW
        assert "变更行数" in v.reasons[0]

    def test_disabled_test_rejected(self) -> None:
        p = _patch(GOOD_FILE).model_copy(update={"diff": _diff(3, extra="+@Disabled(\"flaky\")")})
        v = RepairPolicy().evaluate_applied(p)
        assert v.decision is RepairDecision.MANUAL_REVIEW
        assert "禁用测试" in v.reasons[0]

    def test_removing_test_annotation_rejected(self) -> None:
        p = _patch(GOOD_FILE).model_copy(update={"diff": _diff(3, extra="-\t@Test")})
        v = RepairPolicy().evaluate_applied(p)
        assert v.decision is RepairDecision.MANUAL_REVIEW
        assert "删测试" in v.reasons[0]

    def test_pom_skip_tests_rejected(self) -> None:
        """pom.xml 在白名单内，但在构建配置层面跳过测试同样算作弊。"""
        for line in (
            "+        <skipTests>true</skipTests>",
            "+        <maven.test.skip>true</maven.test.skip>",
            "+                <testFailureIgnore>true</testFailureIgnore>",
        ):
            p = _patch("pom.xml").model_copy(update={"diff": _diff(3, extra=line)})
            v = RepairPolicy().evaluate_applied(p)
            assert v.decision is RepairDecision.MANUAL_REVIEW, line
            assert "禁用测试" in v.reasons[0]
