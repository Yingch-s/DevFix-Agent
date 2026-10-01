"""FileTool 测试：读取 / 行范围 / 路径越界防护。"""

from __future__ import annotations

import pytest

from devfix.tools import FileTool, ToolError

SERVICE = "src/main/java/com/example/OrderService.java"


class TestRead:
    def test_read_file(self, git_repo) -> None:
        text = FileTool(git_repo).read_file(SERVICE)
        assert "public class OrderService" in text

    def test_read_range(self, git_repo) -> None:
        text = FileTool(git_repo).read_range(SERVICE, 1, 3)
        lines = text.splitlines()
        assert len(lines) == 3
        assert lines[0] == "package com.example;"

    def test_read_range_clamps_to_eof(self, git_repo) -> None:
        text = FileTool(git_repo).read_range(SERVICE, 4, 999)
        assert len(text.splitlines()) == 2  # 文件共 5 行，取到末尾自动截断

    def test_read_range_start_beyond_eof_raises(self, git_repo) -> None:
        with pytest.raises(ToolError, match="超出文件总行数"):
            FileTool(git_repo).read_range(SERVICE, 100, 200)

    def test_read_range_invalid_lines_raises(self, git_repo) -> None:
        with pytest.raises(ToolError, match="行号非法"):
            FileTool(git_repo).read_range(SERVICE, 0, 3)

    def test_missing_file_raises(self, git_repo) -> None:
        with pytest.raises(ToolError, match="文件不存在"):
            FileTool(git_repo).read_file("src/main/java/Nope.java")

    def test_truncation_marker(self, git_repo) -> None:
        ft = FileTool(git_repo)
        text = ft.read_file(SERVICE, max_chars=10)
        assert text.startswith("package co")
        assert "已截断" in text


class TestPathSafety:
    """设计文档 10.2：只能访问仓库内路径，越界一律拒绝。"""

    def test_relative_traversal_rejected(self, git_repo, tmp_path) -> None:
        (tmp_path / "outside.txt").write_text("secret", encoding="utf-8")
        with pytest.raises(ToolError, match="路径越界"):
            FileTool(git_repo).read_file("../../outside.txt")

    def test_absolute_outside_rejected(self, git_repo, tmp_path) -> None:
        outside = tmp_path / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        with pytest.raises(ToolError, match="路径越界"):
            FileTool(git_repo).read_file(outside)

    def test_absolute_inside_allowed(self, git_repo) -> None:
        text = FileTool(git_repo).read_file(git_repo / SERVICE)
        assert "OrderService" in text

    def test_exists_false_on_traversal(self, git_repo) -> None:
        assert FileTool(git_repo).exists("../../whatever") is False


class TestListFiles:
    def test_excludes_git_dir(self, git_repo) -> None:
        files = FileTool(git_repo).list_files()
        assert all(".git" not in f.split("/") for f in files)

    def test_excludes_build_dirs(self, git_repo) -> None:
        target = git_repo / "target/classes"
        target.mkdir(parents=True)
        (target / "OrderService.class").write_bytes(b"\xca\xfe")
        files = FileTool(git_repo).list_files()
        assert all(not f.startswith("target") for f in files)

    def test_suffix_filter(self, git_repo) -> None:
        files = FileTool(git_repo).list_files(suffix=".java")
        assert files and all(f.endswith(".java") for f in files)
        assert "README.md" not in files
