"""StateGraph 构建（对应系统设计第 6 节 Agent Loop 与第 8 节状态机）。

完整闭环：

    triage ─→ collect_context ─→ diagnosis ─→ prepare_workspace
                                                   │
                                                   ▼
                              ┌──────────────→  repair  ←──────────┐
                              │                 │      │           │
                              │      策略拒绝 ───┘      └─── 允许    │
                              │          │                  │       │
                              │          │                  ▼       │
                              │          │               verify     │
                              │          │                  │       │
                              │          │      ┌───────────┴────┐  │
                              │          │    PASS            FAIL  │
                              │          │      │               │   │
                              │          │     END              │   │
                              │          ▼                     ▼   │
                              │       reflect ←────────────────────┘
                              │          │
                              │   STOP / 达上限
                              │          │
                              │         END
                              └──（REPAIR_AGAIN）

图只负责流转；每个节点的业务逻辑与依赖（diagnoser/patcher/reflector/verifier）
全部从外部注入，保证可独立单测与替换。
"""

from __future__ import annotations

from functools import partial
from pathlib import Path

from langgraph.graph import END, START, StateGraph

from devfix.graph.state import RunState
from devfix.models import NextAction, RepairDecision
from devfix.nodes.acquisition import acquire_node
from devfix.nodes.context_collector import collect_context
from devfix.nodes.diagnosis import Diagnoser, diagnosis_node
from devfix.nodes.human_gate import _after_human_gate, human_gate_node
from devfix.nodes.reflection import Reflector, reflection_node
from devfix.nodes.repair import Patcher, repair_node
from devfix.nodes.verification import Verifier, verification_node
from devfix.nodes.workspace import prepare_workspace_node
from devfix.parsing import parse_maven_log
from devfix.repair import RepairPolicy

DEFAULT_MAX_ATTEMPTS = 3


def triage_node(state: RunState) -> dict:
    """确定性 Triage：日志 → FailureContext + TriageResult（Phase 2 能力复用）。"""
    failure, triage = parse_maven_log(state["log_text"])
    return {"failure": failure, "triage": triage}


def context_node(state: RunState) -> dict:
    """确定性上下文收集：失败证据 → 代码片段 + git diff。"""
    try:
        bundle = collect_context(
            failure=state["failure"],
            triage=state["triage"],
            repo=Path(state["repo"]),
            base=state.get("base"),
            target=state.get("diff_target"),
        )
    except Exception as e:  # noqa: BLE001 —— 工具异常统一进 error 字段
        return {"error": f"上下文收集失败：{type(e).__name__}: {e}"}
    return {"context_bundle": bundle}


def _after_diagnose(state: RunState) -> str:
    """诊断决策路由（Agent Engineering V0.2 §1）。

    - DIAGNOSIS_READY        → 进入修复流程（workspace → repair）
    - INSUFFICIENT_EVIDENCE  → 诚实停止，**但必须说明缺什么**：
                               unresolved_questions 为空的"无因停止"不被承认
                               （说不出缺什么 → 证据未必不足 → 默认采取行动，
                               修复的成本低于无据放弃；policy 仍会把关置信度）
    - NEED_MORE_CONTEXT      → 执行取证（轮数/调用预算由 diagnosis 节点
      确定性控制：预算耗尽时节点已强制走最终决策，不会到达这里）
    - 无 decision（旧协议 Fake）→ 原有单轮流程
    """
    if state.get("error"):
        return END
    decision = state.get("decision")
    if decision is None:
        return "prepare_workspace"
    if decision.status == "NEED_MORE_CONTEXT":
        return "acquire"
    if decision.status == "INSUFFICIENT_EVIDENCE":
        return END if decision.unresolved_questions else "prepare_workspace"
    return "prepare_workspace"


def invoke_config(max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> dict:
    """graph.invoke 的 config：按尝试数与取证预算设置 recursion_limit。

    LangGraph 默认 25：启动 3 步 + 每轮尝试最多 4 步（repair/verify/reflect/
    acquire_reflect）+ 诊断取证循环 2×3 步，--max-attempts 较大时会撞限抛
    GraphRecursionError 而非优雅停止。
    """
    from devfix.nodes.acquisition import MAX_ACQUIRE_ROUNDS

    return {
        "recursion_limit": max(
            25, 8 + 4 * max_attempts + 2 * MAX_ACQUIRE_ROUNDS
        ),
    }


def _after_repair(state: RunState) -> str:
    """补丁后路由（V0.2 §5/§6）。

    策略拒绝时**不跑验证**（什么都没改，验证只会产生误导性证据）：

    - MANUAL_REVIEW  → 尝试耗尽或连续语义无进展 → 人工门控；
                       否则进入反思（把拒绝原因告诉模型，给改正机会）
    - 其他终态决策    → 直接结束（证据不足/不支持，设计文档 2.5 主动停止）
    - AUTO_REPAIR    → 正常进入验证
    """
    if state.get("error") or state.get("stop_reason"):
        return END
    verdict = state.get("policy_verdict")
    if verdict is not None and verdict.decision is not RepairDecision.AUTO_REPAIR:
        if verdict.decision is RepairDecision.MANUAL_REVIEW:
            exhausted = state.get("attempt", 1) >= state.get("max_attempts", DEFAULT_MAX_ATTEMPTS)
            if exhausted or state.get("no_progress_streak", 0) >= 2:
                return "human_gate"
            return "reflect"
        return END
    return "verify"


def _after_verify(state: RunState) -> str:
    """验证后路由：通过 / 出错 / 达上限 → 结束，否则进入反思。"""
    if state.get("error") or state.get("stop_reason"):
        return END
    verification = state.get("verification")
    if verification is not None and verification.passed:
        return END
    if state.get("attempt", 1) >= state.get("max_attempts", DEFAULT_MAX_ATTEMPTS):
        return END
    return "reflect"


def _after_reflect(state: RunState) -> str:
    """反思后路由（V0.2 §3/§5）：

    - 无解 / 达上限 / 出错 → 结束
    - 反思发起了取证请求且预算允许 → acquire_reflect（执行后进修复）
    - 否则直接再次修复
    """
    if state.get("error") or state.get("stop_reason"):
        return END
    if state.get("attempt", 1) >= state.get("max_attempts", DEFAULT_MAX_ATTEMPTS):
        return END
    reflection = state.get("reflection")
    if reflection is not None and reflection.next_action is NextAction.STOP:
        return END
    from devfix.nodes.acquisition import MAX_ACQUIRE_ROUNDS, MAX_TOOL_CALLS

    if (
        state.get("context_requests")
        and state.get("tool_calls_used", 0) < MAX_TOOL_CALLS
        and state.get("acquire_rounds", 0) < MAX_ACQUIRE_ROUNDS
    ):
        return "acquire_reflect"
    return "repair"


def build_repair_graph(
    diagnoser: Diagnoser,
    patcher: Patcher,
    reflector: Reflector,
    verifier: Verifier,
    policy: RepairPolicy | None = None,
    checkpointer=None,
):
    """组装完整 Repair Loop 图。

    Args:
        diagnoser: 诊断能力（LLMDiagnoser 或 Fake）
        patcher:   补丁生成能力（LLMPatcher 或 Fake）
        reflector: 反思能力（LLMReflector 或 Fake）
        verifier:  验证能力（LayeredVerifier 或 Fake）
        policy:    Repair Policy（默认使用标准安全策略）
        checkpointer: LangGraph checkpointer——HITL interrupt 的暂停/恢复
            依赖它保存图状态；headless（benchmark/CI）不传即无中断。
            传入时调用方须在 invoke config 里提供 thread_id。
    """
    g = StateGraph(RunState)
    g.add_node("triage", triage_node)
    g.add_node("collect_context", context_node)
    g.add_node("diagnosis", partial(diagnosis_node, diagnoser=diagnoser))
    g.add_node("acquire", acquire_node)
    g.add_node("acquire_reflect", acquire_node)  # 反思取证：同一执行器，回边到 repair
    g.add_node("prepare_workspace", prepare_workspace_node)
    g.add_node("repair", partial(repair_node, patcher=patcher, policy=policy))
    g.add_node("verify", partial(verification_node, verifier=verifier))
    g.add_node("reflect", partial(reflection_node, reflector=reflector))
    g.add_node("human_gate", human_gate_node)

    g.add_edge(START, "triage")
    g.add_edge("triage", "collect_context")
    g.add_edge("collect_context", "diagnosis")
    g.add_conditional_edges(
        "diagnosis", _after_diagnose,
        {"acquire": "acquire", "prepare_workspace": "prepare_workspace", END: END},
    )
    g.add_edge("acquire", "diagnosis")
    g.add_edge("prepare_workspace", "repair")
    g.add_conditional_edges(
        "repair", _after_repair,
        {"verify": "verify", "reflect": "reflect", "human_gate": "human_gate", END: END},
    )
    g.add_conditional_edges("verify", _after_verify, {"reflect": "reflect", END: END})
    g.add_conditional_edges(
        "reflect", _after_reflect,
        {"acquire_reflect": "acquire_reflect", "repair": "repair", END: END},
    )
    g.add_edge("acquire_reflect", "repair")
    g.add_conditional_edges("human_gate", _after_human_gate, {"verify": "verify", END: END})
    return g.compile(checkpointer=checkpointer)


def build_diagnosis_graph(diagnoser: Diagnoser):
    """仅诊断的子图（含取证循环，供快速诊断/调试使用）。"""
    g = StateGraph(RunState)
    g.add_node("triage", triage_node)
    g.add_node("collect_context", context_node)
    g.add_node("diagnosis", partial(diagnosis_node, diagnoser=diagnoser))
    g.add_node("acquire", acquire_node)
    g.add_edge(START, "triage")
    g.add_edge("triage", "collect_context")
    g.add_edge("collect_context", "diagnosis")
    g.add_conditional_edges(
        "diagnosis", _after_diagnose,
        # 旧协议 Fake（无 decision）路由到 prepare_workspace——诊断子图里
        # 没有该节点，映射为 END
        {"acquire": "acquire", "prepare_workspace": END, END: END},
    )
    g.add_edge("acquire", "diagnosis")
    return g.compile()
