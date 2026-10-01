"""Failure Log Parser（Phase 2）。

对外统一入口 parse_maven_log：从一段 Maven 构建日志产出
FailureContext（结构化失败证据）与 TriageResult（确定性故障分类）。
"""

from devfix.models import (
    CompileError,
    FailedTest,
    FailureContext,
    FailureType,
    StackFrame,
    StackTrace,
    TestSummary,
    TriageResult,
)
from devfix.parsing.compiler import parse_compile_errors
from devfix.parsing.stack_trace import parse_stack_traces
from devfix.parsing.surefire import parse_failed_tests, parse_summary
from devfix.parsing.surefire_xml import parse_surefire_reports, test_key
from devfix.parsing.triage import triage

__all__ = [
    "CompileError",
    "FailedTest",
    "FailureContext",
    "FailureType",
    "StackFrame",
    "StackTrace",
    "TestSummary",
    "TriageResult",
    "parse_compile_errors",
    "parse_failed_tests",
    "parse_maven_log",
    "parse_stack_traces",
    "parse_summary",
    "parse_surefire_reports",
    "test_key",
    "triage",
]


def _extract_key_error(ctx: FailureContext) -> str | None:
    """提取关键异常简名：优先失败测试的错误类型，其次第一条异常栈。"""
    for t in ctx.failed_tests:
        if t.error_type:
            return t.error_type.rsplit(".", 1)[-1]
    if ctx.stack_traces:
        return ctx.stack_traces[0].simple_type
    return None


def parse_maven_log(
    text: str, *, raw_log_path: str | None = None
) -> tuple[FailureContext, TriageResult]:
    """解析 Maven 构建日志，返回 (FailureContext, TriageResult)。

    这是 Triage 阶段（系统设计 5.3~5.4）的确定性部分：
    不调用 LLM，全部基于规则与文本特征，输出可解释的证据。
    """
    ctx = FailureContext(
        failed_tests=parse_failed_tests(text),
        test_summary=parse_summary(text),
        stack_traces=parse_stack_traces(text),
        compile_errors=parse_compile_errors(text),
        raw_log_path=raw_log_path,
    )
    ctx.key_error = _extract_key_error(ctx)
    return ctx, triage(ctx, raw_log=text)
