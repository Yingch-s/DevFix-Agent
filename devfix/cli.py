"""DevFix 命令行入口。

- devfix init       生成 devfix.yaml 配置文件
- devfix doctor     检查配置与 LLM 连通性（验证 provider 抽象层可用）
- devfix parse-log  解析 Maven 日志 → FailureContext + Triage（离线）
- devfix analyze    诊断流程：Triage → 上下文收集 → Diagnosis（调用 LLM）
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel

from devfix import __version__
from devfix.config import ConfigError, load_config
from devfix.report import new_run_id, save_report

app = typer.Typer(
    name="devfix",
    help="CI 故障诊断与修复 Agent（Java / Spring Boot 项目）",
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)
console = Console()

EXAMPLE_CONFIG = """\
# DevFix 用户配置（本文件为私有配置，勿提交到 git）
llm:
  provider: deepseek       # ark | openai | deepseek
  model: deepseek-v4-pro   # 主力模型；便宜替代：deepseek-flash
  # base_url: ""           # 留空使用 provider 默认值
  # api_key_env: DEEPSEEK_API_KEY  # API Key 从该环境变量读取
  temperature: 0
  max_tokens: 8192
"""


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"devfix {__version__}")
        raise typer.Exit()


@app.callback()
def main_callback(
    version: bool = typer.Option(
        False, "--version", "-V", help="显示版本号并退出",
        callback=_version_callback, is_eager=True,
    ),
) -> None:
    """DevFix —— 面向 Java / Spring Boot 项目的 CI 故障诊断与修复 Agent。"""


@app.command("init")
def init_config(
    force: bool = typer.Option(False, "--force", "-f", help="覆盖已存在的配置文件"),
) -> None:
    """在项目根目录生成 devfix.yaml 配置文件。"""
    target = Path("devfix.yaml")
    if target.exists() and not force:
        console.print(f"[yellow]已存在 {target}，如需重新生成请加 --force[/yellow]")
        raise typer.Exit(1)
    target.write_text(EXAMPLE_CONFIG, encoding="utf-8")
    console.print(f"[green]已生成 {target.resolve()}[/green]")
    console.print("下一步：填写 model，并设置对应的环境变量（如 ARK_API_KEY），"
                  "然后运行 [bold]devfix doctor[/bold] 验证连通性。")


@app.command("doctor")
def doctor(
    config: Path | None = typer.Option(None, "--config", "-c", help="配置文件路径"),
) -> None:
    """检查配置与 LLM 连通性。"""
    from langchain_core.messages import HumanMessage

    from devfix.llm.provider import get_chat_model

    # 1. 配置加载
    try:
        cfg = load_config(config) if config else load_config()
    except ConfigError as e:
        console.print(Panel(f"[red]{e}[/red]", title="配置检查", border_style="red"))
        raise typer.Exit(1)

    llm = cfg.llm
    console.print(Panel(
        f"provider   : {llm.provider}\n"
        f"model      : {llm.model}\n"
        f"base_url   : {llm.effective_base_url or '（SDK 默认）'}\n"
        f"api_key_env: {llm.api_key_env}",
        title="配置检查", border_style="green",
    ))

    # 2. API Key
    try:
        llm.resolve_api_key()
    except ConfigError as e:
        console.print(Panel(f"[red]{e}[/red]", title="API Key", border_style="red"))
        raise typer.Exit(1)
    console.print(f"[green]✓[/green] API Key 已就绪（{llm.api_key_env}）")

    # 3. 连通性：发送最小请求
    console.print("正在发送测试请求…", style="dim")
    try:
        model = get_chat_model(llm)
        reply = model.invoke([HumanMessage(content="只回复两个字：正常")])
        text = reply.content if isinstance(reply.content, str) else str(reply.content)
        console.print(Panel(
            f"[green]✓ LLM 连通[/green]\n\n模型回复：{text.strip()[:200]}",
            title="连通性检查", border_style="green",
        ))
    except Exception as e:  # noqa: BLE001 —— doctor 需要捕获一切异常并友好展示
        console.print(Panel(
            f"[red]✗ LLM 调用失败[/red]\n\n{type(e).__name__}: {e}",
            title="连通性检查", border_style="red",
        ))
        raise typer.Exit(1)


@app.command("parse-log")
def parse_log(
    path: Path = typer.Argument(..., help="Maven 构建日志文件路径"),
) -> None:
    """解析 Maven 日志，输出 FailureContext 与 Triage 结果（离线，不调用 LLM）。"""
    from devfix.parsing import parse_maven_log

    if not path.exists():
        console.print(f"[red]日志文件不存在：{path}[/red]")
        raise typer.Exit(1)
    text = path.read_text(encoding="utf-8", errors="replace")
    ctx, tri = parse_maven_log(text, raw_log_path=str(path))
    console.print("[bold]FailureContext[/bold]")
    console.print_json(data=ctx.model_dump(mode="json"))
    console.print("[bold]TriageResult[/bold]")
    console.print_json(data=tri.model_dump(mode="json"))


def _prompt_human_decision(payload: dict) -> dict:
    """HITL 人工裁决界面（Agent Engineering V0.2 §6）。

    展示根因、证据摘要、补丁 diff 与拒绝原因，返回 resume 决策。
    空补丁（cannot_fix）没有可应用的内容，只允许 reject。
    """
    cannot_fix = bool(payload.get("cannot_fix"))
    body = (
        f"触发原因：{payload.get('gate_reason')}\n\n"
        f"根因：{payload.get('root_cause')}（置信度 {payload.get('confidence')}）\n"
        f"补丁：{payload.get('patch_id') or '（无）'}，涉及："
        f"{', '.join(payload.get('patch_files') or []) or '（无文件）'}\n"
    )
    if cannot_fix:
        body += f"模型自述无法修复：{payload.get('cannot_fix_reason')}\n"
    if payload.get("evidence_preview"):
        body += "\n证据摘要：\n  - " + "\n  - ".join(payload["evidence_preview"])
    console.print(Panel(
        body, title=f"人工裁决 · 尝试 {payload.get('attempt')}",
        border_style="yellow",
    ))
    if payload.get("patch_diff"):
        console.print(payload["patch_diff"])
    hint = "reject（空补丁无可批准内容）" if cannot_fix else "approve/reject"
    while True:
        answer = typer.prompt(f"裁决（{hint}）", default="reject").strip().lower()
        if answer in ("approve", "a") and not cannot_fix:
            return {"action": "approve"}
        if answer in ("reject", "r"):
            return {"action": "reject"}
        console.print("[red]请输入 approve 或 reject[/red]")


@app.command("analyze")
def analyze(
    repo: Path = typer.Argument(..., help="目标 Java 仓库路径"),
    log: Path = typer.Option(..., "--log", "-l", help="Maven 构建日志文件"),
    base: str = typer.Option("HEAD~1", "--base", "-b", help="git diff 基线引用"),
    repair: bool = typer.Option(
        False, "--repair", help="诊断后继续执行修复-验证-反思循环（隔离工作区）"
    ),
    max_attempts: int = typer.Option(3, "--max-attempts", help="最大修复尝试次数"),
    config: Path | None = typer.Option(None, "--config", "-c", help="配置文件路径"),
) -> None:
    """对仓库 + 构建日志执行诊断；--repair 时跑完整修复闭环。

    闭环：Triage → 上下文收集 → Diagnosis → 补丁 → 隔离应用 →
          分层验证 → 失败则反思 → 再修复（至多 max-attempts 次）。
    """
    from devfix.graph import build_diagnosis_graph, build_repair_graph, invoke_config
    from devfix.llm.provider import get_chat_model
    from devfix.models import VerificationLevel
    from devfix.nodes.diagnosis import LLMDiagnoser
    from devfix.nodes.reflection import LLMReflector
    from devfix.nodes.repair import LLMPatcher
    from devfix.verify import MavenVerifier

    cfg = load_config(config) if config else load_config()
    if not (repo / ".git").exists():
        console.print(f"[red]不是 git 仓库：{repo}[/red]")
        raise typer.Exit(1)
    if not log.exists():
        console.print(f"[red]日志文件不存在：{log}[/red]")
        raise typer.Exit(1)

    log_text = log.read_text(encoding="utf-8", errors="replace")
    model = get_chat_model(cfg.llm)
    diagnoser = LLMDiagnoser(model)

    if repair:
        from langgraph.checkpoint.memory import MemorySaver

        vcfg = cfg.verification
        graph = build_repair_graph(
            diagnoser=diagnoser,
            patcher=LLMPatcher(model),
            reflector=LLMReflector(model),
            verifier=MavenVerifier(
                mvn=vcfg.maven_command,
                timeouts={
                    VerificationLevel.FOCUSED: vcfg.focused_timeout_s,
                    VerificationLevel.RELATED: vcfg.related_timeout_s,
                    VerificationLevel.FULL: vcfg.full_timeout_s,
                },
                extra_args=vcfg.effective_maven_args(),
            ),
            # HITL（V0.2 §6）：交互模式开启人工门控——checkpointer 支撑
            # interrupt 的暂停/恢复；benchmark/headless 不传即无中断
            checkpointer=MemorySaver(),
        )
    else:
        graph = build_diagnosis_graph(diagnoser)

    mode = "修复闭环" if repair else "诊断"
    run_id = new_run_id()
    run_dir = Path("runs") / run_id
    console.print(f"[dim]正在运行{mode}（{cfg.llm.provider}/{cfg.llm.model}）… {run_id}[/dim]")
    initial = {"repo": str(repo), "log_text": log_text, "base": base}
    config = invoke_config(max_attempts if repair else 1)
    if repair:
        initial["max_attempts"] = max_attempts
        initial["workspace_dir"] = str(run_dir)  # 工作区落在 Run 目录内
        initial["hitl_enabled"] = True
        config["configurable"] = {"thread_id": run_id}
    final = graph.invoke(initial, config)

    # HITL 循环：human_gate 暂停 → 展示决策材料 → 人工裁决 → 恢复执行
    from langgraph.types import Command

    while isinstance(final, dict) and final.get("__interrupt__"):
        payload = final["__interrupt__"][0].value
        final = graph.invoke(
            Command(resume=_prompt_human_decision(payload)),
            config,
        )

    if final.get("error"):
        console.print(Panel(
            f"[red]{final['error']}[/red]", title="运行失败", border_style="red",
        ))
        raise typer.Exit(1)

    tri = final["triage"]
    console.print(Panel(
        f"故障类型：{tri.failure_type.value}\n"
        f"关键异常：{tri.key_error or '无'}\n"
        f"置信度：{tri.confidence}\n"
        f"依据：{tri.reason}",
        title="Triage", border_style="cyan",
    ))
    console.print(Panel(
        f"代码片段：{len(final['context_bundle'].snippets)} 段；"
        f"diff：{len(final['context_bundle'].git_diff)} 字符",
        title="上下文收集", border_style="cyan",
    ))
    console.print("[bold]Diagnosis[/bold]")
    console.print_json(data=final["diagnosis"].model_dump(mode="json"))

    if repair:
        _print_repair_loop(final)

    # 结构化报告落盘（设计文档 17 节）
    out_dir, saved = save_report(final, output_root="runs", run_id=run_id, repo_path=repo)
    console.print(Panel(
        f"结果：{saved.final_status}\n"
        f"报告：{out_dir / 'report.md'}\n"
        f"数据：{out_dir / 'run.json'}",
        title=f"Repair Report · {run_id}", border_style="cyan",
    ))


def _print_repair_loop(final: dict) -> None:
    """打印修复闭环的完整叙事：每次尝试的补丁 / 策略 / 验证 / 反思。"""
    from devfix.models import VerificationStatus

    attempts = final.get("attempts", [])
    for record in attempts:
        verdict_detail = ""
        if record.verdict is not None:
            verdict_detail = (
                f"策略：{record.verdict.decision.value}\n"
                + "\n".join(f"  - {r}" for r in record.verdict.reasons)
            )
        edits_detail = ""
        if record.patch is not None and record.patch.edits:
            edits_detail = "\n涉及文件：" + "、".join(e.file for e in record.patch.edits)
        console.print(Panel(
            f"补丁 {record.patch.patch_id if record.patch else '—'}\n"
            f"修改原因：{record.patch.reason if record.patch else '—'}\n"
            f"预期效果：{record.patch.expected_effect if record.patch else '—'}"
            f"{edits_detail}\n{verdict_detail}"
            + (f"\n无法修复：{record.patch.cannot_fix_reason}" if record.patch and record.patch.cannot_fix_reason else ""),
            title=f"尝试 #{record.attempt} · 补丁", border_style="blue",
        ))
        if record.patch and record.patch.diff:
            console.print(f"[dim]{record.patch.diff}[/dim]")
        if record.verification is not None:
            color = "green" if record.verification.passed else "red"
            # 逐层展示：L1 聚焦 → L2 相关 → L3 完整（只跑到第一层失败为止）
            lines = [
                f"{v.level.value} → {v.status.value}"
                f"（{v.duration_ms / 1000:.1f}s，退出码 {v.exit_code}）"
                for v in (record.verifications or [record.verification])
            ]
            v = record.verification
            if v.failed_tests:
                lines.append("失败用例：" + "；".join(
                    f"{t.display_name} - {t.error_type}: {t.message}" for t in v.failed_tests
                ))
            elif v.new_errors:
                lines.append("错误：" + "；".join(v.new_errors[:3]))
            console.print(Panel(
                "\n".join(lines),
                title=f"尝试 #{record.attempt} · 验证", border_style=color,
            ))
        if record.reflection is not None:
            r = record.reflection
            console.print(Panel(
                f"补丁为何失败：{r.failure_reason}\n"
                + ("新证据：\n" + "\n".join(f"  - {e}" for e in r.new_evidence) if r.new_evidence else "")
                + (f"\n修订后根因：{r.updated_root_cause}" if r.should_update_diagnosis and r.updated_root_cause else "")
                + f"\n下一步：{r.next_action.value}",
                title=f"尝试 #{record.attempt} · 反思", border_style="magenta",
            ))

    verification = final.get("verification")
    if verification is not None and verification.passed:
        status, color = "FIXED", "green"
    elif final.get("stop_reason"):
        status, color = f"STOPPED（{final['stop_reason']}）", "yellow"
    else:
        status, color = "FAILED_TO_FIX", "red"
    console.print(Panel(
        f"{status}\n\n尝试次数：{final.get('attempt', 0)}/{final.get('max_attempts', 0)}\n"
        f"隔离工作区：{final.get('workspace', '—')}\n"
        f"清理：git worktree remove --force {final.get('workspace', '')}",
        title="Repair Run 结果", border_style=color,
    ))
    if verification is not None and not verification.passed:
        for result in final.get("verification_results", []):
            if result.status is VerificationStatus.FAIL:
                console.print(f"[dim]最后一次失败输出（尾部）：\n{result.log_tail[-1500:]}[/dim]")
                break

