"""Repair 节点：生成候选补丁 → 策略校验 → 应用/回滚。

流程（对应系统设计 5.9~5.11 与 10.6）：

    Diagnosis ──→ Patcher 生成 Patch
                     │
        RepairPolicy.evaluate（应用前：证据/文件范围/规模）
                     │ AUTO_REPAIR
              PatchTool.apply（写入隔离 worktree）
                     │
        RepairPolicy.evaluate_applied（应用后：diff 规模/测试保护）
                     │ 不通过 → git restore 回滚
                  完成
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from devfix.llm.prompts.repair import SYSTEM_PROMPT
from devfix.llm.structured import invoke_structured
from devfix.models import (
    Diagnosis,
    DiagnosisContext,
    Evidence,
    Patch,
    PolicyVerdict,
    RepairDecision,
)
from devfix.nodes.diagnosis import format_evidence_ledger, format_snippets
from devfix.observability import record_llm
from devfix.repair import RepairPolicy
from devfix.tools import GitTool, PatchTool, ToolError

# 补丁应用失败（多数是 search 片段与真实文件不符）后，把工具错误喂回模型重试的次数
MAX_APPLY_RETRIES = 2


class Patcher(Protocol):
    """补丁生成能力抽象（Fake 与 LLM 实现可互换）。

    evidence：Agent 取证所得的证据账本（V0.2 §3）——补丁的 search 片段
    可以取自这里，而不只是初始注入的 snippets。
    """

    def propose(
        self,
        diagnosis: Diagnosis,
        bundle: DiagnosisContext,
        attempt: int = 1,
        previous_attempts: list[str] | None = None,
        feedback: str | None = None,
        evidence: list[Evidence] | None = None,
    ) -> Patch: ...


class LLMPatcher:
    """基于 LLM 的补丁生成实现（bind_tools 统一路径，保留 usage 供 trace）。"""

    def __init__(self, model: BaseChatModel) -> None:
        self._model = model

    def propose(
        self,
        diagnosis: Diagnosis,
        bundle: DiagnosisContext,
        attempt: int = 1,
        previous_attempts: list[str] | None = None,
        feedback: str | None = None,
        evidence: list[Evidence] | None = None,
    ) -> Patch:
        patch, msg = invoke_structured(
            self._model, Patch,
            build_messages(diagnosis, bundle, attempt, previous_attempts, feedback, evidence),
        )
        record_llm("repair", msg, attempt=attempt)
        return patch


def render_repair_message(
    diagnosis: Diagnosis,
    bundle: DiagnosisContext,
    attempt: int = 1,
    previous_attempts: list[str] | None = None,
    feedback: str | None = None,
    evidence: list[Evidence] | None = None,
) -> str:
    """组装修复请求的用户消息（独立函数，便于测试断言内容）。"""
    diagnosis_block = (
        f"【诊断结论】\n"
        f"根因：{diagnosis.root_cause}\n"
        f"摘要：{diagnosis.summary}\n"
        f"置信度：{diagnosis.confidence}"
        f"{'（证据不足）' if diagnosis.insufficient_evidence else ''}\n"
        f"相关文件：{', '.join(diagnosis.related_files) or '无'}\n"
        f"相关行：{', '.join(diagnosis.related_lines) or '无'}"
    )
    failure_block = (
        f"【失败测试/错误】\n"
        f"{chr(10).join('  - ' + t.display_name for t in bundle.failure.failed_tests) or '无'}"
    )
    attempts_block = ""
    if previous_attempts:
        attempts_block = (
            f"\n\n【此前尝试（第 {attempt} 次修复，避免重复同样的改法）】\n"
            + "\n".join(f"  {i}. {a}" for i, a in enumerate(previous_attempts, 1))
        )
    feedback_block = ""
    if feedback:
        feedback_block = (
            f"\n\n【上一版补丁未能应用，请据此修正】\n{feedback}"
        )
    # 取证账本（V0.2 §3）：诊断阶段 Agent 自主获取的源码/搜索结果。
    # 只渲染 tool: 来源——initial 来源与 bundle.snippets 内容重复。
    # 实测教训：不渲染账本时，模型在诊断阶段读到源码、repair 阶段仍
    # 声称"上下文未提供"而输出空补丁。
    acquired = [e for e in (evidence or []) if e.source.startswith("tool:")]
    evidence_block = ""
    if acquired:
        evidence_block = (
            "\n\n【取证证据（Agent 自主获取；search 同样必须取自这里的原文）】\n"
            + format_evidence_ledger(acquired)
        )
    return (
        f"请针对以下诊断生成最小修复补丁。\n\n"
        f"{diagnosis_block}\n\n"
        f"{failure_block}\n\n"
        f"【相关代码片段（search 必须取自这里的原文）】\n{format_snippets(bundle)}"
        f"{evidence_block}"
        f"{attempts_block}"
        f"{feedback_block}"
    )


def build_messages(
    diagnosis: Diagnosis,
    bundle: DiagnosisContext,
    attempt: int = 1,
    previous_attempts: list[str] | None = None,
    feedback: str | None = None,
    evidence: list[Evidence] | None = None,
) -> list[SystemMessage | HumanMessage]:
    return [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(
            content=render_repair_message(
                diagnosis, bundle, attempt, previous_attempts, feedback, evidence
            )
        ),
    ]


# ---------------------------------------------------------------------- 节点
class RepairState(TypedDict, total=False):
    diagnosis: Diagnosis
    context_bundle: DiagnosisContext
    workspace: str
    attempt: int                 # 当前尝试轮次（本节点自增）
    attempt_history: list[str]   # 历史尝试摘要（供 LLM 避免重复改法）
    evidence: list[Evidence]     # 证据账本（取证所得，渲染给 Patcher）
    no_progress_streak: int      # 连续"语义无进展"次数（Agent Engineering V0.2 §5）
    gate_reason: str             # 人工门控触发原因（V0.2 §6）
    patch: Patch
    policy_verdict: PolicyVerdict
    stop_reason: str
    error: str


def _no_progress(verdict: PolicyVerdict, patch: Patch) -> bool:
    """语义无进展（V0.2 §5）：补丁未被应用——环境没有发生任何变化，
    没有产生新证据。区分于"已应用但验证失败"（那是值得反思的新反馈）。"""
    return verdict.decision is not RepairDecision.AUTO_REPAIR


def repair_node(
    state: RepairState,
    patcher: Patcher,
    policy: RepairPolicy | None = None,
) -> dict:
    """生成并（在策略允许时）应用补丁。

    失败一律写入 state["error"]，由编排层决定后续处理（设计文档 19 节）。
    本节点负责自增尝试轮次，并把历史尝试摘要传给 Patcher——
    Reflection Loop 的"不要重复同样的改法"依赖于此。
    """
    policy = policy or RepairPolicy()
    diagnosis = state.get("diagnosis")
    bundle = state.get("context_bundle")
    workspace = state.get("workspace")
    if diagnosis is None or bundle is None:
        return {"error": "repair 节点缺少 diagnosis 或 context_bundle"}
    if not workspace:
        return {"error": "repair 节点缺少 workspace（隔离工作区尚未创建）"}

    attempt = state.get("attempt", 0) + 1
    previous_attempts = list(state.get("attempt_history", []))
    evidence = list(state.get("evidence") or [])
    streak = state.get("no_progress_streak", 0)

    # 重试前回滚上一轮补丁：每次尝试都是针对**原始代码**的独立候选修复
    # （设计文档 5.10 Candidate Patch 语义），而不是在前一轮改动上叠加。
    # 若不回滚，模型基于原始代码给出的 search 片段会匹配失败，
    # 且 diff 规模会累计污染后续的规模校验。
    previous_patch = state.get("patch")
    if attempt > 1 and previous_patch is not None and previous_patch.diff:
        try:
            GitTool(Path(workspace)).restore_working_tree()
        except ToolError as e:
            return {"attempt": attempt, "error": f"回滚上一轮补丁失败：{e}"}

    # 1. 生成候选补丁 → 策略校验 → 应用。
    #    search 匹配失败等"工具级错误"会把**文件真实内容**喂回模型立即重试
    #    （技术选型 2.6 的自纠回路），避免把外层尝试次数浪费在格式问题上。
    feedback: str | None = None
    patch: Patch | None = None
    applied: Patch | None = None
    for _inner in range(MAX_APPLY_RETRIES + 1):
        try:
            patch = patcher.propose(
                diagnosis, bundle, attempt=attempt,
                previous_attempts=previous_attempts, feedback=feedback,
                evidence=evidence,
            )
        except Exception as e:  # noqa: BLE001 —— LLM 异常统一进 error 字段
            return {
                "attempt": attempt,
                "error": f"补丁生成失败：{type(e).__name__}: {e}",
            }

        # 应用前策略校验（不通过则不写盘，交给编排层分流）
        verdict = policy.evaluate(diagnosis, patch)
        if verdict.decision is not RepairDecision.AUTO_REPAIR:
            summary = f"第{attempt}次：{patch.reason or '（无说明）'}"
            streak += 1
            update = {
                "attempt": attempt,
                "patch": patch,
                "policy_verdict": verdict,
                "no_progress_streak": streak,
                "attempt_history": [
                    *previous_attempts, summary, f"→ 策略拒绝（{verdict.decision.value}）"
                ],
            }
            # 证据不足 / 不支持的类型属于终态：主动停止（设计文档 2.5）
            if verdict.decision in (
                RepairDecision.INSUFFICIENT_EVIDENCE,
                RepairDecision.UNSUPPORTED,
            ):
                update["stop_reason"] = "；".join(verdict.reasons) or verdict.decision.value
            elif streak >= 2:
                # 连续语义无进展（V0.2 §5）：环境未变化、无新证据——
                # 交给人工门控（headless 自动拒绝，交互模式 interrupt）
                update["gate_reason"] = (
                    f"连续 {streak} 次语义无进展（补丁为空或被策略拒绝，"
                    f"环境未发生任何变化）。最近一次原因："
                    + "；".join(verdict.reasons)[:300]
                )
            return update

        try:
            applied = PatchTool(Path(workspace)).apply(patch)
            break
        except ToolError as e:
            feedback = str(e)  # 把工具错误（含文件真实内容）回给模型
            applied = None

    summary = f"第{attempt}次：{patch.reason or '（无说明）'}"
    history = [*previous_attempts, summary]

    if applied is None:
        streak += 1
        update = {
            "attempt": attempt,
            "patch": patch,
            "no_progress_streak": streak,
            "policy_verdict": PolicyVerdict(
                decision=RepairDecision.MANUAL_REVIEW,
                reasons=[f"补丁应用失败（已重试 {MAX_APPLY_RETRIES} 次）：{feedback}"],
            ),
            "attempt_history": [*history, "→ 应用失败"],
        }
        if streak >= 2:
            update["gate_reason"] = (
                f"连续 {streak} 次语义无进展（补丁反复无法应用，环境未发生变化）。"
                f"最后错误：{str(feedback)[:300]}"
            )
        return update

    # 4. 应用后复核（diff 规模 / 测试保护），不通过则回滚
    post = policy.evaluate_applied(applied)
    if post.decision is not RepairDecision.AUTO_REPAIR:
        try:
            GitTool(Path(workspace)).restore_working_tree()
        except ToolError as e:
            return {"error": f"策略拒绝补丁后回滚失败：{e}"}
        streak += 1
        return {
            "attempt": attempt,
            "patch": patch,
            "no_progress_streak": streak,
            "policy_verdict": post,
            "attempt_history": [*history, f"→ {post.decision.value}"],
        }

    return {
        "attempt": attempt,
        "patch": applied,
        "no_progress_streak": 0,  # 补丁已应用：环境发生变化，进展归零
        "policy_verdict": verdict,
        "attempt_history": history,
    }
