"""Agent Trace（Agent Engineering V0.2 §7）：LLM 调用 / 工具调用 / token 开销。

设计取舍：
- 自建轻量 trace，不引 LangSmith（避免外部依赖；事件格式对齐其思路）；
- 全局 current_trace 由运行入口（CLI / bench）设置、finally 清除——
  单进程串行执行下够用，节点代码只在 trace 存在时记录（离线测试零影响）；
- token 计量优先取 provider 返回的 usage_metadata，不返回时记 None
  （metrics 层 fallback 到字符数，见 V0.2 §8）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class Trace:
    """一次 Repair Run 的行为记录（写入 runs/<runId>/trace.jsonl）。"""

    def __init__(self, run_id: str = "") -> None:
        self.run_id = run_id
        self.events: list[dict[str, Any]] = []
        self.llm_calls = 0
        self.tool_calls = 0
        self.input_tokens = 0
        self.output_tokens = 0

    # ------------------------------------------------------------------ 事件
    def run_start(self, repo: str, provider: str = "", model: str = "") -> None:
        self._add("run_start", repo=repo, provider=provider, model=model)

    def llm_call(
        self, node: str, *, input_tokens: int | None = None,
        output_tokens: int | None = None, **extra: Any,
    ) -> None:
        self.llm_calls += 1
        self._add(
            "llm_call", node=node, call_no=self.llm_calls,
            input_tokens=input_tokens, output_tokens=output_tokens,
            **extra,
        )
        if input_tokens:
            self.input_tokens += input_tokens
        if output_tokens:
            self.output_tokens += output_tokens

    def tool_call(
        self, tool: str, args: dict, *, ok: bool, evidence_id: str | None = None,
        duration_ms: int = 0, **extra: Any,
    ) -> None:
        self.tool_calls += 1
        self._add(
            "tool_call", tool=tool, args=args, ok=ok,
            evidence_id=evidence_id, duration_ms=duration_ms,
            call_no=self.tool_calls, **extra,
        )

    def stop(self, status: str, stop_reason: str = "") -> None:
        self._add("stop", status=status, stop_reason=stop_reason)

    def _add(self, event: str, **fields: Any) -> None:
        self.events.append({
            "t": event,
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            **{k: v for k, v in fields.items() if v is not None},
        })

    # ------------------------------------------------------------------ 落盘
    def dump(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "\n".join(json.dumps(e, ensure_ascii=False) for e in self.events) + "\n",
            encoding="utf-8",
        )
        return path

    @property
    def totals(self) -> dict:
        return {
            "llm_calls": self.llm_calls,
            "tool_calls": self.tool_calls,
            "input_tokens": self.input_tokens or None,
            "output_tokens": self.output_tokens or None,
        }


# 单进程串行执行下的全局 trace：运行入口设置/清除，节点读取时判空。
# （不传 trace 对象贯穿所有节点签名，避免污染协议；多线程并发 run 不支持。）
current_trace: Trace | None = None


def record_llm(node: str, message: Any, **extra: Any) -> None:
    """从 LangChain AIMessage 记录一次 LLM 调用（含 usage）。"""
    if current_trace is None:
        return
    usage = getattr(message, "usage_metadata", None) or {}
    current_trace.llm_call(
        node,
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
        **extra,
    )


def record_tool(tool: str, args: dict, *, ok: bool, evidence_id: str | None = None,
                duration_ms: int = 0, **extra: Any) -> None:
    if current_trace is None:
        return
    current_trace.tool_call(
        tool, args, ok=ok, evidence_id=evidence_id,
        duration_ms=duration_ms, **extra,
    )
