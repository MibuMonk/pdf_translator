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
        Apply 80th-percentile cap per base_size group.
        Returns render_sizes[i] = min(fitting_sizes[i], cap[base_sizes[i]]).
        Title blocks (title_mask[i]=True) are uncapped.
        """
        n = len(fitting_sizes)

        # Collect non-title indices per base_size group
        groups: dict = {}
        for i in range(n):
            if not title_mask[i]:
                key = base_sizes[i]
                groups.setdefault(key, []).append(i)

        # Compute cap per base_size
        cap: dict = {}
        for base_size, indices in groups.items():
            if len(indices) < 3:
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
