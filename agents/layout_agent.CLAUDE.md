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

### Newline-unaware em-width estimation causing truncation
- **Root cause**: `estimate_em_width()` treated `\n` as a 0.55-em character instead of
  a forced line break.  `_find_fitting_size()` (CJK path) and Step 10b overflow check
  both used this estimate, so they under-counted visual lines for multi-line text.
- Example: text "line1\nlong_line2" at width W — the old code counted total em width
  as one stream and divided by chars_per_line, missing that `\n` forces a new line
  even when the first line has unused space.
- **Fix**: added `_estimate_lines_needed()` that splits text on `\n`, estimates wrapped
  lines per segment, then sums.  Used in both `_find_fitting_size` CJK path and Step 10b.

### Fullwidth punctuation under-counted in em-width estimation
- `estimate_em_width()` only counted CJK ideographs (U+3000-9FFF etc.) as 1.0 em.
  Fullwidth punctuation like `：` (U+FF1A), `（` (U+FF08), `）` (U+FF09) are in
  U+FF01-FF60 range and were counted as 0.55 em, but CJK fonts render them at full width.
- This caused width underestimation → fewer lines predicted → insufficient bbox expansion.
- **Fix**: added `_is_fullwidth()` helper covering U+FF01-FF60 and U+FFE0-FFE6 ranges.
  Used consistently in `estimate_em_width`, `_estimate_text_width`, `_wrap_char_colors`.

### overflow_bbox tight fit causing glyph clipping
- `overflow_bbox` binary-searches for the minimum bbox height where `insert_textbox`
  returns rc >= 0.  But rc >= 0 means "last baseline fits", not "all glyph pixels fit".
  When the last line has minimal remaining space (rc ≈ 0.05px), the character descenders
  or glyph edges may be visually clipped.
- **Fix**: added `target_size * 0.3` safety margin to the final expanded height in
  `visual_agent.overflow_bbox`, accounting for descender height.

### Text color contrast on dark backgrounds
- topology_agent detects container rects AND their fill colors per block
- Container colors flow through two paths:
  1. **Precomputed**: space_planner writes `container_color` (optional) into layout_plan.json cells
  2. **Live fallback**: topology_agent.analyze() returns `container_colors` in TopologyResult
- In Step 11, `visual.adjust_color(src_color, bg_color)` uses the container fill color
  to flip dark-on-dark text to white, or white-on-light text to black
- Color format: RGB float tuples (0-1), same space as PyMuPDF `page.get_drawings()` fill values
- All three adjust_color call sites updated: single-color, translated_spans, and color_spans paths

## I/O

- 输入：源 PDF + translated.json + layout_plan.json
- 输出：output.pdf
