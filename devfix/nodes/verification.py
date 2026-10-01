"""验证节点：对隔离工作区执行分层验证，并把结果作为证据写回 state。

设计文档 2.4：模型说"应该修好了"不算数，只有真实执行结果算数。
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, TypedDict

from devfix.models import (
    FailureContext,
    Patch,
    PolicyVerdict,
    RepairAttempt,
    VerificationResult,
)


class Verifier(Protocol):
    """验证能力抽象。

    workspace 由图中 prepare_workspace 动态创建，因此作为参数传入而不是
    在构造验证器时绑定——这样验证器可以在图组装前就创建好。
    """

    def verify(
        self, failure: FailureContext, workspace: Path
    ) -> list[VerificationResult]: ...


class VerificationState(TypedDict, total=False):
    failure: FailureContext
    workspace: str
    patch: Patch
    policy_verdict: PolicyVerdict
    attempt: int
    verification: VerificationResult
    verification_results: list[VerificationResult]
    attempts: list[RepairAttempt]
    baseline_failures: list[str]
    error: str


def verification_node(state: VerificationState, verifier: Verifier) -> dict:
    """执行验证；验证通过时把本次尝试记入 Attempt History。"""
    failure = state.get("failure")
    workspace = state.get("workspace")
    if failure is None:
        return {"error": "verification 节点缺少 failure"}
    if not workspace:
        return {"error": "verification 节点缺少 workspace"}
    if state.get("error"):
        return {}  # 上游已出错，不再执行验证

    # 失败基线（benchmark 提供）：验证判定用"无新增失败"。
    # 不传时用两参调用，保持无基线场景（CLI/离线 Fake）的签名兼容。
    baseline = state.get("baseline_failures")
    kwargs = (
        {"baseline_failures": {str(b) for b in baseline}} if baseline else {}
    )
    try:
        results = verifier.verify(failure, Path(workspace), **kwargs)
    except Exception as e:  # noqa: BLE001 —— 工具/环境异常统一进 error 字段
        return {"error": f"验证执行失败：{type(e).__name__}: {e}"}

    if not results:
        return {"error": "验证未返回任何结果"}

    # 每次验证都追加一条尝试记录——即使随后循环终止（达上限/主动停止），
    # 本轮失败也必须留在历史里；反思节点会补全该条记录的 reflection 字段。
    record = RepairAttempt(
        attempt=state.get("attempt", 1),
        patch=state.get("patch"),
        verdict=state.get("policy_verdict"),
        verification=results[-1],
        verifications=results,
    )
    return {
        "verification": results[-1],
        "verification_results": results,
        "attempts": [*state.get("attempts", []), record],
    }
