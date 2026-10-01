"""PatchTool 测试：search/replace 应用、唯一性校验、原子性、换行风格保持。"""

from __future__ import annotations

import pytest

from devfix.models import Patch, PatchEdit
from devfix.tools import PatchTool, ToolError

SERVICE = "src/main/java/com/example/OrderService.java"
SERVICE_OLD = '    public String greet(String name) { return "hi " + name; }'
SERVICE_NEW = '    public String greet(String name) { return "hello " + name; }'


def _patch(*edits: PatchEdit, patch_id: str = "PATCH-001") -> Patch:
    return Patch(patch_id=patch_id, edits=list(edits), reason="r", expected_effect="e")


class TestApplySuccess:
    def test_replace_and_canonical_diff(self, git_worktree) -> None:
        tool = PatchTool(git_worktree)
        result = tool.apply(_patch(PatchEdit(file=SERVICE, search=SERVICE_OLD, replace=SERVICE_NEW)))
        # 文件确实被修改
        content = (git_worktree / SERVICE).read_text(encoding="utf-8")
        assert SERVICE_NEW in content
        # changed_files 与 canonical diff 已由 git 产出
        assert result.changed_files == [SERVICE]
        assert "+" in result.diff and "-" in result.diff
        assert "hello " in result.diff

    def test_multiple_files(self, git_worktree) -> None:
        tool = PatchTool(git_worktree)
        result = tool.apply(_patch(
            PatchEdit(file=SERVICE, search=SERVICE_OLD, replace=SERVICE_NEW),
            PatchEdit(
                file="README.md",
                search="# demo",
                replace="# demo\n\nDevFix 演示仓库",
            ),
        ))
        assert result.changed_files == ["README.md", SERVICE]
        assert len(result.diff.splitlines()) > 5

    def test_full_content_mode(self, git_worktree) -> None:
        tool = PatchTool(git_worktree)
        new_content = "package com.example;\n\npublic class OrderService {\n    // 完全重写\n}\n"
        tool.apply(_patch(PatchEdit(file=SERVICE, full_content=new_content)))
        assert (git_worktree / SERVICE).read_text(encoding="utf-8") == new_content


class TestMatchingErrors:
    def test_search_not_found_gives_hint(self, git_worktree) -> None:
        bad = PatchEdit(file=SERVICE, search="完全不存在的代码片段", replace="x")
        with pytest.raises(ToolError, match="未找到匹配"):
            PatchTool(git_worktree).apply(_patch(bad))

    def test_search_not_found_includes_file_content(self, git_worktree) -> None:
        """匹配失败时必须回传文件真实内容——否则模型只能凭印象编造 search 片段
        （实测：连续三次编出同一段不存在的代码）。"""
        bad = PatchEdit(file=SERVICE, search="Order order = new Order(userId, user.getEmail());", replace="x")
        with pytest.raises(ToolError) as exc:
            PatchTool(git_worktree).apply(_patch(bad))
        message = str(exc.value)
        assert "实际内容" in message
        assert "greet" in message          # 文件真实代码出现在错误信息里
        assert "请直接复制" in message

    def test_indent_mismatch_hint(self, git_worktree) -> None:
        # 内容一致但缩进不同（如模型多给了一层缩进）→ 错误信息应给出缩进提示
        bad = PatchEdit(
            file=SERVICE,
            search="        " + SERVICE_OLD.strip(),  # 8 空格缩进，文件中不存在
            replace="x",
        )
        with pytest.raises(ToolError, match="缩进"):
            PatchTool(git_worktree).apply(_patch(bad))

    def test_ambiguous_search_reports_lines(self, git_worktree) -> None:
        (git_worktree / "src/main/java/com/example/Dup.java").write_text(
            "package com.example;\n\npublic class Dup {\n    int a = 1;\n    int b = 1;\n}\n",
            encoding="utf-8",
        )
        bad = PatchEdit(file="src/main/java/com/example/Dup.java", search="= 1;", replace="= 2;")
        with pytest.raises(ToolError, match="出现 2 次"):
            PatchTool(git_worktree).apply(_patch(bad))

    def test_noop_edit_rejected(self, git_worktree) -> None:
        same = PatchEdit(file=SERVICE, search=SERVICE_OLD, replace=SERVICE_OLD)
        with pytest.raises(ToolError, match="未产生任何变化"):
            PatchTool(git_worktree).apply(_patch(same))

    def test_missing_file(self, git_worktree) -> None:
        e = PatchEdit(file="src/main/java/Nope.java", search="a", replace="b")
        with pytest.raises(ToolError, match="文件不存在"):
            PatchTool(git_worktree).apply(_patch(e))

    def test_path_traversal_rejected(self, git_worktree, tmp_path) -> None:
        (tmp_path / "outside.txt").write_text("secret", encoding="utf-8")
        e = PatchEdit(file="../../outside.txt", search="secret", replace="hacked")
        with pytest.raises(ToolError, match="路径越界"):
            PatchTool(git_worktree).apply(_patch(e))

    def test_empty_patch_rejected(self, git_worktree) -> None:
        with pytest.raises(ToolError, match="不包含任何编辑"):
            PatchTool(git_worktree).apply(_patch())


class TestAtomicity:
    def test_no_partial_write_on_failure(self, git_worktree) -> None:
        before = (git_worktree / SERVICE).read_text(encoding="utf-8")
        patch = _patch(
            PatchEdit(file=SERVICE, search=SERVICE_OLD, replace=SERVICE_NEW),  # 第 1 条合法
            PatchEdit(file=SERVICE, search="不存在", replace="x"),  # 第 2 条会失败
        )
        with pytest.raises(ToolError):
            PatchTool(git_worktree).apply(patch)
        # 第 1 条也不能落盘
        assert (git_worktree / SERVICE).read_text(encoding="utf-8") == before

    def test_same_file_twice_rejected(self, git_worktree) -> None:
        patch = _patch(
            PatchEdit(file=SERVICE, search=SERVICE_OLD, replace=SERVICE_NEW),
            PatchEdit(file=SERVICE, search="public class", replace="public final class"),
        )
        with pytest.raises(ToolError, match="同一文件"):
            PatchTool(git_worktree).apply(patch)


class TestLineEndings:
    def test_crlf_file_not_rewritten_to_lf(self, git_worktree) -> None:
        import subprocess

        rel = "src/main/java/com/example/Crlf.java"
        path = git_worktree / rel
        crlf_content = "package com.example;\r\n\r\npublic class Crlf {\r\n    int v = 1;\r\n}\r\n"
        path.write_bytes(crlf_content.encode("utf-8"))
        # 纳入版本跟踪，使其变更出现在 git diff 中
        subprocess.run(
            ["git", "add", rel], cwd=git_worktree,
            capture_output=True, text=True, check=False,
        )

        tool = PatchTool(git_worktree)
        # 模型通常给 LF 换行的 search
        result = tool.apply(_patch(PatchEdit(
            file=rel,
            search="    int v = 1;",
            replace="    int v = 2;",
        )))
        raw = path.read_bytes().decode("utf-8")
        assert "int v = 2;" in raw
        assert "\r\n" in raw  # 换行风格保持 CRLF
        # diff 只含该行，不应整文件重写
        assert sum(
            1 for line in result.diff.splitlines()
            if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
        ) == 2


class TestEncodings:
    def _track(self, git_worktree, rel: str) -> None:
        import subprocess

        subprocess.run(
            ["git", "add", rel], cwd=git_worktree,
            capture_output=True, text=True, check=False,
        )

    def test_non_utf8_file_rejected_not_corrupted(self, git_worktree) -> None:
        """GBK 文件必须拒绝编辑而非 errors="replace" 硬读——否则即使补丁
        正确，全部非 ASCII 字节也会变成 U+FFFD 永久写回（整文件乱码）。"""
        rel = "src/main/java/com/example/Gbk.java"
        path = git_worktree / rel
        gbk_source = "// 中文注释：订单服务\npublic class Gbk {\n    int v = 1;\n}\n"
        path.write_bytes(gbk_source.encode("gbk"))
        self._track(git_worktree, rel)

        with pytest.raises(ToolError, match="不是 UTF-8 编码"):
            PatchTool(git_worktree).apply(_patch(PatchEdit(
                file=rel, search="    int v = 1;", replace="    int v = 2;",
            )))
        # 拒绝后文件原字节不能被动过
        assert path.read_bytes() == gbk_source.encode("gbk")

    def test_utf8_bom_preserved_and_first_line_matches(self, git_worktree) -> None:
        """BOM 剥离后首行 search 可匹配；写回时 BOM 原样保留。"""
        rel = "src/main/java/com/example/Bom.java"
        path = git_worktree / rel
        source = "package com.example;\n\npublic class Bom {\n    int v = 1;\n}\n"
        path.write_bytes(b"\xef\xbb\xbf" + source.encode("utf-8"))
        self._track(git_worktree, rel)

        PatchTool(git_worktree).apply(_patch(PatchEdit(
            file=rel,
            search="package com.example;",  # 首行——BOM 不剥掉就永远匹配不上
            replace="package com.example;\n// fixed",
        )))
        raw = path.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf")   # BOM 保留
        assert b"// fixed" in raw                 # 编辑已应用
