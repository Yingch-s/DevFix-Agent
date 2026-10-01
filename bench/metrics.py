"""指标汇总（对应系统设计 26 节 Evaluation 设计）。

从 bench/results/*.json 汇总 DevFix 与 baseline 的对比指标，输出 Markdown 表格。

用法：
    .venv/Scripts/python -m bench.metrics
    .venv/Scripts/python -m bench.metrics --out bench/report.md
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

RESULTS_DIR = Path("bench/results")


def load_results(results_dir: Path = RESULTS_DIR) -> list[dict]:
    results = []
    for path in sorted(results_dir.glob("*.json")):
        results.append(json.loads(path.read_text(encoding="utf-8")))
    return results


def _rate(items: list[bool]) -> str:
    if not items:
        return "—"
    return f"{sum(items) / len(items) * 100:.0f}% ({sum(items)}/{len(items)})"


def compute_metrics(results: list[dict], approach: str) -> dict:
    rows = [r for r in results if r["approach"] == approach]
    if not rows:
        return {}
    fixed = [r["fixed"] for r in rows]
    first = [r.get("first_attempt_fixed", False) for r in rows]
    attempts = [r["attempts"] for r in rows if r["attempts"]]
    top1 = [r.get("top1_localization", False) for r in rows]
    top3 = [r.get("top3_localization", False) for r in rows]
    unsafe = [r.get("unsafe_modification", False) for r in rows]
    durations = [r.get("duration_s", 0) for r in rows]

    # Agent 行为成本（V0.2 §8：effectiveness 与 cost 同框）
    def _avg(key: str) -> str:
        vals = [r[key] for r in rows if r.get(key) is not None]
        return f"{statistics.mean(vals):.1f}" if vals else "—"

    return {
        "approach": approach,
        "cases": len(rows),
        "fix_rate": _rate(fixed),
        "first_attempt_rate": _rate(first),
        "avg_attempts": f"{statistics.mean(attempts):.2f}" if attempts else "—",
        "top1": _rate(top1),
        "top3": _rate(top3),
        "unsafe_rate": _rate(unsafe),
        "avg_llm_calls": _avg("llm_calls"),
        "avg_tool_calls": _avg("tool_calls"),
        "avg_input_tokens": _avg("input_tokens"),
        "avg_output_tokens": _avg("output_tokens"),
        "avg_duration_s": f"{statistics.mean(durations):.0f}s" if durations else "—",
        # 修复耗时中位数比均值更能反映典型成本
        "median_duration_s": f"{statistics.median(durations):.0f}s" if durations else "—",
    }


METRIC_LABELS = {
    "cases": "Case 数",
    "fix_rate": "最终修复率",
    "first_attempt_rate": "首次修复率",
    "avg_attempts": "平均尝试次数",
    "top1": "Top-1 文件定位",
    "top3": "Top-3 文件定位",
    "unsafe_rate": "不安全修改率",
    "avg_llm_calls": "平均 LLM 调用",
    "avg_tool_calls": "平均工具调用",
    "avg_input_tokens": "平均输入 tokens",
    "avg_output_tokens": "平均输出 tokens",
    "avg_duration_s": "平均耗时",
    "median_duration_s": "耗时中位数",
}


def render_markdown(results: list[dict]) -> str:
    # 对比指标必须在**两方法共有的 case 交集**上计算：结果池按 case 文件
    # 覆盖写入，改模型/参数后重跑部分 case 时，两边的 case 集合会不一致，
    # 直接各算各的会让 fix_rate 的分母悄悄不同（对比失去意义）。
    by_case: dict[str, dict[str, dict]] = {}
    for r in results:
        by_case.setdefault(r["case_id"], {})[r["approach"]] = r
    common = {
        cid for cid, approaches in by_case.items()
        if "devfix" in approaches and "baseline" in approaches
    }
    comparison = (
        [r for r in results if r["case_id"] in common] if common else results
    )
    devfix = compute_metrics(comparison, "devfix")
    baseline = compute_metrics(comparison, "baseline")

    lines = ["# DevFix Benchmark", ""]
    lines.append(f"- 总 case 数：{len({r['case_id'] for r in results})}")
    repos = sorted({r["repo_name"] for r in results})
    lines.append(f"- 项目：{', '.join(repos)}")
    if common:
        lines.append(f"- 对比口径：两方法均完成评测的 {len(common)} 个 case 的交集")
    else:
        lines.append("- 对比口径：目前只有单侧结果，各指标按各自完成的 case 计算")
    lines.append("")
    lines.append("## 指标对比")
    lines.append("")
    lines.append("| 指标 | DevFix（agent 闭环） | Baseline（一次调用） |")
    lines.append("|---|---|---|")
    for key, label in METRIC_LABELS.items():
        lines.append(
            f"| {label} | {devfix.get(key, '—')} | {baseline.get(key, '—')} |"
        )
    lines.append("")

    lines.append("## 逐 case 结果")
    lines.append("")
    lines.append("| case | 项目 | DevFix | 尝试 | 首次修复 | Top-1 | Baseline |")
    lines.append("|---|---|---|---|---|---|---|")
    for case_id, approaches in sorted(by_case.items()):
        d = approaches.get("devfix", {})
        b = approaches.get("baseline", {})
        lines.append(
            f"| {case_id} | {d.get('repo_name', b.get('repo_name', ''))} "
            f"| {'✅' if d.get('fixed') else d.get('status', '—')} "
            f"| {d.get('attempts', '—')} "
            f"| {'✅' if d.get('first_attempt_fixed') else '❌'} "
            f"| {'✅' if d.get('top1_localization') else '❌'} "
            f"| {'✅' if b.get('fixed') else b.get('status', '—')} |"
        )
    lines += [
        "",
        "## 口径说明",
        "",
        (
            "- case 来源：`scripts/mine_cases.py` 从真实开源项目修复提交中挖掘，"
            "要求「父提交上目标测试真实失败 + 修复提交上通过」；"
        ),
        (
            "- 修复判定：分层验证（原失败用例 → 相关测试类 → 完整构建）"
            "全部通过才算 FIXED，不接受模型自评；"
        ),
        "- baseline：同一模型、同一上下文、同一验证命令，但不做诊断/策略/反思、只尝试一次；",
        (
            "- 不安全修改率：补丁触及 src/test 下的测试文件（改测试让验证变绿）。"
            "DevFix 的策略禁止改测试，baseline 无策略——该列显性化两者的诚信差异；"
        ),
        "- Top-K 定位：诊断给出的相关文件（按顺序）是否覆盖修复提交改动的生产代码文件。",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", help="Markdown 输出路径")
    parser.add_argument("--results-dir", default=str(RESULTS_DIR))
    args = parser.parse_args()

    results = load_results(Path(args.results_dir))
    if not results:
        raise SystemExit("没有结果：先运行 bench.run_devfix 与 bench.baseline")
    markdown = render_markdown(results)
    print(markdown)
    if args.out:
        Path(args.out).write_text(markdown, encoding="utf-8")
        print(f"\n已写入 {args.out}")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
