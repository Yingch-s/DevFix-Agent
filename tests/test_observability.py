"""Observability 测试（Agent Engineering V0.2 §7/§8）。"""

from __future__ import annotations

import json

from devfix import observability
from devfix.observability import Trace


class TestTrace:
    def test_events_and_totals(self, tmp_path) -> None:
        t = Trace(run_id="RF-1")
        t.run_start(repo="r", model="m")
        t.llm_call("diagnosis", input_tokens=100, output_tokens=10)
        t.llm_call("repair", input_tokens=50, output_tokens=None)
        t.tool_call("read_file", {"path": "a"}, ok=True, evidence_id="EV-1")
        t.stop("FIXED")
        assert t.llm_calls == 2
        assert t.tool_calls == 1
        assert t.input_tokens == 150
        assert t.output_tokens == 10  # None 不累计
        assert t.totals["llm_calls"] == 2

        path = t.dump(tmp_path / "trace.jsonl")
        events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        assert [e["t"] for e in events] == ["run_start", "llm_call", "llm_call", "tool_call", "stop"]
        assert events[2].get("output_tokens") is None  # 缺 usage 时省略键（metrics 层 fallback）

    def test_current_trace_none_is_noop(self) -> None:
        """trace 未设置时 record_* 静默跳过——离线测试/旧路径零影响。"""
        assert observability.current_trace is None
        observability.record_llm("diagnosis", object())  # 不抛异常
        observability.record_tool("read_file", {}, ok=True)


class TestMetricsCostColumns:
    def test_avg_columns(self) -> None:
        from bench.metrics import compute_metrics

        results = [
            {"approach": "devfix", "fixed": True, "attempts": 1,
             "llm_calls": 3, "tool_calls": 2, "input_tokens": 100},
            {"approach": "devfix", "fixed": False, "attempts": 3,
             "llm_calls": 9, "tool_calls": 8, "input_tokens": None},
        ]
        m = compute_metrics(results, "devfix")
        assert m["avg_llm_calls"] == "6.0"
        assert m["avg_tool_calls"] == "5.0"
        assert m["avg_input_tokens"] == "100.0"  # None 行被排除

    def test_baseline_without_trace_shows_dash(self) -> None:
        from bench.metrics import compute_metrics

        m = compute_metrics([
            {"approach": "baseline", "fixed": True, "attempts": 1, "llm_calls": 1, "tool_calls": 0},
        ], "baseline")
        assert m["avg_llm_calls"] == "1.0"
        assert m["avg_input_tokens"] == "—"
