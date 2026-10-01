"""Surefire XML 报告解析测试（验证层的权威测试结果来源）。"""

from __future__ import annotations

from devfix.parsing.surefire_xml import parse_surefire_reports
from devfix.parsing.surefire_xml import test_key as norm_key

XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="com.example.OrderServiceTest" tests="3" failures="1" errors="1" skipped="1">
  <testcase name="testPass" classname="com.example.OrderServiceTest" time="0.01"/>
  <testcase name="testAssert" classname="com.example.OrderServiceTest" time="0.02">
    <failure message="expected: &lt;2&gt; but was: &lt;1&gt;" type="org.opentest4j.AssertionFailedError">stack line</failure>
  </testcase>
  <testcase name="testError" classname="com.example.OrderServiceTest" time="0.03">
    <error message="boom" type="java.lang.IllegalStateException">at com.example.X</error>
  </testcase>
  <testcase name="testSkip[1]" classname="com.example.ParamTest" time="0.01">
    <skipped/>
  </testcase>
</testsuite>
"""


class TestParseSurefireReports:
    def _write(self, tmp_path, xml: str, rel: str = "target/surefire-reports/TEST-com.example.OrderServiceTest.xml"):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(xml, encoding="utf-8")
        return p

    def test_summary_and_failures_parsed(self, tmp_path) -> None:
        self._write(tmp_path, XML)
        summary, failed = parse_surefire_reports(tmp_path)
        assert summary is not None
        assert (summary.run, summary.failures, summary.errors, summary.skipped) == (3, 1, 1, 1)
        assert len(failed) == 2
        kinds = {t.method_name: t.kind for t in failed}
        assert kinds == {"testAssert": "FAILURE", "testError": "ERROR"}
        assert failed[0].error_type == "org.opentest4j.AssertionFailedError"

    def test_multi_module_reports_aggregated(self, tmp_path) -> None:
        self._write(tmp_path, XML.replace('tests="3"', 'tests="2"'), "module-a/target/surefire-reports/TEST-a.xml")
        self._write(tmp_path, XML, "module-b/target/surefire-reports/TEST-b.xml")
        summary, _ = parse_surefire_reports(tmp_path)
        assert summary.run == 2 + 3

    def test_no_reports_returns_none(self, tmp_path) -> None:
        summary, failed = parse_surefire_reports(tmp_path)
        assert summary is None
        assert failed == []

    def test_malformed_report_skipped(self, tmp_path) -> None:
        self._write(tmp_path, "<testsuite><unclosed>", "target/surefire-reports/TEST-bad.xml")
        summary, failed = parse_surefire_reports(tmp_path)
        # 唯一报告损坏 → 视同无报告（调用方回退 console 解析），不抛异常
        assert summary is None
        assert failed == []


class TestTestKey:
    def test_parameterized_suffix_stripped(self) -> None:
        assert norm_key("T", "testFoo[1]") == norm_key("T", "testFoo[2]")
        assert norm_key("T", "testFoo") == "T#testFoo"

    def test_parenthesized_display_name_stripped(self) -> None:
        assert norm_key("com.x.T", "testFoo(com.x.T)") == "com.x.T#testFoo"
