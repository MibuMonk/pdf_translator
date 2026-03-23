#!/usr/bin/env python3
"""
PDF Translator - 翻译 PDF 文件同时保留排版
使用已登录的 Claude Code CLI，无需额外 API Key
支持中文（简体/繁体）、日语、英语互译，特别针对 PPTX 导出的 PDF 优化
"""

import fitz  # PyMuPDF
import argparse
import os
import sys
import subprocess
import shutil
import math
import json
import re
import numpy as np
from typing import Optional, List, Tuple

SUPPORTED_LANGUAGES = {
    "en": "English",
    "zh": "中文（简体）",
    "zh-TW": "中文（繁體）",
    "ja": "日本語",
}

# 日文优先的 CJK 字体路径（按优先级）
JA_FONT_PATHS = [
    # macOS 日文字体
    "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",
    "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
    "/System/Library/Fonts/YuGo-Medium.otf",
    "/System/Library/Fonts/YuGo-Bold.otf",
    # fallback: 通用 CJK
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/PingFang.ttc",
]

ZH_FONT_PATHS = [
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
]

GENERIC_CJK_FONT_PATHS = [
    # Linux
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    # Windows
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msgothic.ttc",
]

# claude CLI 路径
CLAUDE_CLI = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")

# 这些文本模式原样保留，不翻译也不重绘（watermark / footer 等）
SKIP_TEXT_PATTERNS = [
    re.compile(r"confidential\s+for\s+honda", re.IGNORECASE),
    re.compile(r"confidential\s+view\s+only", re.IGNORECASE),
    re.compile(r"^\s*confidential\s*$", re.IGNORECASE),
]


def is_skip_text(text: str) -> bool:
    """返回 True 表示该文本块应原样保留，不做任何处理。"""
    t = text.strip()
    if any(p.search(t) for p in SKIP_TEXT_PATTERNS):
        return True
    # 碎片水印检测：短文本中含有 Confidential/Honda 片段
    if len(t) <= 25:
        tl = t.lower().replace('\xa0', ' ')  # normalize nbsp to space
        if any(frag in tl for frag in ('confid', 'nfide', 'fidenti', 'honda', 'view only', 'ential', 'w only', ' only')):
            return True
    return False


def find_cjk_font(target_lang: str, hint: Optional[str] = None) -> Optional[str]:
    if hint and os.path.exists(hint):
        return hint
    candidates = (
        JA_FONT_PATHS if target_lang == "ja"
        else ZH_FONT_PATHS if target_lang in ("zh", "zh-TW")
        else []
    ) + GENERIC_CJK_FONT_PATHS
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def color_from_int(c: int) -> Tuple[float, float, float]:
    return ((c >> 16 & 0xFF) / 255.0, (c >> 8 & 0xFF) / 255.0, (c & 0xFF) / 255.0)


def _normalize_span_text(span: dict) -> str:
    """
    把特殊符号字体（Wingdings/Symbol）里的伪装字符还原为标准 Unicode。
    例如 Wingdings 的 § (U+00A7) 在幻灯片里实际显示为 bullet •。
    """
    text = span["text"]
    font = span.get("font", "")
    if "Wingdings" in font or "Webdings" in font:
        # 常见 Wingdings → Unicode bullet 映射
        text = text.replace("§", "•").replace("v", "•").replace("Ø", "•")
        text = text.replace("q", "■")   # Wingdings q (0x71) → black square
        text = text.replace("ü", "✔")  # Wingdings ü (0xFC) → heavy check mark
    return text


def is_watermark_block(block: dict, page_rect: fitz.Rect) -> bool:
    """检测文本块是否是水印（旋转文字）。
    注意：曾有一个面积 >50% 的判断，但会误杀正文大块（如整页正文列表），已移除。
    对内容水印（"Confidential" 等）由 is_skip_text 处理；隐藏块由 is_hidden 处理。
    """
    # 检测旋转文字（水印通常斜置，|sin θ| > 0.1 约 6° 以上）
    for line in block.get("lines", []):
        _, dir_y = line.get("dir", (1, 0))
        if abs(dir_y) > 0.1:
            return True

    return False


def _has_cjk(text: str) -> bool:
    """返回 True 若 text 包含任意 CJK / 假名字符，或需要 CJK 字体的特殊符号。"""
    for c in text:
        cp = ord(c)
        if (0x4E00 <= cp <= 0x9FFF or
                0x3040 <= cp <= 0x30FF or   # 平假名 + 片假名
                0xFF00 <= cp <= 0xFFEF or   # 全角字符
                0x3000 <= cp <= 0x303F or   # CJK 标点
                0x25A0 <= cp <= 0x25FF or   # Geometric Shapes (■□▲▶◆○●▸►…)
                0x2600 <= cp <= 0x26FF or   # Miscellaneous Symbols (✓☆★…)
                0x2700 <= cp <= 0x27BF or   # Dingbats
                0x2010 <= cp <= 0x2027 or   # 一般的なダッシュ・ハイフン類 (—–…‐)
                0x2030 <= cp <= 0x205E):    # 引用符・記号類 (‹›«»†‡…)
            return True
    return False


def estimate_em_width(text: str) -> float:
    """估算文本宽度（以 em 为单位）。CJK 字符 = 1em，半角 = 0.55em。"""
    w = 0.0
    for c in text:
        cp = ord(c)
        if (0x4E00 <= cp <= 0x9FFF or   # CJK 统一汉字
            0x3040 <= cp <= 0x309F or   # 平假名
            0x30A0 <= cp <= 0x30FF or   # 片假名
            0xFF00 <= cp <= 0xFFEF or   # 全角字符
            0x3000 <= cp <= 0x303F):    # CJK 标点
            w += 1.0
        else:
            w += 0.55
    return w


def _truncate_to_em_width(text: str, max_em: float) -> str:
    """截断 text 至 max_em em 宽度并追加省略号；宽度足够时原样返回。"""
    if estimate_em_width(text) <= max_em:
        return text
    keep, em = [], 0.0
    for ch in text:
        cp = ord(ch)
        w = (1.0 if (0x4e00 <= cp <= 0x9fff or 0x3040 <= cp <= 0x30ff
                     or 0xff00 <= cp <= 0xffef or 0x3000 <= cp <= 0x303f) else 0.55)
        if em + w > max_em - 1.0:
            break
        keep.append(ch)
        em += w
    return "".join(keep) + "…"


def try_expand_bbox(orig_bbox: fitz.Rect, text: str, font_size: float,
                    page_rect: fitz.Rect,
                    other_bboxes: List[fitz.Rect],
                    orig_pixmap=None) -> fitz.Rect:
    """
    弹性文本框：尝试扩展 orig_bbox，使 text 在 font_size 下放得下。
    约束：
    1. 不与 other_bboxes 中的任何文本框发生碰撞（重叠超过 1px）
    2. 扩展出去的新增区域中，非白色像素占比不超过 5%
       （防止扩展进入图表框线/彩色区域，导致译文浮在矢量图上层）
    优先向右扩展宽度（单行），其次向下扩展高度（多行）。
    最多扩展到原始面积的 4 倍。
    """
    em_w = estimate_em_width(text)
    if em_w == 0 or font_size <= 0:
        return orig_bbox

    line_height = font_size * _LINE_HEIGHT_FACTOR
    EDGE = 2        # 距页面边缘最小留白
    MAX_MUL = 4.0   # 最大扩展倍率（面积）
    GRAPHIC_DARK_THRESHOLD = 0.05  # 扩展新增区域暗像素 > 5% → 有矢量图形 → 放弃扩展
    orig_area = max(1.0, orig_bbox.width * orig_bbox.height)

    # 预先提取 pixmap 参数（可能为 None）
    pix_samples = orig_pixmap.samples if orig_pixmap else None
    pix_w = orig_pixmap.width if orig_pixmap else 0
    pix_h = orig_pixmap.height if orig_pixmap else 0
    pix_stride = pix_w * 3

    def expansion_is_clear(cand: fitz.Rect) -> bool:
        """扩展新增区域（cand - orig_bbox）的暗像素占比 < GRAPHIC_DARK_THRESHOLD。"""
        if pix_samples is None:
            return True  # 无 pixmap 时不做检查
        # 新增区域 = cand 减去 orig_bbox（取差集的包围矩形，用两个子矩形近似）
        # 简化：只检查扩展出 orig_bbox 右边和下边的矩形
        regions = []
        if cand.x1 > orig_bbox.x1 + 1:   # 向右扩展的新列
            regions.append((orig_bbox.x1, cand.y0, cand.x1, cand.y1))
        if cand.y1 > orig_bbox.y1 + 1:   # 向下扩展的新行（原宽度部分）
            regions.append((cand.x0, orig_bbox.y1, orig_bbox.x1, cand.y1))
        if not regions:
            return True
        total = dark = 0
        for rx0, ry0, rx1, ry1 in regions:
            x0 = max(0, int(rx0)); y0 = max(0, int(ry0))
            x1 = min(pix_w, int(rx1)); y1 = min(pix_h, int(ry1))
            for py in range(y0, y1):
                row_off = py * pix_stride + x0 * 3
                for po in range(row_off, row_off + (x1 - x0) * 3, 3):
                    r = pix_samples[po]; g = pix_samples[po+1]; b = pix_samples[po+2]
                    brightness = (r*299 + g*587 + b*114) // 1000
                    total += 1
                    if brightness < 200:  # 稍宽松：非纯白区域
                        dark += 1
        return (dark / total) < GRAPHIC_DARK_THRESHOLD if total > 0 else True

    def no_collision(cand: fitz.Rect) -> bool:
        for ob in other_bboxes:
            # 元の bbox と既に重なっているブロックは同一セルの兄弟ブロック → スキップ
            if orig_bbox.intersects(ob):
                continue
            inter = cand & ob
            if inter.width > 1 and inter.height > 1:
                return False
        return True

    # 宽度扩展上限：只有当单行所需宽度在原宽的 N 倍以内时才尝试横向扩展。
    # 默认 1.3 倍；对短数字串（如 >1000万、200K）放宽至 2.0 倍，
    # 防止数字在中间被 insert_textbox 折行（>100\n0万 这类视觉错误）。
    # 判定条件：≤15字符、含有数字、不含3个以上连续 CJK（排除日文句子）。
    MAX_WIDTH_EXPAND = 1.3
    _t = text.strip()
    if (len(_t) <= 15
            and re.search(r'\d', _t)
            and not re.search(r'[\u3040-\u30ff\u4e00-\u9fff]{3,}', _t)):
        MAX_WIDTH_EXPAND = 2.5
    elif (len(_t) <= 8
            and re.search(r'^[\u3040-\u30ff\u4e00-\u9fff\uff66-\uff9f]+$', _t)):
        # 短い単語（≤8字の純CJK）: 単行に収めるため幅を最大2.5倍まで拡張。
        # no_collision チェックにより表セルなど隣接ブロックへの重なりは防止。
        MAX_WIDTH_EXPAND = 2.5

    best = fitz.Rect(orig_bbox)

    # ── Phase 1: 轻度向右扩展，仅在单行所需宽度 ≤ 原宽 × MAX_WIDTH_EXPAND 时生效 ────
    needed_w_1row = em_w * font_size * 1.05
    if orig_bbox.width < needed_w_1row <= orig_bbox.width * MAX_WIDTH_EXPAND:
        new_x1 = min(page_rect.width - EDGE, orig_bbox.x0 + needed_w_1row)
        new_y1 = max(orig_bbox.y1,
                     min(page_rect.height - EDGE, orig_bbox.y0 + line_height * 1.1))
        cand = fitz.Rect(orig_bbox.x0, orig_bbox.y0, new_x1, new_y1)
        if cand.width * cand.height <= orig_area * MAX_MUL and no_collision(cand) and expansion_is_clear(cand):
            best = cand

    # ── Phase 2: 以当前最优宽度，向下扩展高度（多行）───────────
    # 文本超过原宽 1.3 倍时 best 仍为 orig_bbox，多余内容通过换行向下延伸。
    best_w = best.x1 - best.x0
    chars_per_row = max(1.0, best_w / font_size)
    # 明示的な改行（\n）を含む場合は行ごとに計算し合計する
    total_rows = 0
    for raw_line in text.split("\n"):
        line_em = estimate_em_width(raw_line)
        total_rows += math.ceil(line_em / chars_per_row) if line_em > 0 else 1
    rows = max(1, total_rows)
    # CJK フォントの実効行高は line_height より大きい（PyMuPDF の lineheight パラメータは
    # フォント固有の自然行高に対する倍率のため）。安全マージンを 1.1 → 1.4 に変更。
    needed_h = rows * line_height * 1.4
    if needed_h > (best.y1 - best.y0):
        new_y1 = min(page_rect.height - EDGE, best.y0 + needed_h)
        cand2 = fitz.Rect(best.x0, best.y0, best.x1, new_y1)
        if cand2.width * cand2.height <= orig_area * MAX_MUL and no_collision(cand2) and expansion_is_clear(cand2):
            best = cand2

    return best


# ── Z-order helpers ───────────────────────────────────────────────────────────

_TM_RE = re.compile(
    rb'([-+]?[\d.]+)[ \t]+([-+]?[\d.]+)[ \t]+([-+]?[\d.]+)[ \t]+'
    rb'([-+]?[\d.]+)[ \t]+([-+]?[\d.]+)[ \t]+([-+]?[\d.]+)[ \t]+Tm'
)


def _find_bt_et_ranges(stream: bytes) -> List[Tuple[int, int]]:
    """Return (start, end) byte-offset pairs for every BT...ET block in stream."""
    result = []
    i, n = 0, len(stream)
    bt_start = -1

    while i < n:
        b = stream[i]
        # Skip literal string (...)
        if b == 0x28:
            i += 1
            depth = 1
            while i < n and depth:
                c = stream[i]
                if c == 0x5C:
                    i += 2
                elif c == 0x28:
                    depth += 1; i += 1
                elif c == 0x29:
                    depth -= 1; i += 1
                else:
                    i += 1
            continue
        # Skip hex string <...>
        if b == 0x3C and i + 1 < n and stream[i + 1] != 0x3C:
            i += 2
            while i < n and stream[i] != 0x3E:
                i += 1
            i += 1
            continue
        # Skip comment
        if b == 0x25:
            while i < n and stream[i] not in (0x0A, 0x0D):
                i += 1
            continue

        def _ws(k):
            return k >= n or stream[k] in b' \t\n\r\x0c\x00'

        if b == 0x42 and i + 1 < n and stream[i + 1] == 0x54 and _ws(i + 2):
            bt_start = i; i += 2; continue
        if b == 0x45 and i + 1 < n and stream[i + 1] == 0x54 and _ws(i + 2):
            if bt_start >= 0:
                result.append((bt_start, i + 2))
                bt_start = -1
            i += 2; continue
        i += 1
    return result


def _bt_get_tm_xy(bt_bytes: bytes, page_height: float):
    """Extract (x, y_mupdf) from the LAST Tm operator in a BT...ET block."""
    best = None
    for m in _TM_RE.finditer(bt_bytes):
        x = float(m.group(5))
        y = page_height - float(m.group(6))
        best = (x, y)
    return best


def _stream_anchor(stream: bytes, bt_start: int, ctx: int = 48) -> bytes:
    """Return up to `ctx` bytes immediately before bt_start (whitespace-stripped)."""
    raw = stream[max(0, bt_start - ctx * 2):bt_start]
    stripped = raw.rstrip(b' \t\n\r\x0c\x00')
    return stripped[-ctx:] if len(stripped) > ctx else stripped


def _find_q_before(stream: bytes, bt_start: int, max_lb: int = 200) -> int:
    """Scan backward from bt_start to find the standalone 'q' that opens the wrapper."""
    end = max(0, bt_start - max_lb)
    j = bt_start - 1
    while j >= end:
        if stream[j] == 0x71:  # 'q'
            before_ok = j == 0 or stream[j - 1] in b' \t\n\r\x0c\x00'
            after_ok  = j + 1 >= len(stream) or stream[j + 1] in b' \t\n\r\x0c\x00'
            if before_ok and after_ok:
                return j
        j -= 1
    return bt_start   # fallback: treat BT itself as start


def _find_Q_after(stream: bytes, et_end: int, max_la: int = 50) -> int:
    """Scan forward from et_end to find the standalone 'Q' that closes the wrapper."""
    end = min(len(stream), et_end + max_la)
    j = et_end
    while j < end:
        if stream[j] == 0x51:  # 'Q'
            before_ok = j == 0 or stream[j - 1] in b' \t\n\r\x0c\x00'
            after_ok  = j + 1 >= len(stream) or stream[j + 1] in b' \t\n\r\x0c\x00'
            if before_ok and after_ok:
                return j + 1  # inclusive
        j += 1
    return et_end   # fallback


def _apply_zorder(page: fitz.Page, item_anchors: List, translations: List[str],
                  verbose: bool = False):
    """
    Move the n_new BT...ET blocks that insert_textbox just appended to the page
    stream back to the z-positions specified by their anchors in the pre-redaction
    stream.  Blocks whose anchor could not be found remain at the end (on top).
    """
    n_new = sum(1 for t in translations if t)
    if n_new == 0:
        return
    if not any(a is not None for a in item_anchors):
        return  # nothing to reorder

    stream = page.read_contents()
    all_bt = _find_bt_et_ranges(stream)

    if len(all_bt) < n_new:
        return  # stream structure unexpected – bail out

    # The last n_new BT…ET blocks are our newly inserted text.
    # Each is wrapped in q … Q by insert_textbox/shape.commit(); capture full wrapper.
    first_new = len(all_bt) - n_new
    base_end = _find_q_before(stream, all_bt[first_new][0])
    base = bytearray(stream[:base_end])
    new_blocks = []
    for s, e in all_bt[first_new:]:
        q_start = _find_q_before(stream, s)
        Q_end   = _find_Q_after(stream, e)
        new_blocks.append(stream[q_start:Q_end])

    # Pair each non-empty translation with its new block, anchor, and frac
    paired = []
    nb_iter = iter(new_blocks)
    for (anchor, frac), trans in zip(item_anchors, translations):
        if not trans:
            continue
        blk = next(nb_iter, None)
        if blk is None:
            break
        paired.append((anchor, frac, blk))

    # Collect positions immediately after each standalone 'Q' in the base.
    # These are the only safe points to insert new q..BT..ET..Q wrappers.
    safe_q_positions: List[int] = []
    _i, _n = 0, len(base)
    while _i < _n:
        _b = base[_i]
        if _b == 0x28:                          # '(' literal string — skip
            _i += 1; _depth = 1
            while _i < _n and _depth:
                _c = base[_i]
                if _c == 0x5C: _i += 2
                elif _c == 0x28: _depth += 1; _i += 1
                elif _c == 0x29: _depth -= 1; _i += 1
                else: _i += 1
            continue
        if _b == 0x3C and _i + 1 < _n and base[_i + 1] != 0x3C:  # hex string
            _i += 2
            while _i < _n and base[_i] != 0x3E: _i += 1
            _i += 1; continue
        if _b == 0x25:                          # '%' comment
            while _i < _n and base[_i] not in (0x0A, 0x0D): _i += 1
            continue
        if _b == 0x51:                          # 'Q'
            _before = _i == 0 or base[_i - 1] in b' \t\n\r\x0c\x00'
            _after  = _i + 1 >= _n or base[_i + 1] in b' \t\n\r\x0c\x00'
            if _before and _after:
                safe_q_positions.append(_i + 1)  # position right after Q
        _i += 1

    def _snap_to_safe(pos: int, min_pos: int) -> int:
        """Return the first safe-Q position >= max(pos, min_pos), or -1 if none."""
        target = max(pos, min_pos)
        for sp in safe_q_positions:
            if sp >= target:
                return sp
        return -1

    insertions = []   # (insert_after_pos, block_bytes)
    no_anchor  = []   # block_bytes with no anchor → keep at end
    search_from = 0
    base_len = len(base)

    for anchor, frac, blk in paired:
        raw_pos = -1
        # 1. Try anchor bytes (progressively shorter suffixes survive normalization)
        if anchor:
            for trim in ([len(anchor)] if len(anchor) <= 16 else []) + [16, 12, 8]:
                if trim > len(anchor):
                    continue
                short = anchor[-trim:]
                pos = base.find(short, search_from)
                if pos >= 0:
                    raw_pos = pos + trim
                    break
        # 2. Fallback: fractional position in the post-redaction base stream
        if raw_pos < 0 and frac < 1.0:
            raw_pos = max(int(frac * base_len), search_from)
        # 3. Snap to nearest safe Q position
        if raw_pos >= 0:
            safe = _snap_to_safe(raw_pos, search_from)
            if safe >= 0:
                insertions.append((safe, blk))
                search_from = safe
            else:
                no_anchor.append(blk)
        else:
            no_anchor.append(blk)

    if not insertions and not no_anchor:
        return

    # Reconstruct stream
    result = bytearray()
    prev = 0
    for ins_pos, blk in sorted(insertions, key=lambda x: x[0]):
        result.extend(base[prev:ins_pos])
        result.extend(b'\n')
        result.extend(blk)
        result.extend(b'\n')
        prev = ins_pos
    result.extend(base[prev:])
    for blk in no_anchor:
        result.extend(b'\n')
        result.extend(blk)

    # Write back
    doc = page.parent
    contents = page.get_contents()
    if not contents:
        return
    doc.update_stream(contents[0], bytes(result))
    if len(contents) > 1:
        for xref in contents[1:]:
            try:
                doc.update_stream(xref, b'')
            except Exception:
                pass


def _find_fitting_size(
    page: fitz.Page, bbox: fitz.Rect, text: str,
    base_size: float, color: tuple, align: int,
    fontname: Optional[str] = None,
    min_size: float = 4.0,
) -> float:
    """
    Binary search for the largest font size in [min_size, base_size] that fits
    text in bbox. Uses Shape dry-run (no page write).
    """
    if fontname and not _has_cjk(text):
        fontname = None
    font_kw: dict = {}
    if fontname:
        font_kw["fontname"] = fontname

    if any('\u4e00' <= c <= '\u9fff' or '\u3040' <= c <= '\u30ff' for c in text):
        text = text.replace(' ', '\u00a0')

    # Small bbox: single-line shortcut
    if bbox.height < base_size * _LINE_HEIGHT_FACTOR and bbox.height > 0:
        return max(min_size, min(base_size, bbox.height / _LINE_HEIGHT_FACTOR))

    # ASCII pre-check at base_size with reduced lineheight
    if not _has_cjk(text):
        for _lh in [1.2, 1.0]:
            _shape = page.new_shape()
            try:
                _rc = _shape.insert_textbox(bbox, text, fontsize=base_size,
                                            color=color, align=align,
                                            lineheight=_lh, **font_kw)
                if _rc >= 0:
                    return base_size
            except Exception:
                break

    def _fits(size: float) -> bool:
        shape = page.new_shape()
        try:
            rc = shape.insert_textbox(bbox, text, fontsize=size,
                                      color=color, align=align,
                                      lineheight=_LINE_HEIGHT_FACTOR, **font_kw)
            return rc >= 0
        except Exception:
            return True  # unsupported font → assume fits

    if _fits(base_size):
        return base_size
    if not _fits(min_size):
        return min_size

    lo, hi = min_size, base_size
    for _ in range(8):  # 8 iterations → ~1pt precision
        mid = (lo + hi) / 2.0
        if _fits(mid):
            lo = mid
        else:
            hi = mid
    return max(min_size, lo)


def insert_text_fitting(page: fitz.Page, bbox: fitz.Rect, text: str,
                        base_size: float, color: tuple, align: int,
                        fontname: Optional[str] = None,
                        fontfile: Optional[str] = None,
                        min_factor: float = 0.4) -> bool:
    """
    用 Shape dry-run 精确验证字号是否放得下，找到最大合适字号后提交。
    fontname 是已通过 page.insert_font 注册的字体名，Shape 只接受 fontname。
    fontfile 仅用于兜底的 page.insert_textbox（不走 Shape）。
    """
    # 纯 ASCII / 拉丁文本（无 CJK / 假名）不使用 CJK 字体：
    # ヒラギノ等 CJK 字体渲染拉丁字母比原 PPT 字体宽约 30%，
    # 导致"Data SPA"等产品名在原本能容纳的宽度内溢出而被缩小。
    # 使用系统默认字体（Helvetica 系）可还原与原文相近的字宽。
    if fontname and not _has_cjk(text):
        fontname = None
        fontfile = None

    font_kw: dict = {}
    if fontname:
        font_kw["fontname"] = fontname

    # CJK 文本：将普通空格替换为不换行空格（U+00A0），防止 PyMuPDF 在空格处提前
    # 换行——日文中空格不是词语边界，每行应填满后才换行。
    if any('\u4e00' <= c <= '\u9fff' or '\u3040' <= c <= '\u30ff' for c in text):
        text = text.replace(' ', '\u00a0')

    # 对于 bbox 高度小于一行文字（legend 标签、小角标等），
    # 用 bbox 能容纳的最大字号直接插入，跳过缩减循环
    one_line_height = base_size * _LINE_HEIGHT_FACTOR
    if bbox.height < one_line_height and bbox.height > 0:
        fb_base: dict = {"color": color, "align": align}
        if fontname:
            fb_base["fontname"] = fontname
        elif fontfile:
            fb_base["fontfile"] = fontfile
            fb_base["fontname"] = "F0"
        # 先用 base_size + 压缩行距尝试（bbox 仅略矮于标准行高时仍能保持原字号）
        for lh in [1.0, 0.9, 0.8]:
            if bbox.height >= base_size * lh:
                try:
                    rc = page.insert_textbox(bbox, text, fontsize=base_size,
                                             lineheight=lh, **fb_base)
                    if rc >= 0:
                        return True
                except Exception:
                    break
        # base_size 行不通，用 forced_size 往下缩减
        forced_size = max(4.0, min(base_size, bbox.height / _LINE_HEIGHT_FACTOR))
        # 依次尝试更小字号 + 更小行距，直到文字不溢出（CJK 字体比英文需要更多垂直空间）
        # 行距从 1.4 逐步压缩到 0.8，确保即使 bbox 非常矮（如 5pt）也能放下单行文字。
        for trial_size in [forced_size, forced_size * 0.9, forced_size * 0.8,
                            forced_size * 0.7, forced_size * 0.6, 4.0]:
            trial_size = max(4.0, trial_size)
            # 大字号（≥20pt）不使用 lineheight<1.0：行间距小于字号会导致行间视觉重叠
            _lh_list = ([_LINE_HEIGHT_FACTOR, 1.2, 1.0, 0.8]
                        if trial_size < 20.0 else [_LINE_HEIGHT_FACTOR, 1.2, 1.0])
            for lh in _lh_list:
                fb = {**fb_base, "lineheight": lh}
                try:
                    rc = page.insert_textbox(bbox, text, fontsize=trial_size, **fb)
                    if rc >= 0:
                        return True
                except Exception:
                    return True  # 字体不支持，无法改善
        # 最终兜底：截断为单行能容纳的字数（+ 省略号）再插入。
        # 这比空白格更好；调用者已经 redact 了原文，不截断会留下空格。
        max_chars = max(1, int(bbox.width / 4.0))   # 4pt 时每字 4pt
        display_text = _truncate_to_em_width(text, max_chars)
        try:
            page.insert_textbox(bbox, display_text, fontsize=4.0,
                                **{**fb_base, "lineheight": 1.0})
        except Exception:
            pass
        return True

    # 对于纯 ASCII 文本（无 CJK）使用系统默认字体（Helvetica），
    # 其自然行距比 CJK 字体小，可用压缩 lineheight 来保持原字号。
    # 先以 base_size + lh∈[1.2, 1.0] 尝试；成功则直接提交，跳过缩减循环。
    if not _has_cjk(text):
        for _lh in [1.2, 1.0]:
            _shape = page.new_shape()
            try:
                _rc = _shape.insert_textbox(bbox, text, fontsize=base_size,
                                            color=color, align=align,
                                            lineheight=_lh, **font_kw)
                if _rc >= 0:
                    _shape.commit()
                    return True
            except Exception:
                break

    # Binary search for fitting size, then commit
    _min_size = max(4.0, base_size * min_factor)
    size = _find_fitting_size(page, bbox, text, base_size, color, align,
                              fontname=fontname, min_size=_min_size)
    shape = page.new_shape()
    try:
        rc = shape.insert_textbox(bbox, text, fontsize=size,
                                  color=color, align=align,
                                  lineheight=_LINE_HEIGHT_FACTOR, **font_kw)
        if rc >= 0:
            shape.commit()
            return True
    except Exception:
        pass  # unsupported font → fall through to page.insert_textbox

    # 兜底：直接用 page.insert_textbox（支持 fontfile）
    fb: dict = {"fontsize": size, "color": color, "align": align,
                "lineheight": _LINE_HEIGHT_FACTOR}
    if fontname:
        fb["fontname"] = fontname
    elif fontfile:
        fb["fontfile"] = fontfile
        fb["fontname"] = "F0"
    try:
        rc = page.insert_textbox(bbox, text, **fb)
        if rc >= 0:
            return True
    except Exception:
        return False
    # rc < 0 → 截断为单行能容纳的字数，保证至少有内容可见
    max_chars = max(1, int(bbox.width / size))
    display_text = _truncate_to_em_width(text, max_chars)
    try:
        page.insert_textbox(bbox, display_text, **fb)
    except Exception:
        pass
    return True


def _get_fitting_size(page: fitz.Page, bbox: fitz.Rect, text: str,
                      base_size: float, color: tuple, align: int,
                      fontname: Optional[str] = None,
                      fontfile: Optional[str] = None,
                      min_factor: float = 0.4) -> float:
    """Thin wrapper around _find_fitting_size for backwards compatibility."""
    return _find_fitting_size(
        page, bbox, text, base_size, color, align,
        fontname=fontname,
        min_size=max(4.0, base_size * min_factor),
    )


def _build_size_map(samples: dict) -> dict:
    """
    {source_size: [actual_size, ...]} → {source_size: mapped_size}

    mapped_size = 80th percentile from top（= 上位 80% のセルが収まる最大字号）。
    サンプル数 < 3 の場合はそのまま source_size を返す（外れ値対策）。
    """
    mapping: dict = {}
    for src_size, sizes in samples.items():
        if len(sizes) < 3:
            mapping[src_size] = src_size
            continue
        sorted_desc = sorted(sizes, reverse=True)
        # index at 20% from top → 80% of cells fit at ≥ this size
        k = min(len(sorted_desc) - 1, max(0, int(len(sorted_desc) * 0.2)))
        mapping[src_size] = round(sorted_desc[k], 2)
    return mapping


_BULLET_RE = re.compile(r'^\s*(?:\d+\.?\s|[•§·▪▸►▶◆◇○●]\s?)')


def _is_bullet(text: str) -> bool:
    """テキストが箇条書き（番号付き・記号付き）で始まるか判定。"""
    return bool(_BULLET_RE.match(text))


# ── 隐式网格检测 ──────────────────────────────────────────────────────────────

def _cluster(vals: list, tol: float = 3.0, min_count: int = 2) -> list:
    """
    将一组浮点数按容差 tol 分组，返回出现次数 ≥ min_count 的分组的重心列表。
    用于从 bbox 边缘坐标中提取"频繁出现的对齐线"（即隐式网格线）。
    """
    if not vals:
        return []
    vals = sorted(set(round(v, 2) for v in vals))
    clusters: list = []
    group = [vals[0]]
    for v in vals[1:]:
        if v - group[-1] <= tol:
            group.append(v)
        else:
            if len(group) >= min_count:
                clusters.append(sum(group) / len(group))
            group = [v]
    if len(group) >= min_count:
        clusters.append(sum(group) / len(group))
    return clusters


def _detect_grid(bboxes: List[fitz.Rect], tol: float = 3.0, min_count: int = 2):
    """
    从所有 block bbox 的四条边中检测隐式网格线。
    返回 (x_lines, y_lines)：出现 ≥ min_count 次的 x / y 坐标列表（已排序）。
    """
    x_vals: list = []
    y_vals: list = []
    for b in bboxes:
        x_vals += [b.x0, b.x1]
        y_vals += [b.y0, b.y1]
    return _cluster(x_vals, tol, min_count), _cluster(y_vals, tol, min_count)


def _cell_of(bbox: fitz.Rect, x_lines: list, y_lines: list,
             tol: float = 3.0) -> fitz.Rect:
    """
    根据网格线，找到 bbox 所在的"格子"（cell）。

    - cell_x0：bbox.x0 左侧（含）最近的网格线
    - cell_x1：bbox.x1 右侧（含）最近的网格线（须与 cell_x0 有足够间距）
    - cell_y0：bbox.y0 上方（含）最近的网格线
    - cell_y1：bbox.y1 **之后 5pt 以上**的最近网格线
              （+5pt 跨过少数派块自身的 y1，找到真实行底边）

    返回值保证 cell ≥ bbox（不会收缩原始 bbox）。
    """
    # 左壁
    left = [x for x in x_lines if x <= bbox.x0 + tol]
    cell_x0 = max(left) if left else bbox.x0

    # 右壁：必须比 cell_x0 宽出足够空间（> tol），避免退化为零宽格
    right = [x for x in x_lines if x >= bbox.x1 - tol and x > cell_x0 + tol]
    cell_x1 = min(right) if right else bbox.x1

    # 顶壁
    top = [y for y in y_lines if y <= bbox.y0 + tol]
    cell_y0 = max(top) if top else bbox.y0

    # 底壁：严格在 y1+5pt 之后，跳过少数派块自身的 y1 形成的伪网格线
    bottom = [y for y in y_lines if y > bbox.y1 + 5]
    cell_y1 = min(bottom) if bottom else bbox.y1

    # 保证 cell ≥ bbox（不收缩）
    return fitz.Rect(
        min(cell_x0, bbox.x0),
        min(cell_y0, bbox.y0),
        max(cell_x1, bbox.x1),
        max(cell_y1, bbox.y1),
    )


def _build_cell_tree(
    bboxes: List[fitz.Rect],
    page_rect: fitz.Rect,
    obstacles: Optional[List[fitz.Rect]] = None,
) -> List[fitz.Rect]:
    """
    ソープバブル膨張（軸平行 Voronoi）によるセル算出。

    各 bbox を上下左右に独立して膨張させ、最近傍 bbox のエッジとの中点で停止。
    obstacles: 画像ブロック等の障害物。Voronoi の中点計算に参加するが、セルを持たない。
    """
    all_rects = list(bboxes) + list(obstacles or [])
    cells = []
    for i, b in enumerate(bboxes):
        others = [all_rects[j] for j in range(len(all_rects)) if j != i]

        # Row-aware Voronoi: x-boundaries only consider obstacles in the same
        # row-band (y-overlap); y-boundaries only consider same column-band (x-overlap).
        # This prevents far-away text in other rows from narrowing horizontal cells.
        Y_TOL = b.height  # one bbox-height of tolerance for row membership
        X_TOL = b.width

        x0 = page_rect.x0
        for o in others:
            if o.x1 <= b.x0 + 0.5:
                if o.y0 < b.y1 + Y_TOL and o.y1 > b.y0 - Y_TOL:
                    x0 = max(x0, (o.x1 + b.x0) / 2.0)

        x1 = page_rect.x1
        for o in others:
            if o.x0 >= b.x1 - 0.5:
                if o.y0 < b.y1 + Y_TOL and o.y1 > b.y0 - Y_TOL:
                    x1 = min(x1, (o.x0 + b.x1) / 2.0)

        y0 = page_rect.y0
        for o in others:
            if o.y1 <= b.y0 + 0.5:
                if o.x0 < b.x1 + X_TOL and o.x1 > b.x0 - X_TOL:
                    y0 = max(y0, (o.y1 + b.y0) / 2.0)

        y1 = page_rect.y1
        for o in others:
            if o.y0 >= b.y1 - 0.5:
                if o.x0 < b.x1 + X_TOL and o.x1 > b.x0 - X_TOL:
                    y1 = min(y1, (o.y0 + b.y1) / 2.0)

        cells.append(fitz.Rect(x0, y0, x1, y1))
    return cells


def _cell_insert_bbox(
    bbox: fitz.Rect,
    cell: fitz.Rect,
    align: int,
) -> fitz.Rect:
    """
    cell とオリジナル bbox からレンダリング用 bbox を計算する。

    - 水平: テキストの先頭エッジを原文 bbox のエッジに固定し、
            反対側はセル境界まで広げる（余白 1pt）。
      * 左揃え / 中央揃え: x0=bbox.x0  x1=cell.x1-1
      * 右揃え           : x0=cell.x0+1 x1=bbox.x1
    - 垂直: 上端を bbox.y0 に固定し、下端はセル境界まで広げる（余白 1pt）。

    返り値は常に原文 bbox 以上のサイズを保証する。
    """
    MARGIN = 1.0
    if align == 2:          # 右揃え
        x0 = cell.x0 + MARGIN
        x1 = bbox.x1
    else:                   # 左揃え / 中央揃え
        x0 = bbox.x0
        x1 = cell.x1 - MARGIN
    y0 = bbox.y0
    y1 = cell.y1 - MARGIN
    return fitz.Rect(
        min(x0, bbox.x0), min(y0, bbox.y0),
        max(x1, bbox.x1), max(y1, bbox.y1),
    )


def _expand_for_minority(
    page: fitz.Page,
    bbox: fitz.Rect,
    text: str,
    target_size: float,
    page_rect: fitz.Rect,
    other_bboxes: List[fitz.Rect],
    color: tuple,
    align: int,
    fontname: Optional[str] = None,
    fontfile: Optional[str] = None,
    cell: Optional[fitz.Rect] = None,
) -> fitz.Rect:
    """
    mapped_size に収まらない少数派ブロックに対して、方向別の bbox 拡張を試みる。

    - bullet テキスト：右 → 下 の順に拡張（リスト縦揃えを保つ）
    - テーブルセル / caption：cell（グリッドセル境界）内で垂直拡張
      cell が指定されていない場合は従来の上下対称拡張にフォールバック

    拡張後の bbox で target_size が収まれば返す。収まらなければ cell 矩形
    （または元の bbox）を返し、insert_text_fitting でのフォント縮小に委ねる。
    """
    EDGE = 4.0
    STEPS_RIGHT = [20, 40, 60, 80, 120, 160, 200]
    STEPS_DOWN  = [6, 12, 18, 24, 36]

    def no_col(r: fitz.Rect) -> bool:
        """ページ端 + 他ブロックとの衝突なし"""
        if r.x0 < EDGE or r.y0 < EDGE:
            return False
        if r.x1 > page_rect.width - EDGE or r.y1 > page_rect.height - EDGE:
            return False
        for ob in other_bboxes:
            # 元の bbox と既に重なるブロックは同一セルの兄弟 → スキップ
            if bbox.intersects(ob):
                continue
            if r.intersects(ob):
                return False
        return True

    def fits(r: fitz.Rect) -> bool:
        sz = _get_fitting_size(page, r, text, target_size,
                               color, align, fontname, fontfile)
        return sz >= target_size - 0.05

    if _is_bullet(text):
        # ── 右方向に拡張 ────────────────────────────────────────────
        best_right = bbox
        for d in STEPS_RIGHT:
            cand = fitz.Rect(bbox.x0, bbox.y0,
                             min(page_rect.width - EDGE, bbox.x1 + d), bbox.y1)
            if no_col(cand):
                best_right = cand
                if fits(cand):
                    return cand
            else:
                break  # 衝突したらそれ以上は試さない

        # ── 下方向に拡張（右拡張後の幅を維持）──────────────────────
        for d in STEPS_DOWN:
            cand = fitz.Rect(best_right.x0, best_right.y0,
                             best_right.x1,
                             min(page_rect.height - EDGE, bbox.y1 + d))
            if no_col(cand):
                if fits(cand):
                    return cand
            else:
                break

    else:
        # ── テーブルセル / caption：グリッドセル内で垂直拡張 ────────
        # x 幅は固定（セル境界を越えるリスクを避ける）。
        # 試みる順序：① 上下一括（cell 全体）→ ② 下方向のみ → ③ 上方向のみ
        # → ④ 従来の増分ステップ（隣接ブロック間の隙間を狙う）
        if cell is not None:
            # ① 上下一括
            cand = fitz.Rect(
                bbox.x0,
                max(EDGE, min(bbox.y0, cell.y0)),
                bbox.x1,
                min(page_rect.height - EDGE, max(bbox.y1, cell.y1)),
            )
            if no_col(cand):
                return cand
            # ② 下方向のみ（cell_y1 まで）：実際に拡張した場合のみ返す
            cand_down = fitz.Rect(
                bbox.x0, bbox.y0, bbox.x1,
                min(page_rect.height - EDGE, max(bbox.y1, cell.y1)),
            )
            if no_col(cand_down) and cand_down.height > bbox.height + 0.5:
                return cand_down
            # ③ 上方向のみ（cell_y0 まで）：実際に拡張した場合のみ返す
            cand_up = fitz.Rect(
                bbox.x0,
                max(EDGE, min(bbox.y0, cell.y0)),
                bbox.x1, bbox.y1,
            )
            if no_col(cand_up) and cand_up.height > bbox.height + 0.5:
                return cand_up

        # ④ 増分ステップ（cell 有無を問わず：隣接ブロック間の隙間を逐次探索）
        STEPS_V = [4, 8, 12, 18, 24, 36, 48, 60, 80]
        for d in STEPS_V:
            for cand in [
                fitz.Rect(bbox.x0, max(EDGE, bbox.y0 - d),
                          bbox.x1, min(page_rect.height - EDGE, bbox.y1 + d)),
                fitz.Rect(bbox.x0, max(EDGE, bbox.y0 - d),
                          bbox.x1, bbox.y1),
                fitz.Rect(bbox.x0, bbox.y0,
                          bbox.x1, min(page_rect.height - EDGE, bbox.y1 + d)),
            ]:
                if no_col(cand) and fits(cand):
                    return cand

    return bbox  # 拡張しても収まらない → 元の bbox を返す（shrink に委ねる）


def _load_context() -> str:
    """加载翻译背景文件（脚本同目录下的 context.md）。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "context.md")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    return ""


# 模块级缓存，避免每个 batch 重复读文件
_CONTEXT_CACHE: Optional[str] = None


_MAX_BATCH = 60  # 每次 Claude 调用的最大文本块数，避免超时

# 人名固定变换表：翻译前将原文中的罗马字人名替换为正式汉字表记。
# key = 用于 re.sub 的正则模式（忽略大小写），value = 正式日文表记
_PERSON_NAME_PATTERNS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r'\bshikama\b', re.IGNORECASE), '四竃'),
    (re.compile(r'\bnagashima\b', re.IGNORECASE), '長島'),
]


def _apply_person_names(text: str) -> str:
    """翻译前预处理：将原文中的罗马字人名替换为日文表记，Claude 会原样保留。"""
    for pat, replacement in _PERSON_NAME_PATTERNS:
        text = pat.sub(replacement, text)
    return text

# 字号低于此阈值的块视为"截图内嵌小字"——人眼无法阅读，不翻译也不 redact，
# 保留原始英文。数据分析表明：截图表格内容 ≤3.8pt，正常可读文字 ≥4.1pt，
# 之间有天然断层，故选 4.0pt 作为分界。
_MIN_TRANSLATE_FONTSIZE = 4.0

# 行间距系数：控制翻译文本的行间距视觉效果。
# 原始 PPT 通常有较宽松的行间距（视觉上约 1.3-1.5×字号），
# 设为 1.4 让译文与原文排版风格匹配、避免过于拥挤。
# 此值同时用于行高估算（fit_fontsize、try_expand_bbox）和
# 实际渲染（insert_textbox 的 lineheight 参数）。
_LINE_HEIGHT_FACTOR = 1.4

# 纯数字/符号的正则 —— 匹配无需翻译的不变文本（页码、百分比、坐标等）
_INVARIANT_RE = re.compile(
    r'^\s*[\d\s.,;:()\[\]%°±×÷=+\-/<>~^*#@&|²³µ€$¥£₹∞≤≥≠∑∏√]+\s*$'
)


def _is_trivially_invariant(text: str) -> bool:
    """返回 True 表示该文本不需要翻译（纯数字/符号串），可直接原样保留。"""
    return bool(_INVARIANT_RE.match(text.strip())) if text.strip() else True


def _detect_lang_hint(texts: List[str]) -> str:
    """粗略的语言检测：取前 10 条文本，统计 CJK / 拉丁字符比例。"""
    sample = " ".join(t for t in texts[:10] if t)
    cjk   = sum(1 for c in sample
                if '\u4e00' <= c <= '\u9fff'
                or '\u3040' <= c <= '\u30ff'
                or '\u31f0' <= c <= '\u31ff')
    latin = sum(1 for c in sample if 'a' <= c.lower() <= 'z')
    if cjk > max(5, latin * 0.3):
        kana = sum(1 for c in sample if '\u3040' <= c <= '\u30ff')
        return "日本語" if kana > cjk * 0.15 else "中文（简体）"
    return "English"


def _call_claude_translate(batch: List[tuple], src_name: str, tgt_name: str,
                           _depth: int = 0) -> dict:
    """向 Claude CLI 发送一批翻译请求，返回 {batch内序号 → 译文} 字典。
    超时时自动对半拆分重试（最多递归 2 层）。
    """
    input_json = json.dumps(
        [{"id": k, "text": t} for k, (_, t) in enumerate(batch)],
        ensure_ascii=False,
    )
    context_section = (
        f"\n\n## 背景知识与术语规范\n{_CONTEXT_CACHE}\n"
        if _CONTEXT_CACHE else ""
    )
    prompt = (
        f"你是一位资深演示文稿本地化专家，具备丰富的企业级幻灯片翻译经验，熟悉技术、商业与工程领域术语。\n\n"
        f"## 任务\n"
        f"将以下幻灯片文本从 {src_name} 翻译成 {tgt_name}。\n\n"
        f"## 翻译规范\n\n"
        f"**格式保真（最高优先级）**\n"
        f"- 换行符（\\n）必须原样保留，不得增减\n"
        f"- 数字、单位、产品型号、代码片段保持原样\n"
        f"- 项目符号（•、-、▶ 等）及其后的空格保持原样\n\n"
        f"**术语准确性**\n"
        f"- 专业术语使用行业标准译名\n"
        f"- 人名、品牌名、型号等专有名词保持原文\n"
        f"- 常用缩略词可保留原文（如 AI、ROI、KPI）\n\n"
        f"**幻灯片语言风格**\n"
        f"- 简洁精炼，避免冗长；标题类文本尤其要简短有力\n"
        f"- 自然流畅，符合 {tgt_name} 母语者表达习惯\n"
        f"- 纯数字、标点、符号构成的文本：原样输出，不翻译\n"
        f"{context_section}\n"
        f"## 输入格式\n"
        f"JSON 数组，每个元素有 id 和 text 字段。\n\n"
        f"## 输出格式\n"
        f"仅返回 JSON 数组，结构相同，将每个 text 替换为对应译文。\n"
        f"禁止输出任何说明文字、注释或 markdown 代码块，仅纯 JSON。\n\n"
        f"{input_json}"
    )
    try:
        result = subprocess.run(
            [CLAUDE_CLI, "-p", prompt],
            capture_output=True, text=True, timeout=180,
        )
        if result.returncode != 0:
            print(f"  ⚠ claude CLI 错误: {result.stderr[:200]}")
            return {}
        raw = result.stdout.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        translated_list = json.loads(raw)
        return {item["id"]: item["text"] for item in translated_list}
    except subprocess.TimeoutExpired:
        if _depth < 2 and len(batch) > 1:
            # 对半拆分重试，两半的 id 都从 0 起，合并时右半段 id 偏移 mid
            mid = len(batch) // 2
            print(f"  ⚠ 超时（{len(batch)} 块），拆成 {mid}+{len(batch)-mid} 重试...", end="", flush=True)
            r1 = _call_claude_translate(batch[:mid],  src_name, tgt_name, _depth + 1)
            r2 = _call_claude_translate(batch[mid:],  src_name, tgt_name, _depth + 1)
            merged = dict(r1)
            for k, v in r2.items():
                merged[k + mid] = v
            return merged
        print(f"  ⚠ 翻译超时（{len(batch)} 块），跳过本批次")
        return {}
    except (json.JSONDecodeError, KeyError) as e:
        print(f"  ⚠ JSON 解析失败（{e}），跳过本批次")
        return {}
    except Exception as e:
        print(f"  ⚠ 翻译错误: {e}")
        return {}


def translate_blocks_via_cli(texts: List[str], src: str, tgt: str,
                             trans_cache: Optional[dict] = None) -> List[str]:
    """通过 claude CLI 批量翻译（无需 API Key，使用已登录账号）。

    trans_cache: 文档级翻译缓存 {源文本 → 译文}，相同文本跨页复用，保证翻译一致性。
    超过 _MAX_BATCH 块时自动分批，避免超时。
    """
    global _CONTEXT_CACHE
    if _CONTEXT_CACHE is None:
        _CONTEXT_CACHE = _load_context()

    non_empty = [(i, t) for i, t in enumerate(texts) if t.strip()]
    if not non_empty:
        return texts

    results = list(texts)
    need_translate = []

    # 单次遍历：不变文本原样保留，缓存命中直接复用，其余加入待翻译队列
    for i, t in non_empty:
        if _is_trivially_invariant(t):
            results[i] = t
            if trans_cache is not None:
                trans_cache[t] = t
        elif trans_cache is not None and t in trans_cache:
            results[i] = trans_cache[t]
        else:
            need_translate.append((i, t))

    if not need_translate:
        return results

    # 人名预处理：将罗马字人名替换为日文表记，Claude 翻译时会原样保留
    need_translate = [(i, _apply_person_names(t)) for i, t in need_translate]

    # 自动语言检测：如果 src=="auto"，从待翻译文本中采样推断实际语言
    if src == "auto":
        src_name = _detect_lang_hint([t for _, t in need_translate])
    else:
        src_name = SUPPORTED_LANGUAGES.get(src, src)
    tgt_name = SUPPORTED_LANGUAGES.get(tgt, tgt)

    # 分批翻译：超过 _MAX_BATCH 块时拆成多次调用
    for batch_start in range(0, len(need_translate), _MAX_BATCH):
        batch = need_translate[batch_start: batch_start + _MAX_BATCH]
        if len(need_translate) > _MAX_BATCH:
            print(f"    (批次 {batch_start+1}-{min(batch_start+_MAX_BATCH, len(need_translate))}/{len(need_translate)})", end="", flush=True)
        id_to_text = _call_claude_translate(batch, src_name, tgt_name)
        for k, (orig_idx, src_text) in enumerate(batch):
            if k in id_to_text:
                results[orig_idx] = id_to_text[k]
                if trans_cache is not None:
                    trans_cache[src_text] = id_to_text[k]
    if len(need_translate) > _MAX_BATCH:
        print()  # 换行

    return results


_BULLET_ONLY = {"•", "§", "v", "Ø", "-", "–", "—"}


def lines_are_scattered(block: dict, x_threshold: float = 60.0) -> bool:
    """
    检测一个块中是否有多行在同一 y 位置但 x 位置相差很远。
    例如图例中 'Bad'（x≈457）和 'Good'（x≈542）x_gap≈85px。

    bullet-only 行（•、§ 等）排除在外，避免 bullet 段落被误判为散列。
    普通段落（多行上下排列）不会触发此检测，因为它们的各行 y 不重叠。
    """
    all_lines = block.get("lines", [])
    # 排除 bullet-only 行参与散列检测
    lines = [l for l in all_lines
             if "".join(s["text"] for s in l["spans"]).strip() not in _BULLET_ONLY]
    if len(lines) < 2:
        return False
    # 检查是否有两行 y 范围重叠（同行），且 x 中心相差很大
    for i in range(len(lines)):
        li = lines[i]
        liy0, liy1 = li["bbox"][1], li["bbox"][3]
        lix_c = (li["bbox"][0] + li["bbox"][2]) / 2
        for j in range(i + 1, len(lines)):
            lj = lines[j]
            ljy0, ljy1 = lj["bbox"][1], lj["bbox"][3]
            ljx_c = (lj["bbox"][0] + lj["bbox"][2]) / 2
            # y 范围重叠（允许少量误差）
            y_overlap = min(liy1, ljy1) - max(liy0, ljy0)
            if y_overlap > 2:  # y 重叠超过 2pt 认为是同行
                x_gap = abs(lix_c - ljx_c)
                if x_gap > x_threshold:
                    return True
    return False


def translate_page(page: fitz.Page, src: str, tgt: str,
                   cjk_font: Optional[str], verbose: bool,
                   trans_cache: Optional[dict] = None):
    """翻译单页（原地修改）。"""
    page_rect = page.rect
    text_dict = page.get_text(
        "dict",
        flags=fitz.TEXT_PRESERVE_WHITESPACE | fitz.TEXT_PRESERVE_LIGATURES,
    )
    blocks = [b for b in text_dict["blocks"] if b["type"] == 0]
    if not blocks:
        return

    # ── 隐藏文字检测：渲染原始页面，用像素方差识别被实心色块遮挡的文字 ──
    # 在 redact 之前渲染，保留原始视觉内容。
    # 原理：可见文字 → bbox 内有深色文字像素 + 浅色背景 → 亮度方差高
    #       被实心色块遮住 → bbox 内颜色均一（深色或浅色均可）→ 方差极低
    orig_pixmap = page.get_pixmap(matrix=fitz.Matrix(1, 1), alpha=False)
    _pix_arr = np.frombuffer(orig_pixmap.samples, dtype=np.uint8).reshape(
        orig_pixmap.height, orig_pixmap.width, 3)
    # 亮度矩阵（整数，0-255）
    _lum = (_pix_arr[:, :, 0].astype(np.int32) * 299
            + _pix_arr[:, :, 1].astype(np.int32) * 587
            + _pix_arr[:, :, 2].astype(np.int32) * 114) // 1000

    def _bbox_lum(bbox: fitz.Rect):
        """返回 bbox 对应区域的亮度子矩阵，越界安全。"""
        x0 = max(0, int(bbox.x0)); y0 = max(0, int(bbox.y0))
        x1 = min(orig_pixmap.width, int(bbox.x1))
        y1 = min(orig_pixmap.height, int(bbox.y1))
        if x1 <= x0 or y1 <= y0:
            return None
        return _lum[y0:y1, x0:x1]

    def is_hidden(bbox: fitz.Rect) -> bool:
        """
        判断文字块是否被图形遮挡（应跳过翻译）。
        三重检测：
          1. 暗像素率 < 3%   → 被浅色/白色实心块覆盖
          2. 暗像素率 < 10% 且标准差 < 18 且均值 < 245
             → 极少深色像素、颜色均一 → 被有色实心块覆盖
          3. 标准差 < 8 且均值 < 180
             → 非常均一的深色区域 → 被深色/彩色实心块覆盖（如深色页脚）
        注：有可见文字的区域，即使背景均一，dark_ratio 也通常 ≥ 10%（文字本身贡献暗像素）。
        """
        region = _bbox_lum(bbox)
        if region is None:
            return False  # 越界保守处理：认为可见
        total = region.size
        if total < 4:
            return False
        dark_ratio = float((region < 180).sum()) / total
        std = float(region.std())
        mean = float(region.mean())
        # 检测 1：几乎无深色像素 + 颜色极其均一 → 被浅色/白色块遮挡
        # 加 std<15 条件：微小字体的抗锯齿渲染也会产生接近白色的像素，
        # 但 std 较高（文字边缘色差）；真实遮挡则几乎全白无色差。
        if dark_ratio < 0.03 and std < 15:
            return True
        # 检测 2：极少深色像素 + 颜色均一 + 非纯白 → 被有色块遮挡
        if dark_ratio < 0.10 and std < 18 and mean < 245:
            return True
        # 检测 3：颜色极其均一 + 深色均值 → 被深色实心块遮挡（深色页脚上的页码等）
        if std < 8 and mean < 180:
            return True
        return False

    font_name = None  # 字体在 apply_redactions 之后注册

    # ── 预处理：合并"续行孤立块" ──────────────────────────────────────────────
    # 有些 PDF 的文字被拆成两个 block，如：
    #   block i:  "transfer data via 4G/5G or "  [x=208..393, y=372..419]
    #   block i+1: "WiFi"                         [x=226..246, y=420..431]
    # block i+1 x范围完全在 block i 内，且紧接在 block i 下方。
    # 若单独翻译，句子语义断裂；合并后翻译得到正确结果。
    # 合并条件：
    #   1. block[j] x 范围完全在 block[i] x 范围内（含 5px 容差）
    #   2. block[j] y0 ≤ block[i] y1 + font_size（紧接下方）
    #   3. block[j] 宽度 ≤ block[i] 宽度的 60%（是较窄的"尾行"）
    # 合并效果：block[i] 的文字追加 block[j] 的文字；block[j] 进入 skip_continuation
    #           同时将 block[j] 的 bbox 加入 extra_redact，确保其原文被擦除。
    _skip_continuation: set = set()       # block 索引：已合并到上一块，本循环跳过
    _continuation_appends: dict = {}      # block_rank -> list of (text, bbox)

    for _ci in range(len(blocks) - 1):
        _bi = blocks[_ci]
        _bj = blocks[_ci + 1]
        if _ci + 1 in _skip_continuation:
            continue
        # 跳过水印块（水印块本身在主循环中也会被跳过）
        if is_watermark_block(_bi, page_rect) or is_watermark_block(_bj, page_rect):
            continue
        xi0, yi0, xi1, yi1 = _bi["bbox"]
        xj0, yj0, xj1, yj1 = _bj["bbox"]
        wi = xi1 - xi0
        wj = xj1 - xj0
        if wi <= 0 or wj <= 0:
            continue
        # 条件 1：x 包含
        if xj0 < xi0 - 5 or xj1 > xi1 + 5:
            continue
        # 条件 3：bj 宽度 ≤ bi 宽度 60%
        if wj > wi * 0.60:
            continue
        # 条件 2：垂直紧接
        fs_i = _bi["lines"][0]["spans"][0]["size"] if _bi["lines"] and _bi["lines"][0]["spans"] else 10
        if yj0 > yi1 + fs_i:
            continue
        # 获取文本（原始，保留尾部空格）
        _bi_text_raw = "".join(
            "".join(_normalize_span_text(span) for span in line["spans"])
            for line in _bi["lines"]
        )
        _bj_text = "".join(
            "".join(_normalize_span_text(span) for span in line["spans"])
            for line in _bj["lines"]
        ).strip()
        if not _bj_text:
            continue
        # 条件 4：bi 原始文本以空格（句子中断）或连字符（断词）结尾
        # 这是区分"续行孤立块"与"独立并列标签"的关键：
        # 真正的续行往往在 PDF 流中保留了尾部空格；独立标签则不会。
        if not (_bi_text_raw.endswith(" ") or _bi_text_raw.endswith("-")):
            continue
        # 条件 5：bj 不是待跳过的水印/页脚文本
        if is_skip_text(_bj_text):
            continue
        # 满足所有条件，合并
        _skip_continuation.add(_ci + 1)
        _continuation_appends.setdefault(_ci, []).append((_bj_text, fitz.Rect(_bj["bbox"])))

    # insertion_items: list of dicts with keys bbox, text, font_size, color, align
    # Each item represents one region to redact and re-insert.
    # For scattered blocks, we split them into per-line items.
    insertion_items = []
    # redact_bboxes: bboxes to redact (may be block bbox or line bboxes)
    redact_bboxes = []
    skipped_watermarks = 0

    for block_rank, block in enumerate(blocks):
        # ── 续行孤立块：已被合并到前一块，跳过 ─────────────────────────────
        if block_rank in _skip_continuation:
            continue
        # ── 过滤水印 ──────────────────────────────────────────────
        if is_watermark_block(block, page_rect):
            # 旋转水印：不翻译，也不 redact（避免误擦除被水印覆盖的底层内容）
            # 水印原样保留在译文中。
            skipped_watermarks += 1
            continue

        # ── 隐藏文字过滤：被图块遮挡的文字，跳过翻译和重插入 ──────
        block_bbox = fitz.Rect(block["bbox"])
        if is_hidden(block_bbox):
            if verbose:
                first_text = block["lines"][0]["spans"][0].get("text", "")[:20] if block["lines"] else ""
                print(f"  ↷ 隐藏文字（色块遮挡）跳过: '{first_text}'")
            # 将隐藏块加入 redact_bboxes，避免 apply_redactions 改写流顺序后
            # 该块的文字意外出现在遮盖图形之上（"僵尸文字"问题）。
            # 不加入 insertion_items，故不翻译也不重插入。
            redact_bboxes.append(block_bbox)
            continue

        lines_text = []
        for line in block["lines"]:
            line_text = "".join(_normalize_span_text(span) for span in line["spans"])
            lines_text.append(line_text)
        # 合并 bullet-only 行：§/• 单独成行时，与下一行合并为 "• 文字"
        i = 0
        merged_lines = []
        while i < len(lines_text):
            t = lines_text[i].strip()
            if t in _BULLET_ONLY and i + 1 < len(lines_text):
                merged_lines.append(t + "\xa0" + lines_text[i + 1].strip())
                i += 2
            else:
                merged_lines.append(lines_text[i])
                i += 1
        block_text = "\n".join(merged_lines).strip()
        if not block_text:
            continue

        # ── 追加已合并的续行文本，并将其 bbox 加入 redact_bboxes ──────
        if block_rank in _continuation_appends:
            for _ct, _cb in _continuation_appends[block_rank]:
                block_text = block_text.rstrip() + " " + _ct
                redact_bboxes.append(_cb)

        first_span = block["lines"][0]["spans"][0]

        # 对齐检测
        bx0, _, bx1, _ = block["bbox"]
        sx0 = first_span["bbox"][0]
        if abs(sx0 - bx0) < 5:
            align = fitz.TEXT_ALIGN_LEFT
        elif abs(sx0 - (bx0 + bx1) / 2) < (bx1 - bx0) * 0.3:
            align = fitz.TEXT_ALIGN_CENTER
        else:
            align = fitz.TEXT_ALIGN_LEFT

        # ── 文本内容匹配跳过（watermark / footer 等无需翻译）──────
        if is_skip_text(block_text):
            skipped_watermarks += 1
            continue

        # ── 截图内嵌小字：字号低于阈值，人眼不可读，原样保留不翻译 ──────
        # （不加入 redact_bboxes，也不加入 insertion_items，原文不变）
        block_fontsize = first_span["size"]
        if block_fontsize < _MIN_TRANSLATE_FONTSIZE:
            continue

        if lines_are_scattered(block):
            # 各行独立处理：每行用自己的 bbox 和文字
            # 連続行が文中断 → 次行が小文字開始の場合は結合（センテンス継続検出）
            scattered_lines = block["lines"]
            _sl_merged: list = []   # [(merged_text, merged_bbox, first_span)]
            _si = 0
            while _si < len(scattered_lines):
                line = scattered_lines[_si]
                lt = "".join(_normalize_span_text(s) for s in line["spans"]).strip()
                merged_text = lt
                merged_bbox = list(line["bbox"])
                span0 = line["spans"][0]
                # 次行が continuation（現行が文末ではなく次行が小文字）なら結合
                # ただし水平方向に大きく離れている行（別列）は結合しない
                _si2 = _si + 1
                while _si2 < len(scattered_lines):
                    next_line = scattered_lines[_si2]
                    next_lt = "".join(_normalize_span_text(s) for s in next_line["spans"]).strip()
                    nb = next_line["bbox"]
                    # 次行の x0 が現行の x1 より右にあれば別列 → 結合しない
                    # （同一列の行は x 範囲が重なるか隙間が数 px 以内）
                    if nb[0] > merged_bbox[2] + 5:
                        break
                    if (merged_text and merged_text[-1] not in '.!?\u3002\uff01\uff1f' and
                            next_lt and next_lt[0].islower()):
                        merged_text = merged_text.rstrip() + ' ' + next_lt
                        merged_bbox = [
                            min(merged_bbox[0], nb[0]), min(merged_bbox[1], nb[1]),
                            max(merged_bbox[2], nb[2]), max(merged_bbox[3], nb[3]),
                        ]
                        _si2 += 1
                    else:
                        break
                _sl_merged.append((merged_text, merged_bbox, span0))
                _si = _si2

            for (line_text, lbbox, span) in _sl_merged:
                if not line_text or is_skip_text(line_text):
                    continue
                if span["size"] < _MIN_TRANSLATE_FONTSIZE:
                    continue
                lx0, ly0, lx1, ly1 = lbbox
                # 给行 bbox 增加少量垂直空间以便文字能放入
                line_bbox = fitz.Rect(lx0, ly0, lx1, ly1 + span["size"] * 0.5)
                redact_bboxes.append(fitz.Rect(lbbox))
                insertion_items.append({
                    "bbox": line_bbox,
                    "text": line_text,
                    "font_size": span["size"],
                    "color": color_from_int(span["color"]),
                    "align": fitz.TEXT_ALIGN_LEFT,
                    "stream_rank": block_rank,
                })
        else:
            redact_bboxes.append(fitz.Rect(block["bbox"]))
            insertion_items.append({
                "bbox": fitz.Rect(block["bbox"]),
                "text": block_text,
                "font_size": first_span["size"],
                "color": color_from_int(first_span["color"]),
                "align": align,
                "stream_rank": block_rank,
            })

    if skipped_watermarks and verbose:
        print(f"  ↷ 跳过 {skipped_watermarks} 个水印/旋转文字块", flush=True)

    if not insertion_items:
        return

    # ── 去重：移除被更大邻居块高度重叠（> 70%）的小块 ──────────────────────
    # 源 PDF 中（尤其 PPT 导出）同一表格单元格有时产生多个几乎完全重叠的 block，
    # 保留面积最大的那个，丢弃其余（原文仍会被 redact，不会残留）。
    if len(insertion_items) > 1:
        _iboxes = [item["bbox"] for item in insertion_items]
        _keep = [True] * len(insertion_items)
        for _i in range(len(_iboxes)):
            if not _keep[_i]:
                continue
            for _j in range(_i + 1, len(_iboxes)):
                if not _keep[_j]:
                    continue
                _inter = _iboxes[_i] & _iboxes[_j]
                if _inter.is_empty:
                    continue
                _ai = _iboxes[_i].width * _iboxes[_i].height
                _aj = _iboxes[_j].width * _iboxes[_j].height
                _a_inter = _inter.width * _inter.height
                _min_a = min(_ai, _aj)
                if _min_a > 0 and _a_inter / _min_a > 0.70:
                    if _ai <= _aj:
                        _keep[_i] = False
                    else:
                        _keep[_j] = False
        insertion_items = [item for _i, item in enumerate(insertion_items) if _keep[_i]]

    # ── Z-order 预处理：记录各文字块在原始流中的锚点 ─────────────────────────
    # 在 redact 之前读取原始流，找到每个 BT...ET 的位置，作为后续重排的定位锚点。
    # 匹配方式：PyMuPDF 按流顺序返回 blocks，故 insertion_item 的 stream_rank
    # (在 blocks 中的序号）可以线性映射到 BT...ET 块序号，无需解析 CTM 坐标。
    pre_stream = page.read_contents()
    pre_bt_ranges = _find_bt_et_ranges(pre_stream)

    n_bt = len(pre_bt_ranges)
    n_blocks = len(blocks)  # blocks 已在函数顶部定义（type==0 的全部块）

    item_anchors: List = []
    pre_len = max(1, len(pre_stream))
    for item in insertion_items:
        rank = item.get("stream_rank", -1)
        if rank >= 0 and n_bt > 0 and n_blocks > 0:
            bt_idx = min(n_bt - 1, int(rank * n_bt / n_blocks))
            anchor = _stream_anchor(pre_stream, pre_bt_ranges[bt_idx][0])
            frac = pre_bt_ranges[bt_idx][0] / pre_len
        else:
            anchor = None
            frac = 1.0
        item_anchors.append((anchor, frac))

    texts = [item["text"] for item in insertion_items]
    if verbose:
        print(f"  → 翻译 {len(texts)} 个文本块...", flush=True)

    translations = translate_blocks_via_cli(texts, src, tgt, trans_cache=trans_cache)

    # ── 遮盖原文：使用精确 bbox，不膨胀，避免 caption 错位 ──────
    for bbox in redact_bboxes:
        page.add_redact_annot(bbox, fill=[])

    # 只移除文字层，保留图片和矢量背景
    page.apply_redactions(
        images=fitz.PDF_REDACT_IMAGE_NONE,
        graphics=fitz.PDF_REDACT_LINE_ART_NONE,
    )

    # 字体必须在 apply_redactions 之后注册，否则会被当作未使用资源清除
    if cjk_font:
        try:
            page.insert_font(fontname="F0", fontfile=cjk_font)
            font_name = "F0"
        except Exception:
            font_name = None

    # ── 隣接同列ブロックマージ：PyMuPDF が1つのテーブルセルを複数ブロックに
    # 分割した場合、縦に隣接かつ x が大きく重なるアイテムを1つに統合する。
    # 典型例: "UNP CUTIN Collision Risk"（y=317-329）と "Takeover Filter"
    # （y=330-342）が同じ列で別ブロックになる場合。
    _MERGE_Y_GAP   = 6.0   # px: この gap 以内を「隣接」とみなす
    _MERGE_X_RATIO = 0.30  # x 重複率がこれ以上なら同一列とみなす
    _new_items: list = []
    _new_trans: list = []
    _mi = 0
    while _mi < len(insertion_items):
        _item = insertion_items[_mi]
        _t    = translations[_mi]
        while _mi + 1 < len(insertion_items):
            _ni = insertion_items[_mi + 1]
            _nt = translations[_mi + 1]
            if not _t or not _nt:
                break
            _ygap = _ni["bbox"].y0 - _item["bbox"].y1
            if _ygap < 0 or _ygap > _MERGE_Y_GAP:
                break
            _xlo  = max(_item["bbox"].x0, _ni["bbox"].x0)
            _xhi  = min(_item["bbox"].x1, _ni["bbox"].x1)
            _xuni = max(_item["bbox"].x1, _ni["bbox"].x1) - min(_item["bbox"].x0, _ni["bbox"].x0)
            if _xuni <= 0 or (_xhi - _xlo) / _xuni < _MERGE_X_RATIO:
                break
            # マージ実行
            _merged_bbox = fitz.Rect(
                min(_item["bbox"].x0, _ni["bbox"].x0), _item["bbox"].y0,
                max(_item["bbox"].x1, _ni["bbox"].x1), _ni["bbox"].y1,
            )
            _item = dict(_item)
            _item["bbox"] = _merged_bbox
            _t = _t.rstrip('\n') + '\n' + _nt.lstrip('\n')
            _mi += 1
        _new_items.append(_item)
        _new_trans.append(_t)
        _mi += 1
    insertion_items = _new_items
    translations    = _new_trans

    # ── 插入译文：弹性 bbox + Shape dry-run 精确适配字号 ──────────
    # 预建所有插入项的 bbox 列表，供碰撞检测使用
    all_insert_bboxes = [item["bbox"] for item in insertion_items]

    # ── 隐式网格检测：BSP 递归 guillotine 分割，为每个 block 分配唯一 cell ──
    # 画像ブロックを障害物として収集（Voronoi 境界として機能）
    # get_image_info() でページ上の全画像 bbox を取得（text_dict には含まれない）
    _img_obstacles: List[fitz.Rect] = []
    try:
        for _info in page.get_image_info(hashes=False, xrefs=False):
            _r = fitz.Rect(_info["bbox"])
            if _r.width > 30 and _r.height > 30:
                _img_obstacles.append(_r)
    except Exception:
        pass

    # テーブルセル検出（PyMuPDF 1.23+）→ コンテナとして使用
    _table_cells: List[fitz.Rect] = []
    try:
        for _tbl in page.find_tables().tables:
            for _row in _tbl.cells:
                for _cell in _row:
                    if _cell is not None:
                        _cr = fitz.Rect(_cell)
                        if _cr.width > 5 and _cr.height > 5:
                            _table_cells.append(_cr)
    except Exception:
        pass

    _item_cells_raw = _build_cell_tree(all_insert_bboxes, page_rect, obstacles=_img_obstacles)

    # Voronoi セルをコンテナ（テーブルセル）で clip
    _item_cells = []
    for _idx, (_cell, _bbox) in enumerate(zip(_item_cells_raw, all_insert_bboxes)):
        _container = None
        for _tc in _table_cells:
            if (_tc.x0 <= _bbox.x0 + 2 and _bbox.x1 <= _tc.x1 + 2 and
                    _tc.y0 <= _bbox.y0 + 2 and _bbox.y1 <= _tc.y1 + 2):
                if _container is None or (_tc.width * _tc.height < _container.width * _container.height):
                    _container = _tc
        if _container is not None:
            # clip: intersect Voronoi cell with container (with 1pt margin)
            clipped = fitz.Rect(
                max(_cell.x0, _container.x0 + 1),
                max(_cell.y0, _container.y0 + 1),
                min(_cell.x1, _container.x1 - 1),
                min(_cell.y1, _container.y1 - 1),
            )
            # ensure not smaller than bbox
            _item_cells.append(fitz.Rect(
                min(clipped.x0, _bbox.x0),
                min(clipped.y0, _bbox.y0),
                max(clipped.x1, _bbox.x1),
                max(clipped.y1, _bbox.y1),
            ))
        else:
            _item_cells.append(_cell)

    # ── bbox.y0 を行単位でスナップ（同一行の微小ズレを投票で補正）──────────
    _y0_centers = _cluster([b.y0 for b in all_insert_bboxes], tol=4.0, min_count=1)
    def _snap_y0(y: float) -> float:
        if not _y0_centers:
            return y
        nearest = min(_y0_centers, key=lambda c: abs(c - y))
        return nearest if abs(nearest - y) <= 4.0 else y

    # 检测标题块：页面上方 25% 内字号最大的文本块，或页面任意位置字号 ≥ 40pt 的大字
    # 标题处理：强制单行 + bbox 扩展至页面宽度（避免碰撞限制导致过度缩小）
    _max_fs = max((item["font_size"] for item in insertion_items), default=0)
    title_indices: set = {
        i for i, item in enumerate(insertion_items)
        if item["font_size"] >= _max_fs * 0.85
        and item["font_size"] >= 16
        and (item["bbox"].y0 < page_rect.height * 0.25
             or item["font_size"] >= 40)
    }

    # ── Phase 1: bullet 替换 + 预计算 insert_bbox（供 dry-run 和渲染共用）──
    translated_texts: List[str] = []
    insert_bboxes: List[fitz.Rect] = []
    for idx, (item, translated) in enumerate(zip(insertion_items, translations)):
        # Hiragino に収録されていないグリフを近似文字に置換
        translated = translated.replace('\u25B8', '\u25B6').replace('\u25BA', '\u25B6')  # ▸►→▶
        translated = translated.replace('\u2705', '\u2713')  # ✅→✓ (emoji→Hiragino対応)
        translated = translated.replace('\u0394', '\u25B3').replace('\u03B4', '\u25B3')  # Δδ→△
        # bullet 后的空白（包括换行）→ 非换行空格，防止 PyMuPDF 在 bullet 后断行
        translated = re.sub(r'([•§·▪▸►▶◆◇○●✓])[\s\n]+', lambda m: m.group(1) + '\xa0', translated)
        # 専有名詞（英字語）+ スペース + CJK → \xa0 で改行防止（例: Honda チーム、Box + DFDI）
        translated = re.sub(
            r'([A-Za-z0-9])(\s+)(?=[\u3040-\u30FF\u4E00-\u9FFF])',
            lambda m: m.group(1) + '\xa0',
            translated,
        )
        # 英字語 + スペース + [+\-/] + スペース → \xa0（例: Box\xa0+\xa0DFDI）
        translated = re.sub(
            r'([A-Za-z0-9])\s+([+\-/])\s+([A-Za-z0-9])',
            lambda m: m.group(1) + '\xa0' + m.group(2) + '\xa0' + m.group(3),
            translated,
        )
        # 各行頭スペースを除去（LLM が行頭に空白を出力・原文インデントを引き継ぐことがある）
        translated = '\n'.join(line.lstrip(' ') for line in translated.split('\n'))
        translated_texts.append(translated)
        if not translated:
            insert_bboxes.append(item["bbox"])
            continue
        # y0 を同行グループ内でスナップ（表ヘッダー等の高さ揃え）
        snapped_y0 = _snap_y0(item["bbox"].y0)
        snap_bbox = fitz.Rect(item["bbox"].x0, snapped_y0,
                              item["bbox"].x1, item["bbox"].y1)
        if idx in title_indices:
            title_y1 = max(snap_bbox.y1,
                           snap_bbox.y0 + item["font_size"] * _LINE_HEIGHT_FACTOR * 2)
            insert_bboxes.append(fitz.Rect(snap_bbox.x0, snap_bbox.y0,
                                           page_rect.width - 2, title_y1))
        else:
            ibx = _cell_insert_bbox(snap_bbox, _item_cells[idx], item["align"])
            # 画像障害物が insert_bbox の右側に重なる場合、x1 をクリップして画像へのはみ出しを防ぐ
            for _obs in _img_obstacles:
                if (ibx.x1 > _obs.x0 + 5 and ibx.x0 < _obs.x1
                        and ibx.y1 > _obs.y0 and ibx.y0 < _obs.y1
                        and _obs.x0 > ibx.x0 + 10):
                    ibx = fitz.Rect(ibx.x0, ibx.y0, min(ibx.x1, _obs.x0 - 2), ibx.y1)
            insert_bboxes.append(ibx)

    # 跨文本框续行检测：译文以助词開始 → 前フレームにマージ（字号も継承）
    _JA_PARTICLES = re.compile(r'^[のがをはでにへともやかなどからまでけどより]')
    for _i in range(1, len(translated_texts)):
        # タイトルブロックはマージ対象から除外（例：「まとめ：…」で始まるタイトルが前フレームに吸収されるのを防ぐ）
        if _i in title_indices or (_i - 1) in title_indices:
            continue
        if translated_texts[_i] and _JA_PARTICLES.match(translated_texts[_i].lstrip()):
            prev_item = insertion_items[_i - 1]
            curr_item = insertion_items[_i]
            v_gap = curr_item["bbox"].y0 - prev_item["bbox"].y1
            # 垂直距離が 2 行以内の場合は前フレームにマージして重複表示を防ぐ
            if v_gap < prev_item["font_size"] * 2.5 and translated_texts[_i - 1]:
                translated_texts[_i - 1] = (translated_texts[_i - 1].rstrip()
                                             + translated_texts[_i].lstrip())
                translated_texts[_i] = ""
                # 前フレームの insert_bbox を縦方向に拡張して current 分をカバー
                prev_ibx = insert_bboxes[_i - 1]
                curr_ibx = insert_bboxes[_i]
                insert_bboxes[_i - 1] = fitz.Rect(
                    prev_ibx.x0, prev_ibx.y0,
                    max(prev_ibx.x1, curr_ibx.x1),
                    max(prev_ibx.y1, curr_ibx.y1),
                )
            # Always inherit font size from previous item
            insertion_items[_i] = dict(insertion_items[_i])
            insertion_items[_i]["font_size"] = insertion_items[_i - 1]["font_size"]

    # ── Phase 2: per-item binary-search fitting size ─────────────────────
    from collections import defaultdict as _defaultdict
    fitting_sizes: List[float] = []
    for idx, (item, translated, ibbox) in enumerate(
            zip(insertion_items, translated_texts, insert_bboxes)):
        if not translated or idx in title_indices:
            fitting_sizes.append(item["font_size"])
            continue
        fs = _find_fitting_size(
            page, ibbox, translated, item["font_size"],
            item["color"], item["align"],
            fontname=font_name,
            min_size=max(4.0, item["font_size"] * 0.4),
        )
        fitting_sizes.append(fs)

    # Consistency pass: 80th-percentile cap per source_size group
    _samples: dict = _defaultdict(list)
    for idx, item in enumerate(insertion_items):
        if idx not in title_indices and translated_texts[idx]:
            _samples[item["font_size"]].append(fitting_sizes[idx])
    consistent_size: dict = {}
    for src_size, sizes in _samples.items():
        if len(sizes) < 3:
            consistent_size[src_size] = src_size
            continue
        sorted_desc = sorted(sizes, reverse=True)
        k = min(len(sorted_desc) - 1, max(0, int(len(sorted_desc) * 0.2)))
        consistent_size[src_size] = round(sorted_desc[k], 2)

    # ── Phase 3: render at min(fitting_size, consistent_size) ────────────
    for idx, (item, translated, insert_bbox) in enumerate(
            zip(insertion_items, translated_texts, insert_bboxes)):
        if not translated:
            continue
        if idx in title_indices:
            translated = translated.replace('\n', ' ').strip()
            render_size = item["font_size"]
        else:
            cons = consistent_size.get(item["font_size"], item["font_size"])
            render_size = min(fitting_sizes[idx], cons)

        insert_text_fitting(
            page, insert_bbox, translated, render_size,
            color=item["color"], align=item["align"],
            fontname=font_name,
            fontfile=cjk_font,
            min_factor=0.4,
        )

    # ── Z-order 重排（暂时禁用，safe-Q-snap 方案在复杂页面效果不佳）────────────
    # _apply_zorder(page, item_anchors, translations, verbose=verbose)


def parse_pages(spec: str, total: int) -> list:
    """解析页码规格（1-based），返回 0-based 列表。如 '1,3,5-8' → [0,2,4,5,6,7]"""
    result = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            result.update(range(int(a) - 1, min(int(b), total)))
        else:
            idx = int(part) - 1
            if 0 <= idx < total:
                result.add(idx)
    return sorted(result)


def _disk_cache_path(input_path: str, target_lang: str) -> str:
    """返回持久化翻译缓存文件路径（{dir}/{stem}.{tgt}.transcache.json）。"""
    stem = os.path.splitext(os.path.basename(input_path))[0]
    return os.path.join(os.path.dirname(os.path.abspath(input_path)),
                        f"{stem}.{target_lang}.transcache.json")


def _load_disk_cache(cache_path: str) -> dict:
    if os.path.exists(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def _save_disk_cache(cache_path: str, cache: dict):
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"  ⚠ 缓存保存失败: {e}")


def translate_pdf(input_path: str, output_path: str,
                  source_lang: str, target_lang: str,
                  cjk_font_hint: Optional[str] = None,
                  verbose: bool = True,
                  pages: Optional[list] = None):
    """翻译 PDF 文件。pages 为 0-based 页码列表，None 表示全部。"""
    if not os.path.exists(input_path):
        raise FileNotFoundError(f"文件不存在: {input_path}")

    if not os.path.exists(CLAUDE_CLI):
        print(f"错误: 找不到 claude CLI（路径: {CLAUDE_CLI}）")
        sys.exit(1)

    cjk_font = None
    if target_lang in ("zh", "zh-TW", "ja"):
        cjk_font = find_cjk_font(target_lang, cjk_font_hint)
        if cjk_font:
            if verbose:
                print(f"✓ CJK 字体: {cjk_font}")
        else:
            print("⚠ 未找到 CJK 字体，中日文可能显示为方块")

    if verbose:
        print(f"📄 打开: {input_path}")

    doc = fitz.open(input_path)
    total = len(doc)

    # 若指定页面子集，提取为临时文档
    if pages is not None:
        sub = fitz.open()
        sub.insert_pdf(doc, from_page=0, to_page=total - 1)
        sub.select(pages)
        doc.close()
        doc = sub
        page_labels = [p + 1 for p in pages]
    else:
        page_labels = list(range(1, total + 1))

    # 持久化翻译缓存：跨次调用复用，减少重复翻译 API 请求
    cache_path = _disk_cache_path(input_path, target_lang)
    trans_cache: dict = _load_disk_cache(cache_path)
    if trans_cache and verbose:
        print(f"  ✓ 读取磁盘缓存: {len(trans_cache)} 条（{os.path.basename(cache_path)}）")

    try:
        for i, page in enumerate(doc):
            label = page_labels[i]
            if verbose:
                print(f"  [{i+1}/{len(doc)}] 处理第 {label} 页...", flush=True)
            prev_cache_size = len(trans_cache)
            translate_page(page, source_lang, target_lang, cjk_font, verbose, trans_cache)
            # 每页翻译后保存缓存（即使中途崩溃也能复用已完成的翻译）
            if len(trans_cache) > prev_cache_size:
                _save_disk_cache(cache_path, trans_cache)

        if verbose:
            print(f"💾 保存: {output_path}", flush=True)
        doc.save(output_path, garbage=4, deflate=True, clean=True)

        if verbose:
            print("✅ 完成！")
    finally:
        doc.close()


def _text_similarity(a: str, b: str) -> float:
    """Jaccard similarity on character 4-grams."""
    def ngrams(s: str, n: int = 4):
        return set(s[i:i + n] for i in range(len(s) - n + 1))
    a_ng = ngrams(a)
    b_ng = ngrams(b)
    if not a_ng or not b_ng:
        return 0.0
    return len(a_ng & b_ng) / len(a_ng | b_ng)


def apply_page_copy_from_reference(
        output_path: str,
        src_path: str,
        ref_src_path: str,
        ref_output_path: str,
        similarity_threshold: float = 0.75,
        verbose: bool = True) -> None:
    """
    出力 PDF のページを参照出力 PDF（既に翻訳済み）の対応ページで置き換える。
    ページの一致判定はソース PDF 同士のテキスト類似度で行う。
    これにより、成果物3 内の成果物1 重複ページを再レンダリングせず高品質版で代替できる。
    """
    if not os.path.exists(ref_output_path) or not os.path.exists(ref_src_path):
        if verbose:
            print(f"⚠ 参照ファイルが見つかりません: {ref_src_path} / {ref_output_path}")
        return

    def page_fingerprint(page) -> str:
        text = page.get_text("text")
        text = re.sub(r'\s+', ' ', text).strip()
        return text[:400]

    src_doc = fitz.open(src_path)
    ref_src_doc = fitz.open(ref_src_path)

    ref_fingerprints = {i: page_fingerprint(p) for i, p in enumerate(ref_src_doc)
                        if page_fingerprint(p)}

    page_map: dict = {}
    for i, page in enumerate(src_doc):
        fp = page_fingerprint(page)
        if not fp:
            continue
        best_sim, best_ref = 0.0, -1
        for ref_idx, ref_fp in ref_fingerprints.items():
            sim = _text_similarity(fp, ref_fp)
            if sim > best_sim:
                best_sim, best_ref = sim, ref_idx
        if best_sim >= similarity_threshold:
            page_map[i] = best_ref

    src_doc.close()
    ref_src_doc.close()

    if not page_map:
        if verbose:
            print("  参照ページとの一致なし（置き換え不要）")
        return

    if verbose:
        for out_idx, ref_idx in sorted(page_map.items()):
            print(f"  ページ {out_idx + 1} → 参照ページ {ref_idx + 1} で置き換え")

    out_doc = fitz.open(output_path)
    ref_out_doc = fitz.open(ref_output_path)

    # 後ろから処理してインデックスのズレを防ぐ
    for out_idx in sorted(page_map.keys(), reverse=True):
        ref_idx = page_map[out_idx]
        if out_idx < len(out_doc) and ref_idx < len(ref_out_doc):
            out_doc.delete_page(out_idx)
            out_doc.insert_pdf(ref_out_doc, from_page=ref_idx, to_page=ref_idx,
                               start_at=out_idx)

    import tempfile
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".pdf")
    os.close(tmp_fd)
    try:
        out_doc.save(tmp_path, garbage=4, deflate=True, clean=True)
        ref_out_doc.close()
        out_doc.close()
        os.replace(tmp_path, output_path)
    except Exception:
        ref_out_doc.close()
        out_doc.close()
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

    if verbose:
        print(f"✅ 参照ページ置き換え完了（{len(page_map)} ページ）")


def main():
    parser = argparse.ArgumentParser(
        description="PDF 翻译工具 - 保留排版，使用 Claude Code 账号（无需额外 API Key）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
语言代码:  en | zh | zh-TW | ja

示例:
  python pdf_translator.py slides.pdf output.pdf -t zh
  python pdf_translator.py slides.pdf output.pdf -s zh -t ja
  python pdf_translator.py slides.pdf output.pdf -s ja -t en
        """,
    )
    parser.add_argument("input", help="输入 PDF 路径")
    parser.add_argument("output", nargs="?", default=None,
                        help="输出 PDF 路径（默认：~/Downloads/<原文件名>_ja.pdf）")
    parser.add_argument("-s", "--source", default="auto", help="源语言（默认自动检测）")
    parser.add_argument("-t", "--target", required=True, help="目标语言: en / zh / zh-TW / ja")
    parser.add_argument("--font", default=None, help="CJK 字体路径（通常自动找到，可省略）")
    parser.add_argument("-q", "--quiet", action="store_true", help="安静模式")
    parser.add_argument("--pages", default=None,
                        help="翻译指定页（1-based），如 1 或 1,3 或 2-5")
    parser.add_argument("--ref-src", default=None,
                        help="参照源 PDF（成果物1 等）：用于置换重复页")
    parser.add_argument("--ref-output", default=None,
                        help="参照输出 PDF（成果物1_ja 等）：重复页的高质量版本")

    args = parser.parse_args()

    if args.target not in SUPPORTED_LANGUAGES:
        print(f"错误: 不支持的目标语言 '{args.target}'，支持: {', '.join(SUPPORTED_LANGUAGES)}")
        sys.exit(1)

    if args.output is None:
        downloads = os.path.expanduser("~/Downloads")
        stem = os.path.splitext(os.path.basename(args.input))[0]
        args.output = os.path.join(downloads, f"{stem}_{args.target}.pdf")

    pages = None
    if args.pages:
        doc_tmp = fitz.open(args.input)
        total_tmp = len(doc_tmp)
        doc_tmp.close()
        pages = parse_pages(args.pages, total_tmp)

    translate_pdf(
        input_path=args.input,
        output_path=args.output,
        source_lang=args.source,
        target_lang=args.target,
        cjk_font_hint=args.font,
        verbose=not args.quiet,
        pages=pages,
    )

    if args.ref_src and args.ref_output:
        apply_page_copy_from_reference(
            output_path=args.output,
            src_path=args.input,
            ref_src_path=args.ref_src,
            ref_output_path=args.ref_output,
            verbose=not args.quiet,
        )


if __name__ == "__main__":
    main()
