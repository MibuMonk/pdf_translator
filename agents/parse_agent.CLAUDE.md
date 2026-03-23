# Parse Agent

从 PDF 提取文本 block，输出 parsed.json。

## 核心原则

- **不拆分原文 bbox**：PyMuPDF 返回的一个 block 就是一个 block，即使内部有多色/多字体也不拆分
- 颜色取 dominant color（字符数最多的 span 的颜色）
- 多色 block 输出 `color_spans`：记录每个 span 的实际文本和颜色，相邻同色 span 合并
- 散乱块（scattered lines）按原有逻辑合并，但也记录 color_spans

## 踩过的坑

- 曾经按颜色/字体大小拆分 block，导致 block 数暴增、下游 space_planner 的 title_indices 错乱、标题消失。已回滚。原则：只合并碎片，不拆分原文。
- `color_from_int()` 转换 PyMuPDF 的 int 颜色为 RGB float tuple，注意 span["color"] 是 int 不是 tuple

## I/O

- 输入：PDF 文件路径
- 输出：parsed.json（contracts/parsed.schema.json）
