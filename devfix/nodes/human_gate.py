"""Human-in-the-Loop 门控节点（Agent Engineering V0.2 §6）。

把 MANUAL_REVIEW 从"状态码"变成"协作点"：策略拒绝且尝试耗尽、或连续
语义无进展时，图在此暂停（LangGraph interrupt），把根因、证据、补丁与
拒绝原因交给人工裁决——approve（人工覆盖策略，应用补丁进验证）/
reject（终止）。

Headless 兼容（benchmark/CI）：hitl_enabled=False 时**不调用 interrupt**，
直接按自动拒绝处理——评测管道行为与 A2 之前完全一致，零改动。

协议细节：interrupt() 暂停后，图以 Command(resume=...) 恢复时本节点
从头重新执行，interrupt() 返回恢复值——因此本节点必须是幂等纯函数
（除 interrupt 调用外不产生副作用）。
"""

from __future__ import annotations

from pathlib import Path
from typing import TypedDict

from langgraph.graph import END
from langgraph.types import interrupt

from devfix.models import PolicyVerdict, RepairDecision
from devfix.tools import PatchTool, ToolError


class HumanGateState(TypedDict, total=False):
    diagnosis: dict          # Diagnosis（避免循环导入，按 duck-typing 取字段）
    patch: object            # Patch | None
    policy_verdict: PolicyVerdict | None
    gate_reason: str
    no_progress_streak: int
    attempt: int
    max_attempts: int
    workspace: str
    evidence: list
    hitl_enabled: bool
    human_decision: str
    stop_reason: str
    error: str


def _patch_preview(patch) -> str:
    """人工裁决用的补丁预览：已应用的给 canonical diff，
    被拒绝未应用的给 edit 块原文（否则人看不到要批准什么）。"""
    diff = (getattr(patch, "diff", "") or "").strip()
    if diff:
        return diff[:8000]
    blocks = []
    for e in getattr(patch, "edits", []) or []:
        blocks.append(
            f"--- {e.file} ---\n"
            f"<<<< search\n{e.search}\n====\n{e.replace}\n>>>>"
        )
    return "\n\n".join(blocks)[:8000]


def _build_payload(state: HumanGateState) -> dict:
    """给人看的决策材料：根因 + 证据摘要 + 补丁 + 拒绝原因。"""
    diagnosis = state.get("diagnosis")
    patch = state.get("patch")
    verdict = state.get("policy_verdict")
    reason = state.get("gate_reason")
    if not reason and verdict is not None:
        reason = "；".join(verdict.reasons)
    evidence_preview = [
        f"{e.id} {e.source}: {e.content[:100].replace(chr(10), ' ')}"
        for e in (state.get("evidence") or [])[-5:]
        if e.source.startswith("tool:")
    ]
    return {
        "gate_reason": reason or "策略拒绝",
        "root_cause": getattr(diagnosis, "root_cause", "") if diagnosis else "",
        "confidence": getattr(diagnosis, "confidence", 0.0) if diagnosis else 0.0,
        "patch_id": getattr(patch, "patch_id", "") if patch else "",
        "patch_diff": _patch_preview(patch) if patch else "",
        "patch_files": sorted({e.file for e in getattr(patch, "edits", []) or []}),
        "cannot_fix": bool(getattr(patch, "cannot_fix_reason", "")) if patch else False,
        "cannot_fix_reason": getattr(patch, "cannot_fix_reason", "") if patch else "",
        "evidence_preview": evidence_preview,
        "attempt": state.get("attempt", 0),
        "streak": state.get("no_progress_streak", 0),
    }


def human_gate_node(state: HumanGateState) -> dict:
    """人工门控：headless 自动拒绝；交互模式 interrupt 等待裁决。"""
    payload = _build_payload(state)

    if not state.get("hitl_enabled"):
        return {
            "human_decision": "auto_rejected",
            "stop_reason": (
                f"{payload['gate_reason']}"
                f"（headless 模式自动拒绝；交互模式下此处将暂停等待人工裁决）"
            ),
        }

    decision = interrupt(payload)
    action = decision.get("action", "reject") if isinstance(decision, dict) else str(decision)

    if action != "approve":
        return {
            "human_decision": "rejected",
            "stop_reason": f"人工拒绝：{payload['gate_reason']}",
        }

    # 人工批准 = 覆盖策略判断，把被拒补丁应用进工作区 → 进入验证。
    # 空补丁（cannot_fix）没有可应用的内容，视为拒绝。
    patch = state.get("patch")
    if patch is None or not getattr(patch, "edits", None):
        return {
            "human_decision": "rejected",
            "stop_reason": f"人工批准了空补丁，无内容可应用，按拒绝处理。原因：{payload['gate_reason']}",
        }
    workspace = state.get("workspace")
    if not workspace:
        return {"error": "human_gate 批准后缺少 workspace，无法应用补丁"}
    try:
        applied = PatchTool(Path(workspace)).apply(patch)
    except ToolError as e:
        return {
            "human_decision": "approved",
            "stop_reason": f"人工批准的补丁应用失败：{e}",
        }
    return {
        "human_decision": "approved",
        "patch": applied,
        "policy_verdict": PolicyVerdict(
            decision=RepairDecision.AUTO_REPAIR,
            reasons=[f"人工批准覆盖策略判断：{payload['gate_reason']}"],
        ),
    }


def _after_human_gate(state: HumanGateState) -> str:
    if state.get("error") or state.get("stop_reason"):
        return END
    return "verify"
