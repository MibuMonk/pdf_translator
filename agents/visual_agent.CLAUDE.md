# Visual Agent

layout_agent 的辅助模块，处理字号拟合、一致性归一化、颜色调整。

## 核心原则

- `fitting_size`：二分搜索找最大能放入 bbox 的字号
- CJK 文本必须走 `_fits()` 实际渲染测试，不能用估算 shortcut
- `consistency_map`：90th percentile cap，标题块不受 cap
- `adjust_color`：背景色对比度保证。background_color 来自 topology_agent 的 container fill color（RGB 0-1 float tuple）。dark bg + dark text → white; light bg + white text → black。luminance 阈值：bg < 0.3 为 dark，bg > 0.7 为 light

## 踩过的坑

- 小 bbox shortcut 对 CJK 估算错误：`bbox.height >= base_size` 判断没考虑 lineheight，导致返回过大字号，insert_textbox 实际放不下，文字消失。已修复：CJK 不走 shortcut。
- LINE_HEIGHT 常量必须和 layout_agent 的 _LINE_HEIGHT_FACTOR 保持一致（都是 1.2）

## overflow_bbox

- 当 `fitting_size` 返回值 < 可读阈值（8pt）时，layout_agent 调用 `overflow_bbox` 替代缩小字号
- 逻辑：二分搜索扩展 bbox 高度（向下），直到 text 在 target_size 下能放入
- 扩展受 page_rect 限制，不超出页面边界
- 也用于 content_truncated 场景：翻译后文本超出 bbox 容量时扩展

## 不是独立进程

本模块由 layout_agent.py import，不单独运行。
