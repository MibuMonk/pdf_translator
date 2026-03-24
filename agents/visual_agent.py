"""
visual_agent.py — Visual optimization module for the PDF layout agent.

Handles typography decisions: font size fitting, consistency normalization,
and color adjustment for translated PDF pages.
"""

from typing import List, Optional

import fitz  # PyMuPDF

from shared_utils import has_cjk  # noqa: E402

LINE_HEIGHT = 1.2


class VisualOptimizer:
    """Encapsulates visual optimization operations for a single PDF page."""

    def __init__(
        self,
        page: fitz.Page,
        fontname: Optional[str] = None,
        fontfile: Optional[str] = None,
    ):
        self.page = page
        self.fontname = fontname
        self.fontfile = fontfile

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _font_kw(self, fontname: Optional[str]) -> dict:
        """Build keyword args for insert_textbox based on font settings."""
        kw: dict = {}
        if fontname:
            kw["fontname"] = fontname
        if self.fontfile:
            kw["fontfile"] = self.fontfile
        return kw

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def overflow_bbox(
        self,
        bbox: fitz.Rect,
        text: str,
        target_size: float,
        color: tuple,
        align: int,
        page_rect: fitz.Rect,
    ) -> fitz.Rect:
        """Expand *bbox* vertically so that *text* fits at *target_size*.

        Used when fitting_size would go below the readability floor (8pt).
        Instead of shrinking font below 8pt, we expand the bbox downward
        (clamped to page bounds) so text remains readable.

        Returns the expanded bbox (or original if it already fits).
        """
        fontname = self.fontname
        if fontname and not has_cjk(text):
            fontname = None
        if has_cjk(text):
            text = text.replace(" ", "\u00a0")
        font_kw = self._font_kw(fontname)

        # Check if text already fits
        try:
            shape = self.page.new_shape()
            result = shape.insert_textbox(
                bbox, text,
                fontsize=target_size,
                color=color,
                align=align,
                lineheight=LINE_HEIGHT,
                **font_kw,
            )
            if result >= 0:
                return bbox
        except Exception:
            return bbox

        # Binary search for required height (vertical expansion only first)
        lo_h = bbox.height
        hi_h = max(bbox.height * 4, target_size * 20)  # generous upper bound
        max_y1 = page_rect.height - 2  # stay within page

        for _ in range(10):
            mid_h = (lo_h + hi_h) / 2.0
            new_y1 = min(bbox.y0 + mid_h, max_y1)
            test_bbox = fitz.Rect(bbox.x0, bbox.y0, bbox.x1, new_y1)
            try:
                shape = self.page.new_shape()
                result = shape.insert_textbox(
                    test_bbox, text,
                    fontsize=target_size,
                    color=color,
                    align=align,
                    lineheight=LINE_HEIGHT,
                    **font_kw,
                )
                if result >= 0:
                    hi_h = mid_h
                else:
                    lo_h = mid_h
            except Exception:
                hi_h = mid_h

        final_y1 = min(bbox.y0 + hi_h, max_y1)
        vert_bbox = fitz.Rect(bbox.x0, bbox.y0, bbox.x1, final_y1)

        # Check if vertical-only expansion is sufficient
        try:
            shape = self.page.new_shape()
            result = shape.insert_textbox(
                vert_bbox, text,
                fontsize=target_size,
                color=color,
                align=align,
                lineheight=LINE_HEIGHT,
                **font_kw,
            )
            if result >= 0:
                return vert_bbox
        except Exception:
            pass

        # Vertical expansion hit page bounds — also try horizontal expansion.
        # Expand left/right symmetrically, clamped to page margins (2px inset).
        min_x0 = page_rect.x0 + 2
        max_x1 = page_rect.x1 - 2
        avail_left = bbox.x0 - min_x0
        avail_right = max_x1 - bbox.x1
        # Binary search for horizontal expansion amount
        lo_dx, hi_dx = 0.0, max(avail_left, avail_right)
        best_bbox = vert_bbox
        for _ in range(10):
            mid_dx = (lo_dx + hi_dx) / 2.0
            exp_x0 = max(bbox.x0 - min(mid_dx, avail_left), min_x0)
            exp_x1 = min(bbox.x1 + min(mid_dx, avail_right), max_x1)
            test_bbox = fitz.Rect(exp_x0, bbox.y0, exp_x1, final_y1)
            try:
                shape = self.page.new_shape()
                result = shape.insert_textbox(
                    test_bbox, text,
                    fontsize=target_size,
                    color=color,
                    align=align,
                    lineheight=LINE_HEIGHT,
                    **font_kw,
                )
                if result >= 0:
                    best_bbox = test_bbox
                    hi_dx = mid_dx
                else:
                    lo_dx = mid_dx
            except Exception:
                hi_dx = mid_dx

        return best_bbox

    def fitting_size(
        self,
        bbox: fitz.Rect,
        text: str,
        base_size: float,
        color: tuple,
        align: int,
        min_size: float = 6.0,
    ) -> float:
        """Binary search for largest font size in [min_size, base_size] that fits text in bbox."""

        # Preprocess: clear fontname for pure-ASCII text
        fontname = self.fontname
        if fontname and not has_cjk(text):
            fontname = None

        # CJK: replace spaces with non-breaking spaces to avoid unwanted wrapping
        if has_cjk(text):
            text = text.replace(" ", "\u00a0")

        font_kw = self._font_kw(fontname)

        # Small bbox shortcut: ASCII-only text can use height/LINE_HEIGHT estimate.
        # CJK text always goes through _fits() binary search for accurate measurement.
        if bbox.height > 0 and bbox.height < base_size * LINE_HEIGHT and not has_cjk(text):
            return max(min_size, min(base_size, bbox.height / LINE_HEIGHT))

        def _fits(size: float) -> bool:
            try:
                shape = self.page.new_shape()
                result = shape.insert_textbox(
                    bbox,
                    text,
                    fontsize=size,
                    color=color,
                    align=align,
                    lineheight=LINE_HEIGHT,
                    **font_kw,
                )
                # shape is discarded (not committed)
                return result >= 0
            except Exception:
                # Unsupported font or other error → treat as fits
                return True

        # ASCII pre-check: try base_size with reduced line heights first
        if not has_cjk(text):
            for lh in (1.2, 1.0):
                try:
                    shape = self.page.new_shape()
                    result = shape.insert_textbox(
                        bbox,
                        text,
                        fontsize=base_size,
                        color=color,
                        align=align,
                        lineheight=lh,
                        **font_kw,
                    )
                    if result >= 0:
                        return base_size
                except Exception:
                    return base_size

        # Binary search: 8 iterations
        if _fits(base_size):
            return base_size
        if not _fits(min_size):
            return min_size

        lo = min_size
        hi = base_size
        for _ in range(8):
            mid = (lo + hi) / 2.0
            if _fits(mid):
                lo = mid
            else:
                hi = mid

        return max(min_size, lo)

    def consistency_map(
        self,
        fitting_sizes: List[float],
        base_sizes: List[float],
        title_mask: List[bool],
        percentile: float = 0.90,
    ) -> List[float]:
        """
        Apply 80th-percentile cap per base_size group.
        Returns render_sizes[i] = min(fitting_sizes[i], cap[base_sizes[i]]).
        Title blocks (title_mask[i]=True) are uncapped.

        REQ-4: Compute global_body_cap from all non-title fitting sizes (when
        there are >= 3 non-title blocks) and apply it to small non-title groups
        (< 3 members) to prevent isolated body blocks from rendering at title scale.
        """
        n = len(fitting_sizes)

        # Collect non-title indices per base_size group
        groups: dict = {}
        for i in range(n):
            if not title_mask[i]:
                key = base_sizes[i]
                groups.setdefault(key, []).append(i)

        # REQ-4: Compute global_body_cap from all non-title fitting sizes
        non_title_indices = [i for i in range(n) if not title_mask[i]]
        if len(non_title_indices) >= 3:
            global_body_cap: Optional[float] = max(
                fitting_sizes[i] for i in non_title_indices
            )
        else:
            global_body_cap = None

        # Compute cap per base_size
        cap: dict = {}
        for base_size, indices in groups.items():
            if len(indices) < 3:
                # REQ-4: small group → cap at min(base_size, global_body_cap)
                if global_body_cap is not None:
                    cap[base_size] = min(base_size, global_body_cap)
                else:
                    cap[base_size] = base_size
            else:
                sorted_desc = sorted(
                    [fitting_sizes[i] for i in indices], reverse=True
                )
                k = int(len(sorted_desc) * (1 - percentile))  # int(n * 0.20)
                cap[base_size] = sorted_desc[k]

        # Build result
        result: List[float] = []
        for i in range(n):
            if title_mask[i]:
                result.append(fitting_sizes[i])
            else:
                c = cap.get(base_sizes[i], base_sizes[i])
                result.append(min(fitting_sizes[i], c))
        return result

    def adjust_color(
        self,
        source_color: tuple,
        background_color: Optional[tuple] = None,
    ) -> tuple:
        """
        Ensure text is legible against its background.
        - If background is dark (luminance < 0.3) and source_color is dark → return white (1,1,1)
        - If background is light and source_color is white → return black (0,0,0)
        - Otherwise return source_color unchanged
        """
        if background_color is None:
            return source_color

        def luminance(color: tuple) -> float:
            r, g, b = color[0], color[1], color[2]
            return 0.299 * r + 0.587 * g + 0.114 * b

        bg_lum = luminance(background_color)
        src_lum = luminance(source_color)

        if bg_lum < 0.3 and src_lum < 0.3:
            return (1.0, 1.0, 1.0)  # dark bg, dark text → white
        if bg_lum > 0.7 and src_lum > 0.7:
            return (0.0, 0.0, 0.0)  # light bg, white text → black
        return source_color
