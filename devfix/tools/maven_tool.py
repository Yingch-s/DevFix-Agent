"""Maven 进程执行封装（对应系统设计 10.7~10.9 与 5.12）。

设计要点：
- Agent 永不拼接 shell 命令：命令由系统按模板构造，本模块只负责执行；
- 超时即终止进程并返回 TIMEOUT（区别于 PASS/FAIL）；
- 输出截取末尾若干行作为证据，避免超长日志撑爆 Agent 上下文；
- mvn 定位顺序：显式配置 > 环境变量 DEVFIX_MVN > PATH > 常见安装目录。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from devfix.tools.errors import ToolError

# 定位 mvn：Windows 下优先 .cmd
_MVN_NAMES = ("mvn", "mvn.cmd")
LOG_TAIL_LINES = 200


def find_mvn(explicit: str | None = None) -> str:
    """定位 mvn 可执行文件（不依赖用户手动配置 PATH）。"""
    if explicit:
        if Path(explicit).is_file() or shutil.which(explicit):
            return explicit
        raise ToolError(f"配置的 Maven 路径不存在：{explicit}")

    env = os.environ.get("DEVFIX_MVN")
    if env:
        return env

    for name in _MVN_NAMES:
        found = shutil.which(name)
        if found:
            return found

    # 常见安装位置（含 DevFix 一键安装的用户目录）
    candidates: list[Path] = []
    user_tools = Path.home() / "tools"
    if user_tools.is_dir():
        candidates += sorted(user_tools.glob("apache-maven-*/bin/mvn.cmd"))
    for base in (Path("C:/Program Files"), Path("C:/apache-maven")):
        if base.is_dir():
            candidates += sorted(base.glob("apache-maven-*/bin/mvn.cmd"))
    for c in candidates:
        if c.is_file():
            return str(c)

    raise ToolError(
        "未找到 mvn。请安装 Maven 并加入 PATH，或设置环境变量 DEVFIX_MVN，"
        "或在 devfix.yaml 配置 verification.maven_command。"
    )


@dataclass
class RawRun:
    """一次 Maven 执行的原始结果。"""

    command: str
    exit_code: int | None
    duration_ms: int
    log: str
    timed_out: bool


class MavenTool:
    """在指定工作目录执行 Maven 命令。"""

    def __init__(self, workspace: Path, mvn: str | None = None) -> None:
        self.workspace = Path(workspace).resolve()
        if not self.workspace.is_dir():
            raise ToolError(f"工作目录不存在：{self.workspace}")
        self.mvn = find_mvn(mvn)

    def run(
        self,
        goals: list[str],
        timeout_seconds: int = 600,
        extra_args: list[str] | None = None,
    ) -> RawRun:
        """执行 `mvn -B <extra_args...> <goals...>`，超时即终止**整棵进程树**。

        extra_args 用于附加跳过质量门禁等参数（见 config.QUALITY_GATE_SKIP_ARGS）。

        必须杀整棵树：Windows 下 mvn 是 mvn.cmd → java.exe 的多层包装，
        只杀直接子进程会让 Surefire fork 的 JVM 变孤儿——继续持有
        target/ 文件锁与端口，后续所有验证层连坐失败（实测教训）。
        """
        cmd = [self.mvn, "-B", *(extra_args or []), *goals]
        started = time.monotonic()
        kwargs: dict = {}
        if os.name == "nt":
            # 新进程组：taskkill /T 需要能枚举整棵树
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            # 独立进程组：超时时可对整组发信号
            kwargs["start_new_session"] = True
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=self.workspace,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                **kwargs,
            )
        except OSError as e:
            raise ToolError(f"Maven 执行失败：{e}") from e

        timed_out = False
        try:
            out, err = proc.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._kill_tree(proc)
            out, err = proc.communicate()
        duration = int((time.monotonic() - started) * 1000)
        return RawRun(
            command=" ".join(cmd),
            exit_code=None if timed_out else proc.returncode,
            duration_ms=duration,
            log=(out or "") + (err or ""),
            timed_out=timed_out,
        )

    @staticmethod
    def _kill_tree(proc: subprocess.Popen) -> None:
        """终止 mvn 及其全部子孙进程（JVM、fork 的 surefire booter）。"""
        if os.name == "nt":
            # /T = 整棵树，/F = 强制；taskkill 拿不到句柄时进程可能已自行退出
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=30, check=False,
            )
        else:
            import signal

            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()


def tail_lines(text: str, limit: int = LOG_TAIL_LINES) -> str:
    """截取末尾 limit 行（验证证据只保留最有信息量的尾部）。"""
    lines = text.splitlines()
    if len(lines) <= limit:
        return text
    return "\n".join(lines[-limit:])
