# PDF Translator

Translates PDFs (mainly slide decks) between languages while preserving layout.
Uses Claude CLI for translation/analysis, PyMuPDF for parsing and rendering.

## Role of This Thread

- Defines I/O contracts, writes requirements, delegates implementation to agents
- Maintains CLAUDE.md
- Does NOT directly edit agent implementation code

Working agents own their implementation. They may freely change internal algorithms as long as output passes schema validation. I/O contract changes require coordinator approval.

## Pipeline

```
parse_agent → consolidator → translate ∥ space_planner → layout_agent → test_agent
```

- `translate` and `space_planner` run in parallel; all others sequential
- Entry point: `run_pipeline.py`

## Agents

| Agent | Directory | Output | LLM? |
|-------|-----------|--------|------|
| parse_agent | agents/ | {stem}.parsed.json | No |
| consolidator | agents/ | {stem}.parsed.json (overwrite) | No |
| translate_agent | agents/ | {stem}.translated.json | Yes |
| space_planner | agents/ | layout_plan.json | No |
| layout_agent | agents/ | output.pdf | No |
| test_agent | agents/ | test_report.json | No |

`visual_agent.py` is not a standalone process — it is a helper module (VisualOptimizer) imported by layout_agent.py.
`topology_agent.py` is not a standalone process — it is a helper module (TopologyAnalyzer) imported by layout_agent.py and space_planner.py.

## I/O Contracts

All schemas in `contracts/`. Agents self-validate using `contracts/validate.py`.

| Schema | Producer | Consumer |
|--------|----------|----------|
| parsed.schema.json | parse_agent, consolidator | consolidator, translate_agent, space_planner |
| consolidator_log.schema.json | consolidator | (informational) |
| translated.schema.json | translate_agent | layout_agent, test_agent |
| layout_plan.schema.json | space_planner | layout_agent |
| test_report.schema.json | test_agent | (final output) |

## Architectural Constraints

**Layout is split into two phases — do not merge back.**
Topology/Voronoi (space_planner) is text-independent and runs in parallel. Font fitting (render) needs translated text and runs after.

**Text merging belongs in parse, not layout.**
Consolidator must produce semantically complete blocks before translation. layout_agent's legacy `_merge_adjacent_blocks()` is a historical artifact pending migration to consolidator.

## Codebase Notes

- `agents/` directory contains all pipeline agents
- Font auto-switcher: validates CJK coverage with a probe string; excludes LastResort.otf and placeholder fonts
- test_agent has two modes: pipeline QA mode (--json/--pdf flags) and testcase regression mode (--testcase flag)
- `agents/shared_utils.py` contains shared helpers (`has_cjk`, `cluster`) imported by layout_agent, visual_agent, test_agent, and space_planner

## Agent 架构设计

**常驻核心 agent**：parse、consolidate、translate、space_planner、layout、test——每次 pipeline 都跑，不变。

**按需专项 agent**（coordinator 根据 test_report 决定是否招募）：

| Agent | 触发条件 | 职责 |
|-------|---------|------|
| retry_agent | test_agent 标记翻译质量不合格的块 | 对指定 block 重新翻译，局部重渲染 |
| term_agent | 文档属于高度专业化技术领域 | 预提取领域术语和缩写，生成词表注入 translate_agent prompt |
| batch_agent | 文档超过 50 页 | 将 translate 步骤拆分并行分段处理 |

**治理规则**：
- 专项 agent 由 coordinator 招募，不自主触发其他 agent
- 反馈环不在 agent 内部闭合，决策权在 coordinator
- 只招聘有明确需求的 agent，不为假设需求预建
- 所有新 agent 的 I/O 合约先定义，再实现

## TODO

- [x] consolidator: `_ends_hard` too conservative for bullet points — fixed with `_should_block_merge_on_ending()` context-aware check
- [ ] agent registry: mechanism for activating optional agents is designed but not implemented
- [ ] QA → re-translate loop: automatic re-translation of blocks flagged by test_agent coverage_check

## Test Files

All test data lives under `testdata/`. Structure: `source.pdf`, `work/` (intermediate files), `output.pdf`.
Work files and intermediate outputs are gitignored (regenerable). Only `baseline/` is version-controlled.

| Name | Pages | Direction | Notes |
|------|-------|-----------|-------|
| 成果物1 | ~68 | ja→zh | Primary test case. Also has work_ja/ (zh→ja reverse) |
| 成果物3 | 88 | en→ja | Legacy work files (old naming: parsed.json) |
| 成果物4 | 8 | ja→zh | Has regression baseline/ (committed) |
