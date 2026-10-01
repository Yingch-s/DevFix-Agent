"""Triage 规则测试：每类故障样本 + 优先级与兜底行为。"""

from __future__ import annotations

from devfix.models import FailureType
from devfix.parsing import parse_maven_log
from tests.sample_logs import (
    COMPILATION_ERROR,
    DEPENDENCY_ERROR,
    GARBAGE_LOG,
    JUNIT4_NPE_ERROR,
    JUNIT5_ASSERTION_FAILURE,
    SPRING_CONTEXT_FAILURE,
)


def _triage(log: str):
    return parse_maven_log(log)[1]


class TestFailureTypes:
    def test_junit5_assertion_is_unit_test_failure(self) -> None:
        r = _triage(JUNIT5_ASSERTION_FAILURE)
        assert r.failure_type == FailureType.UNIT_TEST_FAILURE
        assert r.supported
        assert r.confidence >= 0.9
        assert r.key_error == "AssertionError"
        assert "失败测试" in r.reason

    def test_junit4_npe_is_unit_test_failure(self) -> None:
        r = _triage(JUNIT4_NPE_ERROR)
        assert r.failure_type == FailureType.UNIT_TEST_FAILURE
        assert r.key_error == "NullPointerException"

    def test_compilation_error(self) -> None:
        r = _triage(COMPILATION_ERROR)
        assert r.failure_type == FailureType.COMPILE_ERROR
        assert r.confidence >= 0.9
        assert "2 条编译错误" in r.reason

    def test_dependency_error(self) -> None:
        r = _triage(DEPENDENCY_ERROR)
        assert r.failure_type == FailureType.DEPENDENCY_ERROR
        assert "依赖错误" in r.reason

    def test_spring_context_failure_beats_unit_test(self) -> None:
        # Spring 样本里也含失败测试，但 Spring 规则优先级更高
        r = _triage(SPRING_CONTEXT_FAILURE)
        assert r.failure_type == FailureType.SPRING_CONTEXT_FAILURE
        assert "Spring" in r.reason

    def test_garbage_is_unknown_and_unsupported(self) -> None:
        r = _triage(GARBAGE_LOG)
        assert r.failure_type == FailureType.UNKNOWN
        assert not r.supported
        assert r.confidence <= 0.3


class TestKeyError:
    def test_compile_error_has_no_key_error(self) -> None:
        ctx, _ = parse_maven_log(COMPILATION_ERROR)
        assert ctx.key_error is None

    def test_npe_key_error(self) -> None:
        ctx, _ = parse_maven_log(JUNIT4_NPE_ERROR)
        assert ctx.key_error == "NullPointerException"
