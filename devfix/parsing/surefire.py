"""Maven Surefire 测试输出解析。

失败头格式随 surefire 版本演进，实测遇到过的变体：

    老格式 (surefire 2.x / JUnit 4):  shouldRejectDeletedUser(com.example.T)  Time elapsed: 0.045 sec  <<< FAILURE!
    新格式 (surefire 3.x / JUnit 5):  shouldRejectDeletedUser  Time elapsed: 0.045 s  <<< FAILURE!
    实测 3.2.5 + JUnit 5:             com.example.T.shouldRejectDeletedUser -- Time elapsed: 0.005 s  <<< FAILURE!

因此解析策略是"以 `<<< FAILURE!/ERROR!` 为锚点，自行识别前缀形态"，
而不是为每种格式写死正则——新增变体时只需扩展 _interpret_prefix。
"""

from __future__ import annotations

import re

from devfix.models import FailedTest, TestSummary
from devfix.parsing.stack_trace import _strip_level_prefix

# 汇总行：Tests run: 5, Failures: 1, Errors: 0, Skipped: 0
_SUMMARY_RE = re.compile(
    r"Tests run:\s*(?P<run>\d+),\s*Failures:\s*(?P<failures>\d+),"
    r"\s*Errors:\s*(?P<errors>\d+),\s*Skipped:\s*(?P<skipped>\d+)"
)

# 类上下文（新格式）："-- in com.example.OrderServiceTest"
_CLASS_RE = re.compile(r"--\s+in\s+([\w.$]+)")

# 失败头：任意前缀 + 可选 "-- " + Time elapsed + <<< FAILURE!/ERROR!
_HEADER_RE = re.compile(
    r"^(?P<prefix>.+?)\s+(?:--\s+)?Time elapsed:[^<]*<<<\s*(?P<kind>FAILURE|ERROR)!"
)

# 前缀形态
_OLD_PREFIX_RE = re.compile(r"^(?P<method>[\w$]+)\((?P<class>[\w.$]+)\)$")   # method(Class)
_FQ_PREFIX_RE = re.compile(r"^(?P<class>[\w.$]+)\.(?P<method>[\w$]+)$")      # Class.method
_BARE_PREFIX_RE = re.compile(r"^(?P<method>[\w$]+)$")                        # method

# 异常行：java.lang.AssertionError: expected: <2> but was: <1>
_EXCEPTION_RE = re.compile(
    r"^(?P<type>[\w.$]+(?:Exception|Error|Throwable))(?::\s?(?P<msg>.*))?\s*$"
)


def _interpret_prefix(prefix: str, current_class: str | None) -> tuple[str, str] | None:
    """把失败头前缀解释为 (class, method)；无法解释时返回 None。"""
    if prefix.startswith(("Tests run:", "Results")):
        return None  # 这是汇总行，不是失败头
    m = _OLD_PREFIX_RE.match(prefix)
    if m:
        return m.group("class"), m.group("method")
    m = _FQ_PREFIX_RE.match(prefix)
    if m:
        return m.group("class"), m.group("method")
    m = _BARE_PREFIX_RE.match(prefix)
    if m and current_class:
        return current_class, m.group("method")
    return None


def parse_summary(text: str) -> TestSummary | None:
    """提取 Tests run 汇总。日志中可能出现多次（分测试类 + Results 总表），取最后一次。"""
    last: TestSummary | None = None
    for m in _SUMMARY_RE.finditer(text):
        last = TestSummary(
            run=int(m.group("run")),
            failures=int(m.group("failures")),
            errors=int(m.group("errors")),
            skipped=int(m.group("skipped")),
        )
    return last


def parse_failed_tests(text: str) -> list[FailedTest]:
    """提取全部失败/报错的测试用例及其首个异常信息。"""
    tests: list[FailedTest] = []
    current_class: str | None = None
    pending: FailedTest | None = None  # 等待填充 error_type/message 的测试

    for raw in text.splitlines():
        line = _strip_level_prefix(raw).rstrip()

        m = _CLASS_RE.search(line)
        if m:
            current_class = m.group(1)

        m = _HEADER_RE.match(line)
        if m:
            interpreted = _interpret_prefix(m.group("prefix"), current_class)
            if interpreted:
                class_name, method_name = interpreted
                current_class = class_name
                pending = FailedTest(
                    class_name=class_name,
                    method_name=method_name,
                    kind=m.group("kind"),
                )
                tests.append(pending)
                continue

        if pending is not None and pending.error_type is None:
            m = _EXCEPTION_RE.match(line)
            if m:
                pending.error_type = m.group("type")
                pending.message = m.group("msg")

    return tests
