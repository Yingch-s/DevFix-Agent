"""上下文收集节点测试（确定性，不调用 LLM）。"""

from __future__ import annotations

from devfix.nodes.context_collector import (
    _build_symbol_content,
    _extract_test_symbols,
    collect_context,
    locate_source_file,
)
from devfix.parsing import parse_maven_log
from devfix.tools import FileTool
from tests.sample_logs import JUNIT4_NPE_ERROR


def _bundle(git_repo):
    failure, triage = parse_maven_log(JUNIT4_NPE_ERROR)
    return collect_context(failure, triage, repo=git_repo, base="HEAD~1")


class TestLocateSourceFile:
    def test_package_path_guess(self, git_repo) -> None:
        files = FileTool(git_repo)
        rel = locate_source_file("com.example.OrderService", "OrderService.java", files)
        assert rel == "src/main/java/com/example/OrderService.java"

    def test_find_by_name_fallback(self, git_repo) -> None:
        files = FileTool(git_repo)
        rel = locate_source_file("com.example.Unknown", "Util.java", files)
        assert rel == "src/main/java/com/example/Util.java"

    def test_not_found_returns_none(self, git_repo) -> None:
        files = FileTool(git_repo)
        assert locate_source_file("com.example.Nope", "Nope.java", files) is None


class TestTestSymbolExtraction:
    """从失败测试方法体提取被引用的类型名（5.5 Code Localization 的一环）。"""

    SOURCE = """\
package com.example;

import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.assertEquals;

class OrderServiceTest {
    @Test
    void shouldRejectDeletedUser() {
        UserRepository repo = new StubUserRepository();
        OrderService service = new OrderService(repo);
        assertEquals(2, service.countActiveOrders(1L));
    }

    @Test
    void otherTest() {
        UnrelatedHelper helper = new UnrelatedHelper();
    }
}
"""

    def test_extracts_types_from_target_method_only(self) -> None:
        symbols = _extract_test_symbols(self.SOURCE, "shouldRejectDeletedUser")
        assert "OrderService" in symbols
        assert "UserRepository" in symbols
        assert "StubUserRepository" in symbols
        # 其他测试方法里的类型不应混进来
        assert "UnrelatedHelper" not in symbols

    def test_stoplist_filters_noise(self) -> None:
        symbols = _extract_test_symbols(self.SOURCE, "shouldRejectDeletedUser")
        assert "Assertions" not in symbols
        assert "Test" not in symbols

    def test_missing_method_returns_empty(self) -> None:
        assert _extract_test_symbols(self.SOURCE, "nonexistentMethod") == []


class TestSymbolLocalization:
    """断言失败类故障：异常栈里没有项目代码，必须靠测试引用的符号定位生产代码。"""

    def _prepare_repo(self, git_repo) -> None:
        src = git_repo / "src/main/java/com/example"
        test = git_repo / "src/test/java/com/example"
        test.mkdir(parents=True, exist_ok=True)
        (src / "OrderService.java").write_text(
            "package com.example;\n\npublic class OrderService {\n"
            "    public int countActiveOrders(Long id) { return 0; }\n}\n",
            encoding="utf-8",
        )
        (test / "OrderServiceTest.java").write_text(
            "package com.example;\n\nimport org.junit.jupiter.api.Test;\n"
            "import static org.junit.jupiter.api.Assertions.assertEquals;\n\n"
            "class OrderServiceTest {\n"
            "    @Test\n"
            "    void shouldRejectDeletedUser() {\n"
            "        OrderService service = new OrderService();\n"
            "        assertEquals(2, service.countActiveOrders(1L));\n"
            "    }\n}\n",
            encoding="utf-8",
        )

    def test_production_class_pulled_into_context(self, git_repo) -> None:
        self._prepare_repo(git_repo)
        # 断言失败：没有指向项目代码的栈帧
        failure, triage = parse_maven_log(
            "Tests run: 1, Failures: 1, Errors: 0, Skipped: 0\n"
            "shouldRejectDeletedUser(com.example.OrderServiceTest)  "
            "Time elapsed: 0.01 sec  <<< FAILURE!\n"
            "java.lang.AssertionError: expected: <2> but was: <0>\n"
            "\tat org.junit.jupiter.api.AssertEquals.fail(AssertEquals.java:1)\n"
        )
        bundle = collect_context(failure, triage, repo=git_repo, base=None)
        symbol_snips = [s for s in bundle.snippets if s.source == "test_symbol"]
        assert symbol_snips, "必须由测试引用的符号反查到生产代码"
        assert any(
            s.file_path == "src/main/java/com/example/OrderService.java"
            for s in symbol_snips
        )
        assert any("countActiveOrders" in s.content for s in symbol_snips)

    def test_missing_production_file_skipped_silently(self, git_repo) -> None:
        self._prepare_repo(git_repo)
        failure, triage = parse_maven_log(
            "Tests run: 1, Failures: 1, Errors: 0, Skipped: 0\n"
            "shouldRejectDeletedUser(com.example.OrderServiceTest)  "
            "Time elapsed: 0.01 sec  <<< FAILURE!\n"
            "java.lang.AssertionError: x\n"
        )
        bundle = collect_context(failure, triage, repo=git_repo, base=None)  # 不抛异常即可
        assert bundle.snippets

    def test_deep_method_beyond_head_is_injected(self, git_repo) -> None:
        """根因方法在 160 行之后也必须进入上下文——固定头部窗口的教训：
        CSVParser 的 nextRecord 在 300 行以后，头部窗口让模型只能"诚实停止"。"""
        src = git_repo / "src/main/java/com/example"
        test = git_repo / "src/test/java/com/example"
        test.mkdir(parents=True, exist_ok=True)
        filler = "\n".join(f"    // filler {i}" for i in range(1, 190))
        (src / "OrderService.java").write_text(
            "package com.example;\n\npublic class OrderService {\n"
            f"{filler}\n"
            "    public int countActiveOrders(Long id) { return 0; }\n}\n",
            encoding="utf-8",
        )
        (test / "OrderServiceTest.java").write_text(
            "package com.example;\n\nimport org.junit.jupiter.api.Test;\n"
            "import static org.junit.jupiter.api.Assertions.assertEquals;\n\n"
            "class OrderServiceTest {\n"
            "    @Test\n"
            "    void shouldRejectDeletedUser() {\n"
            "        OrderService service = new OrderService();\n"
            "        assertEquals(2, service.countActiveOrders(1L));\n"
            "    }\n}\n",
            encoding="utf-8",
        )
        failure, triage = parse_maven_log(
            "Tests run: 1, Failures: 1, Errors: 0, Skipped: 0\n"
            "shouldRejectDeletedUser(com.example.OrderServiceTest)  "
            "Time elapsed: 0.01 sec  <<< FAILURE!\n"
            "java.lang.AssertionError: expected: <2> but was: <0>\n"
        )
        bundle = collect_context(failure, triage, repo=git_repo, base=None)
        snip = next(s for s in bundle.snippets if s.file_path.endswith("OrderService.java"))
        assert "countActiveOrders" in snip.content   # 热点方法被命中
        assert "省略" in snip.content                # 中段被压缩，没有整文件注入


class TestBuildSymbolContent:
    def test_hot_region_located_by_call_name(self) -> None:
        lines = ["class A {"] + [f"    // x{i}" for i in range(300)] + ["    void getBytePosition() {}"]
        content = _build_symbol_content("\n".join(lines), ["getBytePosition"])
        assert "getBytePosition" in content
        assert "省略" in content

    def test_no_tokens_falls_back_to_head(self) -> None:
        text = "\n".join(f"line {i}" for i in range(200))
        content = _build_symbol_content(text, [])
        assert content == (
            "\n".join(f"line {i}" for i in range(45)) + "\n……（第 46–200 行省略）……"
        )


class TestCollectContext:
    def test_git_diff_attached(self, git_repo) -> None:
        bundle = _bundle(git_repo)
        assert "diff --git" in bundle.git_diff

    def test_stack_trace_snippet(self, git_repo) -> None:
        bundle = _bundle(git_repo)
        stack_snips = [s for s in bundle.snippets if s.source == "stack_trace"]
        assert stack_snips
        paths = {s.file_path for s in stack_snips}
        assert "src/main/java/com/example/OrderService.java" in paths
        # 片段应围绕异常行（82 行超出 5 行小文件，取到末尾即可）
        assert any("greet" in s.content for s in stack_snips)

    def test_missing_files_skipped_silently(self, git_repo) -> None:
        # 日志引用的 OrderServiceTest / UserRepository 在夹具仓库中不存在
        bundle = _bundle(git_repo)
        for s in bundle.snippets:
            assert "OrderServiceTest" not in s.file_path
            assert "UserRepository" not in s.file_path

    def test_failure_and_triage_passthrough(self, git_repo) -> None:
        failure, triage = parse_maven_log(JUNIT4_NPE_ERROR)
        bundle = collect_context(failure, triage, repo=git_repo, base="HEAD~1")
        assert bundle.failure.key_error == "NullPointerException"
        assert bundle.triage.failure_type.value == "UNIT_TEST_FAILURE"

    def test_no_base_means_no_diff(self, git_repo) -> None:
        failure, triage = parse_maven_log(JUNIT4_NPE_ERROR)
        bundle = collect_context(failure, triage, repo=git_repo, base=None)
        assert bundle.git_diff == ""

    def test_diff_target_overrides_head(self, git_repo) -> None:
        """显式 target 覆盖 HEAD：benchmark 中共享仓库 HEAD 是
        "buggy 提交 + harness test patch"，diff 必须只取 buggy 提交本身。"""
        failure, triage = parse_maven_log(JUNIT4_NPE_ERROR)
        # target == base → 区间为空 → 无 diff
        bundle = collect_context(
            failure, triage, repo=git_repo, base="HEAD~1", target="HEAD~1"
        )
        assert bundle.git_diff == ""
        # target 缺省仍取 HEAD
        bundle_default = collect_context(failure, triage, repo=git_repo, base="HEAD~1")
        assert "diff --git" in bundle_default.git_diff
