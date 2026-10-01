"""代码搜索封装（对应系统设计 10.3 search_code）。

优先使用 ripgrep（rg）；环境缺少 rg 时自动降级为 Python 实现，
保证工具在任何机器上可用。两种引擎返回相同的数据结构。
"""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess
from pathlib import Path

from devfix.models import SearchMatch
from devfix.tools.errors import ToolError
from devfix.tools.file_tool import SKIP_DIRS


class SearchTool:
    """绑定到仓库根目录的搜索工具。"""

    def __init__(self, root: Path, max_results: int = 50, timeout: int = 60) -> None:
        self.root = Path(root).resolve()
        self.max_results = max_results
        self._timeout = timeout
        self._use_rg = shutil.which("rg") is not None

    # ------------------------------------------------------------------ 公共 API
    def search_text(self, query: str, file_pattern: str | None = None) -> list[SearchMatch]:
        """固定字符串文本搜索。"""
        return self._search(query, file_pattern, fixed=True)

    def search_symbol(self, symbol: str, file_pattern: str = "*.java") -> list[SearchMatch]:
        """符号搜索：词边界匹配，避免子串误命中（greet 不匹配 greeting）。"""
        return self._search(rf"\b{re.escape(symbol)}\b", file_pattern, fixed=False)

    # ------------------------------------------------------------------ 引擎分派
    def _search(self, query: str, file_pattern: str | None, fixed: bool) -> list[SearchMatch]:
        if self._use_rg:
            return self._search_rg(query, file_pattern, fixed)
        return self._search_fallback(query, file_pattern, fixed)

    def _to_match(self, file_path: str, line_number: int, line_text: str) -> SearchMatch:
        # 统一为正斜杠相对路径（Windows 下 rg/relpath 都产出反斜杠）
        rel = Path(os.path.relpath(file_path, self.root)).as_posix()
        return SearchMatch(file_path=rel, line_number=line_number, line_text=line_text)

    # ------------------------------------------------------------------ ripgrep
    def _search_rg(self, query: str, file_pattern: str | None, fixed: bool) -> list[SearchMatch]:
        args = ["rg", "-n", "--no-heading"]
        if fixed:
            args.append("-F")
        if file_pattern:
            args += ["-g", file_pattern]
        args += ["-e", query, str(self.root)]
        try:
            r = subprocess.run(
                args,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._timeout,
                check=False,  # rg 以 1 表示无匹配，不能交给 check 处理
            )
        except subprocess.TimeoutExpired as e:
            raise ToolError(f"rg 搜索超时（{self._timeout}s）：{query}") from e
        if r.returncode not in (0, 1):  # rg：1 表示无匹配，非错误
            raise ToolError(f"rg 搜索失败：{r.stderr.strip()[:300]}")
        matches: list[SearchMatch] = []
        for line in r.stdout.splitlines():
            if len(matches) >= self.max_results:
                break
            # Windows 路径含盘符冒号（C:\...），从行首非贪婪匹配到 "：<行号>："
            m = re.match(r"^(?P<file>.+?):(?P<line>\d+):(?P<text>.*)$", line)
            if m:
                matches.append(
                    self._to_match(m.group("file"), int(m.group("line")), m.group("text"))
                )
        return matches

    # ------------------------------------------------------------------ Python 降级
    def _search_fallback(self, query: str, file_pattern: str | None, fixed: bool) -> list[SearchMatch]:
        pattern = re.escape(query) if fixed else query
        rx = re.compile(pattern)
        matches: list[SearchMatch] = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for filename in filenames:
                if file_pattern and not fnmatch.fnmatch(filename, file_pattern):
                    continue
                fp = Path(dirpath) / filename
                try:
                    lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
                except OSError:
                    continue
                for i, text in enumerate(lines, 1):
                    if rx.search(text):
                        matches.append(self._to_match(str(fp), i, text))
                        if len(matches) >= self.max_results:
                            return matches
        return matches
