# Consolidator

合并 parse_agent 产生的碎片 block，输出覆盖 parsed.json。

## 核心原则

- 只合并 parser 产生的碎片，不合并语义上独立的 block
- **颜色不同的 block 不合并**：`_color_compatible()` 检查每个分量差异 < 0.05
- 合并时 color_spans 要正确拼接，相邻同色 span 合并文本

## 踩过的坑

- 曾经合并时用 `**a` 展开，第二个 block 的颜色被丢弃。已修复：合并前检查颜色兼容性。
- `_ends_hard` 对 bullet point 过于保守（stops merging on period），TODO 待优化

## I/O

- 输入：parsed.json
- 输出：parsed.json（原地覆盖，同 schema）
