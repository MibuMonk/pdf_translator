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
    return any(p.search(t) for p in SKIP_TEXT_PATTERNS)


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
    orig_area = max(1.0, orig_bbox.get_area())

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
        if cand.get_area() <= orig_area * MAX_MUL and no_collision(cand) and expansion_is_clear(cand):
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
        if cand2.get_area() <= orig_area * MAX_MUL and no_collision(cand2) and expansion_is_clear(cand2):
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


def fit_fontsize(text: str, bbox: fitz.Rect, base_size: float,
                 min_factor: float = 0.4) -> float:
    """
    用字符宽度估算找到能放入 bbox 的最大字号。
    不需要实际插入，速度快，作为 Shape dry-run 前的预筛选。
    只尝试 >= min_factor 的缩放比例。
    """
    candidates = [f for f in _SHRINK_FACTORS if f >= min_factor - 0.001]
    if not candidates:
        candidates = [min_factor]
    for factor in candidates:
        size = base_size * factor
        if size < 4:
            return 4.0
        line_height = size * _LINE_HEIGHT_FACTOR
        total_height = 0.0
        for raw_line in text.split("\n"):
            em_w = estimate_em_width(raw_line)
            chars_per_row = max(1.0, bbox.width / size)
            rows = math.ceil(em_w / chars_per_row) if em_w > 0 else 1
            total_height += rows * line_height
        if total_height <= bbox.height + size * 0.3:  # 允许小误差
            return size
    return max(4.0, base_size * min_factor)


# `insert_text_fitting` の因子リストにも 0.75 を追加（fit_fontsize と揃える）
_SHRINK_FACTORS = [1.0, 0.9, 0.8, 0.75, 0.7, 0.6, 0.5, 0.4]


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
        # 确保行高不超过 bbox 高度，否则 PyMuPDF 不渲染
        forced_size = max(4.0, min(base_size, bbox.height / _LINE_HEIGHT_FACTOR))
        fb_base: dict = {"color": color, "align": align}
        if fontname:
            fb_base["fontname"] = fontname
        elif fontfile:
            fb_base["fontfile"] = fontfile
            fb_base["fontname"] = "F0"
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

    # 用估算缩小搜索范围，但至少保留 4 个梯度
    start_size = fit_fontsize(text, bbox, base_size, min_factor=min_factor)
    start_factor = (start_size / base_size) if base_size > 0 else min_factor
    all_factors = [f for f in _SHRINK_FACTORS if f >= min_factor - 0.001]
    if not all_factors:
        all_factors = [min_factor]
    factors = [f for f in all_factors if f <= start_factor + 0.15] or all_factors

    for factor in factors:
        size = max(4.0, base_size * factor)
        shape = page.new_shape()
        try:
            rc = shape.insert_textbox(bbox, text, fontsize=size,
                                      color=color, align=align,
                                      lineheight=_LINE_HEIGHT_FACTOR, **font_kw)
            if rc >= 0:
                shape.commit()
                return True
            # 溢出 → 不 commit，继续缩小
        except Exception:
            # Shape 不支持该字体参数 → 跳出，走 page.insert_textbox 兜底
            break

    # 兜底：直接用 page.insert_textbox（支持 fontfile）
    fb: dict = {"fontsize": max(4.0, base_size * min_factor), "color": color, "align": align,
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
    # rc < 0 → 文本在 bbox 里放不下（通常因为窄列需要多行但高度不足）
    # 截断为单行能容纳的字数 + 省略号，保证至少有内容可见
    fs = fb["fontsize"]
    max_chars = max(1, int(bbox.width / fs))
    display_text = _truncate_to_em_width(text, max_chars)
    try:
        page.insert_textbox(bbox, display_text, **fb)
    except Exception:
        pass
    return True


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
        f"你是一位专业演示文稿翻译。将以下幻灯片文本从 {src_name} 翻译成 {tgt_name}。\n"
        f"要求：简洁自然，保持幻灯片风格，专业术语准确，换行符（\\n）原样保留。"
        f"{context_section}\n"
        f"输入格式：JSON 数组，每个元素有 id 和 text 字段。\n"
        f"输出格式：仅返回 JSON 数组，结构相同，将每个 text 翻译后原样输出。"
        f"不要输出任何其他内容，不要 markdown 代码块，仅纯 JSON。\n\n"
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
            for line in block["lines"]:
                line_text = "".join(_normalize_span_text(span) for span in line["spans"]).strip()
                if not line_text or is_skip_text(line_text):
                    continue
                span = line["spans"][0]
                if span["size"] < _MIN_TRANSLATE_FONTSIZE:
                    continue
                lx0, ly0, lx1, ly1 = line["bbox"]
                # 给行 bbox 增加少量垂直空间以便文字能放入
                line_bbox = fitz.Rect(lx0, ly0, lx1, ly1 + span["size"] * 0.5)
                redact_bboxes.append(fitz.Rect(line["bbox"]))
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

    # ── 插入译文：弹性 bbox + Shape dry-run 精确适配字号 ──────────
    # 预建所有插入项的 bbox 列表，供碰撞检测使用
    all_insert_bboxes = [item["bbox"] for item in insertion_items]

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

    for idx, (item, translated) in enumerate(zip(insertion_items, translations)):
        if not translated:
            continue

        # bullet 后的空白（包括换行）→ 非换行空格，防止 PyMuPDF 在 bullet 后断行
        translated = re.sub(r'([•§·▪▸►▶◆◇○●])[\s\n]+', lambda m: m.group(1) + '\xa0', translated)

        if idx in title_indices:
            # 标题/大字：强制单行 + 扩展至页面宽度
            # y1 同时保证不小于原字号一行高度，避免"小 bbox"路径强制缩减字号
            translated = translated.replace('\n', ' ').strip()
            title_y1 = max(item["bbox"].y1,
                           item["bbox"].y0 + item["font_size"] * _LINE_HEIGHT_FACTOR * 2)
            insert_bbox = fitz.Rect(item["bbox"].x0, item["bbox"].y0,
                                    page_rect.width - 2, title_y1)
        else:
            other_bboxes = [b for i, b in enumerate(all_insert_bboxes) if i != idx]
            insert_bbox = try_expand_bbox(
                item["bbox"], translated.strip(), item["font_size"],
                page_rect, other_bboxes,
                orig_pixmap=orig_pixmap,
            )
        min_factor = 0.4

        insert_text_fitting(
            page, insert_bbox, translated, item["font_size"],
            color=item["color"], align=item["align"],
            fontname=font_name,
            fontfile=cjk_font,
            min_factor=min_factor,
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


if __name__ == "__main__":
    main()
