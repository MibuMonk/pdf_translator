"""
visual_agent.py — Visual optimization module for the PDF layout agent.

Handles typography decisions: font size fitting, consistency normalization,
and color adjustment for translated PDF pages.
"""

from typing import List, Optional

import fitz  # PyMuPDF

LINE_HEIGHT = 1.4


def has_cjk(text: str) -> bool:
    """Return True if text contains any CJK or kana character."""
    for ch in text:
        cp = ord(ch)
        if 0x4E00 <= cp <= 0x9FFF or 0x3040 <= cp <= 0x30FF:
            return True
    return False


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

    def fitting_size(
        self,
        bbox: fitz.Rect,
        text: str,
        base_size: float,
        color: tuple,
        align: int,
        min_size: float = 4.0,
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

        # Small bbox shortcut
        if bbox.height > 0 and bbox.height < base_size * 1.4:
            return max(min_size, min(base_size, bbox.height / 1.4))

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
        percentile: float = 0.80,
    ) -> List[float]:
        """
        Apply 80th-percentile cap per base_size group (body blocks only).
        Title blocks are uncapped but isolated from body groups.
        No body block may exceed the global body maximum fitting_size.
        """
        n = len(fitting_sizes)

        # Global body ceiling: body blocks must not exceed the largest body fitting_size
        body_fitting = [fitting_sizes[i] for i in range(n) if not title_mask[i]]
        global_body_cap = max(body_fitting) if body_fitting else float("inf")

        # Collect non-title indices per base_size group
        groups: dict = {}
        for i in range(n):
            if not title_mask[i]:
                key = base_sizes[i]
                groups.setdefault(key, []).append(i)

        # Compute 80th-percentile cap per base_size group (body only)
        cap: dict = {}
        for base_size, indices in groups.items():
            if len(indices) < 3:
                cap[base_size] = min(base_size, global_body_cap)
            else:
                sorted_desc = sorted(
                    [fitting_sizes[i] for i in indices], reverse=True
                )
                k = int(len(sorted_desc) * (1 - percentile))
                cap[base_size] = min(sorted_desc[k], global_body_cap)

        # Build result
        result: List[float] = []
        for i in range(n):
            if title_mask[i]:
                result.append(fitting_sizes[i])
            else:
                c = cap.get(base_sizes[i], min(base_sizes[i], global_body_cap))
                result.append(min(fitting_sizes[i], c))
        return result

    def parallel_normalize(
        self,
        render_sizes: List[float],
        source_colors: List[tuple],
        bboxes: List["fitz.Rect"],
        x_tol: float = 15.0,
        y_tol: float = 6.0,
        min_group: int = 2,
    ) -> tuple:
        """
        Detect parallel sibling blocks and normalize their font sizes and colors.

        Parallel = blocks sharing the same x0 (vertical stack in same column)
                   OR same y0 (horizontal row of siblings).
        Within each group: font size → min(fitting_sizes); color → most common.

        Returns (normalized_sizes, normalized_colors).
        """
        import statistics

        n = len(render_sizes)
        sizes = list(render_sizes)
        colors = list(source_colors)

        def _group_by(key_fn):
            buckets: dict = {}
            for i in range(n):
                k = round(key_fn(i) / x_tol) * x_tol  # bin to tolerance grid
                buckets.setdefault(k, []).append(i)
            return [idxs for idxs in buckets.values() if len(idxs) >= min_group]

        x0_groups = _group_by(lambda i: bboxes[i].x0)
        y0_groups = _group_by(lambda i: bboxes[i].y0)

        for group in x0_groups + y0_groups:
            # Unify font size to minimum (most conservative fit)
            min_size = min(sizes[i] for i in group)
            for i in group:
                sizes[i] = min_size

            # Unify color to most common
            color_list = [colors[i] for i in group]
            try:
                dominant = statistics.mode(color_list)
            except statistics.StatisticsError:
                dominant = color_list[0]
            for i in group:
                colors[i] = dominant

        return sizes, colors

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
