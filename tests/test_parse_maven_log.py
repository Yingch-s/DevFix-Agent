"""parse_maven_log 端到端测试：日志 -> FailureContext + TriageResult。"""

from __future__ import annotations

from devfix.models import FailureType
from devfix.parsing import parse_maven_log
from tests.sample_logs import (
    CLEAN_BUILD,
    COMPILATION_ERROR,
    JUNIT4_NPE_ERROR,
    JUNIT5_ASSERTION_FAILURE,
    SPRING_CONTEXT_FAILURE,
)


class TestFailureLogs:
    def test_junit5_full_context(self) -> None:
        ctx, tri = parse_maven_log(
            JUNIT5_ASSERTION_FAILURE, raw_log_path="/ci/logs/build.log"
        )
        assert ctx.raw_log_path == "/ci/logs/build.log"
        assert len(ctx.failed_tests) == 1
        assert ctx.test_summary is not None
        assert ctx.test_summary.failures == 1
        assert len(ctx.stack_traces) == 1
        assert ctx.compile_errors == []
        assert ctx.key_error == "AssertionError"
        assert tri.failure_type == FailureType.UNIT_TEST_FAILURE

    def test_junit4_full_context(self) -> None:
        ctx, tri = parse_maven_log(JUNIT4_NPE_ERROR)
        t = ctx.failed_tests[0]
        assert t.kind == "ERROR"
        # Caused by 链应完整保留
        st = ctx.stack_traces[0]
        assert st.cause is not None
        assert st.cause.frames[0].location == "UserRepository.java:45"
        assert tri.key_error == "NullPointerException"

    def test_compilation_context(self) -> None:
        ctx, tri = parse_maven_log(COMPILATION_ERROR)
        assert len(ctx.compile_errors) == 2
        assert ctx.failed_tests == []
        assert ctx.stack_traces == []
        assert tri.failure_type == FailureType.COMPILE_ERROR

    def test_spring_context(self) -> None:
        ctx, tri = parse_maven_log(SPRING_CONTEXT_FAILURE)
        assert ctx.failed_tests[0].method_name == "contextLoads"
        assert tri.failure_type == FailureType.SPRING_CONTEXT_FAILURE


class TestCleanBuild:
    def test_no_failures(self) -> None:
        ctx, tri = parse_maven_log(CLEAN_BUILD)
        assert ctx.failed_tests == []
        assert ctx.stack_traces == []
        assert ctx.compile_errors == []
        assert ctx.key_error is None
        assert ctx.test_summary is not None
        assert ctx.test_summary.run == 8
        assert tri.failure_type == FailureType.UNKNOWN
        assert not tri.supported
