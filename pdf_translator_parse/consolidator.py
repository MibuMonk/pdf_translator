#!/usr/bin/env python3
"""
Consolidator — merge semantically fragmented text blocks before translation.

Runs between parse_agent and translate_agent.
Input:  parsed.json
Output: consolidated.json  (same schema, blocks merged where appropriate)

Merge strategy:
  - Group blocks into columns (shared x0 within X_COL_TOL)
  - Within a column, merge vertically adjacent blocks when:
      * y-gap is small (< Y_GAP_MAX)
      * font sizes are compatible (same or body-under-header)
      * text does not end with a hard sentence boundary before the next block
  - Never merge across columns (separate layout elements)
  - Never merge diagram labels (small isolated blocks far from column groups)
  - Never merge blocks with very different font sizes (title vs footnote)
"""

import argparse
import json
import sys
from pathlib import Path

# ── Tunables ──────────────────────────────────────────────────────────────────
X_COL_TOL    = 12.0   # px tolerance for blocks to be considered same column
Y_GAP_MAX    = 18.0   # max vertical gap between mergeable blocks (px)
FS_RATIO_MAX = 2.0    # max font-size ratio between merged blocks (skip big jumps)
MIN_ISOLATED_FS = 9.0 # blocks below this size AND isolated are diagram labels → skip
HARD_ENDINGS = set(".!?。！？…")

# Threshold for "missed fragment candidate": x_diff beyond column tolerance but y_gap tiny
MISSED_FRAG_Y_GAP_MAX = 5.0  # px — very small gap signals a fragment

LOG_VERSION = "1.0"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _bbox_x0(block): return block["bbox"][0]
def _bbox_y0(block): return block["bbox"][1]
def _bbox_x1(block): return block["bbox"][2]
def _bbox_y1(block): return block["bbox"][3]


def _same_column(a: dict, b: dict) -> bool:
    """True if both blocks share roughly the same left edge (column)."""
    return abs(_bbox_x0(a) - _bbox_x0(b)) <= X_COL_TOL


def _y_gap(upper: dict, lower: dict) -> float:
    return _bbox_y0(lower) - _bbox_y1(upper)


def _ends_hard(text: str) -> bool:
    """True if text ends with sentence-final punctuation (don't merge across it)."""
    stripped = text.rstrip()
    return bool(stripped) and stripped[-1] in HARD_ENDINGS


def _has_horizontal_neighbor(block: dict, all_blocks: list,
                              y_tol: float = 8.0, x_min_gap: float = 60.0) -> bool:
    """
    True if any other block sits at roughly the same y-level but a different x-column.
    This is the key signal that a block lives inside a table row, not in prose.
    """
    by0 = _bbox_y0(block)
    bx0 = _bbox_x0(block)
    for other in all_blocks:
        if other is block:
            continue
        if (abs(_bbox_y0(other) - by0) <= y_tol
                and abs(_bbox_x0(other) - bx0) > x_min_gap):
            return True
    return False


def _fs_compatible(a: dict, b: dict) -> bool:
    """True if font sizes are close enough to merge."""
    fa, fb = a["font_size"], b["font_size"]
    if fa == 0 or fb == 0:
        return False
    ratio = max(fa, fb) / min(fa, fb)
    return ratio <= FS_RATIO_MAX


def _merge_two(a: dict, b: dict) -> dict:
    """Merge block b into block a, returning a new combined block."""
    merged_text = a["text"].rstrip("\n") + "\n" + b["text"].lstrip("\n")
    merged_bbox = [
        min(_bbox_x0(a), _bbox_x0(b)),
        _bbox_y0(a),
        max(_bbox_x1(a), _bbox_x1(b)),
        _bbox_y1(b),
    ]
    merged_redact = list(a.get("redact_bboxes", [a["bbox"]])) + \
                    list(b.get("redact_bboxes", [b["bbox"]]))
    return {
        **a,
        "text": merged_text,
        "bbox": merged_bbox,
        "redact_bboxes": merged_redact,
        # keep the larger font_size (header dominates)
        "font_size": max(a["font_size"], b["font_size"]),
    }


# ── Column grouping ───────────────────────────────────────────────────────────

def _group_by_column(blocks: list) -> list[list]:
    """
    Partition blocks into column groups by shared x0.
    Returns list of groups, each group sorted by y0.
    """
    groups: list[list] = []
    for block in sorted(blocks, key=_bbox_x0):
        placed = False
        for grp in groups:
            if _same_column(grp[0], block):
                grp.append(block)
                placed = True
                break
        if not placed:
            groups.append([block])
    # sort each group by y0
    for grp in groups:
        grp.sort(key=_bbox_y0)
    return groups


# ── Per-column merge ──────────────────────────────────────────────────────────

def _merge_column(blocks: list, all_page_blocks: list,
                  page_num: int,
                  merges_log: list, skipped_log: list,
                  suspected_table_pages: set) -> list:
    """
    Merge vertically adjacent blocks within a column.
    Skips merge if either block has a horizontal neighbor (table cell indicator).
    Returns a new (smaller or equal) list of blocks.

    Side-effects: appends to merges_log, skipped_log, suspected_table_pages.
    """
    if not blocks:
        return []

    result = [dict(blocks[0])]
    # Track which original IDs were absorbed into each result slot.
    # Each slot is a list of original IDs that have been merged into it.
    absorbed_ids: list[list[str]] = [[blocks[0].get("id", "")]]

    for block in blocks[1:]:
        prev = result[-1]
        gap = _y_gap(prev, block)

        prev_has_horiz = _has_horizontal_neighbor(prev, all_page_blocks)
        block_has_horiz = _has_horizontal_neighbor(block, all_page_blocks)
        fs_ok = _fs_compatible(prev, block)
        ends_hard = _ends_hard(prev["text"])

        can_merge = (
            gap >= 0                          # no overlap
            and gap <= Y_GAP_MAX              # close enough
            and _same_column(prev, block)     # same column (redundant safety)
            and fs_ok                         # similar font sizes
            and not ends_hard                 # previous block doesn't close a sentence
            and not prev_has_horiz            # not a table cell
            and not block_has_horiz           # not a table cell
        )

        if can_merge:
            # Record the merge: block.id is being absorbed into prev (which will get
            # a new id later, but we log the original id of prev's seed block).
            absorbed_original_id = block.get("id", "")
            # The "into" id at this point is still the original id of the accumulator's
            # seed block (first element of the absorbed_ids list for this slot).
            into_original_id = absorbed_ids[-1][0]

            # Build reason string
            reason_parts = ["y_gap_ok", "same_column", "fs_compatible"]
            merges_log.append({
                "page": page_num,
                "absorbed": [absorbed_original_id],
                "into": into_original_id,
                "reason": ", ".join(reason_parts),
            })

            result[-1] = _merge_two(prev, block)
            absorbed_ids[-1].append(absorbed_original_id)
        else:
            # Record why skip happened (only if gap and column were otherwise ok)
            if gap >= 0 and gap <= Y_GAP_MAX and _same_column(prev, block):
                skip_reasons = []
                if prev_has_horiz:
                    skip_reasons.append("horizontal_neighbor")
                    suspected_table_pages.add(page_num)
                if block_has_horiz:
                    skip_reasons.append("horizontal_neighbor")
                    suspected_table_pages.add(page_num)
                if ends_hard:
                    skip_reasons.append("ends_hard")
                if not fs_ok:
                    skip_reasons.append("fs_incompatible")
                if skip_reasons:
                    skipped_log.append({
                        "page": page_num,
                        "block_id": block.get("id", ""),
                        "reason": ", ".join(dict.fromkeys(skip_reasons)),  # deduplicate
                    })

            result.append(dict(block))
            absorbed_ids.append([block.get("id", "")])

    return result


# ── Isolated small-block filter ───────────────────────────────────────────────

def _is_diagram_label(block: dict, all_blocks: list, page_w: float) -> bool:
    """
    Heuristic: a block is a diagram label if it is:
      - small font (< MIN_ISOLATED_FS)
      - narrow (width < 20% of page)
      - not sharing a column with any other block of normal size
    These should be kept as-is (diagram annotations, not prose).
    """
    if block["font_size"] >= MIN_ISOLATED_FS:
        return False
    width = _bbox_x1(block) - _bbox_x0(block)
    if page_w > 0 and width / page_w > 0.20:
        return False
    # Check if any normal-size block shares this column
    for other in all_blocks:
        if other is block:
            continue
        if other["font_size"] >= MIN_ISOLATED_FS and _same_column(other, block):
            return False
    return True


# ── Missed-fragment detection ─────────────────────────────────────────────────

def _detect_missed_fragments(blocks: list, page_num: int) -> list:
    """
    Scan all consecutive pairs of blocks (sorted by y0) on the page.
    Flag pairs where:
      - y_gap < MISSED_FRAG_Y_GAP_MAX (very close vertically — looks like one text)
      - x_diff > X_COL_TOL (different columns — so consolidator skipped them)
    These are candidates for fragments that consolidator may have missed.
    """
    candidates = []
    sorted_blocks = sorted(blocks, key=_bbox_y0)
    for i in range(len(sorted_blocks) - 1):
        a = sorted_blocks[i]
        b = sorted_blocks[i + 1]
        gap = _y_gap(a, b)
        x_diff = abs(_bbox_x0(a) - _bbox_x0(b))
        if 0 <= gap < MISSED_FRAG_Y_GAP_MAX and x_diff > X_COL_TOL:
            candidates.append({
                "page": page_num,
                "block_a": a.get("id", ""),
                "block_b": b.get("id", ""),
                "reason": (
                    f"y_gap={gap:.1f}px < {MISSED_FRAG_Y_GAP_MAX}px "
                    f"but x_diff={x_diff:.1f}px > X_COL_TOL={X_COL_TOL}px"
                ),
            })
    return candidates


# ── Page-level consolidation ──────────────────────────────────────────────────

def consolidate_page(page: dict,
                     merges_log: list, skipped_log: list,
                     suspected_table_pages: set,
                     missed_fragment_candidates: list) -> dict:
    blocks = page.get("blocks", [])
    if not blocks:
        return page

    page_w = page.get("width", 0)
    page_num = page.get("page_num", 0)

    # Separate diagram labels from prose blocks
    prose = [b for b in blocks if not _is_diagram_label(b, blocks, page_w)]
    labels = [b for b in blocks if _is_diagram_label(b, blocks, page_w)]

    # Group prose by column, merge within each column
    columns = _group_by_column(prose)
    merged_prose: list = []
    for col in columns:
        merged_prose.extend(
            _merge_column(col, blocks, page_num,
                          merges_log, skipped_log, suspected_table_pages)
        )

    # Re-sort everything by y0 (reading order)
    all_out = merged_prose + labels
    all_out.sort(key=_bbox_y0)

    # Re-assign block IDs
    for idx, b in enumerate(all_out):
        b["id"] = f"p{page_num:02d}_b{idx:03d}"

    # Detect missed fragments using the NEW ids (post-merge, so pairs are real blocks)
    missed_fragment_candidates.extend(_detect_missed_fragments(all_out, page_num))

    return {**page, "blocks": all_out}


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Consolidator: merge fragmented text blocks before translation."
    )
    parser.add_argument("--input",  required=True, help="Path to parsed.json")
    parser.add_argument("--output", default=None,  help="Output consolidated.json path")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"[consolidator] ERROR: {input_path} not found", file=sys.stderr)
        sys.exit(1)

    output_path = (
        Path(args.output) if args.output
        else input_path.parent / input_path.name.replace(".parsed.", ".consolidated.")
    )

    with open(input_path, encoding="utf-8") as f:
        data = json.load(f)

    pages = data.get("pages", data) if isinstance(data, dict) else data

    total_before = sum(len(p.get("blocks", [])) for p in pages)
    consolidated_pages = []

    # ── Log accumulators ──
    merges_log: list = []
    skipped_log: list = []
    suspected_table_pages: set = set()
    missed_fragment_candidates: list = []

    for page in pages:
        before = len(page.get("blocks", []))
        new_page = consolidate_page(
            page,
            merges_log=merges_log,
            skipped_log=skipped_log,
            suspected_table_pages=suspected_table_pages,
            missed_fragment_candidates=missed_fragment_candidates,
        )
        after = len(new_page.get("blocks", []))
        consolidated_pages.append(new_page)

        if args.verbose or before != after:
            print(f"  Page {page.get('page_num', '?')}: {before} → {after} blocks"
                  f"{' (merged ' + str(before - after) + ')' if before != after else ''}")

    total_after = sum(len(p.get("blocks", [])) for p in consolidated_pages)

    if isinstance(data, dict):
        out_data = {**data, "pages": consolidated_pages}
    else:
        out_data = consolidated_pages

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(out_data, f, ensure_ascii=False, indent=2)

    print(f"[consolidator] {total_before} → {total_after} blocks "
          f"(merged {total_before - total_after})")
    print(f"[consolidator] Output: {output_path}")

    # ── Build and write consolidator_log.json ────────────────────────────────
    n_missed = len(missed_fragment_candidates)
    confidence = max(0.0, round(1.0 - 0.05 * n_missed, 10))

    consolidator_log = {
        "version": LOG_VERSION,
        "total_before": total_before,
        "total_after": total_after,
        "merges": merges_log,
        "skipped": skipped_log,
        "self_eval": {
            "suspected_table_pages": sorted(suspected_table_pages),
            "missed_fragment_candidates": missed_fragment_candidates,
            "confidence": confidence,
        },
    }

    log_path = input_path.parent / "consolidator_log.json"
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(consolidator_log, f, ensure_ascii=False, indent=2)
    print(f"[consolidator] Log:    {log_path}")

    # ── Validate output against parsed schema ─────────────────────────────────
    try:
        contracts_dir = Path(__file__).parent.parent / "contracts"
        sys.path.insert(0, str(contracts_dir.parent))
        from contracts.validate import validate_output
        violations = validate_output(out_data, "parsed")
        if violations:
            print(f"[consolidator] WARNING: output failed schema validation "
                  f"({len(violations)} violation(s)):", file=sys.stderr)
            for v in violations:
                print(f"  {v}", file=sys.stderr)
        else:
            print("[consolidator] Schema validation: OK")
    except Exception as exc:
        print(f"[consolidator] WARNING: could not run schema validation: {exc}",
              file=sys.stderr)


if __name__ == "__main__":
    main()
