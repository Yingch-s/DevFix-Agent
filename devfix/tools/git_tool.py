"""Git 操作封装（对应系统设计 5.7 Git Context Provider 与 5.11 Repair Workspace）。

职责：
- 提交解析（current_commit / resolve）
- 变更分析（changed_files / diff / file_history）——把 Failure 与 Recent Change 建立联系
- 修复工作区（create_worktree / remove_worktree）——隔离验证的基础设施

所有 git 调用对调用方透明：命令、exit code、stderr 都会进入 ToolError 消息。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from devfix.tools.errors import ToolError


class GitTool:
    """绑定到一个本地 git 仓库的工具实例。"""

    def __init__(self, repo: Path, timeout: int = 60) -> None:
        self.repo = Path(repo).resolve()
        self._timeout = timeout
        if not (self.repo / ".git").exists():
            raise ToolError(f"不是 git 仓库：{self.repo}")

    # ------------------------------------------------------------------ 内部
    def _git(self, *args: str) -> str:
        cmd = ["git", *args]
        try:
            r = subprocess.run(
                cmd,
                cwd=self.repo,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._timeout,
                check=False,  # 错误处理走下方 returncode 分支，统一抛 ToolError
            )
        except FileNotFoundError as e:
            raise ToolError("未找到 git 可执行文件，请安装 Git 并加入 PATH") from e
        except subprocess.TimeoutExpired as e:
            raise ToolError(f"git 命令超时（{self._timeout}s）：{' '.join(cmd)}") from e
        if r.returncode != 0:
            raise ToolError(
                f"git 命令失败（exit={r.returncode}）：{' '.join(cmd)}\n"
                f"stderr: {r.stderr.strip()[:500]}"
            )
        return r.stdout

    # ------------------------------------------------------------------ 提交
    def current_commit(self) -> str:
        """当前 HEAD 完整 SHA。"""
        return self._git("rev-parse", "HEAD").strip()

    def resolve(self, ref: str) -> str:
        """把分支/tag/相对引用（如 HEAD~1）解析为完整 SHA。"""
        return self._git("rev-parse", ref).strip()

    # ------------------------------------------------------------------ 变更分析
    def changed_files(self, base: str, target: str = "HEAD") -> list[str]:
        """base..target 之间变更的文件列表（相对路径）。"""
        out = self._git("diff", "--name-only", f"{base}..{target}")
        return [line.strip() for line in out.splitlines() if line.strip()]

    def diff(self, base: str, target: str = "HEAD") -> str:
        """base..target 的 unified diff（Failure 与 Recent Change 的关联证据）。"""
        return self._git("diff", f"{base}..{target}")

    def working_diff(self) -> str:
        """工作区未提交修改的 unified diff（Patch 的 canonical 形式）。"""
        return self._git("diff")

    def restore_working_tree(self) -> None:
        """丢弃工作区全部未提交修改（策略校验失败时的回滚手段）。"""
        self._git("checkout", "--", ".")

    def file_history(self, path: str, max_count: int = 10) -> list[str]:
        """某文件的最近提交（oneline）。path 为相对仓库路径。"""
        out = self._git("log", "--oneline", "-n", str(max_count), "--", path)
        return [line.strip() for line in out.splitlines() if line.strip()]

    def list_files(self) -> list[str]:
        """全部受版本控制的文件（git ls-files，不含未跟踪与忽略文件）。"""
        out = self._git("ls-files")
        return [line.strip() for line in out.splitlines() if line.strip()]

    # ------------------------------------------------------------------ 修复工作区
    def create_worktree(self, path: Path, commit: str | None = None) -> Path:
        """创建隔离工作区（git worktree add --detach）。

        Args:
            path: 工作区目录（需在同一个文件系统/盘符内）。相对路径按
                **进程当前目录**解析——绝不能让相对路径落到 git 子进程的
                cwd（目标仓库目录）下，那会与调用方的预期位置不一致。
            commit: 检出的提交；None 表示当前 HEAD。

        Returns:
            解析后的绝对路径。

        说明：始终使用 --detach——补丁验证不需要新分支，且避免重复运行
        时同名分支冲突；分支 + PR 工作流属于 V4 阶段（设计文档 28 节）。
        """
        p = Path(path).resolve()
        p.parent.mkdir(parents=True, exist_ok=True)
        args = ["worktree", "add", "--detach", str(p)]
        if commit:
            args.append(commit)
        self._git(*args)
        return p

    def prune_worktrees(self) -> None:
        """清理失效的 worktree 注册信息。

        只删目录不清注册（如重跑评测前的 rmtree）会留下"missing but
        already registered worktree"，后续 worktree add 全部失败。
        """
        self._git("worktree", "prune")

    def remove_worktree(self, path: Path, force: bool = False) -> None:
        """移除工作区；force=True 时丢弃其中的未提交修改。"""
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(str(Path(path).resolve()))
        self._git(*args)
