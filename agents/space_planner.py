#!/usr/bin/env python3
"""
space_planner.py — Pre-compute page geometry for render_agent.

Reads a raw PDF (for page geometry / drawings) and a parsed.json (for block
metadata), then produces a layout_plan.json that satisfies the
layout_plan.schema.json contract.

No translated text is needed; this module is purely geometric.

Usage:
    python space_planner.py --input doc.pdf --parsed parsed.json \
                            --output layout_plan.json [--pages "1,3,5-8"]
"""

import argparse
import json
import sys
from pathlib import Path
from statistics import median
from typing import Optional

import fitz  # PyMuPDF

# ---------------------------------------------------------------------------
# Sibling-module imports
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).parent))
from topology_agent import TopologyAnalyzer  # noqa: E402
from shared_utils import cluster              # noqa: E402

# contracts/ is two directories up from this file
_CONTRACTS_DIR = Path(__file__).parent.parent / "contracts"
sys.path.insert(0, str(_CONTRACTS_DIR.parent))
from contracts.validate import validate_output  # noqa: E402

# ---------------------------------------------------------------------------
# Version tag written into layout_plan.json
# ---------------------------------------------------------------------------
_VERSION = "1.0.0"


# ---------------------------------------------------------------------------
# Page-range helper (mirrors layout_agent.parse_pages)
# ---------------------------------------------------------------------------

def _parse_pages(spec: str) -> list[int]:
    """Parse a page spec like "1,3,5-8" into a sorted list of 1-based page numbers."""
    pages: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            pages.update(range(int(a), int(b) + 1))
        else:
            pages.add(int(part))
    return sorted(pages)


# ---------------------------------------------------------------------------
# Group detection
# ---------------------------------------------------------------------------

def _make_group(indices: list[int], bboxes: list[list]) -> dict:
    """Build a group dict from a list of block indices and their bboxes.

    Parameters
    ----------
    indices:
        0-based indices into the page's blocks[] array.
    bboxes:
        List of [x0, y0, x1, y1] for each block (parallel to page blocks[]).
    """
    # Sort by y0 to determine topmost block (anchor)
    sorted_idx = sorted(indices, key=lambda i: bboxes[i][1])
    anchor_bbox = bboxes[sorted_idx[0]]
    anchor = [anchor_bbox[0], anchor_bbox[1]]

    # region_bbox = union of all member bboxes
    x0 = min(bboxes[i][0] for i in indices)
    y0 = min(bboxes[i][1] for i in indices)
    x1 = max(bboxes[i][2] for i in indices)
    y1 = max(bboxes[i][3] for i in indices)

    return {
        "block_indices": sorted(indices),
        "anchor": anchor,
        "region_bbox": [x0, y0, x1, y1],
    }


def _detect_groups(
    page_blocks: list[dict],
    topo_result,  # TopologyResult or None
) -> list[dict]:
    """Detect reflow groups for a page.

    Rules (applied in priority order):

    1. Container-based groups: blocks sharing the same non-(-1) group_id
       (from topology_agent) form one group each.
    2. Column-based groups: unassigned blocks with the same column_id (not -1)
       AND without large vertical gaps. A gap > 2x the median gap of that
       column triggers a split into separate groups.
    3. Table-cell exclusion: unassigned blocks that share a row_id (not -1,
       meaning they are horizontal neighbours) are NOT merged into column
       groups — each becomes its own single-element group.
    4. Ungrouped blocks: any remaining unassigned block forms a single-element
       group (no reflow applied to these).

    Returns a list of group dicts:
        {
            "block_indices": [int, ...],   # 0-based indices into page's blocks[]
            "anchor": [x0, y0],            # top-left of topmost block
            "region_bbox": [x0, y0, x1, y1]
        }

    Each block appears in exactly one group. Single-block groups are valid.

    Edge cases:
    - Empty page (no blocks): returns [].
    - Single-block page: returns one single-element group.
    - No topology data (topo_result is None): all blocks become single-element
      groups.
    - All blocks in containers: column pass is a no-op.
    """
    n = len(page_blocks)
    if n == 0:
        return []

    # Bboxes as plain lists [x0, y0, x1, y1] for JSON-safe arithmetic
    bboxes = [b["bbox"] for b in page_blocks]

    # Pull topology arrays (fall back to all-(-1) if topology is absent)
    if topo_result is not None:
        group_ids  = topo_result.group_ids   # len == n, -1 = no container
        column_ids = topo_result.column_ids  # len == n, -1 = isolated
        row_ids    = topo_result.row_ids     # len == n, -1 = isolated
    else:
        group_ids  = [-1] * n
        column_ids = [-1] * n
        row_ids    = [-1] * n

    # assigned[i] = True once block i has been placed into a group
    assigned = [False] * n
    groups: list[dict] = []

    # ------------------------------------------------------------------
    # Rule 1: Container-based groups
    # ------------------------------------------------------------------
    container_buckets: dict[int, list[int]] = {}
    for i, gid in enumerate(group_ids):
        if gid != -1:
            container_buckets.setdefault(gid, []).append(i)

    for gid in sorted(container_buckets):
        members = container_buckets[gid]
        # Sort top-to-bottom
        members.sort(key=lambda i: bboxes[i][1])

        if len(members) == 1:
            groups.append(_make_group(members, bboxes))
            assigned[members[0]] = True
            continue

        # Compute vertical gaps between consecutive members
        gaps = [
            max(0.0, bboxes[members[k]][1] - bboxes[members[k - 1]][3])
            for k in range(1, len(members))
        ]
        med_gap = median(gaps)
        ref_gap = max(med_gap, 2.0)
        threshold = max(ref_gap * 2.0, 50.0)  # absolute floor: 50px

        # Split wherever gap exceeds threshold
        current_subgroup = [members[0]]
        for k, gap in enumerate(gaps):
            next_member = members[k + 1]
            if gap > threshold:
                groups.append(_make_group(current_subgroup, bboxes))
                for i in current_subgroup:
                    assigned[i] = True
                current_subgroup = [next_member]
            else:
                current_subgroup.append(next_member)
        groups.append(_make_group(current_subgroup, bboxes))
        for i in current_subgroup:
            assigned[i] = True

    # ------------------------------------------------------------------
    # Rule 3 (pre-pass): identify horizontal neighbours (table cells).
    # These must NOT be merged via column grouping.
    # ------------------------------------------------------------------
    in_horizontal_row: set[int] = set()
    for i, rid in enumerate(row_ids):
        if rid != -1 and not assigned[i]:
            in_horizontal_row.add(i)

    # ------------------------------------------------------------------
    # Rule 2: Column-based groups
    # ------------------------------------------------------------------
    column_buckets: dict[int, list[int]] = {}
    for i, cid in enumerate(column_ids):
        if not assigned[i] and cid != -1 and i not in in_horizontal_row:
            column_buckets.setdefault(cid, []).append(i)

    for cid in sorted(column_buckets):
        members = column_buckets[cid]
        # Sort members top-to-bottom by block y0
        members.sort(key=lambda i: bboxes[i][1])

        # Compute vertical gaps between consecutive members
        gaps = []
        for k in range(1, len(members)):
            prev_y1 = bboxes[members[k - 1]][3]  # y1 of previous block
            curr_y0 = bboxes[members[k]][1]       # y0 of current block
            gaps.append(max(0.0, curr_y0 - prev_y1))

        if not gaps:
            # Single member in column bucket (edge case: topology assigned
            # column_id to an isolated block; treat as single-element group)
            groups.append(_make_group(members, bboxes))
            for i in members:
                assigned[i] = True
            continue

        med_gap = median(gaps)
        # Use a minimum reference gap to avoid splitting on very tight slides
        # where all gaps are near-zero (would split on any non-zero gap)
        ref_gap = max(med_gap, 2.0)
        threshold = ref_gap * 2.0

        # Split into sub-groups wherever a gap exceeds the threshold
        current_subgroup = [members[0]]
        for k, gap in enumerate(gaps):
            next_member = members[k + 1]
            if gap > threshold:
                groups.append(_make_group(current_subgroup, bboxes))
                for i in current_subgroup:
                    assigned[i] = True
                current_subgroup = [next_member]
            else:
                current_subgroup.append(next_member)
        # Flush last sub-group
        groups.append(_make_group(current_subgroup, bboxes))
        for i in current_subgroup:
            assigned[i] = True

    # ------------------------------------------------------------------
    # Rules 3 & 4: Single-element groups for all remaining blocks
    # (horizontal-neighbour table cells and truly isolated blocks)
    # ------------------------------------------------------------------
    for i in range(n):
        if not assigned[i]:
            groups.append(_make_group([i], bboxes))
            assigned[i] = True

    return groups


# ---------------------------------------------------------------------------
# Per-page planning
# ---------------------------------------------------------------------------

def _plan_page(
    page: fitz.Page,
    page_data: dict,
    page_rect: fitz.Rect,
    is_dense: bool,
) -> dict:
    """Compute layout plan for a single page and return the page dict.

    Parameters
    ----------
    page:
        The fitz.Page object (used for get_drawings / image obstacle geometry).
    page_data:
        The parsed.json entry for this page (contains blocks, image_obstacles).
    page_rect:
        The page's bounding rectangle.
    is_dense:
        True when this page has above-average block density (pre-computed by
        the caller).
    """
    blocks = page_data.get("blocks", [])
    image_obstacles_raw = page_data.get("image_obstacles", [])

    # Convert bboxes / obstacles to fitz.Rect objects
    bboxes = [fitz.Rect(b["bbox"]) for b in blocks]
    alignments = [int(b.get("align", 0)) for b in blocks]
    font_sizes = [float(b.get("font_size", 10.0)) for b in blocks]
    image_obstacles = [fitz.Rect(ob) for ob in image_obstacles_raw]

    drawings = page.get_drawings()

    # ------------------------------------------------------------------
    # Topology analysis → insert_bboxes (Voronoi)
    # ------------------------------------------------------------------
    topo = TopologyAnalyzer(page_rect)
    topo_result = topo.analyze(bboxes, alignments, drawings, image_obstacles)
    insert_bboxes = topo_result.insert_bboxes
    container_colors = topo_result.container_colors

    # ------------------------------------------------------------------
    # Group detection → groups[] for vertical reflow
    # ------------------------------------------------------------------
    groups = _detect_groups(blocks, topo_result)

    # ------------------------------------------------------------------
    # snap_map — Y-axis clustering alignment
    # ------------------------------------------------------------------
    y0_vals = [b.y0 for b in bboxes]
    clusters = cluster(y0_vals, tol=3.0, min_count=2)
    # Build snap_map: original_y0 (as string key) → snapped_y0 (float)
    snap_map: dict[str, float] = {}
    for rep, members in clusters.items():
        for v in members:
            snap_map[str(v)] = rep

    # ------------------------------------------------------------------
    # title_indices — large font + top-quarter position
    # ------------------------------------------------------------------
    max_fs = max(font_sizes) if font_sizes else 10.0
    title_threshold = max_fs * 0.85
    page_h = page_rect.height
    title_indices: list[int] = []
    for idx, (fs, bbox) in enumerate(zip(font_sizes, bboxes)):
        is_large = fs >= title_threshold and fs >= 16.0
        in_top = bbox.y0 < page_h * 0.25
        if is_large and in_top:
            title_indices.append(idx)

    # ------------------------------------------------------------------
    # Build cells list
    # ------------------------------------------------------------------
    cells: list[dict] = []
    title_set = set(title_indices)
    for idx, (block, ibbox) in enumerate(zip(blocks, insert_bboxes)):
        cell: dict = {
            "block_id":    block["id"],
            "insert_bbox": [ibbox.x0, ibbox.y0, ibbox.x1, ibbox.y1],
            "is_title":    idx in title_set,
            "is_dense":    is_dense,
        }
        cc = container_colors[idx] if idx < len(container_colors) else None
        if cc is not None:
            cell["container_color"] = list(cc)
        cells.append(cell)

    return {
        "page_num":      page_data["page_num"],
        "cells":         cells,
        "snap_map":      snap_map,
        "title_indices": title_indices,
        "groups":        groups,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="space_planner: pre-compute layout geometry for render_agent."
    )
    parser.add_argument("--input",  required=True, help="Source PDF file path")
    parser.add_argument("--parsed", required=True, help="parsed.json file path")
    parser.add_argument("--output", required=True, help="Output layout_plan.json path")
    parser.add_argument("--pages",  default=None,  help='Page spec, e.g. "1,3,5-8"')
    args = parser.parse_args()

    input_path  = Path(args.input)
    parsed_path = Path(args.parsed)
    output_path = Path(args.output)

    # Validate inputs exist
    if not input_path.exists():
        print(f"[ERROR] Input PDF not found: {input_path}", file=sys.stderr)
        sys.exit(1)
    if not parsed_path.exists():
        print(f"[ERROR] parsed.json not found: {parsed_path}", file=sys.stderr)
        sys.exit(1)

    # Load parsed.json
    with open(parsed_path, encoding="utf-8") as f:
        parsed_data = json.load(f)

    if isinstance(parsed_data, dict) and "pages" in parsed_data:
        pages_list = parsed_data["pages"]
    elif isinstance(parsed_data, list):
        pages_list = parsed_data
    else:
        print("[ERROR] Unrecognised parsed.json schema", file=sys.stderr)
        sys.exit(1)

    # Build page map keyed by page_num
    page_map: dict[int, dict] = {int(p["page_num"]): p for p in pages_list}

    # Determine which pages to process
    if args.pages:
        requested = set(_parse_pages(args.pages))
    else:
        requested = set(page_map.keys())

    # ------------------------------------------------------------------
    # Pre-compute global average block count (for is_dense)
    # ------------------------------------------------------------------
    all_block_counts = [len(p.get("blocks", [])) for p in pages_list]
    global_avg = (sum(all_block_counts) / len(all_block_counts)) if all_block_counts else 0.0
    dense_threshold = global_avg * 1.5

    # Open PDF
    doc = fitz.open(str(input_path))

    result_pages: list[dict] = []

    for page_num in sorted(requested):
        if page_num not in page_map:
            print(f"[WARN] Page {page_num} not in parsed.json, skipping.", file=sys.stderr)
            continue

        page_idx = page_num - 1
        if page_idx < 0 or page_idx >= doc.page_count:
            print(f"[WARN] Page {page_num} out of PDF range, skipping.", file=sys.stderr)
            continue

        page_data = page_map[page_num]
        blocks = page_data.get("blocks", [])

        page = doc[page_idx]
        page_rect = page.rect

        is_dense = len(blocks) > dense_threshold

        print(f"[INFO] Planning page {page_num} ({len(blocks)} blocks, dense={is_dense}) ...",
              file=sys.stderr)

        page_plan = _plan_page(page, page_data, page_rect, is_dense)
        result_pages.append(page_plan)

    doc.close()

    # ------------------------------------------------------------------
    # Assemble output document
    # ------------------------------------------------------------------
    output_doc: dict = {
        "version": _VERSION,
        "pages":   result_pages,
    }

    # ------------------------------------------------------------------
    # Validate before writing
    # ------------------------------------------------------------------
    violations = validate_output(output_doc, "layout_plan")
    if violations:
        print(f"[ERROR] Output validation failed ({len(violations)} violations):",
              file=sys.stderr)
        for v in violations:
            print(f"  {v}", file=sys.stderr)
        sys.exit(1)

    # Write output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output_doc, f, ensure_ascii=False, indent=2)

    print(f"[INFO] layout_plan.json written to: {output_path}", file=sys.stderr)
    print(f"[INFO] Pages planned: {len(result_pages)}", file=sys.stderr)

    # Post-write validation (re-read to confirm round-trip)
    with open(output_path, encoding="utf-8") as f:
        roundtrip = json.load(f)
    final_violations = validate_output(roundtrip, "layout_plan")
    if final_violations:
        print(f"[ERROR] Post-write validation failed ({len(final_violations)} violations):",
              file=sys.stderr)
        for v in final_violations:
            print(f"  {v}", file=sys.stderr)
        sys.exit(1)
    else:
        print("[INFO] Output validated successfully against layout_plan.schema.json",
              file=sys.stderr)


if __name__ == "__main__":
    main()
