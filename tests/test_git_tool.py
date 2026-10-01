"""GitTool 测试：提交解析 / 变更分析 / worktree 隔离。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from devfix.tools import GitTool, ToolError


class TestRepoInfo:
    def test_current_commit_is_sha(self, git_repo) -> None:
        sha = GitTool(git_repo).current_commit()
        assert len(sha) == 40

    def test_resolve_head_and_relative(self, git_repo) -> None:
        tool = GitTool(git_repo)
        assert tool.resolve("HEAD") == tool.current_commit()
        assert tool.resolve("HEAD~1") != tool.current_commit()

    def test_not_a_git_repo_raises(self, tmp_path) -> None:
        with pytest.raises(ToolError, match="不是 git 仓库"):
            GitTool(tmp_path)


class TestChangeAnalysis:
    def test_changed_files(self, git_repo) -> None:
        base = GitTool(git_repo).resolve("HEAD~1")
        files = GitTool(git_repo).changed_files(base)
        assert "src/main/java/com/example/OrderService.java" in files
        assert "src/main/java/com/example/Util.java" in files
        assert "README.md" not in files

    def test_diff_contains_both_sides(self, git_repo) -> None:
        base = GitTool(git_repo).resolve("HEAD~1")
        diff = GitTool(git_repo).diff(base)
        assert '-    public String greet(String name) { return "hello " + name; }' in diff
        assert '+    public String greet(String name) { return "hi " + name; }' in diff

    def test_file_history(self, git_repo) -> None:
        history = GitTool(git_repo).file_history("src/main/java/com/example/OrderService.java")
        assert len(history) == 2
        assert any("change greet" in h for h in history)

    def test_list_files_tracked_only(self, git_repo) -> None:
        (git_repo / "untracked.txt").write_text("x", encoding="utf-8")
        files = GitTool(git_repo).list_files()
        assert "src/main/java/com/example/Util.java" in files
        assert "untracked.txt" not in files


class TestWorktree:
    def test_create_at_base_commit(self, git_repo, tmp_path) -> None:
        tool = GitTool(git_repo)
        base = tool.resolve("HEAD~1")
        ws = tool.create_worktree(tmp_path / "ws", commit=base)
        # base 提交时 Util.java 尚不存在
        assert (ws / "src/main/java/com/example/OrderService.java").exists()
        assert not (ws / "src/main/java/com/example/Util.java").exists()
        # 干净工作区可直接移除
        tool.remove_worktree(ws)
        assert not ws.exists()

    def test_worktree_isolated_from_main_repo(self, git_repo, tmp_path) -> None:
        tool = GitTool(git_repo)
        ws = tool.create_worktree(tmp_path / "ws")
        (ws / "README.md").write_text("# changed in worktree\n", encoding="utf-8")
        main_readme = (git_repo / "README.md").read_text(encoding="utf-8")
        assert "changed in worktree" not in main_readme
        # 有未提交修改时 git 拒绝普通移除，需 force（非 force 拒绝见下一用例）
        tool.remove_worktree(ws, force=True)
        assert not ws.exists()

    def test_remove_dirty_worktree_needs_force(self, git_repo, tmp_path) -> None:
        tool = GitTool(git_repo)
        ws = tool.create_worktree(tmp_path / "ws")
        (ws / "README.md").write_text("# dirty\n", encoding="utf-8")
        with pytest.raises(ToolError):
            tool.remove_worktree(ws)
        tool.remove_worktree(ws, force=True)
        assert not ws.exists()

    def test_relative_path_resolved_against_process_cwd(
        self, git_repo, tmp_path, monkeypatch
    ) -> None:
        """回归：相对路径必须按进程 cwd 解析，而不是 git 子进程的 cwd（目标仓库）。

        历史 bug：`git worktree add runs\\ws` 把工作区建到了
        <repo>/runs/ws 下，而调用方按进程 cwd 去 <cwd>/runs/ws 找 → 找不到。
        """
        monkeypatch.chdir(tmp_path)
        ws = GitTool(git_repo).create_worktree(Path("nested/ws"))
        assert ws == (tmp_path / "nested/ws").resolve()
        assert (ws / "README.md").exists()  # 父目录自动创建，内容正确检出

    def test_detached_no_branch_created(self, git_repo, tmp_path) -> None:
        """始终 --detach：批量运行不会产生同名分支冲突。"""
        tool = GitTool(git_repo)
        ws1 = tool.create_worktree(tmp_path / "a" / "ws")
        ws2 = tool.create_worktree(tmp_path / "b" / "ws")  # 同名 basename 也不冲突
        branches = subprocess.run(
            ["git", "branch", "--list"], cwd=git_repo,
            capture_output=True, text=True, check=False,
        ).stdout
        assert "ws" not in branches
        assert ws1.exists() and ws2.exists()
        tool.remove_worktree(ws1, force=True)
        tool.remove_worktree(ws2, force=True)
