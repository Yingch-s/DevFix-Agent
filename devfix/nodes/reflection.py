"""Reflection 节点：验证失败后提取新证据、修订诊断、决定下一步。

对应系统设计 5.13 与 8 节状态机（REFLECTING → DIAGNOSING → REPAIRING）。

实现取舍：反思阶段直接修订 Diagnosis（updated_root_cause 合入），
不再重跑完整的诊断流程——诊断所需的代码上下文并未变化，重跑只是重复消耗；
若未来出现"失败原因与代码上下文强相关"的场景，再改为真正的回边到 diagnose 节点。
"""

from __future__ import annotations

from typing import Protocol, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from devfix.llm.prompts.reflection import SYSTEM_PROMPT
from devfix.llm.structured import invoke_structured
from devfix.models import (
    Diagnosis,
    Evidence,
    Hypothesis,
    NextAction,
    Patch,
    PolicyVerdict,
    ReflectionResult,
    RepairAttempt,
    VerificationResult,
)
from devfix.observability import record_llm


class Reflector(Protocol):
    """反思能力抽象。

    两种失败来源：
    - 验证失败（verification 非空）：补丁落地了但没修好
    - 策略拒绝（verdict 非空、verification 为空）：补丁根本没被允许落地
      —— 必须把拒绝原因完整告诉模型，否则它只会重复同样的违规改法

    V0.2 §3/§5：反思是 Working Memory 的更新者——输入带假设状态与
    证据账本，输出可更新假设、可发起取证请求。
    """

    def reflect(
        self,
        diagnosis: Diagnosis,
        patch: Patch,
        verification: VerificationResult | None = None,
        previous_patches: list[Patch] | None = None,
        verdict: PolicyVerdict | None = None,
        hypotheses: list[Hypothesis] | None = None,
        evidence: list[Evidence] | None = None,
    ) -> ReflectionResult: ...


class LLMReflector:
    """基于 LLM 的反思实现（bind_tools 统一路径，保留 usage 供 trace）。"""

    def __init__(self, model: BaseChatModel) -> None:
        self._model = model

    def reflect(
        self,
        diagnosis: Diagnosis,
        patch: Patch,
        verification: VerificationResult | None = None,
        previous_patches: list[Patch] | None = None,
        verdict: PolicyVerdict | None = None,
        hypotheses: list[Hypothesis] | None = None,
        evidence: list[Evidence] | None = None,
    ) -> ReflectionResult:
        result, msg = invoke_structured(
            self._model, ReflectionResult,
            build_messages(diagnosis, patch, verification, previous_patches, verdict, hypotheses, evidence),
        )
        record_llm("reflect", msg)
        return result


def _verification_block(verification: VerificationResult) -> str:
    lines = [
        f"状态：{verification.status.value}（层级 {verification.level.value}）",
        f"命令：{verification.command}",
        f"退出码：{verification.exit_code}",
    ]
    if verification.new_errors:
        lines.append("新错误：" + "；".join(verification.new_errors[:5]))
    if verification.failed_tests:
        lines.append(
            "失败用例："
            + "；".join(
                f"{t.display_name} - {t.error_type}: {t.message}" for t in verification.failed_tests
            )
        )
    if verification.log_tail:
        lines.append(f"验证日志尾部：\n{verification.log_tail[-4000:]}")
    return "\n".join(lines)


def _rejection_block(verdict: PolicyVerdict, patch: Patch) -> str:
    files = "、".join(e.file for e in patch.edits) or "（无文件）"
    return (
        f"补丁**没有通过 Repair Policy 检查，未被保留在工作区**"
        f"（若已写入则已回滚，工作区保持原始状态）。\n"
        f"决策：{verdict.decision.value}\n"
        f"拒绝原因：\n" + "\n".join(f"  - {r}" for r in verdict.reasons) + "\n"
        f"补丁涉及文件：{files}\n"
        f"提示：下一轮补丁必须在这些约束内给出——尤其注意测试代码禁止修改，"
        f"请改为修改生产代码使其满足测试所表达的业务契约。"
    )


def _working_memory_block(
    hypotheses: list[Hypothesis] | None, evidence: list[Evidence] | None
) -> str:
    """Working Memory 注入（V0.2 §3）：已调查什么、哪些假设还活着。"""
    parts: list[str] = []
    if hypotheses:
        lines = [
            f"  - [{h.status}] {h.statement}（证据：{', '.join(h.supporting) or '无'}）"
            for h in hypotheses
        ]
        parts.append("【当前假设状态】\n" + "\n".join(lines))
    acquired = [e for e in (evidence or []) if e.source.startswith("tool:")]
    if acquired:
        lines = [
            f"  - {e.id} {e.source}: {e.content[:120].replace(chr(10), ' ')}…"
            for e in acquired[-6:]
        ]
        parts.append(
            "【已取证内容（Agent 此前主动获取的证据，完整内容见证据账本）】\n"
            + "\n".join(lines)
        )
    return "\n\n".join(parts)


def render_reflection_message(
    diagnosis: Diagnosis,
    patch: Patch,
    verification: VerificationResult | None = None,
    previous_patches: list[Patch] | None = None,
    verdict: PolicyVerdict | None = None,
    hypotheses: list[Hypothesis] | None = None,
    evidence: list[Evidence] | None = None,
) -> str:
    """组装反思请求的用户消息（独立函数，便于测试断言内容）。"""
    if verification is not None:
        situation = (
            "上一轮修复补丁已应用并真实执行验证，但**验证失败**。\n\n"
            f"【验证结果】\n{_verification_block(verification)}"
        )
    elif verdict is not None:
        situation = _rejection_block(verdict, patch)
    else:
        situation = "上一轮修复未能推进（缺少验证与策略信息）。"

    history_block = ""
    if previous_patches:
        history_block = (
            "\n\n【此前的补丁（不要重复同样的改法）】\n"
            + "\n".join(
                f"  {i}. {p.reason or '（无说明）'}："
                + "；".join(f"{e.file} 的编辑" for e in p.edits)
                for i, p in enumerate(previous_patches, 1)
            )
        )
    memory_block = _working_memory_block(hypotheses, evidence)
    if memory_block:
        memory_block = f"\n\n{memory_block}"

    return (
        f"上一轮修复未能通过，请分析原因并给出下一步。\n\n"
        f"【原诊断根因】\n{diagnosis.root_cause}\n"
        f"（诊断置信度 {diagnosis.confidence}，证据充分性："
        f"{'不足' if diagnosis.insufficient_evidence else '充分'}）\n\n"
        f"【上轮补丁】\n修改原因：{patch.reason or '（无）'}\n"
        f"预期效果：{patch.expected_effect or '（无）'}\n"
        f"diff：\n{patch.diff or '（未被应用）'}\n\n"
        f"{situation}"
        f"{history_block}"
        f"{memory_block}"
    )


def build_messages(
    diagnosis: Diagnosis,
    patch: Patch,
    verification: VerificationResult | None = None,
    previous_patches: list[Patch] | None = None,
    verdict: PolicyVerdict | None = None,
    hypotheses: list[Hypothesis] | None = None,
    evidence: list[Evidence] | None = None,
) -> list[SystemMessage | HumanMessage]:
    return [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(
            content=render_reflection_message(
                diagnosis, patch, verification, previous_patches, verdict,
                hypotheses, evidence,
            )
        ),
    ]


# ---------------------------------------------------------------------- 节点
class ReflectionState(TypedDict, total=False):
    diagnosis: Diagnosis
    patch: Patch
    policy_verdict: PolicyVerdict
    verification: VerificationResult
    previous_patches: list[Patch]
    attempt: int
    attempts: list[RepairAttempt]
    attempt_history: list[str]
    reflection: ReflectionResult
    hypotheses: list[Hypothesis]
    evidence: list[Evidence]
    context_requests: list  # 反思发起的取证请求（acquire_reflect 节点消费）
    stop_reason: str
    error: str


def _merge_hypotheses(
    current: list[Hypothesis], updates: list[Hypothesis]
) -> list[Hypothesis]:
    """按陈述合并假设更新：同述改状态，新述追加（V0.2 §3 Belief Update）。"""
    merged = list(current)
    for u in updates:
        for i, h in enumerate(merged):
            if h.statement == u.statement:
                merged[i] = u
                break
        else:
            merged.append(u)
    return merged


def reflection_node(state: ReflectionState, reflector: Reflector) -> dict:
    """反思节点：产出 ReflectionResult、必要时修订诊断、记录本次失败尝试。

    V0.2 §3/§5：输入带 Working Memory（假设/证据），输出可更新假设、
    可发起取证请求（由 acquire_reflect 节点执行后供下一轮修复使用）。
    LLM 异常进 error 字段，由编排层终止（设计文档 19 节）。
    """
    diagnosis = state.get("diagnosis")
    patch = state.get("patch")
    verification = state.get("verification")
    verdict = state.get("policy_verdict")
    if diagnosis is None or patch is None:
        return {"error": "reflection 节点缺少 diagnosis 或 patch"}
    if verification is None and verdict is None:
        return {"error": "reflection 节点既无验证结果也无策略结论，无法反思"}

    hypotheses = list(state.get("hypotheses") or [])
    evidence = list(state.get("evidence") or [])
    try:
        reflection = reflector.reflect(
            diagnosis,
            patch,
            verification,
            state.get("previous_patches"),
            verdict if verification is None else None,
            hypotheses=hypotheses,
            evidence=evidence,
        )
    except Exception as e:  # noqa: BLE001 —— LLM 异常统一进 error 字段
        return {"error": f"反思失败：{type(e).__name__}: {e}"}

    update: dict = {"reflection": reflection}
    if reflection.should_update_diagnosis and reflection.updated_root_cause:
        update["diagnosis"] = diagnosis.model_copy(
            update={"root_cause": reflection.updated_root_cause}
        )
    if reflection.hypothesis_updates:
        update["hypotheses"] = _merge_hypotheses(hypotheses, reflection.hypothesis_updates)
    # 取证请求交给 acquire_reflect 节点执行（路由层检查预算后决定）
    if reflection.context_requests:
        update["context_requests"] = list(reflection.context_requests)
    # 停止条件：反思判断当前约束下无解
    if reflection.next_action is NextAction.STOP:
        update["stop_reason"] = reflection.failure_reason or "反思判定无法在当前约束内修复"

    # 补全本轮失败尝试的反思信息（验证节点已追加过该条记录）
    attempt_no = state.get("attempt", 1)
    attempts = list(state.get("attempts", []))
    record = RepairAttempt(
        attempt=attempt_no,
        patch=patch,
        verdict=state.get("policy_verdict"),
        verification=verification,
        verifications=list(state.get("verification_results", [])),
        reflection=reflection,
    )
    if attempts and attempts[-1].attempt == attempt_no:
        attempts[-1] = record
    else:
        attempts.append(record)
    update["attempts"] = attempts
    update["attempt_history"] = [
        *state.get("attempt_history", []),
        f"第{attempt_no}次验证失败：{reflection.failure_reason or '（未说明）'}",
    ]
    return update
