"""Verification Runner（对应系统设计 5.12 与 15 节"由小到大"策略）。

    L1 FOCUSED  原失败用例      mvn -Dtest=Cls#method test     5 min
    L2 RELATED  相关测试类      mvn -Dtest=Cls test            10 min
    L3 FULL     完整构建         mvn test                      20 min

关键原则（设计文档 2.4）：模型说"应该修好了"不算数，只有执行结果算数。
验证失败的输出会被解析成结构化证据，供 Reflection 阶段使用（Phase 7）。
"""

from __future__ import annotations

from pathlib import Path

from devfix.models import (
    FailureContext,
    VerificationLevel,
    VerificationResult,
    VerificationStatus,
)
from devfix.parsing import parse_maven_log
from devfix.parsing.surefire_xml import parse_surefire_reports, test_key
from devfix.tools.errors import ToolError
from devfix.tools.maven_tool import MavenTool, tail_lines

# 各层默认超时（秒）：设计文档 2.7 的分层超时
DEFAULT_TIMEOUTS: dict[VerificationLevel, int] = {
    VerificationLevel.FOCUSED: 300,
    VerificationLevel.RELATED: 600,
    VerificationLevel.FULL: 1200,
}

# -Dtest 选择器在多模块 reactor 上会于每个模块执行：不含目标类的模块
# 默认直接失败（"No tests matching pattern"）→ 整个构建非零退出，
# L1/L2 对多模块项目结构性必败。两个开关分别覆盖新旧 Surefire 版本，
# 多余的属性会被旧插件忽略，无害。
FOCUSED_TEST_FLAGS = (
    "-Dsurefire.failIfNoSpecifiedTests=false",
    "-DfailIfNoTests=false",
)


class VerificationRunner:
    """在隔离工作区内分层执行验证。"""

    def __init__(
        self,
        workspace: Path,
        mvn: str | None = None,
        timeouts: dict[VerificationLevel, int] | None = None,
        extra_args: list[str] | None = None,
        baseline_failures: set[str] | None = None,
    ) -> None:
        self.workspace = Path(workspace)
        # 惰性构造 MavenTool：mvn 缺失、工作区不存在等系统错误
        # 应产出 ERROR 结果被记录（设计文档 19 节），而不是让流水线崩溃
        self._mvn = mvn
        self._timeouts = {**DEFAULT_TIMEOUTS, **(timeouts or {})}
        self._extra_args = list(extra_args or [])
        # 环境失败基线（buggy 提交上本来就失败的测试）：SWE-bench 的
        # F2P+P2P 语义——判定"无新增失败"而不是"全绿"，否则本机与补丁
        # 无关的既有失败测试会误杀已修好的补丁
        self._baseline = baseline_failures or set()

    # ------------------------------------------------------------------ 分层入口
    def run_focused(self, test_class: str, test_method: str | None = None) -> VerificationResult:
        """L1：只跑原失败用例（最快反馈）。"""
        selector = f"{test_class}#{test_method}" if test_method else test_class
        return self._run(
            VerificationLevel.FOCUSED,
            ["-Dtest=" + selector, *FOCUSED_TEST_FLAGS, "test"],
            verification_id="VR-L1",
        )

    def run_related(self, test_class: str) -> VerificationResult:
        """L2：跑相关测试类，确认没有破坏同模块其他行为。"""
        return self._run(
            VerificationLevel.RELATED,
            [f"-Dtest={test_class}", *FOCUSED_TEST_FLAGS, "test"],
            verification_id="VR-L2",
        )

    def run_full(self) -> VerificationResult:
        """L3：完整构建。"""
        return self._run(VerificationLevel.FULL, ["test"], verification_id="VR-L3")

    # ------------------------------------------------------------------ 执行
    def _run(
        self,
        level: VerificationLevel,
        goals: list[str],
        verification_id: str,
    ) -> VerificationResult:
        timeout = self._timeouts[level]
        try:
            maven = MavenTool(self.workspace, mvn=self._mvn)
            raw = maven.run(goals, timeout_seconds=timeout, extra_args=self._extra_args)
        except ToolError as e:
            return VerificationResult(
                verification_id=verification_id,
                level=level,
                status=VerificationStatus.ERROR,
                command=f"mvn {' '.join(goals)}",
                new_errors=[str(e)],
            )

        if raw.timed_out:
            return VerificationResult(
                verification_id=verification_id,
                level=level,
                status=VerificationStatus.TIMEOUT,
                command=raw.command,
                duration_ms=raw.duration_ms,
                log_tail=tail_lines(raw.log),
                new_errors=[f"Maven 执行超时（>{timeout}s）"],
            )

        # 复用 Phase 2 的日志解析：失败测试 / 异常 / 编译错误都结构化
        ctx, _ = parse_maven_log(raw.log)
        failed_tests = ctx.failed_tests
        test_summary = ctx.test_summary

        # Surefire XML 是权威来源（console 解析有 locale/格式脆弱性）；
        # console 解析仍提供编译错误——XML 不含编译信息
        xml_summary, xml_failed = parse_surefire_reports(self.workspace)
        if xml_summary is not None:
            failed_tests = xml_failed
            test_summary = xml_summary
        ctx = ctx.model_copy(update={
            "failed_tests": failed_tests, "test_summary": test_summary,
        })

        status = VerificationStatus.PASS if raw.exit_code == 0 else VerificationStatus.FAIL

        # 基线过滤（SWE-bench F2P+P2P 语义）：只有"新增失败"才构成 FAIL。
        # 编译错误不参与过滤——buggy 提交本身能编译，验证时的编译错误
        # 必然是补丁引入的。
        if (
            status is VerificationStatus.FAIL
            and self._baseline
            and not ctx.compile_errors
        ):
            new_failures = [
                t for t in failed_tests
                if test_key(t.class_name, t.method_name) not in self._baseline
            ]
            failed_tests = new_failures
            ctx = ctx.model_copy(update={"failed_tests": new_failures})
            if not new_failures:
                status = VerificationStatus.PASS

        new_errors: list[str] = []
        if status is VerificationStatus.FAIL:
            if ctx.compile_errors:
                new_errors = [
                    f"{ce.file_path}:{ce.line} {ce.message}" for ce in ctx.compile_errors
                ]
            elif failed_tests:
                new_errors = [
                    f"{t.display_name}: {t.error_type or ''} {t.message or ''}".strip()
                    for t in failed_tests
                ]
            elif ctx.stack_traces:
                new_errors = [f"{ctx.stack_traces[0].exception_type}: {ctx.stack_traces[0].message or ''}"]

        return VerificationResult(
            verification_id=verification_id,
            level=level,
            status=status,
            command=raw.command,
            exit_code=raw.exit_code,
            duration_ms=raw.duration_ms,
            failed_tests=failed_tests,
            new_errors=new_errors,
            log_tail=tail_lines(raw.log),
            failure_context=ctx,
        )


class MavenVerifier:
    """Verifier 协议实现：按"由小到大"分层验证并逐层升级（设计文档 15 节）。

    工作区由图中节点动态创建，因此在 verify() 调用时才绑定。
    """

    def __init__(
        self,
        mvn: str | None = None,
        timeouts: dict[VerificationLevel, int] | None = None,
        extra_args: list[str] | None = None,
        baseline_failures: set[str] | None = None,
    ) -> None:
        self._mvn = mvn
        self._timeouts = timeouts
        self._extra_args = extra_args
        self._baseline = baseline_failures

    def verify(
        self, failure: FailureContext, workspace: Path,
        baseline_failures: set[str] | None = None,
    ) -> list[VerificationResult]:
        """L1 原失败用例 → （通过则）L2 相关测试类 → （通过则）L3 完整构建。

        任一层失败即停止，失败结果直接作为 Reflection 的输入证据。
        baseline_failures 在调用时传入优先（graph 经 state 动态提供），
        否则用构造时绑定的值。
        """
        runner = VerificationRunner(
            workspace, mvn=self._mvn, timeouts=self._timeouts,
            extra_args=self._extra_args,
            baseline_failures=baseline_failures if baseline_failures is not None else self._baseline,
        )
        results: list[VerificationResult] = []

        if failure.failed_tests:
            test = failure.failed_tests[0]
            results.append(runner.run_focused(test.class_name, test.method_name))
            if results[-1].passed:
                results.append(runner.run_related(test.class_name))
        else:
            # 无失败用例信息（如编译错误）→ 直接完整构建
            results.append(runner.run_full())

        if results[-1].passed and results[-1].level is not VerificationLevel.FULL:
            results.append(runner.run_full())
        return results
