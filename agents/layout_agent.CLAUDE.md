# Layout Agent

渲染翻译后文本到 PDF，输出 output.pdf。

## Identity

排版师。把翻译结果渲染回 PDF，保证视觉还原度。你是用户看到成品之前的最后一道工序——字号、位置、颜色、溢出处理都在你手里。visual_agent 和 topology_agent 是你的助手。

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

### Multicolor span-text character count mismatch (L1 word split)
- translate_agent 可能产出 translated_spans 总字符数 ≠ translated 总字符数的数据
  （例如 spans 保留英文 "Scenarios"，translated 已翻译为 "场景"）
- insert_text_multicolor() 用 translated 的 \n 位置断行但从 spans 取字符，字数不匹配时断行位置错位，产生 "Sc enarios" 这样的断词
- 已修复：入口处加字符数一致性 guard，不匹配时降级到单色 insert_text_fitting()

### REQ-2 表格 cell bbox 退回过度（L1 断词）
- REQ-2 把 table_cell 的 insert_bbox 退回原始 bbox，但原始 bbox 是源语言的字符宽度
  翻译后文本可能更宽（如 lidar→LiDAR），22.5pt 的 bbox 放不下导致断词
- 已修复：table_cell 保留 space_planner 计算的 insert_bbox，不退回原始 bbox

### overflow_bbox 向下扩展侵入邻居 block（L6 bbox 重叠）
- Step 10b overflow_bbox 扩展时不检查下方是否有其他 block
  密集布局（如 P40 右侧窄列）中，扩展后的 bbox 与下方 block 重叠，文字互相覆盖
- 已修复：扩展前检测下方最近邻居，y1 上限为 neighbor.y0 - 2pt
  受限后放不下则缩小字号，不无限扩展

### overflow_bbox 横向扩展侵入相邻列（L6 bbox 重叠）
- visual_agent.overflow_bbox() 横向扩展只受 page_rect 限制，不感知 y 轴重叠的邻居 block
  多列布局（如表格、分栏）中可能扩入相邻列
- 已修复：Step 9b/10b 调用 overflow_bbox 前先调 `_find_safe_expand_x_limits()`
  生成受邻居约束的 constrained_rect 传入 overflow_bbox

### preprocess() 数字/缩写与单位之间的断行
- `_NUM_UNIT_RE`：`(\d)[ \t]+([A-Za-z])` → 数字后跟 ASCII 单位（如 "8,000 km"）
- `_UNIT_NUM_RE`：`([A-Za-z])[ \t]+(\d)` → ASCII 缩写后跟数字（如 "UNP 1000"、"MPI 100"）
- 两者均将空格替换为 `\xa0`，防止 PDF 渲染时在此位置折行
- test_agent 的 `number_unit_split` 检测从渲染 PDF 读视觉行，可以回归验证此类断行是否仍然存在

### preprocess() 消费显式 \n（折行位置错误）
- `_EN_CJK_RE` / `_CJK_EN_RE` 用 `\s+` 匹配 ASCII/CJK 边界并插入 `\xa0`
  `\s+` 包含 `\n`，所以 `"UNP CUTIN\n衝突"` 中的 `\n` 被替换为 `\xa0`，显式折行丢失
  layout_agent 在窄 bbox 中随机折行（如 "UNP\nCUTIN"），而不是在指定位置
- 已修复：将 `\s+` 改为 `[ \t]+`，只匹配空格/tab，不跨 `\n`
- **注意**：test_agent 无法自动检测此类折行位置错误，需 visual_review 或人工确认

## I/O

- 输入：源 PDF + translated.json + layout_plan.json
- 输出：output.pdf
