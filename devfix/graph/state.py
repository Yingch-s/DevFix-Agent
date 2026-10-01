"""Repair Run 的图状态（对应系统设计 9.1~9.6 数据模型的运行时表示）。"""

from __future__ import annotations

from typing import TypedDict

from devfix.models import (
    ContextRequest,
    Diagnosis,
    DiagnosisContext,
    DiagnosisDecision,
    Evidence,
    FailureContext,
    Hypothesis,
    Patch,
    PolicyVerdict,
    ReflectionResult,
    RepairAttempt,
    TriageResult,
    VerificationResult,
)


class RunState(TypedDict, total=False):
    """LangGraph StateGraph 的 state。

    节点返回某字段即覆盖该字段；累积类数据（尝试历史）由节点显式读取旧值后
    拼接触发返回，语义比隐式 reducer 更清晰。
    """

    # 输入
    repo: str            # 仓库路径
    log_text: str        # Maven 构建日志
    base: str | None     # diff 基线（如 HEAD~1）
    diff_target: str | None  # diff 目标提交（默认 HEAD；benchmark 用于排除 harness 提交）
    workspace: str       # 隔离工作区（git worktree）路径
    workspace_dir: str   # 工作区父目录（默认 runs/）
    workspace_commit: str  # 检出的提交；缺省用仓库当前 HEAD（benchmark 固化 test patch 用）
    baseline_failures: list[str]  # 环境失败基线（Class#method），验证判定用"无新增失败"
    max_attempts: int    # 最大修复尝试次数（设计文档 14.5）

    # 各阶段产物
    failure: FailureContext
    triage: TriageResult
    context_bundle: DiagnosisContext
    diagnosis: Diagnosis
    decision: DiagnosisDecision          # 诊断决策（Agent Engineering V0.2 §1）
    evidence: list[Evidence]             # 证据账本（Working Memory，含预算记账）
    hypotheses: list[Hypothesis]         # 调查假设（POSSIBLE/SUPPORTED/REJECTED）
    inspected: list[str]                 # 已查看的文件/查询（防重复取证）
    context_requests: list[ContextRequest]  # 待执行的取证请求（acquire 消费后清空）
    acquire_rounds: int                  # 已用取证轮数
    tool_calls_used: int                 # 已用工具调用次数
    no_progress_streak: int              # 连续"语义无进展"次数（V0.2 §5）

    # Human-in-the-loop（V0.2 §6）
    hitl_enabled: bool                   # 允许人工门控（CLI 交互模式开启；benchmark/CI 关闭）
    gate_reason: str                     # 触发人工门控的原因（语义无进展/尝试耗尽+策略拒绝）
    human_decision: str                  # approved | rejected | auto_rejected
    patch: Patch
    policy_verdict: PolicyVerdict
    verification: VerificationResult
    verification_results: list[VerificationResult]
    reflection: ReflectionResult

    # 尝试历史。
    # 不用 reducer：验证节点负责"追加记录"，反思节点负责"补全最后一条的反思信息"，
    # 这样即使循环在验证失败后直接终止（达上限/主动停止），
    # 最后一轮失败也完整留在历史里（Repair Report 依赖于此）。
    attempt: int                            # 当前尝试轮次（1 起）
    attempts: list[RepairAttempt]
    attempt_history: list[str]              # 给 LLM 的历史摘要（累积）

    # 终止与错误
    stop_reason: str     # 主动停止的原因（设计文档 2.5）
    error: str           # 系统错误（SYSTEM_ERROR，设计文档 19 节）
