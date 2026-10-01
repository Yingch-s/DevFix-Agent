"""Surefire 失败测试解析器测试。"""

from __future__ import annotations

from devfix.parsing.surefire import parse_failed_tests, parse_summary
from tests.sample_logs import (
    CLEAN_BUILD,
    JUNIT4_NPE_ERROR,
    JUNIT5_ASSERTION_FAILURE,
    SUREFIRE_32_FQ_FAILURE,
)


class TestNewFormatJUnit5:
    def test_failed_test_extracted(self) -> None:
        tests = parse_failed_tests(JUNIT5_ASSERTION_FAILURE)
        assert len(tests) == 1
        t = tests[0]
        assert t.class_name == "com.example.OrderServiceTest"
        assert t.method_name == "shouldRejectDeletedUser"
        assert t.kind == "FAILURE"

    def test_error_info_filled(self) -> None:
        t = parse_failed_tests(JUNIT5_ASSERTION_FAILURE)[0]
        assert t.error_type == "java.lang.AssertionError"
        assert t.message == "expected: <2> but was: <1>"
        assert t.display_name == (
            "com.example.OrderServiceTest#shouldRejectDeletedUser"
        )


class TestOldFormatJUnit4:
    def test_failed_test_extracted(self) -> None:
        tests = parse_failed_tests(JUNIT4_NPE_ERROR)
        assert len(tests) == 1
        t = tests[0]
        assert t.class_name == "com.example.OrderServiceTest"
        assert t.method_name == "shouldRejectDeletedUser"
        assert t.kind == "ERROR"

    def test_error_type_is_npe(self) -> None:
        t = parse_failed_tests(JUNIT4_NPE_ERROR)[0]
        assert t.error_type == "java.lang.NullPointerException"
        assert "user" in (t.message or "")


class TestRealSurefire32Format:
    """真实 surefire 3.2.5 输出：Class.method -- Time elapsed ... <<< FAILURE!"""

    def test_failed_test_extracted(self) -> None:
        tests = parse_failed_tests(SUREFIRE_32_FQ_FAILURE)
        assert len(tests) == 1  # 汇总行不能被误判为失败用例
        t = tests[0]
        assert t.class_name == "com.example.order.OrderServiceTest"
        assert t.method_name == "shouldRejectDeletedUser"
        assert t.kind == "FAILURE"

    def test_assertion_error_captured(self) -> None:
        t = parse_failed_tests(SUREFIRE_32_FQ_FAILURE)[0]
        assert t.error_type == "org.opentest4j.AssertionFailedError"
        assert "OrderRejectedException" in (t.message or "")
        assert "NullPointerException" in (t.message or "")

    def test_summary_still_parsed(self) -> None:
        s = parse_summary(SUREFIRE_32_FQ_FAILURE)
        assert s is not None
        assert (s.run, s.failures) == (2, 1)


class TestSummary:
    def test_junit5_summary_last_wins(self) -> None:
        s = parse_summary(JUNIT5_ASSERTION_FAILURE)
        assert s is not None
        assert (s.run, s.failures, s.errors, s.skipped) == (5, 1, 0, 0)

    def test_junit4_summary(self) -> None:
        s = parse_summary(JUNIT4_NPE_ERROR)
        assert s is not None
        assert (s.run, s.failures, s.errors, s.skipped) == (3, 0, 1, 0)

    def test_clean_build_summary(self) -> None:
        s = parse_summary(CLEAN_BUILD)
        assert s is not None
        assert (s.run, s.failures, s.errors, s.skipped) == (8, 0, 0, 0)

    def test_no_tests_no_summary(self) -> None:
        assert parse_summary("[INFO] BUILD SUCCESS") is None
