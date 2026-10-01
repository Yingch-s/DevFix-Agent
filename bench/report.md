# DevFix Benchmark

- 总 case 数：15
- 项目：commons-csv, commons-lang, commons-text, jackson-core, jsoup
- 对比口径：两方法均完成评测的 15 个 case 的交集

## 指标对比

| 指标 | DevFix（agent 闭环） | Baseline（一次调用） |
|---|---|---|
| Case 数 | 15 | 15 |
| 最终修复率 | 80% (12/15) | 47% (7/15) |
| 首次修复率 | 73% (11/15) | 47% (7/15) |
| 平均尝试次数 | 1.23 | 1.00 |
| Top-1 文件定位 | 87% (13/15) | 80% (12/15) |
| Top-3 文件定位 | 87% (13/15) | 80% (12/15) |
| 不安全修改率 | 0% (0/15) | 0% (0/15) |
| 平均 LLM 调用 | 5.3 | — |
| 平均工具调用 | 4.8 | — |
| 平均输入 tokens | 75021.7 | — |
| 平均输出 tokens | 4977.0 | — |
| 平均耗时 | 100s | 47s |
| 耗时中位数 | 78s | 10s |

## 逐 case 结果

| case | 项目 | DevFix | 尝试 | 首次修复 | Top-1 | Baseline |
|---|---|---|---|---|---|---|
| commons-csv-1d89cd5-testGetBytePositionMultiCharacterDelimiterWithSupplementaryCharacter | commons-csv | STOPPED | 3 | ❌ | ✅ | FAILED_TO_FIX |
| commons-csv-23eb602-testGetBytePositionMultiCharacterDelimiter | commons-csv | ✅ | 1 | ✅ | ✅ | PATCH_APPLY_FAILED |
| commons-csv-9a33a61-testPrintWithEscapesReaderLargeValueIsLinear | commons-csv | ✅ | 2 | ❌ | ✅ | PATCH_APPLY_FAILED |
| commons-lang-55dcbc4-testTimeZoneCacheKeyIsCopied | commons-lang | ✅ | 1 | ✅ | ✅ | ✅ |
| commons-lang-8248377-testGetMatchingAccessibleConstructorOnNonPublicClass | commons-lang | ✅ | 1 | ✅ | ✅ | ✅ |
| commons-lang-fa24703-testCachedHashCodeRecomputed | commons-lang | STOPPED | 0 | ❌ | ❌ | PATCH_APPLY_FAILED |
| commons-text-c68fe3c-testFenceRootWithParentSegment | commons-text | ✅ | 1 | ✅ | ✅ | PATCH_APPLY_FAILED |
| commons-text-e2eb7d1-testCsvUnEscaperSingleQuoteTest | commons-text | ✅ | 1 | ✅ | ✅ | ✅ |
| commons-text-f65ff30-testContainsAllWordsWithNewline | commons-text | ✅ | 1 | ✅ | ✅ | ✅ |
| jackson-core-9958a94-rejectSurrogateD800InSkippedString | jackson-core | STOPPED | 0 | ❌ | ❌ | PATCH_APPLY_FAILED |
| jackson-core-e300169-unexpectedSupplementaryValueQuotesFullCodePoint | jackson-core | ✅ | 1 | ✅ | ✅ | EMPTY_PATCH |
| jackson-core-fe0da01-usesOriginalMessageOfTargetJacksonException | jackson-core | ✅ | 1 | ✅ | ✅ | ✅ |
| jsoup-34fb153-testTagStartsWithToString | jsoup | ✅ | 1 | ✅ | ✅ | PATCH_APPLY_FAILED |
| jsoup-385219d-canConvertToCustomDocument | jsoup | ✅ | 1 | ✅ | ✅ | ✅ |
| jsoup-bcbac42-testCloneCopyTagSet | jsoup | ✅ | 1 | ✅ | ✅ | ✅ |

## 口径说明

- case 来源：`scripts/mine_cases.py` 从真实开源项目修复提交中挖掘，要求「父提交上目标测试真实失败 + 修复提交上通过」；
- 修复判定：分层验证（原失败用例 → 相关测试类 → 完整构建）全部通过才算 FIXED，不接受模型自评；
- baseline：同一模型、同一上下文、同一验证命令，但不做诊断/策略/反思、只尝试一次；
- 不安全修改率：补丁触及 src/test 下的测试文件（改测试让验证变绿）。DevFix 的策略禁止改测试，baseline 无策略——该列显性化两者的诚信差异；
- Top-K 定位：诊断给出的相关文件（按顺序）是否覆盖修复提交改动的生产代码文件。
