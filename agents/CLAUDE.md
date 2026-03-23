# Agents 共通规则

每个 agent 独立维护自己的实现，只要 I/O 合约不变可以自由修改内部算法。

## 设计原则

- 忠实于原文：原文的 bbox、颜色、字体大小等属性要尽量保持
- 不拆分原文 bbox：parser 产生的碎片可以合并，但不能拆分 PDF 原生的 block
- 颜色是 per-span 的：一个 block 内可以有多种颜色，颜色变化发生在语义边界
- I/O 合约在 contracts/ 定义，所有 agent 通过 contracts/validate.py 自校验

## 数据流

```
parse_agent → consolidator → translate_agent ∥ space_planner → layout_agent → test_agent
```

## 共通字段说明

- `color`: block 的 dominant color（RGB float [0,1]），用于 fallback
- `color_spans`: per-span 颜色信息 `[{"text": "...", "color": [r,g,b]}, ...]`，仅多色 block 输出
- `translated_spans`: 翻译后的 per-span 信息，translate_agent 输出

## layout_agent 渲染约束

- **Readability floor**: 8pt。当 fitting_size < 8pt 时，通过 `overflow_bbox` 扩展 bbox 而非缩小字号。
- **overflow_expanded 标记**: Step 9b 扩展过的 block 在 Step 11 渲染时使用 `min_factor=1.0`，防止 `insert_text_fitting` 的二次 fitting 将字号重新缩小到 floor 以下。
- **Step 9b expansion verification**: overflow_bbox 扩展后，用 `fitting_size` 验证文字在扩展 bbox 中能否以 8pt 渲染。若不能，回退到更小字号（最低 4pt），不标记 overflow_expanded。防止扩展不足时 Step 11 以 8pt 渲染导致文字消失。
- **Step 11 insert_text_fitting safety net**: 当 CJK em-width 估算的 fit_size 在实际 `insert_textbox` 中 rc < 0 时，用真实字体二分搜索更小字号（最低 4pt）。防止 em-width 近似误差导致 rendered_font_size=0。
- **Content overflow fallback** (Step 10b): `overflow_bbox` 先向下扩展、再向左右扩展。若扩展后仍放不下，回退缩小字号（允许低至 4pt）。截断文字比缩小字号更严重。
- layout_agent 的 bbox 扩展不会回写 translated.json，test_agent 需要从 PDF 读取实际渲染结果。

## test_agent readability_check 判定规则

- **text_too_small**: 阈值 7.5pt（非 8.0），容忍 PyMuPDF 浮点精度误差（8pt 渲染后读回可能为 7.99x）。
- **content_truncated**: 使用 PDF 中实际渲染的 font_size（而非 translated.json 的 source font_size）估算 bbox 容量，避免因 layout_agent 缩小字号后误判。面积 < 500px² 跳过，< 2000px² 降级为 warning，ratio > 3.0 且面积 >= 2000px² 才报 error。

## Lessons Learned (Bug Fixes)

### parse_agent: 合并不同颜色 block 时必须生成 color_spans

当两个相邻 block 各自是单色（没有 color_spans）但颜色不同时，合并后必须合成 color_spans。否则合并结果只保留一个 color，丢失另一个 block 的颜色信息。

规则：合并前检查两个 block 的 color 是否相同。不同时，即使双方都没有 color_spans，也要从各自的 text + color 合成 color_spans。

### translate_agent: 用占位符保护换行符过 LLM 边界

源文本中的 `\n` 序列化为 JSON 后变成 `\\n`，LLM 经常在输出中丢弃它们。不要依赖 LLM 忠实保留 JSON 转义字符。

修复方式：发送前将 `\n` 替换为可见占位符（`⏎`），LLM 返回后再还原。选择占位符时确保它不会出现在正常文本中。

### layout_agent: 禁止截断文字，必须缩放

文字溢出 bbox 时，绝对不能用 `...` 截断——截断会静默丢失内容。正确做法是缩小字号（最低 4pt）+ 自动换行。`_truncate_to_em_width` 方法是错误的，`_find_fitting_size` + wrapping 才是正确路径。

另外：`insert_text_multicolor` 会直接拼接各 span 的 text。如果多色 block 的 span 之间需要换行，`\n` 必须包含在 span text 的边界内（通常附加到前一个 span 的末尾）。否则拼接后换行会丢失，多行文字被挤到同一行。
