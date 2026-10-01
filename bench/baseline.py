"""Naive baseline：不做诊断、不做策略、不迭代——一次 LLM 调用直接出补丁。

这是"把日志和 diff 丢给同一个模型"的朴素做法，用来回答：
**DevFix 的 agent 闭环到底带来了多少增量？**

公平性保证：同一个模型、同一份上下文（同样由 collect_context 收集）、
同一套分层验证命令；唯一区别是没有诊断/策略/反思/多轮尝试。
"""

from __future__ import annotations

import argparse
import sys

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from bench.runner import (
    Timer,
    compute_failure_baseline,
    load_cases,
    make_worktree,
    parse_case_failure,
    prepare_repo,
    summarize,
    write_result,
)
from devfix.config import load_config
from devfix.llm.provider import get_chat_model
from devfix.models import Patch, VerificationLevel
from devfix.nodes.context_collector import collect_context
from devfix.nodes.diagnosis import format_snippets
from devfix.tools import PatchTool, ToolError
from devfix.verify import MavenVerifier

BASELINE_SYSTEM_PROMPT = """\
你是一个代码修复助手。用户会给你一次构建失败的信息与相关代码，请直接给出修复补丁。

输出要求：
- 每个 edit 提供 file + search + replace；
- search 必须是目标文件里**逐字符精确存在且唯一**的片段（含缩进）；
- 只做最小修复，不要重构，不要修改测试代码。
"""

BENCH_WS_ROOT = "bench/.work"


def render_baseline_message(bundle) -> str:
    failure = bundle.failure
    tests = "\n".join(f"  - {t.display_name}: {t.error_type} {t.message or ''}"
                      for t in failure.failed_tests) or "（无失败测试信息）"
    return (
        f"一次构建失败了，请给出修复补丁。\n\n"
        f"【失败测试】\n{tests}\n\n"
        f"【相关代码片段】\n{format_snippets(bundle)}\n\n"
        f"【本次提交 diff】\n{bundle.git_diff or '（无）'}"
    )


def propose_once(model: BaseChatModel, bundle) -> Patch:
    structured = model.with_structured_output(Patch, method="function_calling")
    return structured.invoke([
        SystemMessage(content=BASELINE_SYSTEM_PROMPT),
        HumanMessage(content=render_baseline_message(bundle)),
    ])


def evaluate_case(case: dict, model, cfg) -> dict:
    repo = prepare_repo(case)
    failure, triage = parse_case_failure(case)
    # diff 显式指向 buggy 提交：共享仓库 HEAD 已是"buggy + harness test patch"，
    # 不指定 target 会把 harness 的测试改动混进"本次提交 diff"（与 DevFix 同口径）
    bundle = collect_context(
        failure, triage, repo=repo,
        base=f"{case['buggy_commit']}^", target=case["buggy_commit"],
    )

    vcfg = cfg.verification
    baseline = compute_failure_baseline(repo, case, cfg)
    verifier = MavenVerifier(
        mvn=vcfg.maven_command,
        timeouts={
            VerificationLevel.FOCUSED: vcfg.focused_timeout_s,
            VerificationLevel.RELATED: vcfg.related_timeout_s,
            VerificationLevel.FULL: vcfg.full_timeout_s,
        },
        extra_args=vcfg.effective_maven_args(),
        baseline_failures=baseline,
    )

    patch_files: list[str] = []
    status = "FAILED_TO_FIX"
    verification = None
    with Timer() as t:
        try:
            patch = propose_once(model, bundle)
            patch_files = sorted(e.file for e in patch.edits)
            # root 不再嵌 case_id：bench/.work/<case_id>/ws-<case_id> 会把
            # 长 case_id（commons-csv 约 95 字符）拼两遍 ≈ 230 字符，
            # git worktree add 报 "fatal: '$GIT_DIR' too big"（Windows 路径
            # 长度限制），曾让 baseline 的长名 case 全部静默假失败
            ws = make_worktree(repo, case, root=BENCH_WS_ROOT)
            if patch.edits:
                try:
                    PatchTool(ws).apply(patch)
                except ToolError:
                    # 朴素做法没有自纠回路：补丁应用失败即结束
                    status = "PATCH_APPLY_FAILED"
                    raise
                results = verifier.verify(failure, ws)
                verification = results[-1]
                status = "FIXED" if verification.passed else "FAILED_TO_FIX"
            else:
                status = "EMPTY_PATCH"
        except ToolError:
            pass

    return summarize(
        case, "baseline", status, 1, verification,
        patch_files,  # baseline 没有独立诊断阶段：以其补丁文件作为"定位"结果
        t.elapsed, patch_files=patch_files,
        extra={"llm_calls": 1, "tool_calls": 0},  # 成本口径：单次 LLM 调用，无工具
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", action="append")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()

    cases = load_cases(only=args.case)
    if args.limit:
        cases = cases[: args.limit]
    if not cases:
        raise SystemExit("没有 case")

    cfg = load_config()
    model = get_chat_model(cfg.llm)
    print(f"baseline 评测 {len(cases)} 个 case\n")
    for case in cases:
        print(f"[{case['case_id']}] {case['fix_subject'][:60]}")
        summary = evaluate_case(case, model, cfg)
        write_result(summary)
        print(f"  -> {summary['status']}（{summary['duration_s']}s）\n")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
