"""
topology_agent.py — Spatial topology analysis for PDF layout.

This module analyzes spatial relationships between text blocks and background
graphics on a PDF page. Given a list of text block bounding boxes, their text
alignments, page drawings (colored rectangles), and image obstacles, it computes:

  - Voronoi-style rendering cells for each block (row-aware expansion)
  - Container detection (which colored drawing rect encloses each block)
  - Group assignment (blocks sharing a container belong to the same group)
  - Column clustering (blocks aligned on the x-axis within a tolerance)
  - Row clustering (blocks aligned on the y-axis within a tolerance)
  - Final insert bboxes (the actual region into which translated text is rendered)

Intended as a library module; no CLI entry point.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import fitz  # PyMuPDF


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class BlockInfo:
    """Lightweight record linking a block index to its geometry."""

    idx: int          # index in the original blocks list
    bbox: fitz.Rect
    font_size: float


@dataclass
class TopologyResult:
    """Output of a full topology analysis pass."""

    # Voronoi cells: cells[i] is the expanded rendering region for block i
    cells: List[fitz.Rect]

    # Container: which drawing rect visually encloses block i (None if none)
    containers: List[Optional[fitz.Rect]]

    # Group id: blocks in the same visual group (e.g. inside the same colored box)
    # -1 = ungrouped
    group_ids: List[int]

    # Column id: blocks in the same x-column cluster (-1 = isolated)
    column_ids: List[int]

    # Row id: blocks in the same y-row cluster (-1 = isolated)
    row_ids: List[int]

    # insert_bboxes: final rendering bbox for each block
    # (derived from cell + original bbox + alignment)
    insert_bboxes: List[fitz.Rect]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _y_overlap(a: fitz.Rect, b: fitz.Rect, tol: float) -> bool:
    """Return True if rect *a* overlaps rect *b* in the Y dimension within *tol*."""
    return a.y0 < b.y1 + tol and a.y1 > b.y0 - tol


def _x_overlap(a: fitz.Rect, b: fitz.Rect, tol: float) -> bool:
    """Return True if rect *a* overlaps rect *b* in the X dimension within *tol*."""
    return a.x0 < b.x1 + tol and a.x1 > b.x0 - tol


def _rect_key(r: fitz.Rect) -> Tuple[float, float, float, float]:
    """Stable dict key for a fitz.Rect (fitz.Rect is not hashable)."""
    return (r.x0, r.y0, r.x1, r.y1)


# ---------------------------------------------------------------------------
# Main analyzer
# ---------------------------------------------------------------------------


class TopologyAnalyzer:
    """Analyze spatial topology of text blocks on a single PDF page."""

    def __init__(self, page_rect: fitz.Rect) -> None:
        self.page_rect = page_rect

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def analyze(
        self,
        bboxes: List[fitz.Rect],
        alignments: List[int],
        drawings: List[dict],
        image_obstacles: List[fitz.Rect],
    ) -> TopologyResult:
        """Full topology analysis pipeline.

        Parameters
        ----------
        bboxes:
            Original text block bounding boxes (one per block).
        alignments:
            Text alignment per block (0 = LEFT, 2 = RIGHT).
        drawings:
            Raw drawing dicts as returned by ``page.get_drawings()``.
        image_obstacles:
            Bounding boxes of images on the page.

        Returns
        -------
        TopologyResult
            All computed topology fields.
        """
        n = len(bboxes)

        # Step 1 — container detection
        containers = self._detect_containers(bboxes, drawings)

        # Step 2 — group assignment
        group_ids = self._assign_groups(containers)

        # Step 3 — column clustering
        column_ids = self._cluster_axis(bboxes, axis="x", tol=15.0)

        # Step 4 — row clustering
        row_ids = self._cluster_axis(bboxes, axis="y", tol=8.0)

        # Step 5 — row-aware Voronoi cells
        cells = self._compute_cells(bboxes, image_obstacles, containers)

        # Step 6 — insert_bbox computation
        insert_bboxes = self._compute_insert_bboxes(
            bboxes, alignments, cells, image_obstacles
        )

        return TopologyResult(
            cells=cells,
            containers=containers,
            group_ids=group_ids,
            column_ids=column_ids,
            row_ids=row_ids,
            insert_bboxes=insert_bboxes,
        )

    # ------------------------------------------------------------------
    # Step 1: Container detection
    # ------------------------------------------------------------------

    def _detect_containers(
        self,
        bboxes: List[fitz.Rect],
        drawings: List[dict],
    ) -> List[Optional[fitz.Rect]]:
        """For each block, find the smallest filled drawing rect that contains it."""

        page_area = self.page_rect.width * self.page_rect.height

        # Pre-filter candidate drawing rects
        candidate_rects: List[fitz.Rect] = []
        for d in drawings:
            fill = d.get("fill")
            if fill is None:
                continue
            # Require at least one non-near-white channel (any channel < 0.9)
            if not any(c < 0.9 for c in fill[:3]):
                continue
            rect = d.get("rect")
            if rect is None:
                continue
            r = fitz.Rect(rect)
            if r.is_empty:
                continue
            rect_area = r.width * r.height
            if rect_area >= 0.60 * page_area:
                continue
            candidate_rects.append(r)

        containers: List[Optional[fitz.Rect]] = []
        for b in bboxes:
            best: Optional[fitz.Rect] = None
            best_area = float("inf")
            for r in candidate_rects:
                if (
                    r.x0 <= b.x0 + 2
                    and r.y0 <= b.y0 + 2
                    and r.x1 >= b.x1 - 2
                    and r.y1 >= b.y1 - 2
                ):
                    area = r.width * r.height
                    if area < best_area:
                        best_area = area
                        best = r
            containers.append(best)

        return containers

    # ------------------------------------------------------------------
    # Step 2: Group assignment
    # ------------------------------------------------------------------

    def _assign_groups(
        self,
        containers: List[Optional[fitz.Rect]],
    ) -> List[int]:
        """Assign group ids based on shared container rects."""

        # Collect distinct containers (non-None), sorted by top-left corner
        seen: Dict[Tuple[float, float, float, float], int] = {}
        distinct: List[fitz.Rect] = []
        for c in containers:
            if c is not None:
                k = _rect_key(c)
                if k not in seen:
                    distinct.append(c)

        # Sort by (y0, x0)
        distinct.sort(key=lambda r: (r.y0, r.x0))
        for idx, r in enumerate(distinct):
            seen[_rect_key(r)] = idx

        group_ids: List[int] = []
        for c in containers:
            if c is None:
                group_ids.append(-1)
            else:
                group_ids.append(seen[_rect_key(c)])

        return group_ids

    # ------------------------------------------------------------------
    # Step 3 & 4: Column / row clustering
    # ------------------------------------------------------------------

    def _cluster_axis(
        self,
        bboxes: List[fitz.Rect],
        axis: str,
        tol: float,
    ) -> List[int]:
        """Greedy 1-D clustering along *axis* ('x' uses x0, 'y' uses y0).

        Returns a list of cluster ids (same length as *bboxes*).
        Isolated blocks (singleton clusters) get id -1.
        """
        n = len(bboxes)
        if n == 0:
            return []

        # Index blocks by their coordinate
        coord_fn = (lambda b: b.x0) if axis == "x" else (lambda b: b.y0)
        order = sorted(range(n), key=lambda i: coord_fn(bboxes[i]))

        # Greedy merge: consecutive elements within *tol* join the same cluster
        raw_cluster: List[int] = [0] * n
        cluster_id = 0
        raw_cluster[order[0]] = cluster_id
        for k in range(1, n):
            prev_coord = coord_fn(bboxes[order[k - 1]])
            curr_coord = coord_fn(bboxes[order[k]])
            if curr_coord - prev_coord >= tol:
                cluster_id += 1
            raw_cluster[order[k]] = cluster_id

        # Count cluster sizes
        sizes: Dict[int, int] = {}
        for cid in raw_cluster:
            sizes[cid] = sizes.get(cid, 0) + 1

        # Re-map: singletons → -1, multi-block clusters keep sequential ids
        multi_ids = sorted(cid for cid, sz in sizes.items() if sz > 1)
        remap: Dict[int, int] = {cid: i for i, cid in enumerate(multi_ids)}

        result: List[int] = []
        for cid in raw_cluster:
            result.append(remap[cid] if cid in remap else -1)

        return result

    # ------------------------------------------------------------------
    # Step 5: Row-aware Voronoi cells
    # ------------------------------------------------------------------

    def _compute_cells(
        self,
        bboxes: List[fitz.Rect],
        image_obstacles: List[fitz.Rect],
        containers: List[Optional[fitz.Rect]],
    ) -> List[fitz.Rect]:
        """Compute a row-aware Voronoi-style cell for each block."""

        cells: List[fitz.Rect] = []

        for i, b in enumerate(bboxes):
            Y_TOL = b.height
            X_TOL = b.width

            # All other block bboxes act as obstacles
            other_bboxes = [bboxes[j] for j in range(len(bboxes)) if j != i]
            obstacles = other_bboxes + list(image_obstacles)

            x0_cell = self.page_rect.x0
            x1_cell = self.page_rect.x1
            y0_cell = self.page_rect.y0
            y1_cell = self.page_rect.y1

            for o in obstacles:
                # Left neighbour (o is to the left of b)
                if o.x1 <= b.x0 + 0.5 and _y_overlap(o, b, Y_TOL):
                    x0_cell = max(x0_cell, (o.x1 + b.x0) / 2.0)

                # Right neighbour (o is to the right of b)
                if o.x0 >= b.x1 - 0.5 and _y_overlap(o, b, Y_TOL):
                    x1_cell = min(x1_cell, (o.x0 + b.x1) / 2.0)

                # Top neighbour (o is above b)
                if o.y1 <= b.y0 + 0.5 and _x_overlap(o, b, X_TOL):
                    y0_cell = max(y0_cell, (o.y1 + b.y0) / 2.0)

                # Bottom neighbour (o is below b)
                if o.y0 >= b.y1 - 0.5 and _x_overlap(o, b, X_TOL):
                    y1_cell = min(y1_cell, (o.y0 + b.y1) / 2.0)

            # Guarantee cell contains original bbox
            x0_cell = min(x0_cell, b.x0)
            x1_cell = max(x1_cell, b.x1)
            y0_cell = min(y0_cell, b.y0)
            y1_cell = max(y1_cell, b.y1)

            cell = fitz.Rect(x0_cell, y0_cell, x1_cell, y1_cell)

            # If block has a container, clip cell to container interior (2px margin)
            c = containers[i]
            if c is not None:
                inner = fitz.Rect(c.x0 + 2, c.y0 + 2, c.x1 - 2, c.y1 - 2)
                cell = cell & inner
                # After clipping, re-guarantee cell contains original bbox
                cell = fitz.Rect(
                    min(cell.x0, b.x0),
                    min(cell.y0, b.y0),
                    max(cell.x1, b.x1),
                    max(cell.y1, b.y1),
                )

            cells.append(cell)

        return cells

    # ------------------------------------------------------------------
    # Step 6: insert_bbox computation
    # ------------------------------------------------------------------

    def _compute_insert_bboxes(
        self,
        bboxes: List[fitz.Rect],
        alignments: List[int],
        cells: List[fitz.Rect],
        image_obstacles: List[fitz.Rect],
    ) -> List[fitz.Rect]:
        """Compute the final rendering bbox for each block."""

        MARGIN = 1.0
        insert_bboxes: List[fitz.Rect] = []

        for i, (b, align, cell) in enumerate(zip(bboxes, alignments, cells)):
            if align == 2:  # RIGHT
                x0 = cell.x0 + MARGIN
                x1 = b.x1
            else:  # LEFT / CENTER
                x0 = b.x0
                x1 = cell.x1 - MARGIN

            y0 = b.y0
            y1 = cell.y1 - MARGIN

            # Guarantee insert_bbox contains original bbox
            x0 = min(x0, b.x0)
            x1 = max(x1, b.x1)
            y0 = min(y0, b.y0)
            y1 = max(y1, b.y1)

            # Clip right edge against image obstacles
            for obs in image_obstacles:
                # Obstacle overlaps right portion of insert_bbox
                if (
                    obs.x0 > x0 + 10
                    and obs.x0 < x1
                    and obs.y0 < y1
                    and obs.y1 > y0
                ):
                    x1 = min(x1, obs.x0 - 2)

            # Final guarantee after obstacle clipping
            x1 = max(x1, b.x1)

            insert_bboxes.append(fitz.Rect(x0, y0, x1, y1))

        return insert_bboxes
