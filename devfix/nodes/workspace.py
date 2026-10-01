"""隔离工作区准备节点（对应系统设计 5.11）。

自动修改绝不触碰原始仓库：所有补丁都在 git worktree 中应用与验证。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TypedDict

from devfix.tools import GitTool, ToolError


class WorkspaceState(TypedDict, total=False):
    repo: str
    workspace: str        # 已存在时直接复用（CLI 可预先创建）
    workspace_dir: str    # 新建工作区的父目录，默认 runs/
    workspace_commit: str  # 检出的提交；缺省用仓库当前 HEAD（benchmark 固化 test patch 用）
    error: str


def prepare_workspace_node(state: WorkspaceState) -> dict:
    """创建隔离工作区；已存在则原样复用（支持断点续跑与外部传入）。"""
    if state.get("workspace"):
        return {}
    repo = state.get("repo")
    if not repo:
        return {"error": "prepare_workspace 节点缺少 repo"}

    parent = Path(state.get("workspace_dir", "runs"))
    workspace = parent / f"ws-{datetime.now().astimezone():%Y%m%d-%H%M%S-%f}"
    try:
        GitTool(Path(repo)).create_worktree(
            workspace, commit=state.get("workspace_commit") or None
        )
    except ToolError as e:
        return {"error": f"创建隔离工作区失败：{e}"}
    return {"workspace": str(workspace)}
