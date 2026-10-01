# DevFix

**CI 失败自动诊断与修复 Agent** · Autonomous CI failure diagnosis & repair agent for Java / Maven projects

输入一份真实的构建失败日志，输出一个通过分层测试验证的**最小修复补丁**。
全程证据驱动——模型说"修好了"不算数，只有真实执行结果算数。

## Results

在自建 benchmark（15 个真实开源项目缺陷，挖掘自 commons-csv / commons-lang /
commons-text / jackson-core / jsoup 的修复提交）上，与 naive baseline
（同一模型、同一上下文、同一验证，单次调用无闭环）对比：

| Metric | DevFix (agent loop) | Baseline (one-shot) |
|---|---|---|
| 最终修复率 Final repair rate | **80% (12/15)** | 47% (7/15) |
| 首次尝试修复率 First-attempt rate | **73% (11/15)** | 47% (7/15) |
| 文件定位 Top-1 | 87% (13/15) | 80% (12/15) |
| 不安全修改率（触及测试代码）Unsafe modifications | 0% | 0% |
| 平均 LLM 调用 Avg LLM calls | 5.3 | 1 |
| 平均工具调用 Avg tool calls | 4.8 | 0 |
| 平均输入 / 输出 tokens | 75k / 5k | — |

成功率与成本同框呈现：agent 闭环的 +33pp 是用 5.3 次 LLM 调用与 75k 输入
tokens 买来的。

**为什么可信**：

- **修复判定不依赖模型自评**——分层验证（原失败用例 → 相关测试类 → 完整构建）
  全部通过才算 FIXED；判定语义对齐 SWE-bench（FAIL_TO_PASS + PASS_TO_PASS），
  测试结果以 Surefire XML 报告为权威来源
- **DevFix ⊇ baseline**：baseline 修好的 case 全部被 DevFix 修好，闭环无净损失；
  baseline 的失败多数是补丁应用失败（无自纠回路）——闭环机制的直接增量
- **已知局限**（诚实标注）：case 挖自近年公开修复提交，模型可能见过对应
  fix commit（靠同条件 baseline 对比缓解）；n=15 不做统计显著性声明

## How it works

```
triage ─→ initial_context ─→ ┌→ diagnosis ⇄ acquire（取证循环）
                             │       │                          ← 3 轮 / 8 次调用 / 120k 字符预算
                             │       │ DIAGNOSIS_READY             只读工具：search_code / read_file /
                             │       │ INSUFFICIENT → END            find_symbol / git_diff / changed_files
                             │       ▼
                             │  prepare_workspace（git worktree 隔离）
                             │       │
                             │   repair（补丁 + 证据账本）──→ policy 双门控 ──→ human_gate（HITL interrupt）
                             │       ↑  │                          │approve → verify
                             │       │  └─→ verify ──┬─ PASS ────┘reject → END
                             │       │        FAIL   └─→ reflect（假设更新 + 取证请求）
                             │       └───────────────────┘
                             └── 每轮尝试前回滚工作区（独立候选补丁语义）
```

LangGraph 状态机编排，节点为纯函数、LLM 能力全部依赖注入——**247 个离线测试
不需要任何 API key**。每个 Agent 能力都由 benchmark 失败案例驱动：

| 能力 | 由什么失败逼出来 |
|---|---|
| **Context Acquisition Loop** | 5 个失败中 3 个是"模型看不到根因源码只能空转放弃"——升级为假设驱动的取证：结构化 Hypothesis（三态）驱动 ContextRequest，只读工具执行后证据入账，预算耗尽在"继续/诚实停止"间做最终决策 |
| **Evidence Working Memory** | 修复阶段看不到诊断阶段取证的源码（断裂实测）——证据账本贯穿诊断/修复/反思，反思可更新假设状态、发起取证 |
| **语义无进展短路** | 连续空补丁时反思是 token-burning loop（环境无变化 = 无新证据）——确定性检测，2 次即转人工 |
| **Human-in-the-Loop** | 策略拒绝曾是死胡同——LangGraph interrupt 暂停，人 approve（覆盖策略）/ reject；headless 自动拒绝，benchmark 管道零改动 |
| **Repair Policy 双门控** | 模型幻觉：改测试让验证变绿、虚构 API、路径穿越（`src/main/../src/test/...`）、pom 层面禁测——应用前白名单 + 应用后 diff 复核 |
| **分层验证 + P2P 基线** | 环境既有失败测试会误杀已修好的补丁（实测）——buggy 提交上记录失败基线，判定"目标测试通过且无新增失败"；Surefire XML 为权威来源 |
| **Observability** | trace.jsonl 记录每次 LLM 调用（含 token usage）与工具调用；报告含证据账本；metrics 公开成本列 |

## Quick start

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"   # Linux/macOS: .venv/bin/python
devfix init                                        # 生成 devfix.yaml 并填写模型 ID
set DEEPSEEK_API_KEY=<your-key>                    # 或 ARK / OpenAI
devfix doctor                                      # 连通性检查
pytest -q                                          # 247 个离线测试（无需 API key）
```

修复一次真实失败：

```bash
devfix analyze <repo路径> --log <maven日志> --repair
# 产出 runs/<runId>/{report.md, run.json, trace.jsonl}
```

跑 benchmark（需 Maven + API key）：

```bash
python -m scripts.mine_cases --repo commons-csv   # 从真实修复提交挖掘 case
python -m bench.run_devfix                        # DevFix 全量评测
python -m bench.baseline                          # naive baseline
python -m bench.metrics --out bench/report.md     # 对比报告
```

## Benchmark methodology

- **bug 对定义**：父提交上目标测试真实失败 + 修复提交上通过（复现验证，非日志推断）；
  修复提交引入的新测试以 SWE-bench 式 test patch 预先注入评测环境
- **公平性**：DevFix 与 baseline 共享同一套 case、上下文收集与验证实现，
  唯一区别是"有没有 agent 闭环"
- **环境纯度**：Windows 检出统一 LF（`core.eol=lf` + 强制重写）、清空陈旧
  构建产物、失败基线排除与补丁无关的既有失败测试
- **验证**：真实 `mvn test`，分层 L1（原失败用例）→ L2（相关测试类）→
  L3（完整构建），逐层升级任一失败即停

## Project structure

```
devfix/
├── parsing/          # Maven 日志 / Surefire XML / javac 错误 → 结构化证据（无 LLM）
├── nodes/            # 诊断取证 / 修复 / 反思 / 验证 / 人工门控（纯函数节点）
├── graph/            # LangGraph 状态机（条件路由、interrupt、checkpointer）
├── tools/            # git worktree / 文件（路径越界防护）/ rg 搜索 / search-replace 补丁 / Maven
├── llm/              # 多 provider 工厂（DeepSeek/ARK/OpenAI）+ 提示词 + 结构化输出解析
├── repair/           # Repair Policy 双门控
├── verify/           # 分层验证 L1/L2/L3 + 失败基线
├── observability.py  # Agent Trace（LLM 调用含 token usage / 工具调用）
bench/                # 评测管道 + 15 个真实缺陷 case（含失败日志与 test patch）
scripts/              # case 挖掘（SWE-bench 式）+ 演示仓库生成
tests/                # 247 个离线测试（Fake 注入，无需 API key）
```


## License

[MIT](LICENSE)
