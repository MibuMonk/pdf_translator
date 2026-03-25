# Defect Taxonomy

Shared vocabulary for describing rendering defects. All agents MUST use these terms
when reporting, diagnosing, or fixing issues.

## Layout Defects

### L1: Word Split (断词)

**Symptom:** A single word is broken across lines at an arbitrary position, not at a
hyphenation point. e.g., "Sc enarios", "Co nfiguration", "Ex ecution".

**Mechanism:** The text block's bbox is too narrow for the rendered line. The layout
engine forces a line break mid-word to fit within the horizontal constraint.

**Affected stage:** layout_agent (rendering). May originate from space_planner
(bbox allocation) or consolidator (block segmentation creating artificially narrow blocks).

### L2: Structure Collapse (结构坍塌)

**Symptom:** A page that originally had clear visual sections (headings, bullet groups,
whitespace separators) renders as a single undifferentiated mass of text. Section
boundaries disappear; bullets merge or lose indentation.

**Mechanism:** Either (a) consolidator merged blocks that should stay separate, losing
section boundaries, or (b) layout_agent failed to preserve vertical spacing between
semantic groups.

**Affected stage:** consolidator (over-merging) or layout_agent (spacing).

### L3: Content Drift (内容漂移)

**Symptom:** Translated text appears in a noticeably different position than the
corresponding source text. Large empty gaps appear where text should be, or text
crowds into one area leaving another empty.

**Mechanism:** bbox assignment in space_planner doesn't account for translated text
length, or layout_agent's reflow pushes content away from its original anchor.

**Affected stage:** space_planner (planning) or layout_agent (reflow).

### L4: Section Fragmentation (段落碎片化)

**Symptom:** A coherent section in the source (e.g., a heading followed by its bullet
list) is broken into disconnected blocks in the output. The heading may render
separately from its content, or bullets scatter across the page.

**Mechanism:** parse_agent or consolidator produced blocks that split a semantic unit.
Each fragment gets independently placed, losing the visual grouping.

**Affected stage:** parse_agent (extraction) or consolidator (under-merging).

### L5: Linebreak Inconsistency (换行不一致)

**Symptom:** The same structural pattern (e.g., `■ heading` followed by `• bullet`)
renders with a line break in some instances but without in others on the same page.

**Mechanism:** Consolidator's cross-color merge produces blocks where `\n` between
heading and bullets is present in some cases but missing in others, depending on
whether the merge path (same-color vs cross-color) was taken.

**Affected stage:** consolidator (merge inconsistency) or translate_agent (inconsistent
`\n` preservation).

### L6: Bbox Overlap (bbox 重叠)

**Symptom:** Adjacent text blocks overlap visually — characters from one block render
on top of another block's content, making both unreadable.

**Mechanism:** layout_agent's `overflow_bbox` expansion pushes a block's insert_bbox
into the space occupied by a neighboring block. No collision detection between
expanded bboxes.

**Affected stage:** layout_agent (overflow expansion without neighbor awareness).

## Translation Defects

### T1: Missing Translation (未翻译)

**Symptom:** Source language text appears verbatim in the output without translation.

### T2: Truncated Translation (翻译截断)

**Symptom:** Translation is visibly incomplete — sentence ends mid-thought or key
information from the source is absent.

### T3: Terminology Inconsistency (术语不一致)

**Symptom:** The same source term is translated differently in different locations.

## Severity Scale

| Grade | Chinese | Meaning |
|-------|---------|---------|
| A | 非常好 | Indistinguishable from professional human translation |
| B | 及格 | Readable and usable, minor cosmetic issues |
| C | 坏 | Layout problems hurt readability, content still recoverable |
| D | 极坏 | Layout severely broken, content hard to read or misplaced |
| F | 不可用 | Content missing, garbled, or completely wrong |

## Observed Instance: 成果物1 P16–P21

| Page | Grade | Defects observed |
|------|-------|-----------------|
| P16 | B | Minor layout looseness |
| P17 | C | L4 (section fragmentation), L3 (content drift — bottom half empty) |
| P18 | C | L4 (section fragmentation), L2 (bullet structure degraded) |
| P19 | C | L4 (section fragmentation), L1 (word split in "Fu nctions") |
| P20 | D | L1 (word split: "Sc enarios", "Co nfiguration", "Ex ecution"), L2 (structure collapse), L3 (content drift) |
| P21 | A | Clean rendering, good structure preservation |
| P36 | C | L1 (word split: "LiDAR" broken inside table cell) |
| P40 | D | L1 (unnatural breaks before "要求"), L6 (bbox overlap in dense right column) |

## Using This Taxonomy

- **test_agent**: Use defect codes (L1, L2, etc.) in test_report findings.
- **layout_agent / space_planner**: Reference defect codes when documenting fixes.
- **consolidator**: L2 and L4 are your primary concern — over-merge vs under-merge.
- **coordinator**: Use grade scale (A–F) and defect codes when describing issues to agents.
