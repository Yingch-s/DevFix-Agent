"""结构化输出统一调用路径（Agent Engineering V0.2 §7 的配套改造）。

with_structured_output 返回解析后的对象、**丢弃 usage**——token 计量需要
AIMessage。统一改走 bind_tools([schema]) + 手工解析：与诊断层已验证的
救援解析路径一致，同时拿到 usage_metadata。

解析容错（实测驱动，与 diagnosis 层同款策略）：
- 工具名大小写/前缀差异容忍（不同 provider 对 schema 名的回吐不完全一致）；
- 响应无工具调用时，尝试从文本内容提取 JSON；
- 失败纠正重试一次（协议合法：先应答 tool_calls 再给纠正提示）。
"""

from __future__ import annotations

import json
import re
from typing import TypeVar

from langchain_core.exceptions import OutputParserException
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, ToolMessage
from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

_CORRECTION = (
    "你的上一条输出无法解析为 {schema} 工具调用。请重新输出一次 "
    "{schema} 工具调用，参数完整。"
)


def _extract_json_object(text: str) -> dict | None:
    """从文本中提取第一个完整 JSON 对象（模型偶发纯文本输出的兜底）。"""
    match = re.search(r"\{", text)
    if not match:
        return None
    start = match.start()
    depth = 0
    in_str = False
    escape = False
    for i, ch in enumerate(text[start:], start):
        if escape:
            escape = False
        elif ch == "\\":
            escape = True
        elif ch == '"':
            in_str = not in_str
        elif not in_str:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        return None
    return None


def _find_tool_call(msg: BaseMessage, schema_name: str) -> dict | None:
    for tc in getattr(msg, "tool_calls", None) or []:
        name = str(tc.get("name", ""))
        if name == schema_name or name.lower() == schema_name.lower() or name.endswith(schema_name):
            return dict(tc.get("args") or {})
    return None


def invoke_structured(
    model: BaseChatModel, schema: type[T], messages: list[BaseMessage],
) -> tuple[T, BaseMessage]:
    """调用模型并解析为 schema 实例，返回 (实例, 原始 AIMessage)。

    AIMessage.usage_metadata 供调用方记录 token 开销。解析失败纠正重试一次。
    """
    bound = model.bind_tools([schema])
    last_error: Exception | None = None
    for _ in range(2):
        msg = bound.invoke(messages)
        args = _find_tool_call(msg, schema.__name__)
        if args is not None:
            try:
                return schema.model_validate(args), msg
            except ValidationError as e:
                last_error = e
        # 无工具调用 → 尝试文本 JSON 兜底
        content = msg.content if isinstance(msg.content, str) else ""
        data = _extract_json_object(content) if content else None
        if data is not None:
            try:
                return schema.model_validate(data), msg
            except ValidationError as e:
                last_error = e
        # 纠正重试：协议合法（先应答 tool_calls 再给纠正提示）
        tcs = msg.tool_calls or []
        if tcs and all(tc.get("id") for tc in tcs):
            replies = [
                ToolMessage(
                    content=f"（无法解析为 {schema.__name__}，请重新输出）",
                    tool_call_id=tc["id"],
                )
                for tc in tcs
            ]
            messages = [*messages, msg, *replies,
                        HumanMessage(content=_CORRECTION.format(schema=schema.__name__))]
        else:
            stripped = msg.model_copy(update={
                "tool_calls": [], "content": msg.content or "（上一条输出无法解析）",
            })
            messages = [*messages, stripped,
                        HumanMessage(content=_CORRECTION.format(schema=schema.__name__))]
    raise OutputParserException(
        f"连续两次输出均无法解析为 {schema.__name__}：{last_error}"
    )
