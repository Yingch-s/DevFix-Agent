"""Patch 应用工具（对应系统设计 10.6 apply_patch）。

约定（技术选型 2.6）：
- 模型输出 search/replace 编辑块，不直接生成 unified diff；
- search 必须在目标文件中**精确且唯一**匹配，否则报错并把失败原因反馈给模型自纠；
- 全部编辑先在内存中校验通过，再统一写入——避免半应用状态；
- 应用完成后由 git diff 产出 canonical unified diff 存回 Patch。

跨平台细节：模型给出的 search 通常是 LF 换行，而仓库文件可能是 CRLF。
本工具按"保留文件原有换行风格"的方式匹配与写回，避免整文件被重写。
"""

from __future__ import annotations

from pathlib import Path

from devfix.models import Patch
from devfix.tools.errors import ToolError
from devfix.tools.git_tool import GitTool

# 缩进不一致提示：去掉每行首尾空白后能匹配时给出的自纠建议
_INDENT_HINT = "去掉每行首尾空白后可以匹配——可能是缩进/空行不一致，请按文件实际内容调整 search。"


def _count_occurrences(text: str, needle: str) -> int:
    return text.count(needle)


def _line_numbers(text: str, needle: str, limit: int = 5) -> list[int]:
    """needle 出现的行号（1 起始）。"""
    lines = text.splitlines()
    hits: list[int] = []
    for i, line in enumerate(lines, 1):
        if needle in line:
            hits.append(i)
            if len(hits) >= limit:
                break
    return hits


def _normalized_match(text: str, needle: str) -> bool:
    """忽略每行首尾空白的宽松匹配，用于诊断"匹配失败"的原因。"""
    norm = lambda s: "\n".join(line.strip() for line in s.splitlines()).strip()
    return norm(needle) in norm(text)


def _excerpt(text: str, max_lines: int = 60, max_chars: int = 2500) -> str:
    """带行号的文件内容摘录。

    匹配失败时把文件真实内容回给模型——否则模型只能凭印象编造 search 片段，
    反复失败（实测：三次尝试都编了同一段不存在的代码）。
    """
    lines = text.splitlines()[:max_lines]
    numbered = "\n".join(f"{i:>4}| {line}" for i, line in enumerate(lines, 1))
    if len(numbered) > max_chars:
        numbered = numbered[:max_chars] + "\n...[已截断]"
    return numbered


class PatchTool:
    """绑定到一个隔离工作区（git worktree）的补丁应用工具。"""

    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace).resolve()
        if not (self.workspace / ".git").exists():
            raise ToolError(f"工作区不是 git worktree：{self.workspace}")

    # ------------------------------------------------------------------ 内部
    # UTF-8 BOM：read 时剥掉保证首行 search 可匹配，写回时原样补回
    _BOM = b"\xef\xbb\xbf"

    def _resolve(self, rel: str) -> Path:
        p = (self.workspace / rel).resolve()
        if p != self.workspace and self.workspace not in p.parents:
            raise ToolError(f"路径越界，已拒绝：{rel}")
        return p

    def _read_source(self, edit_index: int, path: Path, rel: str) -> tuple[str, bool]:
        """读取文件为文本，返回 (内容, 是否有 BOM)。

        拒绝非 UTF-8 文件而不是 errors="replace" 硬读：中文 Windows 下
        GBK 编码的源码并不罕见，replace 会把全部非 ASCII 字节变成 U+FFFD
        再以 UTF-8 写回——即使补丁本身正确，整份文件也永久乱码。
        """
        data = path.read_bytes()
        had_bom = data.startswith(self._BOM)
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ToolError(
                f"编辑 #{edit_index}：{rel} 不是 UTF-8 编码（可能是 GBK/UTF-16），"
                f"为避免整文件乱码已拒绝编辑。请先确认文件编码。"
            ) from None
        return text, had_bom

    def _new_content(self, edit_index: int, original: str, edit) -> str:
        """计算单条编辑应用后的文件内容（不写盘）。"""
        crlf = "\r\n" in original
        original = original.replace("\r\n", "\n")

        if edit.full_content is not None:
            if edit.search is not None or edit.replace is not None:
                raise ToolError(f"编辑 #{edit_index}：full_content 不能与 search/replace 同时使用")
            new = edit.full_content.replace("\r\n", "\n")
        else:
            if not edit.search or edit.replace is None:
                raise ToolError(f"编辑 #{edit_index}：缺少 search 或 replace")
            needle = edit.search.replace("\r\n", "\n")
            occurrence = _count_occurrences(original, needle)
            if occurrence == 0:
                hint = f" {_INDENT_HINT}" if _normalized_match(original, needle) else ""
                raise ToolError(
                    f"编辑 #{edit_index}：search 在 {edit.file} 中未找到匹配。{hint}\n"
                    f"你给的 search 前 120 字符：{needle[:120]!r}\n"
                    f"--- {edit.file} 的实际内容（行号| 内容）---\n"
                    f"{_excerpt(original)}\n"
                    f"--- 请直接复制上面的原文（含缩进）作为新的 search ---"
                )
            if occurrence > 1:
                raise ToolError(
                    f"编辑 #{edit_index}：search 在 {edit.file} 中出现 {occurrence} 次"
                    f"（行 {_line_numbers(original, needle)}），必须唯一。"
                    f"请扩大 search 片段加入上下文行以唯一定位。"
                )
            new = original.replace(needle, edit.replace.replace("\r\n", "\n"), 1)

        if new == original:
            raise ToolError(f"编辑 #{edit_index}：未产生任何变化（replace 与 search 相同？）")
        return new.replace("\n", "\r\n") if crlf else new

    # ------------------------------------------------------------------ 公共 API
    def apply(self, patch: Patch) -> Patch:
        """应用补丁并返回带 changed_files 与 canonical diff 的新 Patch。

        Raises:
            ToolError: 任一条编辑校验失败（此时不会写入任何文件）。
        """
        if not patch.edits:
            raise ToolError("Patch 不包含任何编辑")

        staged: list[tuple[Path, str, bool]] = []  # (路径, 新内容, 原 BOM)
        seen: set[Path] = set()
        for i, edit in enumerate(patch.edits, 1):
            path = self._resolve(edit.file)
            if not path.is_file():
                raise ToolError(f"编辑 #{i}：文件不存在 {edit.file}")
            if path in seen:
                raise ToolError(f"编辑 #{i}：同一文件 {edit.file} 出现多条编辑，请合并为一条")
            seen.add(path)
            raw, had_bom = self._read_source(i, path, edit.file)
            staged.append((path, self._new_content(i, raw, edit), had_bom))

        # 全部校验通过后才落盘（write_bytes：UTF-8 + 按原文件补回 BOM；
        # content 中的换行已是文件原风格，不经 text 模式再翻译）
        for path, content, had_bom in staged:
            data = content.encode("utf-8")
            if had_bom:
                data = self._BOM + data
            path.write_bytes(data)

        git = GitTool(self.workspace)
        return patch.model_copy(update={
            "changed_files": sorted({e.file for e in patch.edits}),
            "diff": git.working_diff(),
        })
