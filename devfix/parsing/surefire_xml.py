"""Surefire XML 报告解析（验证层的权威测试结果来源）。

Maven Surefire 在 target/surefire-reports/ 下同时产出结构化 XML 报告：
每个测试类一个 TEST-*.xml，含每个用例的类名、方法名、失败/错误、
message 与完整栈帧。console 日志解析（surefire.py）存在单锚点脆弱
（locale 依赖、参数化测试名、多模块下"取最后一个汇总"），XML 没有这些问题
——验证层以 XML 为权威，console 解析只作 fallback 与编译错误来源。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from devfix.models import FailedTest, TestSummary

_MESSAGE_MAX_CHARS = 500


def test_key(class_name: str, method_name: str) -> str:
    """测试用例的规范化标识：Class#method，剥掉参数化后缀。

    基线对比要求同一用例在两次运行中被识别为同一个：参数化编号
    （testFoo[1] 与 testFoo[2] 是同一测试的不同数据行）与 surefire 对
    显示名的括号包装都不能影响匹配。
    """
    method = re.sub(r"\[.*?\]|\(.*?\)", "", method_name).strip()
    return f"{class_name}#{method}"


def parse_surefire_reports(workspace: Path) -> tuple[TestSummary | None, list[FailedTest]]:
    """解析工作区下全部 surefire XML 报告（含多模块的 */target/）。

    Returns:
        (聚合的 TestSummary, 失败用例列表)。工作区没有任何 XML 报告时
        返回 (None, [])——调用方应回退到 console 日志解析。
    """
    total = TestSummary()
    failed: list[FailedTest] = []
    found = False
    for path in sorted(workspace.rglob("surefire-reports/TEST-*.xml")):
        try:
            root = ET.parse(path).getroot()
        except (ET.ParseError, OSError):
            continue  # 单个损坏的报告不拖垮整体
        found = True
        total.run += int(root.get("tests", "0") or 0)
        total.failures += int(root.get("failures", "0") or 0)
        total.errors += int(root.get("errors", "0") or 0)
        total.skipped += int(root.get("skipped", "0") or 0)
        for tc in root.iter("testcase"):
            failure = tc.find("failure")
            error = tc.find("error")
            node = failure if failure is not None else error
            if node is None:
                continue
            message = node.get("message") or ""
            failed.append(FailedTest(
                class_name=tc.get("classname") or "",
                method_name=tc.get("name") or "",
                kind="FAILURE" if failure is not None else "ERROR",
                error_type=node.get("type"),
                message=message[:_MESSAGE_MAX_CHARS] or None,
            ))
    return (total if found else None), failed
