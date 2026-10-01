"""VerificationRunner / MavenTool 测试。

用伪造的 mvn（.cmd → python 脚本）驱动，覆盖 PASS / FAIL / 编译错误 / TIMEOUT
全部分支，不依赖真实 Maven；真实 Maven 的端到端验证走 llm/集成测试。
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from devfix.models import VerificationLevel, VerificationStatus
from devfix.tools import ToolError, find_mvn, tail_lines
from devfix.tools.maven_tool import MavenTool
from devfix.verify import VerificationRunner
from tests.sample_logs import COMPILATION_ERROR, JUNIT5_ASSERTION_FAILURE


def _fake_mvn(tmp_path: Path, exit_code: int = 0, log: str = "", sleep: float = 0.0) -> str:
    """生成一个假的 mvn 可执行文件（Windows .cmd / POSIX sh），回放给定输出。"""
    script = tmp_path / "fake_mvn.py"
    script.write_text(
        textwrap.dedent(f"""
            import sys, time
            time.sleep({sleep})
            sys.stdout.write({log!r})
            sys.exit({exit_code})
        """),
        encoding="utf-8",
    )
    if sys.platform == "win32":
        cmd = tmp_path / "fake_mvn.cmd"
        cmd.write_text(
            f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8"
        )
        return str(cmd)
    sh = tmp_path / "fake_mvn.sh"
    sh.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
    sh.chmod(0o755)
    return str(sh)


class TestFindMvn:
    def test_explicit_path_validated(self, tmp_path) -> None:
        with pytest.raises(ToolError, match="不存在"):
            find_mvn(str(tmp_path / "nope"))

    def test_explicit_path_used(self, tmp_path) -> None:
        fake = _fake_mvn(tmp_path)
        assert find_mvn(fake) == fake

    def test_env_var_takes_precedence(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("DEVFIX_MVN", "/custom/mvn")
        assert find_mvn() == "/custom/mvn"


class TestMavenTool:
    def test_run_captures_output_and_exit_code(self, tmp_path) -> None:
        fake = _fake_mvn(tmp_path, exit_code=0, log="BUILD SUCCESS")
        raw = MavenTool(tmp_path, mvn=fake).run(["test"])
        assert raw.exit_code == 0
        assert "BUILD SUCCESS" in raw.log
        assert raw.timed_out is False
        assert raw.duration_ms >= 0
        assert "-B" in raw.command  # 始终批处理模式

    def test_missing_workspace(self, tmp_path) -> None:
        with pytest.raises(ToolError, match="工作目录不存在"):
            MavenTool(tmp_path / "nope", mvn=_fake_mvn(tmp_path))

    def test_timeout_terminates(self, tmp_path) -> None:
        fake = _fake_mvn(tmp_path, sleep=5)
        raw = MavenTool(tmp_path, mvn=fake).run(["test"], timeout_seconds=1)
        assert raw.timed_out is True
        assert raw.exit_code is None


class TestVerificationRunner:
    def test_pass(self, tmp_path) -> None:
        fake = _fake_mvn(tmp_path, exit_code=0, log="[INFO] Tests run: 3, Failures: 0, Errors: 0, Skipped: 0\n[INFO] BUILD SUCCESS")
        r = VerificationRunner(tmp_path, mvn=fake).run_full()
        assert r.status is VerificationStatus.PASS
        assert r.passed
        assert r.failed_tests == []

    def test_focused_goals_carry_multimodule_flags(self, tmp_path) -> None:
        """-Dtest 在多模块 reactor 上会让不含目标类的模块报
        "No tests matching pattern" → 构建非零退出 → L1/L2 结构性必败。
        L1/L2 必须携带 failIfNoSpecifiedTests 开关。"""
        fake = _fake_mvn(tmp_path, exit_code=0, log="[INFO] BUILD SUCCESS")
        r = VerificationRunner(tmp_path, mvn=fake).run_focused("com.example.T", "m")
        assert "-Dsurefire.failIfNoSpecifiedTests=false" in r.command
        assert "-DfailIfNoTests=false" in r.command

    def test_fail_with_failed_tests(self, tmp_path) -> None:
        fake = _fake_mvn(tmp_path, exit_code=1, log=JUNIT5_ASSERTION_FAILURE)
        r = VerificationRunner(tmp_path, mvn=fake).run_focused(
            "com.example.OrderServiceTest", "shouldRejectDeletedUser"
        )
        assert r.status is VerificationStatus.FAIL
        assert not r.passed
        assert len(r.failed_tests) == 1
        assert r.failed_tests[0].method_name == "shouldRejectDeletedUser"
        assert any("AssertionError" in e for e in r.new_errors)
        # 结构化上下文供 Reflection 使用
        assert r.failure_context is not None

    def test_fail_with_compile_error(self, tmp_path) -> None:
        fake = _fake_mvn(tmp_path, exit_code=1, log=COMPILATION_ERROR)
        r = VerificationRunner(tmp_path, mvn=fake).run_full()
        assert r.status is VerificationStatus.FAIL
        assert len(r.new_errors) == 2
        assert any("cannot find symbol" in e for e in r.new_errors)

    def test_timeout_status(self, tmp_path) -> None:
        fake = _fake_mvn(tmp_path, sleep=5)
        runner = VerificationRunner(
            tmp_path, mvn=fake, timeouts={VerificationLevel.FULL: 1}
        )
        r = runner.run_full()
        assert r.status is VerificationStatus.TIMEOUT
        assert any("超时" in e for e in r.new_errors)

    def test_error_when_mvn_unavailable(self, tmp_path) -> None:
        runner = VerificationRunner(tmp_path, mvn=str(tmp_path / "missing-mvn"))
        r = runner.run_full()
        assert r.status is VerificationStatus.ERROR
        assert r.new_errors


class TestCommandConstruction:
    """验证命令由系统构造，Agent 不接触 shell（设计文档 10.7~10.9）。"""

    def test_focused_with_method(self, tmp_path) -> None:
        fake = _fake_mvn(tmp_path)
        r = VerificationRunner(tmp_path, mvn=fake).run_focused("com.example.T", "shouldFail")
        assert "-Dtest=com.example.T#shouldFail" in r.command

    def test_focused_without_method(self, tmp_path) -> None:
        fake = _fake_mvn(tmp_path)
        r = VerificationRunner(tmp_path, mvn=fake).run_focused("com.example.T")
        assert "-Dtest=com.example.T" in r.command
        assert "#" not in r.command

    def test_related_and_full(self, tmp_path) -> None:
        fake = _fake_mvn(tmp_path)
        runner = VerificationRunner(tmp_path, mvn=fake)
        assert "-Dtest=com.example.T" in runner.run_related("com.example.T").command
        full = runner.run_full().command
        assert full.endswith("-B test")


class TestLogTail:
    def test_short_log_unchanged(self) -> None:
        assert tail_lines("a\nb", limit=10) == "a\nb"

    def test_long_log_truncated_to_tail(self) -> None:
        text = "\n".join(f"line {i}" for i in range(500))
        out = tail_lines(text, limit=10)
        assert out.splitlines() == [f"line {i}" for i in range(490, 500)]


_XML_TWO_FAILURES = """\
<?xml version="1.0"?>
<testsuite tests="2" failures="2" errors="0" skipped="0">
  <testcase name="testKnown" classname="com.example.T">
    <failure message="old env failure" type="java.lang.AssertionError"/>
  </testcase>
  <testcase name="testNew[1]" classname="com.example.T">
    <failure message="fresh regression" type="java.lang.AssertionError"/>
  </testcase>
</testsuite>
"""


def _fake_mvn_writing_xml(tmp_path: Path, xml: str, exit_code: int = 1) -> str:
    """伪造 mvn：运行时在 cwd（工作区）写一份 surefire XML 再退出。"""
    script = tmp_path / "fake_mvn_xml.py"
    script.write_text(
        textwrap.dedent(f"""
            import os, sys
            report_dir = os.path.join("target", "surefire-reports")
            os.makedirs(report_dir, exist_ok=True)
            with open(os.path.join(report_dir, "TEST-com.example.T.xml"), "w", encoding="utf-8") as f:
                f.write({xml!r})
            sys.stdout.write("[INFO] console log (locale-dependent, unparseable)")
            sys.exit({exit_code})
        """),
        encoding="utf-8",
    )
    if sys.platform == "win32":
        cmd = tmp_path / "fake_mvn_xml.cmd"
        cmd.write_text(
            f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8"
        )
        return str(cmd)
    sh = tmp_path / "fake_mvn_xml.sh"
    sh.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
    sh.chmod(0o755)
    return str(sh)


class TestSurefireXmlAndBaseline:
    """XML 权威来源 + 失败基线过滤（SWE-bench F2P+P2P 语义）。"""

    def test_xml_is_authoritative_over_console(self, tmp_path) -> None:
        fake = _fake_mvn_writing_xml(tmp_path, _XML_TWO_FAILURES, exit_code=1)
        r = VerificationRunner(tmp_path, mvn=fake).run_full()
        assert r.status is VerificationStatus.FAIL
        # 失败用例来自 XML（console 故意不可解析）
        assert {t.method_name for t in r.failed_tests} == {"testKnown", "testNew[1]"}

    def test_baseline_failures_do_not_block_pass(self, tmp_path) -> None:
        """基线内的失败（环境既有）不算 FAIL：只剩基线失败 → PASS。"""
        xml_known_only = _XML_TWO_FAILURES.replace(
            '  <testcase name="testNew[1]" classname="com.example.T">\n'
            '    <failure message="fresh regression" type="java.lang.AssertionError"/>\n'
            "  </testcase>\n",
            "",
        ).replace('tests="2" failures="2"', 'tests="1" failures="1"')
        fake = _fake_mvn_writing_xml(tmp_path, xml_known_only, exit_code=1)
        runner = VerificationRunner(
            tmp_path, mvn=fake, baseline_failures={"com.example.T#testKnown"}
        )
        r = runner.run_full()
        assert r.status is VerificationStatus.PASS
        assert r.failed_tests == []  # 基线失败被滤掉，反思不会再追它

    def test_new_failure_still_fails(self, tmp_path) -> None:
        fake = _fake_mvn_writing_xml(tmp_path, _XML_TWO_FAILURES, exit_code=1)
        runner = VerificationRunner(
            tmp_path, mvn=fake, baseline_failures={"com.example.T#testKnown"}
        )
        r = runner.run_full()
        # testNew[1] 不在基线里 → 参数化后缀归一化后应识别为"新增失败"
        assert r.status is VerificationStatus.FAIL
        assert [t.method_name for t in r.failed_tests] == ["testNew[1]"]
        assert "fresh regression" in r.new_errors[0]

    def test_compile_errors_never_baseline_filtered(self, tmp_path) -> None:
        """编译错误不参与基线过滤——buggy 提交本身能编译，验证时的
        编译错误必然是补丁引入的。"""
        fake = _fake_mvn(tmp_path, exit_code=1, log=COMPILATION_ERROR)
        r = VerificationRunner(
            tmp_path, mvn=fake, baseline_failures={"a#b"}
        ).run_full()
        assert r.status is VerificationStatus.FAIL
        assert r.new_errors and "cannot find symbol" in r.new_errors[0]
