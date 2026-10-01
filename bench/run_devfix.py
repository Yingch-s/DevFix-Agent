"""在 benchmark case 上评测 DevFix（完整 agent 闭环）。

用法：
    .venv/Scripts/python -m bench.run_devfix --limit 3
    .venv/Scripts/python -m bench.run_devfix --case jsoup-34fb153-testTagStartsWithToString
"""

from __future__ import annotations

import argparse
import sys

from bench.runner import (
    Timer,
    compute_failure_baseline,
    load_case_log,
    load_cases,
    prepare_repo,
    summarize,
    write_result,
)
from devfix import observability
from devfix.config import load_config
from devfix.graph import build_repair_graph, invoke_config
from devfix.llm.provider import get_chat_model
from devfix.models import VerificationLevel
from devfix.nodes.diagnosis import LLMDiagnoser
from devfix.nodes.reflection import LLMReflector
from devfix.nodes.repair import LLMPatcher
from devfix.report import new_run_id, save_report
from devfix.verify import MavenVerifier

BENCH_WS_ROOT = "bench/.work"


def evaluate_case(case: dict, model, cfg, max_attempts: int) -> dict:
    repo = prepare_repo(case)
    log_text = load_case_log(case)
    vcfg = cfg.verification
    # 环境失败基线：验证判定用"无新增失败"（SWE-bench F2P+P2P 语义）
    baseline = compute_failure_baseline(repo, case, cfg)

    graph = build_repair_graph(
        diagnoser=LLMDiagnoser(model),
        patcher=LLMPatcher(model),
        reflector=LLMReflector(model),
        verifier=MavenVerifier(
            mvn=vcfg.maven_command,
            timeouts={
                VerificationLevel.FOCUSED: vcfg.focused_timeout_s,
                VerificationLevel.RELATED: vcfg.related_timeout_s,
                VerificationLevel.FULL: vcfg.full_timeout_s,
            },
            extra_args=vcfg.effective_maven_args(),
        ),
    )

    run_id = new_run_id()
    trace = observability.Trace(run_id=run_id)
    observability.current_trace = trace  # 节点经 observability.current_trace 读取
    try:
        with Timer() as t:
            final = graph.invoke(
                {
                    "repo": str(repo),
                    "log_text": log_text,
                    # case 的 buggy 提交即"CI 失败的这次提交"，base 取它的父提交；
                    # 共享仓库 HEAD 是"buggy 提交 + harness 固化的 test patch"，
                    # diff 必须显式指向 buggy 提交，避免上下文混入 harness 改动
                    "base": f"{case['buggy_commit']}^",
                    "diff_target": case["buggy_commit"],
                    "max_attempts": max_attempts,
                    "workspace_dir": f"{BENCH_WS_ROOT}/{case['case_id']}",
                    # 工作区必须基于"buggy 提交 + 固化的 test patch"：worktree 从提交
                    # 创建，不提交的话失败测试在工作区里不存在，L1 结构性必败
                    "workspace_commit": case.get("_test_patch_commit"),
                    "baseline_failures": sorted(baseline),
                },
                invoke_config(max_attempts),
            )
            save_report(
                final, output_root=f"bench/runs/{case['case_id']}",
                run_id=run_id, repo_path=repo,
            )
    finally:
        observability.current_trace = None

    # Agent 行为成本（V0.2 §8：effectiveness 与 cost 同框）
    trace.stop(
        "ERROR" if final.get("error") else (
            "FIXED" if (final.get("verification") and final["verification"].passed) else "UNFIXED"
        ),
        stop_reason=final.get("stop_reason", ""),
    )

    if final.get("error"):
        return summarize(
            case, "devfix", "ERROR", final.get("attempt", 0), None, [], t.elapsed,
            extra={"error": final["error"][:300], **trace.totals},
        )

    attempts = final.get("attempts", [])
    patch_files = sorted({
        e.file for record in attempts if record.patch for e in record.patch.edits
    })
    return summarize(
        case,
        "devfix",
        "FIXED" if (final.get("verification") and final["verification"].passed)
        else ("STOPPED" if final.get("stop_reason") else "FAILED_TO_FIX"),
        final.get("attempt", 0),
        final.get("verification"),
        (final.get("diagnosis").related_files if final.get("diagnosis") else []),
        t.elapsed,
        patch_files=patch_files,
        extra={
            "run_id": run_id,
            "attempt_history": final.get("attempt_history", []),
            **trace.totals,
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", action="append", help="只评测指定 case（可多次）")
    parser.add_argument("--limit", type=int, help="最多评测多少个 case")
    parser.add_argument("--max-attempts", type=int, default=3)
    args = parser.parse_args()

    cases = load_cases(only=args.case)
    if args.limit:
        cases = cases[: args.limit]
    if not cases:
        raise SystemExit("没有 case：先运行 scripts/mine_cases.py 挖掘")

    cfg = load_config()
    model = get_chat_model(cfg.llm)
    print(f"评测 {len(cases)} 个 case（{cfg.llm.provider}/{cfg.llm.model}）\n")

    for case in cases:
        print(f"[{case['case_id']}] {case['fix_subject'][:60]}")
        summary = evaluate_case(case, model, cfg, args.max_attempts)
        path = write_result(summary)
        mark = "FIXED" if summary["fixed"] else summary["status"]
        print(f"  -> {mark}（尝试 {summary['attempts']} 次，{summary['duration_s']}s）"
              f" 结果：{path}\n")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
