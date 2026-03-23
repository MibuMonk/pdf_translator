# Space Planner

文本无关的布局规划，输出 layout_plan.json。与 translate_agent 并行运行。

## 核心原则

- 只做几何计算（Voronoi/拓扑），不依赖翻译结果
- 输出 insert_bbox、snap_map、title_indices
- title_indices 基于 font_size 和页面位置检测

## 注意事项

- title_indices 必须和 translated.json 的 block 顺序一致（都基于 consolidator 输出的 parsed.json）
- 不携带颜色信息，颜色通过 block_id 回溯到 translated.json

## I/O

- 输入：源 PDF + parsed.json
- 输出：layout_plan.json（contracts/layout_plan.schema.json）
