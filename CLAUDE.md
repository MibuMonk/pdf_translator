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

## 还没做的（TODO）

- [x] **consolidator**：已实现（pdf_translator_parse/consolidator.py）。
      已知问题：`_ends_hard` 对幻灯片子弹点过于保守（句号结尾就停止合并），
      导致同列的 Planning/Perception 子弹点没有并入标题块。待调参。
- [ ] **space_planner.py**：文件还不存在，layout_agent.py 里的 topology 逻辑待拆出。
- [ ] **agent registry**：architect 激活可选 agent 的机制设计好了但没实现。
- [ ] **QA → 重翻闭环**：QA 发现问题后自动重翻失败的块。

## 测试文件

`/Users/qirui/Downloads/【成果物4】ワールドモデルについての補足説明.pdf`
8页，自动驾驶/AI 领域，日→中。architect 跑过，plan.json 在 /tmp/pipeline_test/。

## 代码里值得注意的地方

- `pdf_translator_*/` 四个目录结构重复，是历史演化产物，根目录的 `pdf_translator.py` 是更早的单体版本
- Prompt 已升级为商业级（本 session 改的），在 translate_agent.py 和 review_translation.py
- architect 跑真实子进程，它调用的 claude 和你不是同一个实例，没有上下文共享
