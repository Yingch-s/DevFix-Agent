"""上下文收集节点（确定性，不调用 LLM）。

对应系统设计 5.3 Failure Context Collector + 5.5 Code Localization + 12 节上下文构建：
从 FailureContext 出发，沿着
    堆栈帧 → 失败测试 → **测试引用的符号反查生产代码** → 编译错误 → git diff
逐步缩小范围，产出 DiagnosisContext（小上下文优先，不把整个仓库塞给模型）。

符号反查这一环很关键：断言失败/超时类故障的异常栈里没有项目代码
（栈全在 JUnit 里），只靠栈帧会**只读到测试文件、看不到被测实现**——
Benchmark 实测：这让诊断只能给出"证据不足"（置信度 0.3）而无法定位根因。
"""

from __future__ import annotations

import re
from pathlib import Path

from devfix.models import (
    CodeSnippet,
    DiagnosisContext,
    FailureContext,
    TriageResult,
)
from devfix.tools import FileTool, GitTool, ToolError

STACK_RADIUS = 8    # 堆栈帧上下各取 8 行
COMPILE_RADIUS = 5  # 编译错误行上下文
DIFF_MAX_CHARS = 20_000
MAX_SYMBOL_FILES = 4    # 由失败测试反查的生产文件上限
MAX_CALLCHAIN_FILES = 3  # 调用关系扩展（从已读片段再跳一跳）的文件上限
SYMBOL_HEAD_LINES = 45      # 符号文件注入的类骨架（头部）行数
SYMBOL_HOT_BUDGET = 280     # 头部之外热点区域的总行数预算
TEST_HEAD_LINES = 40        # 失败测试类的骨架（头部）行数
TEST_METHOD_RADIUS = (30, 90)   # 失败方法前后各行数
TEST_SNIPPET_MAX_CHARS = 6_000  # 失败测试类注入的字符封顶

# 测试代码里高频出现但与根因无关的标识符
_SYMBOL_STOPLIST = {
    "Assertions", "Assert", "Test", "Tests", "Before", "After", "BeforeEach",
    "AfterEach", "ParameterizedTest", "String", "Integer", "Long", "Double",
    "Boolean", "List", "Map", "Set", "ArrayList", "HashMap", "HashSet", "Arrays",
    "Objects", "IOException", "Exception", "RuntimeException", "IllegalArgumentException",
    "System", "Math", "Collections", "Optional", "Stream", "Collectors", "File",
    "Path", "Paths", "Files", "Charset", "StandardCharsets", "Mockito", "Mock",
    "Override", "Deprecated", "SuppressWarnings", "TestNG", "IOUtils",
}


def _method_body(test_source: str, method_name: str) -> str:
    """提取失败测试方法体（花括号配平，无 AST）。"""
    lines = test_source.splitlines()
    start = None
    for i, line in enumerate(lines):
        if re.search(rf"\b{re.escape(method_name)}\s*\(", line):
            start = i
            break
    if start is None:
        return ""

    depth = 0
    body: list[str] = []
    for line in lines[start:]:
        body.append(line)
        depth += line.count("{") - line.count("}")
        if depth <= 0 and len(body) > 1:
            break

    return "\n".join(body)


def _extract_test_symbols(test_source: str, method_name: str) -> list[str]:
    """从失败测试方法体中提取被引用的类名（大写开头的标识符）。

    只做粗粒度提取（无需 AST）：测试方法体里出现的类型名，绝大多数就是
    被测对象及其协作者——足以把"被测生产代码"拉进上下文。
    """
    return _extract_type_names(_method_body(test_source, method_name))[:8]


# 测试方法体里高频但与根因定位无关的调用（断言/JDK 通用方法）
_CALL_STOPLIST = {
    "assertEquals", "assertNotEquals", "assertTrue", "assertFalse",
    "assertNull", "assertNotNull", "assertArrayEquals", "assertThrows",
    "assertDoesNotThrow", "assertTimeoutPreemptively", "assertIterableEquals",
    "expectThrows", "fail", "println", "printf", "format", "valueOf",
    "toString", "isEmpty", "size", "get", "add", "put", "equals", "hashCode",
}


def _extract_call_names(test_source: str, method_name: str) -> list[str]:
    """从失败测试方法体提取方法调用名（小写驼峰标识符）。

    调用名是比类型名更强的定位信号：测试调用的被测 API（如
    getBytePosition）在生产代码全文里搜索，通常正好落在根因方法上。
    """
    out: list[str] = []
    for m in re.finditer(r"\b([a-z][A-Za-z0-9]{2,})\s*\(", _method_body(test_source, method_name)):
        name = m.group(1)
        if name in _CALL_STOPLIST or name in out:
            continue
        out.append(name)
    return out[:12]


def _hot_regions(
    lines: list[str], tokens: list[str], radius: int = 30,
    gap: int = 15, max_regions: int = 3,
) -> list[tuple[int, int]]:
    """在文件行列表中找 token 命中的热点区域（1 起始闭区间，按位置排序）。

    相邻命中合并（间隔 ≤ gap 行），按命中数取最多的前 max_regions 个，
    每个向两侧扩展 radius 行。
    """
    if not tokens:
        return []
    hits = [i for i, line in enumerate(lines) if any(t in line for t in tokens)]
    if not hits:
        return []
    clusters: list[list[int]] = [[hits[0], hits[0]]]
    for i in hits[1:]:
        if i - clusters[-1][1] <= gap:
            clusters[-1][1] = i
        else:
            clusters.append([i, i])
    clusters.sort(key=lambda c: -sum(1 for h in hits if c[0] <= h <= c[1]))
    regions: list[tuple[int, int]] = []
    for c in clusters[:max_regions]:
        regions.append((max(1, c[0] + 1 - radius), min(len(lines), c[1] + 1 + radius)))
    return sorted(regions)


def _build_symbol_content(
    full_text: str, tokens: list[str],
    head_lines: int = SYMBOL_HEAD_LINES, hot_budget: int = SYMBOL_HOT_BUDGET,
) -> str:
    """符号文件的注入内容：类骨架（头部）+ 按 token 定位的热点区域。

    固定"前 160 行"窗口的教训：大文件（CSVParser 1500+ 行）的根因逻辑
    （nextRecord 的字节计算）在 300 行以后，头部窗口只有类头与 Builder，
    模型看不到根因代码，只能"诚实停止"——补丁还没生成就被放弃。
    """
    lines = full_text.splitlines()
    if not lines:
        return full_text
    parts = ["\n".join(lines[:head_lines])]
    covered = min(head_lines, len(lines))
    budget = hot_budget
    for start, end in _hot_regions(lines, tokens):
        if end <= covered:
            continue
        actual_start = max(start, covered + 1)
        if actual_start > covered + 1:
            parts.append(f"……（第 {covered + 1}–{actual_start - 1} 行省略）……")
        span = end - actual_start + 1
        if span > budget and parts:
            # 预算用尽：截断本区域尾部而不是丢弃整个区域
            end = actual_start + budget - 1
            span = budget
        parts.append("\n".join(lines[actual_start - 1:end]))
        covered = end
        budget -= span
        if budget <= 0:
            break
    if covered < len(lines):
        parts.append(f"……（第 {covered + 1}–{len(lines)} 行省略）……")
    return "\n".join(parts)


def _strip_comments(text: str) -> str:
    """去掉注释内容。

    必需：Apache 系项目的许可证头有 16 行注释，其中的 Licensed / Apache /
    Foundation 等大写词会被当成类型名，把引用频次统计彻底污染
    （实测频次榜首全是许可证词汇，真正的协作类被挤掉）。
    """
    out: list[str] = []
    in_block = False
    for line in text.splitlines():
        stripped = line.strip()
        if in_block:
            if "*/" in stripped:
                in_block = False
            continue
        if stripped.startswith("/*"):
            in_block = "*/" not in stripped
            continue
        if stripped.startswith(("//", "*")):
            continue
        out.append(line.split("//", 1)[0])
    return "\n".join(out)


def _extract_type_names(text: str) -> list[str]:
    """提取文本中出现的类型名（大写开头的标识符），剥离注释并过滤噪音词。"""
    out: list[str] = []
    for name in re.findall(r"\b([A-Z][A-Za-z0-9_]{2,})\b", _strip_comments(text)):
        if name in _SYMBOL_STOPLIST or name in out:
            continue
        out.append(name)
    return out


def _source_candidates(symbol: str, referring_file: str, files: FileTool) -> list[str]:
    """把符号名映射为候选源文件（相对路径）。

    同一包优先（Java 的包结构即目录结构），再退回全仓库按文件名查找。
    """
    package = ""
    for prefix in ("src/test/java/", "src/main/java/"):
        if referring_file.startswith(prefix):
            # as_posix()：Windows 下 Path.parent 会给出反斜杠，拼出 com\example/X.java
            # 这种混搭路径（同一文件出现两种写法，污染上下文与去重）
            package = Path(referring_file[len(prefix):]).parent.as_posix()
            if package == ".":
                package = ""
            break
    candidates: list[str] = []
    if package:
        for src_root in ("src/main/java", "src/test/java"):
            candidates.append(f"{src_root}/{package}/{symbol}.java")
    by_name = files.find_by_name(f"{symbol}.java")
    if by_name:
        candidates.append(by_name)
    seen: set[str] = set()
    return [c for c in candidates if not (c in seen or seen.add(c))]


def locate_source_file(class_name: str, file_name: str, files: FileTool) -> str | None:
    """把 类名+文件名 映射到仓库相对路径。

    顺序：包路径推断（src/main|test/java）→ 全仓库按文件名搜索。
    """
    package_path = class_name.replace(".", "/")
    for guess in (
        f"src/main/java/{package_path}.java",
        f"src/test/java/{package_path}.java",
    ):
        if files.exists(guess):
            return guess
    return files.find_by_name(file_name)


def _build_test_snippet(full_text: str, method_name: str | None) -> str:
    """失败测试类的注入内容：头部骨架 + 失败方法区域，字符封顶。

    整类注入的教训（benchmark 实测）：CSVParserTest.java 约 90k 字符，
    单条证据就占满整个取证预算（预算耗尽后 Agent 的所有取证请求全被
    拒绝），且其中绝大多数内容与失败无关。模型需要的是失败方法附近。
    """
    lines = full_text.splitlines()
    parts = lines[:TEST_HEAD_LINES]
    covered = len(parts)
    if method_name:
        for i, line in enumerate(lines):
            if re.search(rf"\b{re.escape(method_name)}\s*\(", line):
                start = max(covered + 1, i + 1 - TEST_METHOD_RADIUS[0])
                end = min(len(lines), i + 1 + TEST_METHOD_RADIUS[1])
                if start > covered + 1:
                    parts.append(f"……（第 {covered + 1}–{start - 1} 行省略）……")
                parts.append("\n".join(lines[start - 1:end]))
                covered = end
                break
    if covered < len(lines):
        parts.append(f"……（第 {covered + 1}–{len(lines)} 行省略）……")
    return "\n".join(parts)[:TEST_SNIPPET_MAX_CHARS]


def collect_context(
    failure: FailureContext,
    triage: TriageResult,
    repo: Path,
    base: str | None = "HEAD~1",
    target: str | None = None,
) -> DiagnosisContext:
    """收集诊断所需的最小上下文。

    target：diff 的目标提交（默认 HEAD）。benchmark 里共享仓库 HEAD 可能
    是"buggy 提交 + harness 固化的 test patch"，此时显式传 target=buggy 提交，
    避免"本次提交 diff"被 harness 的改动污染。
    """
    git = GitTool(repo)
    files = FileTool(repo)
    snippets: list[CodeSnippet] = []
    seen: set[tuple[str, int]] = set()

    def add(file_path: str | None, start: int, end: int, source: str) -> None:
        if not file_path:
            return
        key = (file_path, start)
        if key in seen:
            return
        seen.add(key)
        try:
            content = files.read_range(file_path, start, end)
        except ToolError:
            # 行号超出文件实际行数（日志与代码版本不一致等）→ 降级读全文件
            try:
                content = files.read_file(file_path)
            except ToolError:
                return
        snippets.append(
            CodeSnippet(
                file_path=file_path, start_line=start, end_line=end,
                content=content, source=source,
            )
        )

    # 1. 堆栈帧 → 异常点附近代码
    for trace in failure.stack_traces:
        for frame in trace.frames:
            if frame.file_name and frame.line_number:
                rel = locate_source_file(frame.class_name, frame.file_name, files)
                add(
                    rel,
                    max(1, frame.line_number - STACK_RADIUS),
                    frame.line_number + STACK_RADIUS,
                    "stack_trace",
                )

    # 2. 失败测试 → 类骨架 + 失败方法区域（整类注入曾产出 84k 字符的单条
    #    证据——比取证预算还大，见 _build_test_snippet 的教训注释）
    test_sources: list[tuple[str, str]] = []  # (测试文件, 测试方法名)
    for test in failure.failed_tests:
        rel = locate_source_file(test.class_name, test.class_name.rsplit(".", 1)[-1] + ".java", files)
        if not rel:
            continue
        method = getattr(test, "method_name", None)
        try:
            full = files.read_file(rel)
        except ToolError:
            continue
        key = (rel, 0)  # 测试类片段的去重键（与 add() 的行区间键区分开）
        if key not in seen:
            seen.add(key)
            snippets.append(CodeSnippet(
                file_path=rel, start_line=1, end_line=len(full.splitlines()),
                content=_build_test_snippet(full, method), source="failed_test",
            ))
        if method:
            test_sources.append((rel, method))

    # 3. 测试引用的符号反查生产代码（5.5 Code Localization）
    #    断言失败/超时类故障的栈里没有项目代码，只靠栈帧会看不到被测实现
    symbol_added = 0
    call_names: list[str] = []
    for test_file, method_name in test_sources:
        if symbol_added >= MAX_SYMBOL_FILES:
            break
        try:
            source = files.read_file(test_file)
        except ToolError:
            continue
        call_names.extend(_extract_call_names(source, method_name))
        for symbol in _extract_test_symbols(source, method_name):
            if symbol_added >= MAX_SYMBOL_FILES:
                break
            for candidate in _source_candidates(symbol, test_file, files):
                if not files.exists(candidate):
                    continue
                key = (candidate, -1)  # 符号文件的去重键与行区间片段区分开
                if key in seen:
                    break
                try:
                    full_text = files.read_file(candidate)
                except ToolError:
                    break
                seen.add(key)
                snippets.append(CodeSnippet(
                    file_path=candidate, start_line=1,
                    end_line=len(full_text.splitlines()),
                    content=_build_symbol_content(full_text, call_names),
                    source="test_symbol",
                ))
                symbol_added += 1
                break

    # 3b. 调用关系扩展（设计文档 2.2 的"调用关系"一环）：
    #     真实缺陷常藏在**内部协作类**里（测试只引用外观类），
    #     因此从已读片段的内容里再做一跳有界扩展——抓住像
    #     ExtendedBufferedReader 这类被 CSVParser 使用、却不被测试直接引用的类。
    callchain_added = 0
    # 按**引用频次**排序候选：被多段代码反复引用的类型更可能是关键协作类。
    # 测试文件内容长且类型密集，单独降权，避免挤占扩展额度。
    frequency: dict[str, int] = {}
    for snippet in snippets:
        # 用**整份文件**（而非注入的片段）统计引用：协作者常出现在片段截断之外
        # （实测 CSVParser 的 ExtendedBufferedReader 字段在第 160 行之后，
        #  只看片段就永远找不到它）。读全文不消耗 LLM 上下文，只用于统计。
        try:
            full_text = files.read_file(snippet.file_path)
        except ToolError:
            full_text = snippet.content
        for name in _extract_type_names(full_text):
            weight = 1 if snippet.source != "failed_test" else 0
            frequency[name] = frequency.get(name, 0) + weight
    ordered_symbols = sorted(frequency, key=lambda n: -frequency[n])

    for symbol in ordered_symbols:
        if callchain_added >= MAX_CALLCHAIN_FILES:
            break
        referring = next(
            (s.file_path for s in snippets if symbol in s.content), snippets[0].file_path
        )
        for candidate in _source_candidates(symbol, referring, files):
            key = (candidate, -1)
            if key in seen or not files.exists(candidate):
                continue
            try:
                full_text = files.read_file(candidate)
            except ToolError:
                continue
            seen.add(key)
            snippets.append(CodeSnippet(
                file_path=candidate, start_line=1,
                end_line=len(full_text.splitlines()),
                content=_build_symbol_content(full_text, call_names),
                source="call_chain",
            ))
            callchain_added += 1
            break

    # 4. 编译错误 → 出错行附近
    for ce in failure.compile_errors:
        rel = ce.file_path
        try:
            # 绝对路径（Windows /D:/... 已规范化）转仓库相对路径
            p = Path(rel)
            if p.is_absolute():
                resolved = p.resolve()
                root = files.root
                if resolved == root or root in resolved.parents:
                    rel = str(resolved.relative_to(root))
                else:
                    rel = files.find_by_name(p.name)
        except (OSError, ValueError):
            rel = files.find_by_name(Path(ce.file_path).name)
        if ce.line:
            add(rel, max(1, ce.line - COMPILE_RADIUS), ce.line + COMPILE_RADIUS, "compile_error")

    # 5. git diff：Failure 与 Recent Change 建立联系
    git_diff = ""
    if base:
        try:
            git_diff = git.diff(base, target or "HEAD")
        except ToolError:
            git_diff = ""
    if len(git_diff) > DIFF_MAX_CHARS:
        git_diff = git_diff[:DIFF_MAX_CHARS] + "\n...[diff 已截断]"

    return DiagnosisContext(
        failure=failure, triage=triage, git_diff=git_diff, snippets=snippets
    )
