# PDF Translator — 给下一个 Claude 的交接文件

## 这是什么

把 PDF（主要是幻灯片）从一种语言翻译成另一种语言，保留原始排版。
用 Claude CLI 做翻译和分析，PyMuPDF 做解析和渲染。

## 当前 Pipeline

```
parse_agent → consolidator → [intel_agent] → translate ∥ space_planner → render_agent → qa_agent
```

- `parse`、`consolidate`、`intel`、`render`、`qa` 是顺序的
- `translate` 和 `space_planner` 是并行的（intel_agent 设计的）
- 入口：`run_pipeline.py`

## 各 Agent 职责

| Agent | 目录 | 做什么 | 用 LLM？ |
|-------|------|--------|----------|
| parse_agent | pdf_translator_parse/ | 从 PDF 抽文本块 → parsed.json | ❌ |
| consolidator | pdf_translator_parse/ | 合并碎片文本块 → 覆写 parsed.json | ❌ |
| intel_agent | pdf_translator_architect/ | 分析文档，生成 workflow plan → plan.json | ✅ |
| translate_agent | pdf_translator_translate/ | 翻译文本块 → translated.json | ✅ |
| space_planner | pdf_translator_layout/ | 预计算 topology/Voronoi（不依赖译文）| ❌ |
| layout_agent (render) | pdf_translator_layout/ | 字号 fitting + 渲染 → output.pdf | ❌ |
| qa_agent | pdf_translator_qa/ | 检查覆盖率和翻译质量 → qa_report.json | ❌ |

## Agent 合约（I/O Schema）

所有合约定义在 `contracts/` 目录，agent 用 `contracts/validate.py` 自验证输出。

| Schema 文件 | 生产者 | 消费者 |
|-------------|--------|--------|
| `parsed.schema.json` | parse_agent, consolidator | consolidator, translate_agent, space_planner |
| `consolidator_log.schema.json` | consolidator | intel（宪兵） |
| `translated.schema.json` | translate_agent | render_agent, qa_agent |
| `layout_plan.schema.json` | space_planner | render_agent |
| `qa_report.schema.json` | qa_agent | intel（宪兵） |

**规则：agent 可以自由改内部算法，但输出必须通过自己的 schema 验证。**
I/O 合约变更只由 Claude Code（宰相）决定。

## 关键架构决策（别改错）

**1. layout 拆成两阶段**
layout 依赖译文（字号 fitting 要知道文字有多长），但 topology 分析不依赖译文。
所以拆成：space_planner（并行） + render（顺序）。
不要把它们合并回去。

**2. intel_agent 只读+只写 plan，不执行**
原名 architect_agent，改名因为真正的架构决策由 Claude Code（这个对话）+ CLAUDE.md 承担。
intel_agent 做的是文档情报分析：统计特征、领域识别、术语提取、workflow 参数推荐。
发现问题 → 写进 plan.json → run_pipeline.py 决定是否执行。
能激活 registry 里的可选 agent，不能生成代码、不能修改其他 agent。

**3. merge 属于 parse，不属于 layout**
文本碎片合并要在翻译之前完成，翻译需要语义完整的输入。
layout 里现有的 `_merge_adjacent_blocks()` 是历史遗留，待迁移到 consolidator。

## 渲染质量已知问题（客户反馈，待修）

优先级从高到低：

**🔴 P2｜layout_agent — 背景图片被 bbox 遮蔽**
redact 时 `redact_bboxes` 覆盖到了背景图像区域。
要求：redact 前检测 bbox 与 image/drawing obstacle 的重叠面积；超过阈值（30%）时
改用透明 redact 或只消除文字层，不破坏背景。

**🔴 P2｜layout_agent — 表格单元格消失**
横向相邻块（table cell）被 consolidator 正确保留，但 render 阶段 Voronoi insert_bbox
扩展到相邻单元格范围，导致实际绘制面积为零或超出 clip 范围。
要求：table cell（`_has_horizontal_neighbor()` 检出）的 insert_bbox 上限锁定为原始 bbox，
不做 Voronoi 扩张；译文为空时以原文 fallback 渲染，单元格不得消失。

**🟡 P3｜VisualOptimizer — 并列块字号/颜色不统一**
同页内 x0 或 y0 相近的并列块各自独立 fitting，导致视觉上应该一致的兄弟块字号和颜色不同。
要求：检测并列组（同 x0±阈值 或同 y0±阈值 的多块）；组内统一为 min(fitting_size)，
颜色统一为组内代表色。

**🟡 P3｜VisualOptimizer — 本文块被提升为标题样式**
`consistency_map()` 按 base_size 分组，但本文块偶尔落入标题 base_size 组被放大。
要求：title_mask 为 False 的块不得超过页内非标题块的最大 fitting_size；
title 组和 body 组的 cap 分开计算，互不干涉。

## 还没做的（TODO）

- [x] **consolidator**：已实现。`_ends_hard` 对 bullet point 过于保守，待调参。
- [x] **space_planner.py**：已实现（pdf_translator_layout/space_planner.py）。
- [ ] **agent registry**：intel_agent 激活可选 agent 的机制设计好了但没实现。
- [ ] **QA → 重翻闭环**：QA 发现问题后自动重翻失败的块。
- [ ] **上述4个渲染质量问题**：见"渲染质量已知问题"节。

## 测试文件

- 成果物4：`/Users/qirui/Downloads/【成果物4】ワールドモデルについての補足説明.pdf`（8页，ja→zh）
- 成果物3：`/Users/qirui/Downloads/【成果物3】ギャップ分析及びプロポーザル.pdf`（88页，en→ja）
  workdir: `/Users/qirui/Downloads/pipeline_3/`

## 代码里值得注意的地方

- `pdf_translator_*/` 四个目录结构重复，是历史演化产物，根目录的 `pdf_translator.py` 是更早的单体版本
- Prompt 已升级为商业级，在 translate_agent.py
- visual_agent.py は layout_agent.py が import するヘルパーモジュール（独立プロセスではない）
- font 自動切替：target language → CJK probe で主要スクリプトカバレッジを確認してから採用
- architect 跑真实子进程，它调用的 claude 和你不是同一个实例，没有上下文共享
