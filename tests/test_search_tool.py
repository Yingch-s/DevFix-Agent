"""SearchTool 测试：文本搜索 / 符号词边界 / 文件过滤 / 降级引擎。"""

from __future__ import annotations

import pytest

from devfix.tools import SearchTool


@pytest.fixture
def search_repo(git_repo):
    """在共享 git 仓库上补一个含子串干扰项的文件（不提交，搜索不受限）。"""
    extra = git_repo / "src/main/java/com/example/Greeter.java"
    extra.write_text(
        "package com.example;\n\n"
        "public class Greeter {\n"
        "    String greeting = \"good morning\";\n"  # 仅含子串 greeting，符号搜索不应命中
        "}\n",
        encoding="utf-8",
    )
    return git_repo


class TestTextSearch:
    def test_finds_matches_with_lines(self, search_repo) -> None:
        matches = SearchTool(search_repo).search_text("greet")
        files = {m.file_path for m in matches}
        assert any("OrderService.java" in f for f in files)
        assert any("Greeter.java" in f for f in files)
        assert all(m.line_number >= 1 for m in matches)
        assert all("greet" in m.line_text for m in matches)

    def test_file_pattern_filters(self, search_repo) -> None:
        matches = SearchTool(search_repo).search_text("greet", file_pattern="*.md")
        assert matches == []

    def test_no_match_returns_empty(self, search_repo) -> None:
        assert SearchTool(search_repo).search_text("nonexistentSymbol123") == []


class TestSymbolSearch:
    def test_word_boundary_excludes_substring(self, search_repo) -> None:
        matches = SearchTool(search_repo).search_symbol("greet")
        # Greeter.java 中只有 greeting（子串），词边界搜索不应命中它
        assert all("Greeter.java" not in m.file_path for m in matches)

    def test_symbol_found(self, search_repo) -> None:
        matches = SearchTool(search_repo).search_symbol("OrderService")
        assert matches  # 两个 java 文件都应出现

    def test_default_pattern_is_java(self, search_repo) -> None:
        (search_repo / "notes.txt").write_text("greet here\n", encoding="utf-8")
        matches = SearchTool(search_repo).search_symbol("greet")
        assert all(not m.file_path.endswith(".txt") for m in matches)


class TestFallbackEngine:
    @staticmethod
    def _fallback_tool(root, **kwargs) -> SearchTool:
        """强制走 Python 降级引擎（_use_rg 是实例属性，需实例级覆盖）。"""
        tool = SearchTool(root, **kwargs)
        tool._use_rg = False
        return tool

    def test_fallback_same_results(self, search_repo) -> None:
        matches = self._fallback_tool(search_repo).search_symbol("greet")
        assert matches
        assert all("Greeter.java" not in m.file_path for m in matches)

    def test_max_results_respected(self, search_repo) -> None:
        matches = self._fallback_tool(search_repo, max_results=1).search_text("greet")
        assert len(matches) == 1
