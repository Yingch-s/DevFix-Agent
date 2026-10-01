"""文件读取封装（对应系统设计 5.6 Repository Context Provider / 10.2 read_file）。

安全边界（设计文档 10.2 强制要求）：
- 只能访问 root 目录内的文件；
- 相对路径越界（../）与外部绝对路径一律拒绝；
- 读取长度设上限，防止超长文件撑爆 Agent 上下文。
"""

from __future__ import annotations

from pathlib import Path

from devfix.tools.errors import ToolError

# 遍历仓库时跳过的噪音目录
SKIP_DIRS = {
    ".git", "target", "build", "out", "node_modules",
    ".idea", ".gradle", "__pycache__", ".venv", "dist",
}


def rel_posix(path: Path, root: Path) -> str:
    """仓库相对路径，统一为正斜杠（与 git 输出一致，跨平台稳定）。"""
    return path.relative_to(root).as_posix()


class FileTool:
    """绑定到仓库根目录的文件读取工具。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()

    # ------------------------------------------------------------------ 内部
    def _resolve(self, path: str | Path) -> Path:
        """解析并校验路径必须位于 root 内，否则拒绝。"""
        p = Path(path)
        candidate = p.resolve() if p.is_absolute() else (self.root / p).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ToolError(
                f"路径越界，已拒绝访问：{path}（允许范围：{self.root} 内）"
            )
        return candidate

    # ------------------------------------------------------------------ 读取
    def exists(self, path: str | Path) -> bool:
        try:
            return self._resolve(path).is_file()
        except ToolError:
            return False

    def read_file(self, path: str | Path, max_chars: int = 100_000) -> str:
        """读取整个文件；超过 max_chars 时截断并标注。"""
        p = self._resolve(path)
        if not p.is_file():
            raise ToolError(f"文件不存在：{path}")
        text = p.read_text(encoding="utf-8", errors="replace")
        if len(text) > max_chars:
            return text[:max_chars] + f"\n...[已截断：全文 {len(text)} 字符]"
        return text

    def read_range(self, path: str | Path, start_line: int, end_line: int) -> str:
        """读取 [start_line, end_line]（含端点，1 起始）。"""
        p = self._resolve(path)
        if not p.is_file():
            raise ToolError(f"文件不存在：{path}")
        if start_line < 1 or end_line < start_line:
            raise ToolError(f"行号非法：start={start_line}, end={end_line}")
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        if start_line > len(lines):
            raise ToolError(
                f"起始行 {start_line} 超出文件总行数 {len(lines)}：{path}"
            )
        end = min(end_line, len(lines))
        return "\n".join(lines[start_line - 1 : end])

    # ------------------------------------------------------------------ 查找
    def find_by_name(self, file_name: str) -> str | None:
        """按文件名定位仓库内文件（用于把堆栈帧的 OrderService.java
        映射到仓库相对路径），找不到返回 None。"""
        for p in sorted(self.root.rglob(file_name)):
            if p.is_file():
                rel = p.relative_to(self.root)
                if any(part in SKIP_DIRS for part in rel.parts):
                    continue
                return rel_posix(p, self.root)
        return None

    # ------------------------------------------------------------------ 列举
    def list_files(self, suffix: str | None = None) -> list[str]:
        """列举仓库内文件（相对路径），跳过 SKIP_DIRS，可按后缀过滤。"""
        out: list[str] = []
        for p in sorted(self.root.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(self.root)
            if any(part in SKIP_DIRS for part in rel.parts):
                continue
            if suffix is not None and not p.name.endswith(suffix):
                continue
            out.append(rel_posix(p, self.root))
        return out
