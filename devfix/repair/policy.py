"""Repair Policy Engine（对应系统设计 5.9 与 14 节 Repair Safety Policy）。

在补丁写入工作区**之前**判定是否允许自动修复，在写入**之后**再做
diff 层面的复核（规模、禁止删测试）。任何一项不通过都降级为人工复核，
而不是"先改了再说"。
"""

from __future__ import annotations

from fnmatch import fnmatch

from devfix.models import (
    Diagnosis,
    Patch,
    PolicyVerdict,
    RepairDecision,
)

# 设计文档 14.1：允许 / 禁止自动修改的区域
ALLOWED_PATTERNS = ("src/main/java/**", "src/main/resources/**", "pom.xml")
FORBIDDEN_PATTERNS = (
    "src/test/**", ".github/**", "deployment/**", "infrastructure/**",
)

# 设计文档 14.3：测试保护——出现这些内容视为"删测试让 CI 变绿"
TEST_KILL_PATTERNS = ("@Disabled", "@Ignore", "assumeTrue(false)", "assumeThat(false)")

# pom.xml 在白名单内（依赖/插件配置可能是修复的一部分），但"在 pom 层面
# 跳过测试"同样能让 CI 变绿——TEST_KILL_PATTERNS 只查 Java 注解，查不到
# 构建配置层面的作弊。命中任一即拒绝。
POM_KILL_PATTERNS = ("<skipTests>", "<maven.test.skip>", "testFailureIgnore")

# 诊断置信度低于该值时不允许自动修复（设计文档 5.9"根因是否足够明确"）
MIN_CONFIDENCE = 0.5


def _matches(path: str, pattern: str) -> bool:
    """POSIX 风格路径匹配，支持 "dir/**" 前缀匹配。

    先做规范化（反斜杠、前导 "./" 与 "/"），避免模型给出的等价路径
    （如 "./src/main/java/X.java"）被误判为"白名单之外"——
    路径能否被接受不应取决于书写形式（PatchTool 对这些形式都能正确处理）。

    含 ".." 段的路径一律返回 False（不命中任何模式）：这是安全关键——
    PatchTool 会对 ".." 做 resolve，"src/main/java/../src/test/java/X.java"
    在文件系统层面合法，但若让它在**字符串层面**命中白名单前缀
    "src/main/java/"，就能绕开测试保护直接改测试文件。返回 False 使其
    落入"白名单之外"分支被 MANUAL_REVIEW 拦下（安全失败）。
    """
    p = path.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    p = p.lstrip("/")
    if ".." in p.split("/"):
        return False
    if pattern.endswith("/**"):
        prefix = pattern[:-3]
        return p == prefix or p.startswith(prefix + "/")
    return fnmatch(p, pattern)


class RepairPolicy:
    """自动修复安全检查器。"""

    def __init__(self, max_files: int = 3, max_diff_lines: int = 100) -> None:
        self.max_files = max_files
        self.max_diff_lines = max_diff_lines

    # ------------------------------------------------------------------ 应用前
    def evaluate(self, diagnosis: Diagnosis, patch: Patch) -> PolicyVerdict:
        """补丁写入前的检查：证据充分性、文件范围、规模。"""
        reasons: list[str] = []

        if patch.cannot_fix_reason:
            return PolicyVerdict(
                decision=RepairDecision.MANUAL_REVIEW,
                reasons=[f"模型自述无法在约束内安全修复：{patch.cannot_fix_reason}"],
            )
        if not patch.edits:
            return PolicyVerdict(
                decision=RepairDecision.MANUAL_REVIEW,
                reasons=["补丁不包含任何编辑（空补丁）"],
            )
        if diagnosis.insufficient_evidence:
            return PolicyVerdict(
                decision=RepairDecision.INSUFFICIENT_EVIDENCE,
                reasons=["诊断自述证据不足，禁止自动修复（设计文档 9.1 主动停止原则）"],
            )
        if diagnosis.confidence < MIN_CONFIDENCE:
            return PolicyVerdict(
                decision=RepairDecision.INSUFFICIENT_EVIDENCE,
                reasons=[f"诊断置信度 {diagnosis.confidence} < {MIN_CONFIDENCE}，证据不足"],
            )

        files = [e.file for e in patch.edits]
        for f in files:
            if any(_matches(f, pat) for pat in FORBIDDEN_PATTERNS):
                return PolicyVerdict(
                    decision=RepairDecision.MANUAL_REVIEW,
                    reasons=[f"涉及禁止自动修改的区域：{f}（设计文档 14.1）"],
                )
        outside = [f for f in files if not any(_matches(f, pat) for pat in ALLOWED_PATTERNS)]
        if outside:
            return PolicyVerdict(
                decision=RepairDecision.MANUAL_REVIEW,
                reasons=[f"修改了白名单之外的文件：{', '.join(outside)}"],
            )
        if len(files) > self.max_files:
            return PolicyVerdict(
                decision=RepairDecision.MANUAL_REVIEW,
                reasons=[f"修改文件数 {len(files)} 超过上限 {self.max_files}"],
            )

        reasons.append(
            f"通过应用前检查：{len(files)} 个文件，均在允许范围内"
            f"（置信度 {diagnosis.confidence}）"
        )
        return PolicyVerdict(decision=RepairDecision.AUTO_REPAIR, reasons=reasons)

    # ------------------------------------------------------------------ 应用后
    def evaluate_applied(self, patch: Patch) -> PolicyVerdict:
        """补丁写入后的复核：diff 规模与测试保护。"""
        diff = patch.diff or ""
        # +++/--- 之外的增删行（真正的内容变更行）
        changed_lines = [
            line for line in diff.splitlines()
            if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
        ]
        if len(changed_lines) > self.max_diff_lines:
            return PolicyVerdict(
                decision=RepairDecision.MANUAL_REVIEW,
                reasons=[f"diff 变更行数 {len(changed_lines)} 超过上限 {self.max_diff_lines}"],
            )

        added = [line for line in changed_lines if line.startswith("+")]
        for pattern in (*TEST_KILL_PATTERNS, *POM_KILL_PATTERNS):
            if any(pattern in line for line in added):
                return PolicyVerdict(
                    decision=RepairDecision.MANUAL_REVIEW,
                    reasons=[f"补丁新增 '{pattern}'，疑似禁用测试（设计文档 14.3 直接拒绝）"],
                )
        removed = [line for line in changed_lines if line.startswith("-")]
        if any("@Test" in line for line in removed):
            return PolicyVerdict(
                decision=RepairDecision.MANUAL_REVIEW,
                reasons=["补丁删除了 @Test 注解，疑似删测试（设计文档 14.3 直接拒绝）"],
            )

        return PolicyVerdict(
            decision=RepairDecision.AUTO_REPAIR,
            reasons=[f"通过应用后复核：{len(changed_lines)} 行变更，未触碰测试保护规则"],
        )
