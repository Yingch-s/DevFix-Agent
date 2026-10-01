"""bench.runner 测试：结果汇总字段与工作区创建的健壮性。"""

from __future__ import annotations

from bench.runner import _is_test_path, make_worktree, summarize


class TestIsTestPath:
    def test_single_module(self) -> None:
        assert _is_test_path("src/test/java/com/example/T.java")
        assert not _is_test_path("src/main/java/com/example/T.java")

    def test_multi_module(self) -> None:
        assert _is_test_path("module-a/src/test/java/com/example/T.java")
        assert not _is_test_path("module-a/src/main/java/com/example/T.java")

    def test_windows_and_dot_forms(self) -> None:
        assert _is_test_path("src\\test\\java\\T.java")
        assert _is_test_path("./src/test/java/T.java")


def _case() -> dict:
    return {
        "case_id": "demo-abc",
        "repo_name": "demo",
        "buggy_commit": "aaa",
        "fix_commit": "bbb",
        "ground_truth_files": ["src/main/java/com/example/Foo.java"],
    }


class TestSummarize:
    def test_unsafe_modification_computed_from_patch_files(self) -> None:
        """unsafe_modification 必须真实计算——此前没有任何 runner 写入
        这个键，指标永远 0%，静默暗示"从无不安全修改"。"""
        s = summarize(
            _case(), "baseline", "FIXED", 1, None,
            ["src/main/java/com/example/Foo.java"],
            duration_s=1.0,
            patch_files=["src/test/java/com/example/T.java"],
        )
        assert s["unsafe_modification"] is True

    def test_safe_patch_flagged_false(self) -> None:
        s = summarize(
            _case(), "baseline", "FAILED_TO_FIX", 1, None, [], 1.0,
            patch_files=["src/main/java/com/example/Foo.java"],
        )
        assert s["unsafe_modification"] is False

    def test_no_patch_files_means_false(self) -> None:
        s = summarize(_case(), "devfix", "STOPPED", 2, None, [], 1.0)
        assert s["unsafe_modification"] is False


class TestMakeWorktree:
    def test_string_root_accepted(self, git_repo, tmp_path) -> None:
        """root 必须兼容 str：baseline 传的是 f-string，str / str 会
        TypeError 且不被 except ToolError 捕获，整个扫描中途崩溃。"""
        case = _case()
        case["buggy_commit"] = "HEAD"
        ws = make_worktree(git_repo, case, root=str(tmp_path / "wsroot"))
        assert (ws / ".git").exists()

    def test_uses_test_patch_commit_when_present(self, git_repo, tmp_path) -> None:
        case = _case()
        case["buggy_commit"] = "HEAD"
        case["_test_patch_commit"] = "HEAD"
        ws = make_worktree(git_repo, case, root=tmp_path / "wsroot2")
        assert (ws / ".git").exists()
