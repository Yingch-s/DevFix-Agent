"""确定性 Triage 规则（对应系统设计 5.4）。

MVP 不使用 LLM：按优先级匹配特征信号，输出可解释的故障分类。
规则顺序即优先级——靠后的规则不能覆盖靠前规则的结论。
"""

from __future__ import annotations

from devfix.models import FailureContext, FailureType, TriageResult

# 各类型在日志文本中的特征信号（命中任一即触发）
DEPENDENCY_SIGNALS = (
    "Could not resolve dependencies",
    "Could not find artifact",
    "Could not transfer artifact",
    "Failed to read artifact descriptor",
    "The following artifacts could not be resolved",
)
SPRING_SIGNALS = (
    "UnsatisfiedDependencyException",
    "NoSuchBeanDefinitionException",
    "BeanCreationException",
    "BeanDefinitionStoreException",
    "Error creating bean with name",
    "Failed to load ApplicationContext",
)
CONFIG_SIGNALS = (
    "Could not resolve placeholder",
    "Failed to bind properties",
    "Binding to target",
)
COMPILATION_MARKER = "COMPILATION ERROR"


def _hit(text: str, signals: tuple[str, ...]) -> str | None:
    """返回第一个命中的信号，未命中返回 None。"""
    for s in signals:
        if s in text:
            return s
    return None


def triage(ctx: FailureContext, raw_log: str = "") -> TriageResult:
    """基于结构化证据 + 原始日志特征进行分类。

    Args:
        ctx: parse_maven_log 产出的 FailureContext。
        raw_log: 原始日志文本（依赖/配置/Spring 类错误需要关键词特征）。
    """
    text = raw_log or ""

    signal = _hit(text, DEPENDENCY_SIGNALS)
    if signal:
        return TriageResult(
            failure_type=FailureType.DEPENDENCY_ERROR,
            confidence=0.9,
            key_error=ctx.key_error,
            reason=f"日志命中依赖错误特征：'{signal}'",
        )

    if ctx.compile_errors or COMPILATION_MARKER in text:
        return TriageResult(
            failure_type=FailureType.COMPILE_ERROR,
            confidence=0.95,
            key_error=ctx.key_error,
            reason=f"解析到 {len(ctx.compile_errors)} 条编译错误"
            if ctx.compile_errors
            else f"日志含 '{COMPILATION_MARKER}' 标记",
        )

    signal = _hit(text, SPRING_SIGNALS)
    if signal:
        return TriageResult(
            failure_type=FailureType.SPRING_CONTEXT_FAILURE,
            confidence=0.9,
            key_error=ctx.key_error,
            reason=f"日志命中 Spring 上下文失败特征：'{signal}'",
        )

    signal = _hit(text, CONFIG_SIGNALS)
    if signal:
        return TriageResult(
            failure_type=FailureType.CONFIG_ERROR,
            confidence=0.7,
            key_error=ctx.key_error,
            reason=f"日志命中配置错误特征：'{signal}'",
        )

    if ctx.failed_tests:
        return TriageResult(
            failure_type=FailureType.UNIT_TEST_FAILURE,
            confidence=0.9,
            key_error=ctx.key_error,
            reason=f"解析到 {len(ctx.failed_tests)} 个失败测试",
        )

    if ctx.stack_traces:
        return TriageResult(
            failure_type=FailureType.RUNTIME_EXCEPTION,
            confidence=0.7,
            key_error=ctx.key_error,
            reason=f"解析到 {len(ctx.stack_traces)} 条异常栈",
        )

    return TriageResult(
        failure_type=FailureType.UNKNOWN,
        confidence=0.3,
        supported=False,
        reason="无匹配规则（日志中未发现可识别的失败特征）",
    )
