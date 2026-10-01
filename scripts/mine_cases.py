"""从真实开源项目的修复提交中挖掘可复现的 benchmark case。

方法论对齐 BEARS（Madeiral et al., SANER 2019）的 "Case #2" 模式与
GitBug-Java（Silva et al., MSR 2024）的 bug 对定义：

    一个 case = (repo, buggy_commit, fix_commit, failing_test, 真实失败日志)
    其中 buggy_commit = fix_commit^，且该提交新增的测试在 buggy_commit 上**真实失败**。

筛选链路（每一步都在真实执行中验证，不靠猜）：
    1. 找同时改动 src/main 与 src/test 的修复类提交（说明开发者补了回归测试）
    2. 从 diff 中提取新增的测试方法
    3. checkout 父提交，运行该测试
    4. 只有"确实失败且不是编译错误"的才算有效 case，日志原样保存

用法：
    .venv/Scripts/python scripts/mine_cases.py --repo jsoup --max-cases 3
    .venv/Scripts/python scripts/mine_cases.py --list
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from devfix.tools import MavenTool

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CASES_DIR = Path("bench/cases")
WORK_DIR = Path("bench/.work")

# 候选项目：现代、Maven 原生、测试较快、JDK 17+ 可编译
CANDIDATE_REPOS: dict[str, str] = {
    "jsoup": "https://github.com/jhy/jsoup.git",
    "gson": "https://github.com/google/gson.git",
    "json-java": "https://github.com/stleary/JSON-java.git",
    "commons-text": "https://github.com/apache/commons-text.git",
    "commons-csv": "https://github.com/apache/commons-csv.git",
    "commons-lang": "https://github.com/apache/commons-lang.git",
    "commons-io": "https://github.com/apache/commons-io.git",
    "commons-collections": "https://github.com/apache/commons-collections.git",
    "commons-codec": "https://github.com/apache/commons-codec.git",
    "jackson-core": "https://github.com/FasterXML/jackson-core.git",
    "feign": "https://github.com/OpenFeign/feign.git",
}

# 修复类提交的标题特征
FIX_TITLE_RE = re.compile(r"\b(fix|fixes|fixed|bug|issue|wrong|incorrect|regression)\b", re.IGNORECASE)
# diff 中新增的测试方法
NEW_TEST_RE = re.compile(r"^\+\s*(?:public\s+|protected\s+|private\s+)?void\s+(\w+)\s*\(")


def _run(cmd: list[str], cwd: Path | None = None, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout, check=False,
    )


def _git(repo: Path, *args: str) -> str:
    r = _run(["git", *args], cwd=repo, timeout=300)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{r.stderr[:300]}")
    return r.stdout


def clone_or_update(name: str, url: str) -> Path:
    dest = WORK_DIR / name
    if dest.exists():
        print(f"  复用已克隆仓库：{dest}")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"  克隆 {url} …")
    r = _run(["git", "clone", "--quiet", url, str(dest)], timeout=1800)
    if r.returncode != 0:
        raise RuntimeError(f"克隆失败：{r.stderr[:300]}")
    return dest


def find_fix_commits(repo: Path, since: str, max_candidates: int, require_test: bool) -> list[dict]:
    """找修复类提交。

    since 限定时间范围：**历史提交往往编译不过现代 JDK**
    （实测 jsoup 2016-2018 的提交在 JDK 21 上 maven-compiler-plugin 直接失败），
    因此默认只挖近年提交——与"现代项目优先"的选型一致。

    require_test=True 时只保留同时改动 src/test 的提交（开发者补了回归测试）；
    False 时只要改动 src/main 即可（更宽的产出面，但需要另行定位失败测试）。
    """
    log = _git(
        repo, "log", "--no-merges", "--name-only", f"--since={since}",
        "--format=@@@%H%x09%s", "-n", "6000",
    )
    commits: list[dict] = []
    current: dict | None = None
    for line in log.splitlines():
        if line.startswith("@@@"):
            sha, _, subject = line[3:].partition("	")
            current = {"sha": sha, "subject": subject, "files": []}
            commits.append(current)
        elif line.strip() and current is not None:
            current["files"].append(line.strip())

    picked = []
    for c in commits:
        if not FIX_TITLE_RE.search(c["subject"]):
            continue
        main = [f for f in c["files"] if f.startswith("src/main/") and f.endswith(".java")]
        test = [f for f in c["files"] if f.startswith("src/test/") and f.endswith(".java")]
        if not main:
            continue
        if require_test and not test:
            continue
        c["main_files"], c["test_files"] = main, test
        picked.append(c)
        if len(picked) >= max_candidates:
            break
    return picked


def find_related_tests(repo: Path, main_files: list[str], limit: int = 4) -> list[str]:
    """在被改动类的测试源码中反查相关测试类（用于"已有测试捕获缺陷"的挖掘策略）。"""
    class_names = [Path(f).stem for f in main_files]
    test_root = repo / "src/test/java"
    if not test_root.is_dir():
        return []
    found: list[str] = []
    for path in sorted(test_root.rglob("*Test.java")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if any(re.search(rf"\b{name}\b", text) for name in class_names):
            found.append(path.stem)
        if len(found) >= limit:
            break
    return found


def verify_preexisting_test(
    repo: Path, parent_sha: str, test_classes: list[str]
) -> tuple[bool, str, tuple[str, str] | None]:
    """在父提交上运行相关测试类：项目**已有**测试失败才算有效 case。

    返回 (是否有效, 日志或原因, (失败测试类, 方法))。
    """
    from devfix.config import QUALITY_GATE_SKIP_ARGS
    from devfix.parsing import parse_maven_log

    _run(["git", "checkout", "--force", parent_sha], cwd=repo, timeout=300)
    raw = MavenTool(repo).run(
        [f"-Dtest={','.join(test_classes)}", "-DfailIfNoTests=false", "test"],
        timeout_seconds=900,
        extra_args=QUALITY_GATE_SKIP_ARGS,
    )
    if raw.timed_out:
        return False, "运行超时", None
    if "COMPILATION ERROR" in raw.log:
        return False, "父提交编译失败", None
    ctx, triage = parse_maven_log(raw.log)
    if not ctx.failed_tests:
        return False, f"相关测试全部通过（{triage.reason}）", None
    target = ctx.failed_tests[0]
    if not _has_project_frame(ctx, repo):
        return False, "失败栈帧不在项目代码中（疑似环境问题）", None
    return True, clean_log(raw.log), (target.class_name, target.method_name)


def _project_package(repo: Path) -> str:
    """从 pom.xml 推断 groupId 前缀（用于判断栈帧是否落在项目代码里）。"""
    pom = repo / "pom.xml"
    if not pom.exists():
        return ""
    m = re.search(r"<groupId>([\w.]+)</groupId>", pom.read_text(encoding="utf-8", errors="replace"))
    return m.group(1).split(".")[0] if m else ""


def _has_project_frame(ctx, repo: Path) -> bool:
    """栈帧里是否有项目自身代码（排除纯 JDK/第三方栈）。"""
    third_party_prefixes = ("java.", "javax.", "jdk.", "sun.", "kotlin.", "scala.")
    for trace in ctx.stack_traces:
        for frame in trace.frames:
            if not frame.class_name.startswith(third_party_prefixes):
                return True
    return not ctx.stack_traces


def extract_new_tests(repo: Path, commit: dict) -> list[tuple[str, str, str]]:
    """提取**真正新增**的测试方法 -> [(测试文件, 类名, 方法名)]。

    关键：只看 diff 的 + 行会把"被改写的方法"也当成新增——而改写后的测试
    在父提交上当然通过，这类候选会被后续验证全部否掉（实测 17 个候选只活 1 个）。
    因此这里额外要求：方法名**在父提交的同名文件中不存在**。
    """
    parent = f"{commit['sha']}^"
    diff = _run(
        ["git", "show", commit["sha"], "--", *commit["test_files"]], cwd=repo, timeout=120
    ).stdout

    current_file = ""
    candidates: list[tuple[str, str, str]] = []
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:]
        m = NEW_TEST_RE.match(line)
        if m and current_file.endswith(".java"):
            candidates.append((current_file, Path(current_file).stem, m.group(1)))

    added: list[tuple[str, str, str]] = []
    parent_cache: dict[str, str] = {}
    for file_path, class_name, method in candidates:
        if file_path not in parent_cache:
            r = _run(["git", "show", f"{parent}:{file_path}"], cwd=repo, timeout=60)
            parent_cache[file_path] = r.stdout if r.returncode == 0 else ""
        parent_text = parent_cache[file_path]
        if not re.search(rf"\b{re.escape(method)}\b\s*\(", parent_text):
            added.append((file_path, class_name, method))
    return added


def build_test_patch(repo: Path, parent_sha: str, commit: dict) -> str:
    """修复提交中的**测试文件改动**（SWE-bench 的 test patch）。

    关键方法论：bug 对要求"测试 t 在旧版本上失败"。但修复提交新增的测试
    在旧版本里根本不存在——直接跑该测试会执行 0 个用例并返回成功，
    被误判为"父提交通过"（实测卡了很久）。正确做法是把测试改动单独打到
    旧版本上，再运行 fail-to-pass 测试。
    """
    r = _run(
        ["git", "diff", parent_sha, commit["sha"], "--", *commit["test_files"]],
        cwd=repo, timeout=180,
    )
    return r.stdout


def apply_patch(repo: Path, patch: str) -> bool:
    if not patch.strip():
        return False
    r = subprocess.run(
        ["git", "apply", "--whitespace=nowarn", "--ignore-whitespace", "-"], cwd=repo,
        input=patch, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120, check=False,
    )
    return r.returncode == 0


def verify_case(
    repo: Path, parent_sha: str, commit: dict, test_class: str, test_method: str
) -> tuple[bool, str]:
    """在 buggy 版本 + test patch 上运行目标测试：**确实失败**才算有效 case。"""
    from devfix.config import QUALITY_GATE_SKIP_ARGS
    from devfix.parsing import parse_maven_log

    _run(["git", "checkout", "--force", parent_sha], cwd=repo, timeout=300)
    _run(["git", "clean", "-fdq", "src/test"], cwd=repo, timeout=120)

    patch = build_test_patch(repo, parent_sha, commit)
    if not apply_patch(repo, patch):
        _run(["git", "checkout", "--force", parent_sha], cwd=repo, timeout=120)
        return False, "test patch 无法应用到 buggy 版本"

    raw = MavenTool(repo).run(
        [f"-Dtest={test_class}#{test_method}", "-DfailIfNoTests=false", "test"],
        timeout_seconds=600,
        extra_args=QUALITY_GATE_SKIP_ARGS,
    )
    log = raw.log
    if raw.timed_out:
        return False, "运行超时"
    if "COMPILATION ERROR" in log:
        return False, "buggy 版本 + test patch 编译失败"

    ctx, triage = parse_maven_log(log)
    if not ctx.failed_tests:
        return False, f"目标测试未失败（{triage.reason}）"
    if not any(t.method_name == test_method for t in ctx.failed_tests):
        return False, "失败的是别的用例"
    return True, clean_log(log)


def passes_at_fix(repo: Path, fix_sha: str, test_class: str, test_method: str) -> bool:
    """benchmark 完整性检查：目标测试在 fix 提交上必须通过。

    只有"父提交失败 + 修复提交通过"才构成一个自洽的 bug 对
    （BEARS / GitBug-Java 的定义）。
    """
    from devfix.config import QUALITY_GATE_SKIP_ARGS

    _run(["git", "checkout", "--force", fix_sha], cwd=repo, timeout=300)
    raw = MavenTool(repo).run(
        [f"-Dtest={test_class}#{test_method}", "-DfailIfNoTests=false", "test"],
        timeout_seconds=600,
        extra_args=QUALITY_GATE_SKIP_ARGS,
    )
    return raw.exit_code == 0 and not raw.timed_out


def clean_log(log: str) -> str:
    """去掉依赖下载噪音，只保留构建与测试相关输出。

    CI 日志通常不包含首次运行时的下载流水；保留它们只会稀释 Agent 上下文。
    """
    skip = ("[INFO] Downloading", "[INFO] Downloaded", "[INFO] Progress", "Download progress")
    return "\n".join(
        line for line in log.splitlines()
        if not any(line.startswith(p) for p in skip)
    )


def default_branch(repo: Path) -> str:
    r = _run(["git", "symbolic-ref", "refs/remotes/origin/HEAD", "--short"], cwd=repo)
    name = r.stdout.strip().removeprefix("origin/")
    return name or "main"


def _record_case(
    name: str, commit: dict, parent: str, test_class: str, test_method: str,
    log: str, strategy: str, test_patch: str = "",
) -> dict:
    case_id = f"{name}-{commit['sha'][:7]}-{test_method}"
    (CASES_DIR / f"{case_id}.log").write_text(log, encoding="utf-8", newline="")
    patch_file = ""
    if test_patch:
        patch_file = f"{case_id}.test.patch"
        (CASES_DIR / patch_file).write_text(test_patch, encoding="utf-8", newline="")
    case = {
        "case_id": case_id,
        "source": "mined",
        "strategy": strategy,          # new_test：修复提交补的测试；preexisting：项目已有测试
        "repo_name": name,
        "repo_url": CANDIDATE_REPOS[name],
        "buggy_commit": parent,
        "fix_commit": commit["sha"],
        "fix_subject": commit["subject"],
        "test_class": test_class,
        "test_method": test_method,
        "log_file": f"{case_id}.log",
        "test_patch_file": patch_file,  # 环境准备用（非 Agent 产物）
        # ground truth：修复提交改动的生产代码文件（用于"是否改对文件"分析）
        "ground_truth_files": commit["main_files"],
    }
    (CASES_DIR / f"{case_id}.json").write_text(
        json.dumps(case, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return case


def mine(name: str, max_cases: int, max_checks: int, since: str) -> list[dict]:
    repo = clone_or_update(name, CANDIDATE_REPOS[name])
    # 必须回到默认分支：验证过程会把仓库留在 detached HEAD 的历史提交上，
    # 否则下次运行 git log 从旧提交开始，候选恒为空（实测踩过）
    _run(["git", "checkout", "--force", default_branch(repo)], cwd=repo, timeout=300)

    cases: list[dict] = []

    # 策略 A：修复提交同时补了回归测试（BEARS Case #2 模式）
    candidates_a = find_fix_commits(repo, since, max_checks, require_test=True)
    print(f"  [策略 A: 提交自带新测试] 候选 {len(candidates_a)} 个")
    for c in candidates_a:
        if len(cases) >= max_cases:
            return cases
        new_tests = extract_new_tests(repo, c)
        if not new_tests:
            continue
        parent = _git(repo, "rev-parse", f"{c['sha']}^").strip()
        for _file, test_class, test_method in new_tests[:3]:
            ok, payload = verify_case(repo, parent, c, test_class, test_method)
            print(f"    {c['sha'][:8]} {test_class}#{test_method} -> "
                  f"{'OK 有效' if ok else f'NG {payload}'}")
            if not ok or not passes_at_fix(repo, c["sha"], test_class, test_method):
                continue
            patch_text = build_test_patch(repo, parent, c)
            cases.append(_record_case(
                name, c, parent, test_class, test_method, payload, "new_test",
                test_patch=patch_text,
            ))
            break

    # 策略 B：项目**已有**测试在父提交上失败、修复提交后通过
    # （产出率显著更高：修复提交未必改测试，但项目测试往往会捕获该缺陷）
    # 注意：策略 A 的验证会把仓库留在 detached HEAD，必须先回到默认分支，
    # 否则这里的 git log 起点错误、候选恒为 0（同一坑踩了两次，明确记录）
    _run(["git", "checkout", "--force", default_branch(repo)], cwd=repo, timeout=300)
    candidates_b = find_fix_commits(repo, since, max_checks, require_test=False)
    print(f"  [策略 B: 项目已有测试捕获] 候选 {len(candidates_b)} 个")
    for c in candidates_b:
        if len(cases) >= max_cases:
            break
        parent = _git(repo, "rev-parse", f"{c['sha']}^").strip()
        related = find_related_tests(repo, c["main_files"])
        if not related:
            continue
        ok, payload, failing = verify_preexisting_test(repo, parent, related)
        label = f"{','.join(related)}"
        if not ok:
            print(f"    {c['sha'][:8]} {label} -> NG {payload}")
            continue
        assert failing is not None
        test_class, test_method = failing
        print(f"    {c['sha'][:8]} {test_class}#{test_method} -> OK 有效")
        if not passes_at_fix(repo, c["sha"], test_class, test_method):
            print("      NG 该测试在 fix 提交上仍失败（case 不自洽）")
            continue
        cases.append(_record_case(
            name, c, parent, test_class, test_method, payload, "preexisting"
        ))

    return cases


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", help="项目名（见 --list）")
    parser.add_argument("--all", action="store_true", help="挖掘全部候选项目")
    parser.add_argument("--max-cases", type=int, default=3, help="每个项目最多产出 case 数")
    parser.add_argument("--max-checks", type=int, default=25, help="每个项目最多检查的候选提交数")
    parser.add_argument("--since", default="2024-01-01",
                        help="只挖掘该日期之后的提交（旧提交常compile不过现代 JDK）")
    parser.add_argument("--list", action="store_true", help="列出候选项目")
    args = parser.parse_args()

    if args.list or (not args.repo and not args.all):
        print("候选项目：")
        for name, url in CANDIDATE_REPOS.items():
            print(f"  {name:<15} {url}")
        return

    CASES_DIR.mkdir(parents=True, exist_ok=True)
    targets = list(CANDIDATE_REPOS) if args.all else [args.repo]
    total: list[dict] = []
    for name in targets:
        if name not in CANDIDATE_REPOS:
            raise SystemExit(f"未知项目：{name}")
        print(f"\n=== {name} ===")
        try:
            total += mine(name, args.max_cases, args.max_checks, args.since)
        except Exception as e:  # noqa: BLE001 —— 单个项目失败不应中断整体挖掘
            print(f"  NG 挖掘失败：{type(e).__name__}: {e}")

    print(f"\n共产出 {len(total)} 个有效 case（{CASES_DIR}/）")
    for case in total:
        print(f"  {case['case_id']}")


if __name__ == "__main__":
    if sys.platform == "win32":
        pass
    main()
