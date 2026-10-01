"""pytest 共享夹具：现场生成带两次提交的小型 Java 仓库。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,  # 断言在下方统一处理
    )
    assert r.returncode == 0, f"git {' '.join(args)} 失败: {r.stderr}"
    return r.stdout


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """包含两次提交的临时 git 仓库。

    base 提交：OrderService.greet() 返回 "hello" + README.md
    第二次提交：greet() 改为 "hi"，新增 Util.java
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "devfix-test")
    _git(repo, "config", "user.email", "devfix@test.local")
    _git(repo, "config", "commit.gpgsign", "false")

    src = repo / "src/main/java/com/example"
    src.mkdir(parents=True)
    (repo / "src/test/java/com/example").mkdir(parents=True)
    (repo / "README.md").write_text("# demo\n", encoding="utf-8")
    (src / "OrderService.java").write_text(
        "package com.example;\n\n"
        "public class OrderService {\n"
        "    public String greet(String name) { return \"hello \" + name; }\n"
        "}\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base: order service")

    (src / "OrderService.java").write_text(
        "package com.example;\n\n"
        "public class OrderService {\n"
        "    public String greet(String name) { return \"hi \" + name; }\n"
        "}\n",
        encoding="utf-8",
    )
    (src / "Util.java").write_text(
        "package com.example;\n\npublic class Util {}\n", encoding="utf-8"
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "change greet")
    return repo


@pytest.fixture
def git_worktree(git_repo: Path, tmp_path: Path) -> Path:
    """基于 git_repo 当前 HEAD 创建的隔离工作区（git worktree）。"""
    from devfix.tools import GitTool

    ws = GitTool(git_repo).create_worktree(tmp_path / "ws")
    return ws
