"""Repair Report 生成（对应系统设计 5.14 与 17 节）。

一次 Repair Run 的最终交付物不是聊天回复，而是**结构化报告**：
按"失败 → 诊断 → 证据 → 修复 → 验证 → 结果"组织，
并保留完整尝试历史（Observe → Reason → Act → Verify → Reflect 的可追溯记录）。

产物（runs/<runId>/）：
    report.md   人读报告
    run.json    结构化数据（便于 benchmark 统计与二次消费）
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from devfix.models import (
    Diagnosis,
    Evidence,
    FailureContext,
    RepairAttempt,
    TriageResult,
    VerificationResult,
    VerificationStatus,
)

# ---------------------------------------------------------------------- 状态
STATUS_FIXED = "FIXED"
STATUS_FAILED = "FAILED_TO_FIX"
STATUS_STOPPED = "STOPPED"
STATUS_DIAGNOSED_ONLY = "DIAGNOSED_ONLY"


def final_status(state: dict[str, Any]) -> str:
    """计算结果状态（对应系统设计 5.14 的终态集合）。

    - 有补丁且验证通过 → FIXED
    - 主动停止（证据不足/无解/策略终态）→ STOPPED
    - 尝试过但未通过 → FAILED_TO_FIX
    - 只诊断未修复 → DIAGNOSED_ONLY
    """
    verification = state.get("verification")
    if verification is not None and verification.passed:
        return STATUS_FIXED
    if state.get("stop_reason"):
        return STATUS_STOPPED
    if state.get("attempt"):
        return STATUS_FAILED
    return STATUS_DIAGNOSED_ONLY


def new_run_id(now: datetime | None = None) -> str:
    """生成 Run ID，如 RF-20260930-211530。"""
    moment = now or datetime.now().astimezone()
    return f"RF-{moment:%Y%m%d-%H%M%S}"


# ---------------------------------------------------------------------- 数据
class RepairReport(BaseModel):
    """报告的结构化形式（run.json 的内容）。"""

    run_id: str
    generated_at: str
    repository: str = ""
    base_commit: str = ""
    target_commit: str = ""
    final_status: str
    stop_reason: str = ""
    failure: FailureContext | None = None
    triage: TriageResult | None = None
    diagnosis: Diagnosis | None = None
    attempts: list[RepairAttempt] = Field(default_factory=list)
    verification_results: list[VerificationResult] = Field(default_factory=list)
    workspace: str = ""
    # Agent Trace（Agent Engineering V0.2 §7 的最小子集：取证行为可追溯）
    decision_status: str = ""            # 最终诊断决策状态
    acquire_rounds: int = 0              # 取证轮数
    tool_calls_used: int = 0             # 工具调用次数
    evidence: list[Evidence] = Field(default_factory=list)  # 证据账本


def build_report(state: dict[str, Any], run_id: str, repo_path: Path | None = None) -> RepairReport:
    """从图的最终 state 构建报告数据。"""
    repo = repo_path or Path(state.get("repo", ""))
    base = state.get("base") or ""
    target = ""
    base_sha = ""
    try:
        from devfix.tools import GitTool, ToolError

        git = GitTool(repo)
        target = git.current_commit()
        if base:
            base_sha = git.resolve(base)
    except (ToolError, OSError):
        # 版本信息缺失不影响报告生成：报告只标注为未知
        base_sha = ""

    decision = state.get("decision")
    return RepairReport(
        run_id=run_id,
        generated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        repository=str(repo),
        base_commit=base_sha or base,
        target_commit=target,
        final_status=final_status(state),
        stop_reason=state.get("stop_reason", ""),
        failure=state.get("failure"),
        triage=state.get("triage"),
        diagnosis=state.get("diagnosis"),
        attempts=list(state.get("attempts", [])),
        verification_results=list(state.get("verification_results", [])),
        workspace=state.get("workspace", ""),
        decision_status=decision.status if decision is not None else "",
        acquire_rounds=state.get("acquire_rounds", 0) or 0,
        tool_calls_used=state.get("tool_calls_used", 0) or 0,
        evidence=list(state.get("evidence", [])),
    )


# ---------------------------------------------------------------------- 渲染
def _status_badge(status: str) -> str:
    return {
        STATUS_FIXED: "✅ FIXED",
        STATUS_FAILED: "❌ FAILED_TO_FIX",
        STATUS_STOPPED: "🟡 STOPPED",
        STATUS_DIAGNOSED_ONLY: "🔵 DIAGNOSED_ONLY",
    }.get(status, status)


def _failure_section(failure: FailureContext | None, triage: TriageResult | None) -> list[str]:
    lines = ["## Failure", ""]
    if triage is not None:
        lines += [
            f"- 故障类型：{triage.failure_type.value}",
            f"- Triage 置信度：{triage.confidence}（{triage.reason}）",
        ]
    if failure is not None:
        if failure.key_error:
            lines.append(f"- 关键异常：{failure.key_error}")
        if failure.test_summary is not None:
            s = failure.test_summary
            lines.append(
                f"- 测试汇总：run={s.run}, failures={s.failures}, errors={s.errors}"
            )
        for test in failure.failed_tests:
            detail = f"（{test.error_type}: {test.message}）" if test.message else ""
            lines.append(f"- 失败用例：{test.display_name}{detail}")
        for ce in failure.compile_errors:
            lines.append(f"- 编译错误：{ce.file_path}:{ce.line} {ce.message}")
    lines.append("")
    return lines


def _diagnosis_section(diagnosis: Diagnosis | None) -> list[str]:
    lines = ["## Diagnosis", ""]
    if diagnosis is None:
        lines += ["（未产出诊断）", ""]
        return lines
    lines += [
        f"- **根因**：{diagnosis.root_cause}",
        f"- 摘要：{diagnosis.summary}",
        f"- 置信度：{diagnosis.confidence}"
        + ("（⚠️ 证据不足）" if diagnosis.insufficient_evidence else ""),
    ]
    if diagnosis.related_files:
        lines.append(f"- 相关文件：{', '.join(diagnosis.related_files)}")
    if diagnosis.related_lines:
        lines.append(f"- 相关行：{', '.join(diagnosis.related_lines)}")
    if diagnosis.observations:
        lines += ["", "### 观察（可直接验证的事实）", ""]
        lines += [f"- {o}" for o in diagnosis.observations]
    if diagnosis.inference:
        lines += ["", "### 推断链", "", diagnosis.inference]
    if diagnosis.evidence:
        lines += ["", "### 证据", ""]
        lines += [f"- {e}" for e in diagnosis.evidence]
    lines.append("")
    return lines


def _repair_section(attempts: list[RepairAttempt]) -> list[str]:
    lines = ["## Repair", ""]
    if not attempts:
        lines += ["（未产生补丁）", ""]
        return lines
    lines.append(f"- 尝试次数：{len(attempts)}")
    lines.append("")
    for record in attempts:
        lines.append(f"### 尝试 #{record.attempt}")
        lines.append("")
        if record.patch is not None:
            changes = "、".join(e.file for e in record.patch.edits) or "（无文件）"
            lines += [
                f"- 补丁：{record.patch.patch_id}",
                f"- 修改原因：{record.patch.reason or '（无）'}",
                f"- 预期效果：{record.patch.expected_effect or '（无）'}",
                f"- 涉及文件：{changes}",
            ]
            if record.patch.cannot_fix_reason:
                lines.append(f"- 模型自述无法修复：{record.patch.cannot_fix_reason}")
        if record.verdict is not None:
            lines.append(f"- 策略决策：{record.verdict.decision.value}")
            lines += [f"  - {r}" for r in record.verdict.reasons]
        if record.patch is not None and record.patch.diff:
            lines += ["", "```diff", record.patch.diff.rstrip(), "```"]
        lines.append("")
    return lines


def _verification_section(report: RepairReport) -> list[str]:
    lines = ["## Verification", ""]
    if not report.verification_results:
        lines += ["（未执行验证）", ""]
        return lines
    for v in report.verification_results:
        mark = "✅" if v.status is VerificationStatus.PASS else "❌"
        lines.append(
            f"- {mark} {v.level.value}：{v.status.value}"
            f"（{v.duration_ms / 1000:.1f}s，退出码 {v.exit_code}）"
        )
        for test in v.failed_tests:
            lines.append(f"  - 失败用例：{test.display_name} - {test.message}")
        for err in v.new_errors[:3]:
            lines.append(f"  - 错误：{err}")
    lines.append("")
    return lines


def _attempt_history_section(attempts: list[RepairAttempt]) -> list[str]:
    """Observe → Reason → Act → Verify → Reflect 的可追溯记录（设计文档 16 节）。"""
    failed = [a for a in attempts if a.reflection is not None]
    if not failed:
        return []
    lines = ["## Attempt History（反思迭代记录）", ""]
    for record in failed:
        r = record.reflection
        assert r is not None
        lines.append(f"### 尝试 #{record.attempt} 失败 → 反思")
        lines.append("")
        lines.append(f"- 失败原因：{r.failure_reason}")
        if r.new_evidence:
            lines.append("- 新证据：")
            lines += [f"  - {e}" for e in r.new_evidence]
        if r.should_update_diagnosis and r.updated_root_cause:
            lines.append(f"- 修订后根因：{r.updated_root_cause}")
        lines.append(f"- 下一步：{r.next_action.value}")
        lines.append("")
    return lines


def _agent_trace_section(report: RepairReport) -> list[str]:
    """取证行为记录（Agent Engineering V0.2 §7：看 Agent 思考的最小窗口）。"""
    lines = ["## Agent Trace（取证记录）", ""]
    lines += [
        f"- 最终决策：{report.decision_status or '（旧协议单轮诊断）'}",
        (
            f"- 取证轮数：{report.acquire_rounds}，工具调用：{report.tool_calls_used}，"
            f"证据总数：{len(report.evidence)}"
        ),
    ]
    if not report.evidence:
        lines.append("")
        return lines
    lines += ["", "| 证据 | 类型 | 来源 | 位置 | 字符 |", "|---|---|---|---|---|"]
    for ev in report.evidence:
        loc = f"{ev.file_path}:{ev.line_start}-{ev.line_end}" if ev.file_path else "—"
        lines.append(
            f"| {ev.id} | {ev.kind} | {ev.source} | {loc} | {len(ev.content)} |"
        )
    lines.append("")
    return lines


def render_markdown(report: RepairReport) -> str:
    lines = [
        "# DevFix Repair Report",
        "",
        f"**结果：{_status_badge(report.final_status)}**",
        "",
        "## Run",
        "",
        f"- Run ID：{report.run_id}",
        f"- 生成时间：{report.generated_at}",
        f"- 仓库：{report.repository}",
        f"- Commit：{report.target_commit or '（未知）'}"
        + (f"（base: {report.base_commit}）" if report.base_commit else ""),
    ]
    if report.workspace:
        lines.append(f"- 隔离工作区：{report.workspace}")
    if report.stop_reason:
        lines.append(f"- 停止原因：{report.stop_reason}")
    lines.append("")

    lines += _failure_section(report.failure, report.triage)
    lines += _diagnosis_section(report.diagnosis)
    lines += _agent_trace_section(report)
    lines += _repair_section(report.attempts)
    lines += _verification_section(report)
    lines += _attempt_history_section(report.attempts)

    lines += [
        "## Result",
        "",
        f"{_status_badge(report.final_status)}",
        "",
        "> 修复是否成立由真实测试执行结果判定，而非模型自评（设计文档 2.4）。",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------- 落盘
def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text)


def save_report(
    state: dict[str, Any],
    output_root: str | Path = "runs",
    run_id: str | None = None,
    repo_path: Path | None = None,
) -> tuple[Path, RepairReport]:
    """把报告写入 runs/<runId>/report.md 与 run.json，返回 (目录, 报告数据)。"""
    rid = run_id or new_run_id()
    report = build_report(state, rid, repo_path=repo_path)

    out_dir = Path(output_root) / _slug(rid)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.md").write_text(render_markdown(report), encoding="utf-8")
    (out_dir / "run.json").write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    # Agent Trace（V0.2 §7）：运行入口设置了 trace 时落盘 jsonl
    from devfix.observability import current_trace

    if current_trace is not None:
        current_trace.dump(out_dir / "trace.jsonl")
    return out_dir, report
