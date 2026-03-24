# PDF Translator — Development Log

## Goal
Translate 成果物1 (45-page ja→zh slide deck) with high quality, page-by-page tuning.

## Acceptance Criteria
- All test_agent checks pass or failures are justified (e.g., brand names)
- No visible text truncation in output PDF
- Consistent terminology across all pages
- All section headings translated to Chinese

## Current State (2026-03-24)
- Round 9: Fixed multicolor \n rendering bug in layout_agent
- Round 10 (designing): Reflow system — fundamental layout redesign
- **Next:** Implement reflow per design below, then re-render full document

## Open Issues
- readability_check: 4 个 content_truncated（P8/P28/P40），layout_agent auto-sizing 是否视觉可接受待确认
- quality_check / translation_completeness_check FAIL 项均为品牌名误报，考虑加白名单
- style_check (LLM-based) 每次跑出不同结果，不收敛——确定性检查已部分替代

## Design: Block Group Reflow (Round 10)

### Problem Statement

Current layout uses absolute bbox coordinates from the source PDF. When translated text is shorter (or longer) than the original, blocks don't adjust position — creating large vertical gaps or overflow. This is the #1 visual quality issue reported by user on P16-P20+.

### Core Idea

Add **vertical reflow within block groups**. Blocks on a slide are not independent boxes — they form visual groups (a title + bullet list, a labeled diagram, etc.). Within each group, blocks should flow top-to-bottom like a column layout. Between groups, positions stay fixed.

### Design

#### Phase 1: Group Detection (in space_planner)

Leverage topology_agent's existing analysis:
- **Container-based groups**: Blocks sharing a container (colored rect) are one group. topology_agent already computes this via `group_ids`.
- **Column-based groups**: Blocks in the same column cluster (x0 ±15px, from `column_ids`) AND without large vertical gaps (gap > 2× median gap = different group).
- **Ungrouped blocks**: Blocks that don't belong to any container or column cluster stay at original position (no reflow).

Output: `groups[]` in layout_plan.json — each group has an ordered list of block indices, an anchor point (top-left of first block), and a bounding region.

#### Phase 2: Reflow Calculation (new step in layout_agent, between Step 9 and Step 10)

After fitting_size is computed for each block, we know the actual rendered height of each block. Reflow within each group:

```
For each group:
  cursor_y = group.anchor_y  (= first block's original y0)
  For each block in group (top-to-bottom order):
    original_gap = block.y0 - previous_block.y1  (preserve original inter-block spacing)
    cursor_y += original_gap  (for first block, gap = 0)
    new_y0 = cursor_y
    rendered_height = estimate_height(text, fitting_size, bbox.width)
    new_y1 = new_y0 + rendered_height

    # Safety: don't exceed page bottom or image obstacles
    if new_y1 > page_bottom or intersects(new_bbox, image_obstacles):
      stop reflow, remaining blocks stay at original position

    block.reflow_bbox = [original_x0, new_y0, original_x1, new_y1]
    cursor_y = new_y1
```

Key rules:
- **Only vertical (y) changes.** x coordinates stay fixed. This preserves column alignment.
- **Original inter-block gaps are preserved.** If there was 10px between ■ Scenarios and ■ Functions in the original, there's still 10px after reflow.
- **Downward push has hard boundaries.** Page bottom and image_obstacles are impassable. If a group would overflow, stop reflowing and fall back to original positions for remaining blocks.
- **Table cells are excluded from reflow.** Blocks flagged as table cells (horizontal neighbors) retain original positions.

#### Phase 3: Redaction Adjustment (in layout_agent Step 1)

Current redaction uses per-block `redact_bboxes` from parsed data. With reflow:
- Redact the **union bbox** of all blocks in a group (original positions), not individual blocks.
- This ensures the entire reflow region is cleared, so blocks moving within the group don't land on un-redacted original text.
- Keep transparent-fill logic for areas overlapping background drawings/images.

#### Phase 4: Render with Reflow'd Positions (in layout_agent Step 11)

Use `reflow_bbox` instead of `insert_bbox` for positioning. All existing typography logic (fitting_size, multicolor, overflow) applies to the reflow'd bbox.

### Data Flow Changes

```
space_planner (Phase 1)
  └─ layout_plan.json gains: groups[] with block indices + anchor
layout_agent
  Step 1: Redact using group union bbox (Phase 3)
  Step 9: Fitting sizes (unchanged)
  Step 9c (NEW): Reflow calculation (Phase 2)
  Step 10: Consistency pass (unchanged, uses reflow'd bboxes)
  Step 11: Render at reflow'd positions (Phase 4)
```

### Contract Changes

layout_plan.schema.json adds:
```json
"groups": [
  {
    "block_indices": [1, 2, 3, 4],
    "anchor": [35.9, 79.8],
    "region_bbox": [35.9, 79.8, 444.5, 491.4]
  }
]
```

### What Doesn't Change
- parse_agent, consolidator, translate_agent, test_agent — untouched
- topology_agent — untouched (already provides group_ids and column_ids)
- visual_agent — untouched (fitting_size/overflow work on any bbox)

### Risk Mitigation
- Reflow is opt-in per group. Ungrouped blocks and table cells are not affected.
- If reflow hits page bottom, remaining blocks fall back to original positions — never worse than current behavior.
- Redaction union covers both old and new positions — no orphaned text artifacts.
- Can be feature-flagged (`--no-reflow`) for A/B comparison during development.

## Change Log

### Round 1: Core translation fixes
- Translated English section headings on P17/P19/P20 (Configuration→配置, Scenarios→场景, etc.)
- Fixed P31 "滤波器"→"过滤器"
- Translated P40 English (Evaluation Tools→评估工具, Release Dashboard→发布看板, etc.)
- Simplified P34 over-translations (CCB, code identifiers)
- Shortened P04/P05 "DFDI" expansion
- **Result:** style PASS, readability 13→7

### Round 2: Readability overflow fixes
- Shortened/reformatted p06_b009, p07_b008/010, p08_b008, p28_b005, p40_b003
- **Result:** readability 7→4, resolved p06/p07 completely

### Round 3: Terminology consistency
- Standardized LiDAR (not 激光雷达), 摄像头 (not 相机), SoC (not SOC), Momenta Box (not Momenta 盒子)
- Fixed P11 tone "烦请"→"请"
- **Result:** 13 blocks fixed, style found 6 new issues

### Round 4: Second terminology pass
- Standardized 边界工况 (not 边缘场景), 侧摄像头 (not 侧置摄像头)
- Removed DFDI/CFDI Chinese expansions (keep abbreviations)
- Standardized conjunctions "与" (not "和")
- **Result:** style found 4 new issues

### Round 5: Final style convergence
- Fixed FDI "车辆"→"车队", 档位→挡位, VVP Loc→VVP 定位
- **Result:** style_check PASS

### Round 6: Line break restoration
- Fixed 10 blocks (P16-P20) where ■/• markers lost preceding \n
- Root cause: translate_agent LLM dropped JSON-escaped newlines (known issue, placeholder fix exists but cache had stale entries)

### Round 7: Test automation — 3 new deterministic checks
- `linebreak_consistency_check`: detects \n loss before ■/• markers
- `mixed_language_check`: detects untranslated English headings/phrases in Chinese text
- `terminology_consistency_check`: detects same English term translated differently (variant pairs + dynamic detection)
- All 3 checks PASS on current translated.json, confirming fixes are effective
- Schema updated in contracts/test_report.schema.json

### Round 9: Multicolor newline fix
- **Bug**: `insert_text_multicolor()` built `char_colors` from `color_spans` only, which contained inconsistent `\n` — some spans had `\n`, some didn't. The authoritative `text` parameter's `\n` positions were ignored.
- **Fix**: Filter `\n` from `seg_chars` (one-line change), use `text` as sole source of newline positions.
- **Result**: P16-P20 bullet lists (■/•) now render with correct line breaks.

### Round 8: Project housekeeping (new coordinator)
- **P0 止血**: 提交大规模重构（旧分散目录→统一 agents/，移除 intel_agent，qa→test_agent 改名），48 files changed
- **P1 纲领对齐**: CLAUDE.md 修正 pipeline 图（render_agent→layout_agent）、输出文件名（{stem}.parsed.json）、补 review_agent 到按需 agent 表、更新 testdata 表
- **P2 经验沉淀**: 共通 Lessons Learned 去重，迁移到各 agent 独立 CLAUDE.md；补 translate_agent 换行符教训
- **P3 issues**: ISS-001（标题字号过小）确认仍为 open（7 轮迭代在成果物1，ISS-001 在成果物4）
- `.gitignore` 整理：排除 .DS_Store、中间 work 文件，只保留 baseline/ 入版本控制
