"""Diagnosis 节点：Context Acquisition Loop 的决策点（Agent Engineering V0.2 §1）。

可测试性设计：
- Diagnoser 是协议接口。真实实现 LLMDiagnoser 走 structured output 输出
  DiagnosisDecision（NEED_MORE_CONTEXT / DIAGNOSIS_READY / INSUFFICIENT_EVIDENCE）；
- 旧式 Fake 只实现 diagnose(bundle) -> Diagnosis——节点用 getattr 探测并
  走单轮兼容路径（包装为 READY 决策），离线测试无需全部重写；
- 预算（轮数/调用数/字符）在本节点确定性计算：耗尽即进入"受限最终决策"
  模式（禁用取证请求），模型若仍输出 NEED_MORE_CONTEXT 则确定性映射为
  INSUFFICIENT_EVIDENCE——预算耗尽意味着停止取证，不意味着证据充分。
"""

from __future__ import annotations

from typing import Protocol, TypedDict

from langchain_core.exceptions import OutputParserException
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from devfix.llm.prompts.diagnosis import SYSTEM_PROMPT
from devfix.models import (
    ContextRequest,
    Diagnosis,
    DiagnosisContext,
    DiagnosisDecision,
    Evidence,
    Hypothesis,
    budget_used,
)
from devfix.nodes.acquisition import (
    _TOOL_NAME_ALIAS,
    ACQUISITION_TOOL_SPECS,
    AVAILABLE_TOOLS,
    EVIDENCE_BUDGET_CHARS,
    MAX_ACQUIRE_ROUNDS,
    MAX_TOOL_CALLS,
)
from devfix.observability import record_llm


class Diagnoser(Protocol):
    """诊断能力抽象。

    新协议：decide() 支持多轮取证循环；旧实现只提供 diagnose()，
    由节点做兼容降级（单轮 READY）。
    """

    def diagnose(self, bundle: DiagnosisContext) -> Diagnosis: ...


class LLMDiagnoser:
    """基于 LLM 的真实诊断实现（决策式，含原生工具调用救援）。

    解析策略（实测驱动的工程决策）：DeepSeek 会把提示词中描述的取证工具
    直接当原生 function call 发出（绕过 DiagnosisDecision 结构化输出，
    触发 OutputParserException: "Unknown tool type: 'read_file'"）。
    与其对抗，不如两者都 bind 并都能解析：
    - DiagnosisDecision 调用 → 正常决策；
    - 取证工具的原生调用 → 转换为 ContextRequest（NEED_MORE_CONTEXT）；
    - 两者都没有（纯文本）→ 纠正重试一次，仍失败则抛 OutputParserException。
    """

    def __init__(self, model: BaseChatModel) -> None:
        # 取证工具用显式函数名的 OpenAI spec 绑定（传 pydantic 类会让
        # LangChain 以类名当工具名，模型回吐 "ReadFileArgs" 造成解析 miss）
        self._model = model.bind_tools([DiagnosisDecision, *ACQUISITION_TOOL_SPECS])

    def diagnose(self, bundle: DiagnosisContext) -> Diagnosis:
        """兼容旧协议：单轮诊断（等价于首轮即 READY）。"""
        decision = self.decide(bundle, [], [], allow_tools=False, rounds_left=0, tool_calls_left=0, round_no=1)
        if decision.status == "DIAGNOSIS_READY" and decision.diagnosis is not None:
            return decision.diagnosis
        insufficient = decision.diagnosis or Diagnosis()
        return insufficient.model_copy(update={
            "insufficient_evidence": True,
            "confidence": min(insufficient.confidence, 0.3),
        })

    def decide(
        self,
        bundle: DiagnosisContext,
        evidence: list[Evidence],
        hypotheses: list[Hypothesis],
        *,
        allow_tools: bool,
        rounds_left: int,
        tool_calls_left: int,
        round_no: int,
    ) -> DiagnosisDecision:
        messages = build_decision_messages(
            bundle, evidence, hypotheses,
            allow_tools=allow_tools, rounds_left=rounds_left,
            tool_calls_left=tool_calls_left, round_no=round_no,
        )
        last_error: Exception | None = None
        for _ in range(2):  # 纠正重试一次
            msg = self._model.invoke(messages)
            record_llm("diagnosis", msg, round_no=round_no, allow_tools=allow_tools)
            try:
                decision, native = _parse_response(msg)
            except Exception as e:  # noqa: BLE001 —— 解析异常走纠正重试
                last_error = e
                messages = _with_correction(messages, msg)
                continue
            if decision is None and not native:
                messages = _with_correction(messages, msg)
                continue
            if decision is None:
                return DiagnosisDecision(status="NEED_MORE_CONTEXT", context_requests=native)
            if native:
                # 模型"嘴上说 READY、手上发请求"→ 以取证为准；READY 却无请求时保持
                if decision.status == "DIAGNOSIS_READY":
                    return decision.model_copy(update={
                        "status": "NEED_MORE_CONTEXT",
                        "context_requests": [*(decision.context_requests or []), *native],
                    })
                return decision.model_copy(update={
                    "context_requests": [*(decision.context_requests or []), *native],
                })
            return decision
        raise OutputParserException(
            f"连续两次输出均无法解析为 DiagnosisDecision：{last_error}"
        )


_CORRECTION = (
    "你的上一条输出无法解析。注意：取证意图必须通过 DiagnosisDecision 的 "
    "context_requests 字段表达（tool 取值："
    f"{', '.join(AVAILABLE_TOOLS)}；args 按工具填写；必填 reason），"
    "请重新输出一次 DiagnosisDecision 工具调用。"
)


def _with_correction(messages: list, msg: AIMessage) -> list:
    """构造协议合法的纠正轮次。

    OpenAI 协议要求带 tool_calls 的 assistant 消息后面必须跟每条调用的
    ToolMessage 应答——直接接 HumanMessage 会 400（实测）。tool_call 缺 id
    等异常形态下无法完整应答，则剥离 tool_calls 重发。
    """
    tcs = msg.tool_calls or []
    if tcs and all(tc.get("id") for tc in tcs):
        replies = [
            ToolMessage(
                content="（该请求已由系统记录。请改为在 DiagnosisDecision 的 "
                        "context_requests 字段中表达取证意图。）",
                tool_call_id=tc["id"],
            )
            for tc in tcs
        ]
        return [*messages, msg, *replies, HumanMessage(content=_CORRECTION)]
    stripped = msg.model_copy(update={
        "tool_calls": [], "content": msg.content or "（上一条输出无法解析）",
    })
    return [*messages, stripped, HumanMessage(content=_CORRECTION)]


def _parse_response(msg: AIMessage) -> tuple[DiagnosisDecision | None, list[ContextRequest]]:
    """解析模型响应：DiagnosisDecision 调用 / 取证工具原生调用 / 纯文本。"""
    decision: DiagnosisDecision | None = None
    native: list[ContextRequest] = []
    for tc in getattr(msg, "tool_calls", None) or []:
        name = _TOOL_NAME_ALIAS.get(tc.get("name"), tc.get("name"))
        args = dict(tc.get("args") or {})
        if name == "DiagnosisDecision":
            decision = DiagnosisDecision.model_validate(args)
        elif name in AVAILABLE_TOOLS:
            safe_args = {k: v for k, v in args.items() if isinstance(v, (str, int))}
            native.append(ContextRequest(
                tool=str(name), args=safe_args,
                reason="（模型以原生工具调用形式发起，未附 reason）",
            ))
    return decision, native


# ------------------------------------------------------------------ 渲染
def format_failure(bundle: DiagnosisContext) -> str:
    f = bundle.failure
    parts = [f"故障类型：{bundle.triage.failure_type.value}（{bundle.triage.reason}）"]
    if f.key_error:
        parts.append(f"关键异常：{f.key_error}")
    if f.failed_tests:
        parts.append("失败测试：")
        for t in f.failed_tests:
            msg = f" — {t.error_type}: {t.message}" if t.message else ""
            parts.append(f"  - {t.display_name}{msg}")
    if f.compile_errors:
        parts.append("编译错误：")
        for ce in ctx_compile_errors(bundle):
            sym = f"，symbol: {ce.symbol}" if ce.symbol else ""
            parts.append(f"  - {ce.file_path}:{ce.line} {ce.message}{sym}")
    if f.stack_traces:
        parts.append("异常栈（首条）：")
        st = f.stack_traces[0]
        parts.append(f"  {st.exception_type}: {st.message or ''}")
        for fr in st.frames[:8]:
            parts.append(f"    at {fr.class_name}.{fr.method_name}({fr.location})")
    return "\n".join(parts)


def ctx_compile_errors(bundle: DiagnosisContext):
    return bundle.failure.compile_errors


def format_snippets(bundle: DiagnosisContext) -> str:
    blocks = []
    for s in bundle.snippets:
        header = f"===== {s.file_path}:{s.start_line}-{s.end_line}（来源：{s.source}）====="
        blocks.append(f"{header}\n{s.content}")
    return "\n\n".join(blocks) if blocks else "（未能收集到相关代码片段）"


def format_evidence_ledger(evidence: list[Evidence]) -> str:
    blocks = []
    for ev in evidence:
        loc = f"{ev.file_path}:{ev.line_start}-{ev.line_end}" if ev.file_path else ""
        header = f"[{ev.id}] {ev.kind}（来源：{ev.source}）{loc}"
        blocks.append(f"{header}\n{ev.content}")
    return "\n\n".join(blocks) if blocks else "（暂无证据）"


def format_hypotheses(hypotheses: list[Hypothesis]) -> str:
    if not hypotheses:
        return ""
    lines = [f"  - [{h.status}] {h.statement}（证据：{', '.join(h.supporting) or '无'}）" for h in hypotheses]
    return "【当前假设】\n" + "\n".join(lines)


def render_decision_message(
    bundle: DiagnosisContext,
    evidence: list[Evidence],
    hypotheses: list[Hypothesis],
    *,
    allow_tools: bool,
    rounds_left: int,
    tool_calls_left: int,
    round_no: int,
) -> str:
    parts = [
        f"以下是本次 CI 失败的证据（第 {round_no} 轮诊断），请给出结构化诊断决策。",
        f"\n【失败信息】\n{format_failure(bundle)}",
        (
            f"\n【证据账本】（{budget_used(evidence)}/{EVIDENCE_BUDGET_CHARS} 字符）\n"
            f"{format_evidence_ledger(evidence)}"
        ),
    ]
    hyp = format_hypotheses(hypotheses)
    if hyp:
        parts.append(f"\n{hyp}")
    if allow_tools:
        parts.append(
            f"\n【取证能力】\n"
            f"剩余取证轮数：{rounds_left}，剩余工具调用次数：{tool_calls_left}。\n"
            f"可用工具：{', '.join(AVAILABLE_TOOLS)}。\n"
            f"证据不够时输出 NEED_MORE_CONTEXT 并附 context_requests（本轮 ≤3 条，"
            f"每条带 reason 并尽量挂在当前假设下）。"
        )
    else:
        parts.append(
            "\n【取证能力】\n"
            "取证预算已耗尽：**不再接受任何取证请求**。请基于当前全部证据做"
            "最终决策：DIAGNOSIS_READY 或 INSUFFICIENT_EVIDENCE（说明 "
            "unresolved_questions）。不允许输出 NEED_MORE_CONTEXT。"
        )
    return "\n".join(parts)


def build_decision_messages(
    bundle: DiagnosisContext,
    evidence: list[Evidence],
    hypotheses: list[Hypothesis],
    *,
    allow_tools: bool,
    rounds_left: int,
    tool_calls_left: int,
    round_no: int,
) -> list[SystemMessage | HumanMessage]:
    return [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=render_decision_message(
            bundle, evidence, hypotheses,
            allow_tools=allow_tools, rounds_left=rounds_left,
            tool_calls_left=tool_calls_left, round_no=round_no,
        )),
    ]


# 兼容保留：旧单轮诊断的消息构造（baseline 等外部使用方）
def render_user_message(bundle: DiagnosisContext) -> str:
    diff = bundle.git_diff or "（无 git diff 提供）"
    return (
        f"以下是本次 CI 失败的证据，请给出结构化诊断。\n\n"
        f"【失败信息】\n{format_failure(bundle)}\n\n"
        f"【相关代码片段】\n{format_snippets(bundle)}\n\n"
        f"【本次提交 diff（base..HEAD）】\n{diff}"
    )


def build_messages(bundle: DiagnosisContext) -> list[SystemMessage | HumanMessage]:
    return [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=render_user_message(bundle)),
    ]


# ---------------------------------------------------------------------- 节点
class DiagnosisState(TypedDict, total=False):
    """本节点读取/写入的 state 字段（完整定义见 devfix.graph.state.RunState）。"""

    context_bundle: DiagnosisContext
    diagnosis: Diagnosis
    decision: DiagnosisDecision
    evidence: list[Evidence]
    hypotheses: list[Hypothesis]
    acquire_rounds: int
    tool_calls_used: int
    stop_reason: str
    error: str


def _initial_evidence(bundle: DiagnosisContext) -> list[Evidence]:
    """把初始确定性收集的上下文转换为证据账本（EV 编号统一从此起算）。"""
    evidence: list[Evidence] = []
    for s in bundle.snippets:
        evidence.append(Evidence(
            id=f"EV-{len(evidence) + 1:03d}", kind="source_range",
            source=f"initial:{s.source}", file_path=s.file_path,
            line_start=s.start_line, line_end=s.end_line, content=s.content,
        ))
    if bundle.git_diff:
        evidence.append(Evidence(
            id=f"EV-{len(evidence) + 1:03d}", kind="git_diff",
            source="initial:git_diff", content=bundle.git_diff,
        ))
    return evidence


def _final_insufficient(
    decision: DiagnosisDecision,
) -> Diagnosis:
    """最终决策仍 NEED_MORE_CONTEXT → 确定性映射为 INSUFFICIENT（不猜）。

    按"无因停止无效"原则（V0.2 §1 修订）：insufficient_evidence 不置位、
    不写 stop_reason、不压制置信度——说不出缺什么的停止默认转入修复
    （路由层按 unresolved_questions 判定）。
    """
    return (decision.diagnosis or Diagnosis()).model_copy(
        update={"insufficient_evidence": False}
    )


def diagnosis_node(state: DiagnosisState, diagnoser: Diagnoser) -> dict:
    """LangGraph 节点：Context Acquisition Loop 的决策点。

    预算在本节点确定性计算：轮数/调用数任一耗尽 → allow_tools=False，
    进入受限最终决策；模型仍输出 NEED_MORE_CONTEXT 时映射为
    INSUFFICIENT_EVIDENCE 并写 stop_reason（设计原则 1：预算耗尽 =
    停止取证，不 = 证据充分）。
    """
    bundle = state.get("context_bundle")
    if bundle is None:
        return {"error": "diagnosis 节点缺少 context_bundle"}

    evidence = list(state.get("evidence") or [])
    if not evidence:
        evidence = _initial_evidence(bundle)
    hypotheses = list(state.get("hypotheses") or [])
    rounds_used = state.get("acquire_rounds", 0)
    calls_used = state.get("tool_calls_used", 0)
    rounds_left = MAX_ACQUIRE_ROUNDS - rounds_used
    calls_left = MAX_TOOL_CALLS - calls_used
    allow_tools = rounds_left > 0 and calls_left > 0
    round_no = rounds_used + 1

    decider = getattr(diagnoser, "decide", None)
    try:
        if decider is not None:
            decision = decider(
                bundle, evidence, hypotheses,
                allow_tools=allow_tools, rounds_left=rounds_left,
                tool_calls_left=calls_left, round_no=round_no,
            )
        else:
            # 旧协议兼容：单轮诊断包装为 READY
            decision = DiagnosisDecision(
                status="DIAGNOSIS_READY", diagnosis=diagnoser.diagnose(bundle),
            )
    except Exception as e:  # noqa: BLE001 —— LLM 异常统一进 error 字段
        return {"error": f"diagnosis 失败：{type(e).__name__}: {e}"}

    update: dict = {
        "evidence": evidence,
        "hypotheses": decision.hypotheses or hypotheses,
    }

    if decision.status == "NEED_MORE_CONTEXT":
        if allow_tools:
            update["decision"] = decision
            update["context_requests"] = decision.context_requests
            return update
        # 受限最终决策仍要更多上下文 → 决策记录为 INSUFFICIENT 供审计，
        # 但按"无因停止无效"原则处理（不置 insufficient、不写 stop_reason）：
        # 路由层据 unresolved_questions 判定——空则转入修复
        diagnosis = _final_insufficient(decision)
        update.update({
            "decision": decision.model_copy(update={"status": "INSUFFICIENT_EVIDENCE"}),
            "diagnosis": diagnosis,
        })
        return update

    if decision.status == "INSUFFICIENT_EVIDENCE":
        if decision.unresolved_questions:
            # 有因停止：诚实承认不足（设计原则 1）。不压制置信度——
            # stop_reason 已写明原因，路由层直接 END。
            update.update({
                "decision": decision,
                "diagnosis": (decision.diagnosis or Diagnosis()).model_copy(
                    update={"insufficient_evidence": True}
                ),
                "stop_reason": (
                    "诊断自述证据不足（取证预算内无法确认）。未解决问题："
                    + "；".join(decision.unresolved_questions)
                ),
            })
            return update
        # 无因停止不被承认（V0.2 §1 修订）：说不出缺什么 → 证据未必不足 →
        # 默认采取行动。保留模型自身置信度（自己压到 0.49 会让 policy 以
        # 合成理由再次停止，绕回原点）；decision 原状态保留供审计，
        # policy 仍把守文件范围/规模等其余红线。
        update["diagnosis"] = (decision.diagnosis or Diagnosis()).model_copy(
            update={"insufficient_evidence": False}
        )
        update["decision"] = decision
        return update

    # DIAGNOSIS_READY
    diagnosis = decision.diagnosis or Diagnosis()
    update.update({
        "decision": decision.model_copy(update={"diagnosis": diagnosis}),
        "diagnosis": diagnosis,
        "context_requests": [],
    })
    return update
