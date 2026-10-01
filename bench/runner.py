"""Benchmark 公共设施：case 加载、仓库检出、结果记录。

DevFix 与 baseline 共用同一套 case、同一上下文收集、同一验证实现——
两者的唯一区别是"有没有 agent 闭环"，保证对比公平。
"""

from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Self

from devfix.models import FailureContext, VerificationResult
from devfix.parsing import parse_maven_log
from devfix.tools import GitTool

CASES_DIR = Path("bench/cases")
WORK_DIR = Path("bench/.work")
RESULTS_DIR = Path("bench/results")


def load_cases(cases_dir: Path = CASES_DIR, only: list[str] | None = None) -> list[dict]:
    cases = []
    for path in sorted(cases_dir.glob("*.json")):
        case = json.loads(path.read_text(encoding="utf-8"))
        if only and case["case_id"] not in only:
            continue
        case["_dir"] = path.parent
        cases.append(case)
    return cases


def load_case_log(case: dict) -> str:
    return (case["_dir"] / case["log_file"]).read_text(encoding="utf-8", errors="replace")


def apply_test_patch(repo: Path, case: dict) -> None:
    """把 case 的 test patch 应用到工作区（**环境准备**，不是 Agent 的产物）。

    SWE-bench 方法论：bug 定义要求"测试 t 在 buggy 版本上失败"，
    而该测试通常由修复提交引入——因此评测环境必须预先带入这份测试改动。
    这也解释了为什么评测时 Repair Policy 仍禁止 Agent 改测试：
    测试是"环境的一部分"，是判定修复是否成立的标准。
    """
    patch_file = case.get("test_patch_file")
    if not patch_file:
        return
    patch_path = Path(case["_dir"]) / patch_file
    if not patch_path.exists():
        raise RuntimeError(f"test patch 缺失：{patch_path}")
    r = subprocess.run(
        # 必须用绝对路径：git apply 以 repo 为 cwd，相对路径会被解析到仓库内
        ["git", "apply", "--whitespace=nowarn", "--ignore-whitespace",
         str(patch_path.resolve())],
        cwd=repo, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120, check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(f"test patch 应用失败：{r.stderr[:200]}")


def prepare_repo(case: dict, work_dir: Path = WORK_DIR) -> Path:
    """把 case 对应的仓库检出到 buggy 提交并应用 test patch。

    test patch 会以**提交**形式固化在 buggy 提交之上：验证工作区是
    `git worktree add` 从提交创建的，未提交的工作区改动不会进入 worktree——
    否则新增的失败测试在 worktree 里根本不存在，L1 必然
    "no tests matching pattern"，两个 approach 都结构性不可能 FIXED
    （首轮评测暴露的评测有效性 bug）。
    """
    repo = work_dir / case["repo_name"]
    if not repo.exists():
        raise RuntimeError(
            f"仓库不存在：{repo}（先运行 scripts/mine_cases.py --repo {case['repo_name']}）"
        )
    # Windows 检出污染：仓库带 `* text=auto` 属性时，Windows 检出会把 index
    # 里的 LF 数据文件（如 commons-csv 的 csv-141.csv）转成 CRLF，行尾敏感的
    # 测试（testCSV141Excel）在本机必然失败——与补丁无关，却会让 L2/L3 全挂
    # （实测：修复提交上同样失败，证明是环境问题）。core.eol=lf 让检出产出
    # 与 Linux CI 一致的 LF；但 checkout -f/checkout-index -f 对已存在文件会
    # 因 stat 缓存跳过重写，必须先删光跟踪文件再从 index 强制重写。
    for key, value in (("core.autocrlf", "false"), ("core.eol", "lf")):
        subprocess.run(
            ["git", "config", key, value],
            cwd=repo, capture_output=True, text=True, timeout=60, check=False,
        )
    r = subprocess.run(
        ["git", "checkout", "--force", case["buggy_commit"]],
        cwd=repo, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=300, check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(f"检出失败：{r.stderr[:200]}")
    # 清理上一次运行留下的未跟踪/忽略文件：test patch 可能新增测试文件
    # （重跑时 git apply 会因"文件已存在"失败）；-x 连 target/ 等陈旧
    # 构建产物一起清掉——增量编译不清除已删除源文件的 class，
    # 会让"本应失败的测试"空过（假阳 FIXED）
    subprocess.run(
        ["git", "clean", "-xfdq"],
        cwd=repo, capture_output=True, text=True, timeout=120, check=False,
    )
    # 删光跟踪文件后从 index 重写：保证全部文件按 LF 检出（见上方注释）
    tracked = subprocess.run(
        ["git", "ls-files"],
        cwd=repo, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120, check=False,
    ).stdout.splitlines()
    for rel in tracked:
        rel = rel.strip().strip('"')  # quotepath 转义的路径按原样删除失败也无害
        target = repo / rel
        if target.is_file():
            target.unlink()
    subprocess.run(
        ["git", "checkout-index", "-f", "-a"],
        cwd=repo, capture_output=True, text=True, timeout=300, check=False,
    )
    apply_test_patch(repo, case)

    # 把 test patch 固化为提交：
    # - worktree 从这个提交创建（含测试），保证验证环境正确；
    # - HEAD 停在该提交上，工作区里测试文件保持可见——断言/超时类故障
    #   的栈里没有项目代码，上下文收集必须能读到失败测试做符号反查
    #   （拨回 buggy 提交会把测试从工作区清掉，定位链条整个断掉）；
    # - 共享仓库 HEAD 高于 buggy 提交的副作用（git diff base..HEAD 混入
    #   harness 提交）由调用方显式传 diff_target 规避。
    case.pop("_test_patch_commit", None)
    r = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120, check=False,
    )
    if r.stdout.strip():
        r = subprocess.run(
            ["git", "add", "-A"],
            cwd=repo, capture_output=True, text=True, timeout=120, check=False,
        )
        if r.returncode != 0:
            raise RuntimeError(f"git add 失败：{r.stderr[:200]}")
        r = subprocess.run(
            # 克隆仓库可能没有 user.name/email，用 -c 就地提供
            ["git", "-c", "user.name=devfix-bench", "-c", "user.email=devfix@bench.local",
             "commit", "-m", "bench: apply test patch (harness, not part of history)"],
            cwd=repo, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120, check=False,
        )
        if r.returncode != 0:
            raise RuntimeError(f"test patch 提交失败：{r.stderr[:200]}")
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120, check=False,
        )
        case["_test_patch_commit"] = r.stdout.strip()
    return repo


def parse_case_failure(case: dict) -> tuple[FailureContext, object]:
    ctx, triage = parse_maven_log(load_case_log(case))
    return ctx, triage


def localization_hits(diagnosed_files: list[str], truth_files: list[str]) -> tuple[bool, bool]:
    """Top-1 / Top-3 定位命中：诊断给出的文件是否覆盖 ground truth 的生产代码文件。

    以"诊断文件列表的排序位置"为 Top-K 依据（命中任一 ground truth 文件即算命中）。
    """

    def _norm(p: str) -> str:
        return p.replace("\\", "/").lstrip("./")

    truth = {_norm(f) for f in truth_files}
    if not truth:
        return False, False
    top1 = bool(diagnosed_files) and _norm(diagnosed_files[0]) in truth
    top3 = any(_norm(f) in truth for f in diagnosed_files[:3])
    return top1, top3


def _is_test_path(path: str) -> bool:
    """补丁是否触及测试代码（单模块或多模块布局下的 src/test）。"""
    n = path.replace("\\", "/").lstrip("./")
    return n.startswith("src/test/") or "/src/test/" in n


def summarize(
    case: dict,
    approach: str,
    status: str,
    attempts: int,
    verification: VerificationResult | None,
    diagnosed_files: list[str],
    duration_s: float,
    patch_files: list[str] | None = None,
    extra: dict | None = None,
) -> dict:
    top1, top3 = localization_hits(diagnosed_files, case.get("ground_truth_files", []))
    summary = {
        "case_id": case["case_id"],
        "approach": approach,
        "repo_name": case["repo_name"],
        "buggy_commit": case["buggy_commit"],
        "fix_commit": case["fix_commit"],
        "status": status,
        "fixed": status == "FIXED",
        "first_attempt_fixed": status == "FIXED" and attempts <= 1,
        "attempts": attempts,
        "duration_s": round(duration_s, 1),
        "diagnosed_files": diagnosed_files,
        "patch_files": patch_files or [],
        "ground_truth_files": case.get("ground_truth_files", []),
        "top1_localization": top1,
        "top3_localization": top3,
        "verification_status": verification.status.value if verification else None,
        # 不安全修改 = 补丁触及测试代码（改测试让验证变绿是典型的作弊向量，
        # DevFix 有策略拦截，baseline 没有——这一列就是两者诚信差异的显性化）
        "unsafe_modification": any(_is_test_path(f) for f in (patch_files or [])),
    }
    summary.update(extra or {})
    return summary


def write_result(summary: dict, results_dir: Path = RESULTS_DIR) -> Path:
    results_dir.mkdir(parents=True, exist_ok=True)
    path = results_dir / f"{summary['approach']}_{summary['case_id']}.json"
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


class Timer:
    def __enter__(self) -> Self:
        self._start = time.monotonic()
        return self

    def __exit__(self, *exc) -> None:
        self.elapsed = time.monotonic() - self._start


def compute_failure_baseline(repo: Path, case: dict, cfg) -> set[str]:
    """buggy 提交（含 test patch）上全量测试的失败集合 = 环境失败基线。

    验证判定从"全绿"升级为"目标测试通过 + 无新增失败"（SWE-bench 的
    F2P+P2P 语义）：本机存在一批与补丁无关的既有失败测试（Windows 检出/
    JDK 版本/网络依赖），不排除它们，L3 全量验证会误杀已修好的补丁
    （实测 FileStringLookupTest / Jetty ProxyTest 各坑掉一个已修复 case）。

    磁盘缓存按 (repo, buggy_commit) 键控——全量跑一次 2-5 分钟，跨运行复用。
    """
    cache = (
        WORK_DIR / f"baseline-{case['repo_name']}-{case['buggy_commit'][:10]}.json"
    )
    if cache.exists():
        return set(json.loads(cache.read_text(encoding="utf-8")))

    from devfix.parsing import parse_maven_log
    from devfix.parsing.surefire_xml import parse_surefire_reports, test_key
    from devfix.tools.maven_tool import MavenTool

    vcfg = cfg.verification
    raw = MavenTool(repo, mvn=vcfg.maven_command).run(
        ["test"],
        timeout_seconds=vcfg.full_timeout_s + 900,
        extra_args=vcfg.effective_maven_args(),
    )
    summary, failed = parse_surefire_reports(repo)
    if summary is not None:
        keys = {test_key(t.class_name, t.method_name) for t in failed}
    else:
        # 无 XML（构建早期就崩）→ console 解析兜底
        ctx, _ = parse_maven_log(raw.log)
        keys = {test_key(t.class_name, t.method_name) for t in ctx.failed_tests}

    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(sorted(keys)), encoding="utf-8")
    return keys


def make_worktree(repo: Path, case: dict, root: Path | str) -> Path:
    """为一次评测创建隔离工作区（buggy 提交 + 固化的 test patch）。

    root 允许 str：baseline 传入的是 f-string，必须在内部转 Path
    （str / str 会 TypeError，且不被调用方的 except ToolError 捕获，
    曾让整个 baseline 扫描中途崩溃）。
    """
    ws = Path(root) / f"ws-{case['case_id']}"
    if ws.exists():
        from bench.cleanup import force_rmtree

        try:
            force_rmtree(ws)
        except RuntimeError:
            # 瞬态文件锁兜底：换个时间戳后缀继续，不让单个 case 崩掉整个扫描
            ws = ws.with_name(f"{ws.name}-{datetime.now().astimezone():%H%M%S}")
    git = GitTool(repo)
    # 重跑时旧 worktree 的注册信息仍在 .git/worktrees/（删目录不清注册），
    # 不 prune 的话 worktree add 报 "missing but already registered worktree"
    git.prune_worktrees()
    # 有 test patch 时必须用固化提交——buggy 提交本身不含失败测试
    commit = case.get("_test_patch_commit") or case["buggy_commit"]
    return git.create_worktree(ws, commit=commit)
