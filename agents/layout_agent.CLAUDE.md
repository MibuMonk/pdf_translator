# Layout Agent

渲染翻译后文本到 PDF，输出 output.pdf。

## 核心原则

- 忠实于原文颜色：优先用 `translated_spans`（per-span 颜色），fallback 到 `color_spans`，再 fallback 到 `color`
- 多色渲染用 `insert_text_multicolor()`，单色用 `insert_text_fitting()`
- 标题块（title）不参与 sibling normalization 和 consistency cap

## 踩过的坑

### CJK 小 bbox 标题消失
- visual_agent 的 `fitting_size` 有小 bbox shortcut，CJK 分支跳过了 `_fits()` 二分搜索
- 返回的字号太大（24pt），实际 `insert_textbox` 放不下（需要 ≤16pt），导致文字不渲染
- 已修复：CJK 文本始终走 `_fits()` 二分搜索，不走 shortcut

### LINE_HEIGHT_FACTOR
- 从 1.4 调整为 1.2，减少行间距，让字号不会被过度压缩
- `insert_textbox` 的 lineheight 参数对单行文本也有影响（PyMuPDF 行为），estimator 必须考虑

### 字号过于保守
- consistency_map percentile 从 0.80 调为 0.90
- min_size 从 4.0 调为 6.0

### 架构图页面字号过小 (text_too_small)
- P4/P5/P38/P40 等架构图页面有大量小 bbox 标注块，fitting_size 缩到 6-7pt
- sibling normalization (REQ-3) 取 min() 进一步拉低所有同组块的字号
- 修复：引入 _READABILITY_FLOOR=8pt
  - Step 9b：fitting_size < 8pt 时，用 `overflow_bbox` 向下扩展 insert_bbox，保持 8pt 字号
  - sibling normalization 的 min() 结果也 floor 到 8pt
- 策略：宁可溢出 bbox 也不用 <8pt 的不可读字号

### 翻译文本截断 (content_truncated)
- 翻译后文本（尤其中文）比原文长，bbox 放不下导致截断
- 修复：Step 10b 在渲染前检测溢出（em-width 估算），用 `overflow_bbox` 向下扩展 insert_bbox
- 注意：扩展受 page_rect 限制，不会超出页面边界

## I/O

- 输入：源 PDF + translated.json + layout_plan.json
- 输出：output.pdf
