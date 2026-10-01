"""Context Acquisition：执行 Agent 的取证请求（Agent Engineering V0.2 §1~§2）。

确定性节点：把模型给出的 ContextRequest 映射到只读工具（FileTool/SearchTool/
GitTool 的薄封装，安全属性直接继承——路径越界防护、rg+降级、编码保护），
结果追加进 Evidence 账本。

设计要点：
- 工具执行失败不是异常，而是**反馈**——以 tool_result 证据回传给模型自纠
  （复用 PatchTool 内层重试的实测经验：错误信息必须可行动）；
- 证据预算记账：超限的请求被"拒绝"而不是静默截断，模型能看到原因；
- 本模块不做任何 LLM 调用。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TypedDict

from pydantic import BaseModel as PydanticBaseModel

from devfix.models import ContextRequest, Evidence, budget_used, next_evidence_id
from devfix.observability import record_tool
from devfix.tools import FileTool, GitTool, SearchTool, ToolError

# 取证预算（Agent Engineering V0.2 §1）：防止退化成"把整个 repo 塞进去"
MAX_ACQUIRE_ROUNDS = 3        # diagnose → tools → diagnose 算一轮
MAX_TOOL_CALLS = 8            # 单 run 工具调用上限
# 证据账本总字符预算：须容纳初始确定性收集（失败测试类已封顶 6k，
# 符号文件热点注入若干）+ Agent 取证增量。实测初始收集约 50k——
# 预算若只有 60k，Agent 的每条取证请求都会被"预算已满"拒绝
EVIDENCE_BUDGET_CHARS = 120_000
MAX_READ_LINES = 250          # 单次 read_file 最大行数
SEARCH_MAX_MATCHES = 20       # 单次搜索最大命中数
SEARCH_RESULT_MAX_CHARS = 3_000
DIFF_MAX_CHARS = 6_000

AVAILABLE_TOOLS = ("search_code", "read_file", "find_symbol", "get_git_diff", "get_changed_files")


# 取证工具的参数 schema：同时 bind 给模型（见 diagnosis.LLMDiagnoser）——
# DeepSeek 实测会把提示词里描述的"工具"直接当原生 function call 发出，
# 与其让解析器报错，不如把两种表达都接住（提示词请求 + 原生调用），
# 原生调用在解析层转换为 ContextRequest。
class SearchCodeArgs(PydanticBaseModel):
    query: str
    glob: str | None = None


class ReadFileArgs(PydanticBaseModel):
    path: str
    start: int = 1


class FindSymbolArgs(PydanticBaseModel):
    name: str


class GetGitDiffArgs(PydanticBaseModel):
    pass


class GetChangedFilesArgs(PydanticBaseModel):
    pass


ACQUISITION_TOOL_SCHEMAS = (
    SearchCodeArgs, ReadFileArgs, FindSymbolArgs, GetGitDiffArgs, GetChangedFilesArgs,
)

# 工具名 → 参数 schema：bind 时必须用**显式函数名**（直接传 pydantic 类会让
# LangChain 用类名当工具名，模型回吐 "ReadFileArgs" 而不是 "read_file"，
# 实测曾导致救援解析 miss → 误入纠正重试）。
ACQUISITION_TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取仓库内指定文件的片段（最多 250 行）",
            "parameters": ReadFileArgs.model_json_schema(),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": "在仓库内做文本/符号搜索",
            "parameters": SearchCodeArgs.model_json_schema(),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_symbol",
            "description": "按符号名查找定义/引用候选位置",
            "parameters": FindSymbolArgs.model_json_schema(),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_git_diff",
            "description": "获取本次提交的完整 diff",
            "parameters": GetGitDiffArgs.model_json_schema(),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_changed_files",
            "description": "获取本次变更的文件列表",
            "parameters": GetChangedFilesArgs.model_json_schema(),
        },
    },
]

# 兜底别名：其他 provider 若仍以 schema 类名回吐，也能映射回工具名
_TOOL_NAME_ALIAS = {
    "ReadFileArgs": "read_file",
    "SearchCodeArgs": "search_code",
    "FindSymbolArgs": "find_symbol",
    "GetGitDiffArgs": "get_git_diff",
    "GetChangedFilesArgs": "get_changed_files",
}


class AcquisitionState(TypedDict, total=False):
    """本节点读写的 state 字段（完整定义见 devfix.graph.state.RunState）。"""

    repo: str
    base: str | None
    context_requests: list[ContextRequest]
    evidence: list[Evidence]
    inspected: list[str]
    acquire_rounds: int
    tool_calls_used: int
    error: str


def _search_evidence(eid: str, req: ContextRequest, repo: Path) -> Evidence:
    search = SearchTool(repo)
    query = str(req.args.get("query", "")).strip()
    if not query:
        return _feedback(eid, req, "search_code 缺少必填参数 query")
    glob = req.args.get("glob")
    matches = search.search_symbol(query) if len(query.split()) == 1 and query[0].isidentifier() \
        else search.search_text(query, str(glob) if glob else None)
    if not matches:
        return _feedback(eid, req, f"search_code('{query}') 无命中——换更短/更常见的关键词再试")
    lines = [
        f"{m.file_path}:{m.line_number}: {m.line_text.strip()[:200]}"
        for m in matches[:SEARCH_MAX_MATCHES]
    ]
    content = f"search_code('{query}') 命中 {len(matches)} 处：\n" + "\n".join(lines)
    return Evidence(
        id=eid, kind="tool_result", source=f"tool:search_code#{query}",
        content=content[:SEARCH_RESULT_MAX_CHARS],
    )


def _read_evidence(eid: str, req: ContextRequest, repo: Path) -> Evidence:
    files = FileTool(repo)
    path = str(req.args.get("path", "")).strip()
    if not path:
        return _feedback(eid, req, "read_file 缺少必填参数 path")
    try:
        start = max(1, int(req.args.get("start", 1)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        start = 1
    end = start + MAX_READ_LINES - 1
    try:
        content = files.read_range(path, start, end)
    except ToolError as e:
        return _feedback(eid, req, f"read_file({path}, {start}) 失败：{e}")
    return Evidence(
        id=eid, kind="source_range", source=f"tool:read_file#{path}",
        file_path=path, line_start=start,
        line_end=start + len(content.splitlines()) - 1, content=content,
    )


def _symbol_evidence(eid: str, req: ContextRequest, repo: Path) -> Evidence:
    search = SearchTool(repo)
    name = str(req.args.get("name", "")).strip()
    if not name:
        return _feedback(eid, req, "find_symbol 缺少必填参数 name")
    matches = search.search_symbol(name)
    if not matches:
        return _feedback(eid, req, f"find_symbol('{name}') 无命中——确认符号名拼写或改用 search_code")
    lines = [f"{m.file_path}:{m.line_number}: {m.line_text.strip()[:200]}" for m in matches[:SEARCH_MAX_MATCHES]]
    return Evidence(
        id=eid, kind="tool_result", source=f"tool:find_symbol#{name}",
        content=f"find_symbol('{name}') 的定义/引用候选：\n" + "\n".join(lines),
    )


def _diff_evidence(eid: str, req: ContextRequest, repo: Path, base: str | None) -> Evidence:
    git = GitTool(repo)
    if not base:
        return _feedback(eid, req, "get_git_diff 不可用：本 run 未配置 diff 基线")
    try:
        diff = git.diff(base)
    except ToolError as e:
        return _feedback(eid, req, f"get_git_diff 失败：{e}")
    return Evidence(id=eid, kind="git_diff", source="tool:get_git_diff", content=diff[:DIFF_MAX_CHARS])


def _changed_evidence(eid: str, req: ContextRequest, repo: Path, base: str | None) -> Evidence:
    git = GitTool(repo)
    if not base:
        return _feedback(eid, req, "get_changed_files 不可用：本 run 未配置 diff 基线")
    try:
        files = git.changed_files(base)
    except ToolError as e:
        return _feedback(eid, req, f"get_changed_files 失败：{e}")
    return Evidence(
        id=eid, kind="tool_result", source="tool:get_changed_files",
        content="本次变更文件：\n" + ("\n".join(f"  - {f}" for f in files) or "（无）"),
    )


def _feedback(eid: str, req: ContextRequest, message: str) -> Evidence:
    """工具失败/参数问题也以证据形式回传——模型据此自纠，而非节点报错。"""
    return Evidence(id=eid, kind="tool_result", source=f"tool:{req.tool}", content=f"[取证未成功] {message}")


def execute_request(
    req: ContextRequest, repo: Path, base: str | None, evidence: list[Evidence]
) -> Evidence:
    """执行单条取证请求 → 证据（含失败反馈与预算拒绝）。"""
    eid = next_evidence_id(evidence)
    remaining = EVIDENCE_BUDGET_CHARS - budget_used(evidence)
    if remaining <= 0:
        ev = _feedback(
            eid, req,
            f"证据预算已满（{budget_used(evidence)}/{EVIDENCE_BUDGET_CHARS} 字符），"
            f"请求被拒绝。请基于现有证据下结论。",
        )
        record_tool(req.tool, dict(req.args), ok=False, evidence_id=eid)
        return ev
    started = time.monotonic()
    if req.tool == "search_code":
        ev = _search_evidence(eid, req, repo)
    elif req.tool == "read_file":
        ev = _read_evidence(eid, req, repo)
    elif req.tool == "find_symbol":
        ev = _symbol_evidence(eid, req, repo)
    elif req.tool == "get_git_diff":
        ev = _diff_evidence(eid, req, repo, base)
    elif req.tool == "get_changed_files":
        ev = _changed_evidence(eid, req, repo, base)
    else:
        ev = _feedback(
            eid, req,
            f"未知工具 '{req.tool}'。可用工具：{', '.join(AVAILABLE_TOOLS)}",
        )
    record_tool(
        req.tool, dict(req.args),
        ok="[取证未成功]" not in ev.content and "预算已满" not in ev.content,
        evidence_id=eid,
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    # 预算尾差：截断到剩余空间
    over = budget_used(evidence) + len(ev.content) - EVIDENCE_BUDGET_CHARS
    if over > 0:
        ev = ev.model_copy(update={
            "content": ev.content[: max(0, len(ev.content) - over)]
            + f"\n[因证据预算截断，原始请求：{req.tool} {req.args}]",
        })
    return ev


def _inspect_label(req: ContextRequest) -> str:
    path = str(req.args.get("path", req.args.get("query", req.args.get("name", ""))))
    return f"{req.tool}:{path}"


def acquire_node(state: AcquisitionState) -> dict:
    """LangGraph 节点：执行 context_requests，追加证据，推进预算计数。"""
    requests = state.get("context_requests") or []
    evidence = list(state.get("evidence") or [])
    inspected = list(state.get("inspected") or [])
    repo_str = state.get("repo")
    if repo_str is None:
        return {"error": "acquire 节点缺少 repo"}
    repo = Path(repo_str)
    base = state.get("base")

    calls_used = state.get("tool_calls_used", 0)
    for req in requests:
        if calls_used >= MAX_TOOL_CALLS:
            evidence.append(_feedback(
                next_evidence_id(evidence), req,
                f"工具调用次数已达上限（{MAX_TOOL_CALLS}），本请求未执行。"
                f"请基于现有证据下结论。",
            ))
            continue
        ev = execute_request(req, repo, base, evidence)
        evidence.append(ev)
        inspected.append(_inspect_label(req))
        calls_used += 1

    return {
        "evidence": evidence,
        "inspected": inspected,
        "tool_calls_used": calls_used,
        "acquire_rounds": state.get("acquire_rounds", 0) + 1,
        # 请求已消费，防止下一轮诊断节点重复执行
        "context_requests": [],
    }
