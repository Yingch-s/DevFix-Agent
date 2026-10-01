"""DevFix 核心数据模型（对应《系统设计》第 9 节）。

所有跨组件传递的结构化数据（FailureContext / TriageResult / Diagnosis / Patch …）
都在本模块定义，后续 Phase 逐步补充。
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class FailureType(str, Enum):
    """故障类型（对应系统设计 5.4 Triage Engine）。"""

    COMPILE_ERROR = "COMPILE_ERROR"
    UNIT_TEST_FAILURE = "UNIT_TEST_FAILURE"
    RUNTIME_EXCEPTION = "RUNTIME_EXCEPTION"
    SPRING_CONTEXT_FAILURE = "SPRING_CONTEXT_FAILURE"
    CONFIG_ERROR = "CONFIG_ERROR"
    DEPENDENCY_ERROR = "DEPENDENCY_ERROR"
    UNKNOWN = "UNKNOWN"


class StackFrame(BaseModel):
    """Java 栈帧。"""

    class_name: str                # com.example.OrderService
    method_name: str               # createOrder
    file_name: str | None = None   # OrderService.java
    line_number: int | None = None  # 82
    module_name: str | None = None  # JPMS 模块名（如 java.base），可为空

    @property
    def location(self) -> str:
        if self.file_name and self.line_number:
            return f"{self.file_name}:{self.line_number}"
        return self.file_name or "Unknown Source"


class StackTrace(BaseModel):
    """一条异常栈（含 Caused by 链）。"""

    exception_type: str  # java.lang.NullPointerException
    message: str | None = None
    frames: list[StackFrame] = Field(default_factory=list)
    cause: StackTrace | None = None  # Caused by 链

    @property
    def simple_type(self) -> str:
        return self.exception_type.rsplit(".", 1)[-1]


class FailedTest(BaseModel):
    """一个失败的测试用例。"""

    class_name: str                # com.example.OrderServiceTest
    method_name: str               # shouldRejectDeletedUser
    kind: str = "FAILURE"          # FAILURE（断言失败）| ERROR（异常）
    error_type: str | None = None  # java.lang.AssertionError
    message: str | None = None     # expected: <2> but was: <1>

    @property
    def display_name(self) -> str:
        return f"{self.class_name}#{self.method_name}"


class TestSummary(BaseModel):
    """Surefire 汇总计数（Results 段）。"""

    run: int = 0
    failures: int = 0
    errors: int = 0
    skipped: int = 0


class CompileError(BaseModel):
    """一条编译错误（javac / maven-compiler-plugin 输出格式）。"""

    file_path: str                 # D:/.../OrderController.java
    line: int | None = None
    column: int | None = None
    message: str                   # cannot find symbol / incompatible types ...
    symbol: str | None = None      # 续行 symbol: method findActiveById(int)
    location: str | None = None    # 续行 location: variable userRepository ...


class FailureContext(BaseModel):
    """失败上下文（对应系统设计 9.2 FailureContext）。"""

    job: str | None = None
    step: str | None = None
    exit_code: int | None = None
    key_error: str | None = None  # 关键异常简名，如 NullPointerException
    failed_tests: list[FailedTest] = Field(default_factory=list)
    test_summary: TestSummary | None = None
    stack_traces: list[StackTrace] = Field(default_factory=list)
    compile_errors: list[CompileError] = Field(default_factory=list)
    raw_log_path: str | None = None


class TriageResult(BaseModel):
    """Triage 输出（对应系统设计 5.4）。

    confidence 仅是诊断阶段的辅助信息，不能替代真实验证。
    """

    failure_type: FailureType
    confidence: float = 0.0
    key_error: str | None = None
    supported: bool = True
    reason: str = ""  # 命中的规则与证据，供报告与调试


class SearchMatch(BaseModel):
    """一次代码搜索命中（对应系统设计 10.3 search_code 输出）。"""

    file_path: str  # 相对仓库根目录
    line_number: int
    line_text: str


class CodeSnippet(BaseModel):
    """一段提供给 LLM 的代码上下文。"""

    file_path: str  # 相对仓库根目录
    start_line: int
    end_line: int
    content: str
    source: str  # 来源：stack_trace / failed_test / compile_error ...


class Diagnosis(BaseModel):
    """结构化诊断（对应系统设计 5.8 / 9.4）。

    必须区分观察（可直接验证的事实）、推断（由观察推出的假设）与根因。
    """

    diagnosis_id: str = "DG-001"
    summary: str = ""          # 一句话说明问题是什么
    observations: list[str] = Field(default_factory=list)  # 可观察证据
    inference: str = ""        # 从观察到根因的推断链
    root_cause: str = ""       # 为什么发生
    related_files: list[str] = Field(default_factory=list)
    related_lines: list[str] = Field(default_factory=list)  # "OrderService.java:82"
    evidence: list[str] = Field(default_factory=list)       # 判断依据清单
    confidence: float = 0.0
    insufficient_evidence: bool = False  # 证据不足时置位，Repair Policy 据此停止


class DiagnosisContext(BaseModel):
    """Diagnosis Agent 的输入：失败证据 + 代码上下文 + 变更（对应系统设计 12 节 Stage 2~3）。"""

    failure: FailureContext
    triage: TriageResult
    git_diff: str = ""
    snippets: list[CodeSnippet] = Field(default_factory=list)


# ------------------------------------------------------------------ Agent Engineering V0.2
# Context Acquisition Loop 的结构化对象（设计文档：Agent Engineering V0.2 §1~§4）。
# 证据/假设是一等公民：Agent 在有限预算下主动取证，假设驱动调查，
# 预算耗尽 = 停止取证而非强制认为证据充分。

EvidenceKind = str  # "source_range" | "git_diff" | "tool_result"


class Evidence(BaseModel):
    """证据账本中的一条证据（Working Memory 的上下文部分）。"""

    id: str                       # EV-001
    kind: EvidenceKind
    source: str                   # initial:stack_trace / tool:read_file#2 ...
    file_path: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    content: str = ""


class Hypothesis(BaseModel):
    """调查假设。枚举而非数值置信度——LLM 给的 0.61→0.89 是伪精确。"""

    statement: str
    supporting: list[str] = Field(default_factory=list)  # 证据编号 EV-xxx
    status: str = "POSSIBLE"      # POSSIBLE | SUPPORTED | REJECTED


class ContextRequest(BaseModel):
    """取证请求：必须挂在某条假设下（"为什么调用这个工具"可审计）。"""

    tool: str                     # search_code | read_file | find_symbol | get_git_diff | get_changed_files
    args: dict[str, str | int] = Field(default_factory=dict)
    reason: str                   # 为什么需要——进 Trace 与审计
    hypothesis: str | None = None  # 关联假设的陈述（或其编号）


class DiagnosisDecision(BaseModel):
    """诊断决策（Context Acquisition Loop 的节点输出）。

    status 语义：
    - NEED_MORE_CONTEXT：证据不够，附带 context_requests（预算内可继续取证）
    - DIAGNOSIS_READY：根因明确，diagnosis 字段必填
    - INSUFFICIENT_EVIDENCE：取证预算内仍无法确认——诚实停止（不猜）
    预算耗尽后的最终决策只允许 READY / INSUFFICIENT（确定性强制）。
    """

    status: str                   # DIAGNOSIS_READY | NEED_MORE_CONTEXT | INSUFFICIENT_EVIDENCE
    diagnosis: Diagnosis | None = None
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    context_requests: list[ContextRequest] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)  # INSUFFICIENT 时缺什么


def budget_used(evidence: list[Evidence]) -> int:
    """证据账本已用字符数（预算记账，见 Agent Engineering V0.2 §1）。"""
    return sum(len(e.content) for e in evidence)


def next_evidence_id(evidence: list[Evidence]) -> str:
    return f"EV-{len(evidence) + 1:03d}"


# ---------------------------------------------------------------------- 修复
class PatchEdit(BaseModel):
    """单文件编辑指令（技术选型 2.6 的生成层表示）。

    二选一：
    - search/replace：search 必须是目标文件中"逐字符精确且唯一"的片段
    - full_content：整文件替换（大改写场景）
    """

    file: str  # 仓库相对路径
    search: str | None = None
    replace: str | None = None
    full_content: str | None = None


class Patch(BaseModel):
    """候选修复（对应系统设计 5.10 Patch Generator 输出）。"""

    patch_id: str = "PATCH-001"
    edits: list[PatchEdit] = Field(default_factory=list)
    reason: str = ""           # 修改原因
    expected_effect: str = ""  # 预期修复效果
    cannot_fix_reason: str = ""  # 模型自述无法在约束内安全修复时填写
    changed_files: list[str] = Field(default_factory=list)
    diff: str = ""             # canonical unified diff（应用后由 git diff 产出）


class RepairDecision(str, Enum):
    """Repair Policy 决策（对应系统设计 5.9）。"""

    AUTO_REPAIR = "AUTO_REPAIR"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    UNSUPPORTED = "UNSUPPORTED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class PolicyVerdict(BaseModel):
    """策略校验结论。"""

    decision: RepairDecision
    reasons: list[str] = Field(default_factory=list)  # 命中的规则与证据


# ---------------------------------------------------------------------- 验证
class VerificationStatus(str, Enum):
    """验证结果状态（设计文档 5.12：只有 PASS/FAIL，超时单独区分）。"""

    PASS = "PASS"
    FAIL = "FAIL"
    TIMEOUT = "TIMEOUT"
    ERROR = "ERROR"  # 系统错误（Maven 无法执行等）


class VerificationLevel(str, Enum):
    """分层验证（设计文档 15 节：由小到大）。"""

    FOCUSED = "FOCUSED"   # L1 原失败用例
    RELATED = "RELATED"   # L2 相关测试类
    FULL = "FULL"         # L3 完整构建


class VerificationResult(BaseModel):
    """验证结果（对应系统设计 9.6）。"""

    verification_id: str = "VR-001"
    level: VerificationLevel
    status: VerificationStatus
    command: str = ""
    exit_code: int | None = None
    duration_ms: int = 0
    failed_tests: list[FailedTest] = Field(default_factory=list)
    new_errors: list[str] = Field(default_factory=list)
    log_tail: str = ""  # 末尾若干行（避免超长日志撑爆上下文）
    failure_context: FailureContext | None = None  # 复用 Phase 2 的日志解析

    @property
    def passed(self) -> bool:
        return self.status is VerificationStatus.PASS


# ---------------------------------------------------------------------- 反思
class NextAction(str, Enum):
    """反思后的下一步动作（对应系统设计 5.13）。"""

    REPAIR_AGAIN = "REPAIR_AGAIN"
    STOP = "STOP"


class ReflectionResult(BaseModel):
    """反思结论（对应系统设计 5.13 Reflection Agent 输出）。

    V0.2 §3/§5 扩展：反思是 Working Memory 的更新者——
    可更新假设状态、可发起取证请求（由系统执行后回传给下一轮修复）。
    """

    previous_diagnosis_valid: bool = True   # 原诊断是否仍成立
    failure_reason: str = ""                # 补丁为什么失败
    new_evidence: list[str] = Field(default_factory=list)  # 新出现的证据
    should_update_diagnosis: bool = False
    updated_root_cause: str = ""            # 需要修订诊断时的新根因
    next_action: NextAction = NextAction.REPAIR_AGAIN
    confidence: float = 0.0
    hypothesis_updates: list[Hypothesis] = Field(default_factory=list)  # 假设状态更新（POSSIBLE/SUPPORTED/REJECTED）
    context_requests: list[ContextRequest] = Field(default_factory=list)  # 取证请求（系统执行后供下轮修复使用）


class RepairAttempt(BaseModel):
    """一次修复尝试的完整记录（对应系统设计 9.5 / 16 节 Attempt History）。"""

    attempt: int
    patch: Patch | None = None
    verdict: PolicyVerdict | None = None
    verification: VerificationResult | None = None   # 最终结论（最后一层）
    verifications: list[VerificationResult] = Field(default_factory=list)  # 分层明细
    reflection: ReflectionResult | None = None
