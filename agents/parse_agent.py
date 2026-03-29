#!/usr/bin/env python3
"""
Parse Agent — PDF テキストブロック抽出・構造化 JSON 出力

PDF を読み込み、翻訳対象テキストブロックをすべて抽出して parsed.json に書き出す。
水印・フッター・回転文字の除外、散乱ブロックの列分割、縦隣接同一列ブロックのマージを行う。
"""

import fitz  # PyMuPDF
import argparse
import json
import os
import re
import sys
from collections import Counter
from typing import List, Optional, Tuple

# ── 定数 ──────────────────────────────────────────────────────────────────────

MIN_TRANSLATE_FONTSIZE = 6.0  # これ未満の font_size のブロックは翻訳対象外

_BULLET_ONLY = {"•", "§", "v", "Ø", "-", "–", "—"}

SKIP_TEXT_PATTERNS = [
    re.compile(r"confidential\s+for\s+honda", re.IGNORECASE),
    re.compile(r"confidential\s+view\s+only", re.IGNORECASE),
    re.compile(r"^\s*confidential\s*$", re.IGNORECASE),
]

# 純数字列（ページ番号など）のマッチ
_DIGITS_ONLY_RE = re.compile(r'^\s*\d+\s*$')

# ── テキスト正規化ヘルパー ────────────────────────────────────────────────────

def color_from_int(c: int) -> Tuple[float, float, float]:
    """PyMuPDF の整数カラー値を (R, G, B) float タプルに変換する。"""
    return ((c >> 16 & 0xFF) / 255.0, (c >> 8 & 0xFF) / 255.0, (c & 0xFF) / 255.0)


def _dominant_color_int(lines: list) -> int:
    """lines 内の全 span を走査し、字符数が最も多い色（int）を返す。"""
    counts: Counter = Counter()
    for line in lines:
        for span in line["spans"]:
            counts[span["color"]] += len(span["text"])
    if counts:
        return counts.most_common(1)[0][0]
    return lines[0]["spans"][0]["color"]


def _line_dominant_color_int(line: dict) -> int:
    """1 行内の全 span を走査し、字符数が最も多い色（int）を返す。"""
    counts: Counter = Counter()
    for span in line["spans"]:
        counts[span["color"]] += len(span["text"])
    if counts:
        return counts.most_common(1)[0][0]
    return line["spans"][0]["color"]


def _build_color_spans(lines: list) -> list:
    """
    Build color_spans from all spans in lines.
    Adjacent same-color spans are merged. Each entry has text and color.
    Returns list of {"text": str, "color": [R, G, B]} dicts.
    If all text is one color, returns empty list (caller uses 'color' fallback).
    """
    # Collect (text, color_int) for all spans in order
    segments: list = []
    for line in lines:
        for span in line["spans"]:
            text = _normalize_span_text(span)
            if not text:
                continue
            c_int = span["color"]
            # Merge with previous if same color
            if segments and segments[-1][1] == c_int:
                segments[-1] = (segments[-1][0] + text, c_int)
            else:
                segments.append((text, c_int))

    if not segments:
        return []

    # If only one color, return empty (use 'color' field fallback)
    if len(segments) == 1:
        return []

    return [
        {"text": seg_text, "color": list(color_from_int(c_int))}
        for seg_text, c_int in segments
    ]


def _normalize_span_text(span: dict) -> str:
    """
    特殊記号フォント（Wingdings/Webdings）の偽装文字を標準 Unicode に戻す。
    例: Wingdings の § は実際には bullet • として表示される。
    """
    text = span["text"]
    font = span.get("font", "")
    if "Wingdings" in font or "Webdings" in font:
        text = text.replace("§", "•").replace("v", "•").replace("Ø", "•")
        text = text.replace("q", "■")   # Wingdings q (0x71) → black square
        text = text.replace("ü", "✔")  # Wingdings ü (0xFC) → heavy check mark
    return text


# ── フィルタ関数 ───────────────────────────────────────────────────────────────

def is_skip_text(text: str) -> bool:
    """True を返す場合、そのテキストブロックは翻訳対象外（水印・フッター等）。"""
    t = text.strip()
    if not t:
        return True
    # 純数字（ページ番号）
    if _DIGITS_ONLY_RE.match(t):
        return True
    # パターンマッチ
    if any(p.search(t) for p in SKIP_TEXT_PATTERNS):
        return True
    # 短い断片水印検出（25 文字以内に confidential/honda 等を含む）
    if len(t) <= 25:
        tl = t.lower().replace('\xa0', ' ')
        if any(frag in tl for frag in (
            'confid', 'nfide', 'fidenti', 'honda', 'view only', 'ential', 'w only', ' only'
        )):
            return True
    return False


def is_watermark_block(block: dict) -> bool:
    """
    テキストブロックが水印（回転文字）かどうかを判定する。
    いずれかの行の dir の y 成分の絶対値が 0.1 超（約 6° 以上の傾き）なら True。
    """
    for line in block.get("lines", []):
        _, dir_y = line.get("dir", (1.0, 0.0))
        if abs(dir_y) > 0.1:
            return True
    return False


def _has_cjk(text: str) -> bool:
    """Return True if text contains any CJK character."""
    for ch in text:
        cp = ord(ch)
        if (0x3000 <= cp <= 0x9FFF) or (0xF900 <= cp <= 0xFAFF) or (0x20000 <= cp <= 0x2A6DF):
            return True
    return False


def is_hidden_by_drawing(block_bbox_tuple: tuple, drawings: list, page_area: float,
                         block_text: str = "") -> bool:
    """
    ブロックの bbox が暗色の塗り潰し矩形に完全に覆われているか判定する。

    除外条件（= 隠しとみなさない）:
    - fill が白/薄色 (全成分 > 0.85)
    - 矩形がページ面積の 30% 超（スライド背景）
    - block_text が 3 文字超（実質的な内容を持つブロックは隠しとみなさない）
    - block_text に CJK 文字が含まれる（翻訳後テキストは pipeline artifact の描画より優先）

    The last two conditions prevent false positives when white cover rects drawn
    by layout_agent (to mask XObject English text) spatially overlap newly
    rendered translated text at the same position.
    """
    # If the block already has substantive translatable content, don't treat it
    # as hidden regardless of what drawings are present.  This guards against
    # pipeline artifacts (white/bg cover rects written to the content stream to
    # erase XObject text) that coincidentally overlap freshly inserted text.
    if block_text and (len(block_text.strip()) > 3 or _has_cjk(block_text)):
        return False

    bx0, by0, bx1, by1 = block_bbox_tuple
    for d in drawings:
        fill = d.get("fill")
        if fill is None:
            continue
        r = d.get("rect")
        if r is None:
            continue
        # ページ面積の 30% 超 → スライド背景, スキップ
        rect_area = (r.x1 - r.x0) * (r.y1 - r.y0)
        if page_area > 0 and rect_area / page_area > 0.30:
            continue
        # 薄色（ほぼ白）背景 → テキストは読める, スキップ
        if isinstance(fill, (tuple, list)) and len(fill) >= 3:
            if all(c > 0.85 for c in fill[:3]):
                continue
        # 矩形が block_bbox を完全に覆っているか（1px 余裕）
        if r.x0 <= bx0 + 1 and r.y0 <= by0 + 1 and r.x1 >= bx1 - 1 and r.y1 >= by1 - 1:
            return True
    return False


# ── 散乱ブロック検出 ───────────────────────────────────────────────────────────

def lines_are_scattered(block: dict, x_threshold: float = 60.0) -> bool:
    """
    ブロック内に、同じ y 位置でありながら x 中心が大きく離れた行が存在するか判定する。
    例: 凡例中の 'Bad'（x≈457）と 'Good'（x≈542）x_gap≈85px。
    bullet-only 行（•、§ 等）は判定から除外する。
    """
    all_lines = block.get("lines", [])
    lines = [
        ln for ln in all_lines
        if "".join(s["text"] for s in ln["spans"]).strip() not in _BULLET_ONLY
    ]
    if len(lines) < 2:
        return False
    for i in range(len(lines)):
        li = lines[i]
        liy0, liy1 = li["bbox"][1], li["bbox"][3]
        lix_c = (li["bbox"][0] + li["bbox"][2]) / 2.0
        for j in range(i + 1, len(lines)):
            lj = lines[j]
            ljy0, ljy1 = lj["bbox"][1], lj["bbox"][3]
            ljx_c = (lj["bbox"][0] + lj["bbox"][2]) / 2.0
            y_overlap = min(liy1, ljy1) - max(liy0, ljy0)
            if y_overlap > 2:  # y 重複が 2pt 超 → 同行とみなす
                x_gap = abs(lix_c - ljx_c)
                if x_gap > x_threshold:
                    return True
    return False


# ── ページ解析 ─────────────────────────────────────────────────────────────────

def parse_page(page: fitz.Page, page_num: int) -> dict:
    """
    1 ページ分のテキストブロックを抽出し、構造化辞書として返す。

    Returns:
        {
            "page_num": int,
            "width": float,
            "height": float,
            "blocks": [ block_dict, ... ],
            "image_obstacles": [ [x0, y0, x1, y1], ... ],
        }
    """
    page_rect = page.rect
    text_dict = page.get_text(
        "dict",
        flags=fitz.TEXT_PRESERVE_WHITESPACE | fitz.TEXT_PRESERVE_LIGATURES,
    )
    raw_blocks = [b for b in text_dict["blocks"] if b["type"] == 0]

    # 塗り潰し矩形（隠しブロック判定用）
    try:
        drawings = page.get_drawings()
    except Exception:
        drawings = []

    # 画像障害物の収集
    image_obstacles: List[List[float]] = []
    try:
        for info in page.get_image_info(hashes=False, xrefs=False):
            r = fitz.Rect(info["bbox"])
            if r.width > 30 and r.height > 30:
                image_obstacles.append(list(r))
    except Exception:
        pass

    # ── 前処理: 続行孤立ブロックのマージ ────────────────────────────────────
    # block[j] が block[i] の x 範囲に含まれ、垂直に隣接し、かつ block[i] のテキストが
    # 空白またはハイフンで終わる場合、block[j] を block[i] に統合する。
    _skip_continuation: set = set()
    _continuation_appends: dict = {}  # block index → list of (text, bbox_list)

    for ci in range(len(raw_blocks) - 1):
        bi = raw_blocks[ci]
        bj = raw_blocks[ci + 1]
        if ci + 1 in _skip_continuation:
            continue
        if is_watermark_block(bi) or is_watermark_block(bj):
            continue
        xi0, yi0, xi1, yi1 = bi["bbox"]
        xj0, yj0, xj1, yj1 = bj["bbox"]
        wi = xi1 - xi0
        wj = xj1 - xj0
        if wi <= 0 or wj <= 0:
            continue
        # 条件 1: x 包含（5px 余裕）
        if xj0 < xi0 - 5 or xj1 > xi1 + 5:
            continue
        # 条件 3: bj 幅 ≤ bi 幅の 60%
        if wj > wi * 0.60:
            continue
        # 条件 2: 垂直隣接（font_size 以内）
        fs_i = bi["lines"][0]["spans"][0]["size"] if bi["lines"] and bi["lines"][0]["spans"] else 10
        if yj0 > yi1 + fs_i:
            continue
        # bi テキスト末尾が空白またはハイフンか
        bi_text_raw = "".join(
            "".join(_normalize_span_text(s) for s in line["spans"])
            for line in bi["lines"]
        )
        bj_text = "".join(
            "".join(_normalize_span_text(s) for s in line["spans"])
            for line in bj["lines"]
        ).strip()
        if not bj_text:
            continue
        if not (bi_text_raw.endswith(" ") or bi_text_raw.endswith("-")):
            continue
        if is_skip_text(bj_text):
            continue
        _skip_continuation.add(ci + 1)
        _continuation_appends.setdefault(ci, []).append((bj_text, list(bj["bbox"])))

    # ── メインループ: ブロック抽出 ────────────────────────────────────────────
    insertion_items: list = []  # 各エントリ: insertion_item dict
    # redact_bboxes は block レベルで管理（同じ block 内の全行 bbox を集約）
    # insertion_item に redact_bboxes フィールドとして含める

    for block_rank, block in enumerate(raw_blocks):
        if block_rank in _skip_continuation:
            continue
        if is_watermark_block(block):
            continue

        block_bbox_tuple = block["bbox"]  # (x0, y0, x1, y1)

        # テキスト結合（bullet-only 行のマージを含む）
        lines_text = []
        for line in block["lines"]:
            line_text = "".join(_normalize_span_text(s) for s in line["spans"])
            lines_text.append(line_text)

        # 隠しブロック（暗色矩形に完全に覆われている）
        # テキストを先に取得し、実質的な内容があれば隠しとみなさない
        _raw_block_text = "".join(lines_text).strip()
        page_area = page_rect.width * page_rect.height
        if is_hidden_by_drawing(block_bbox_tuple, drawings, page_area, _raw_block_text):
            continue

        # bullet-only 行と次行を結合
        i = 0
        merged_lines: List[str] = []
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

        # 続行孤立ブロックの追記
        extra_redact_bboxes: List[List[float]] = []
        if block_rank in _continuation_appends:
            for ct, cb in _continuation_appends[block_rank]:
                block_text = block_text.rstrip() + " " + ct
                extra_redact_bboxes.append(cb)

        if is_skip_text(block_text):
            continue

        first_span = block["lines"][0]["spans"][0]
        block_fontsize = first_span["size"]
        if block_fontsize < MIN_TRANSLATE_FONTSIZE:
            continue

        # 対齐検出
        bx0, _, bx1, _ = block_bbox_tuple
        sx0 = first_span["bbox"][0]
        if abs(sx0 - bx0) < 5:
            align = 0  # LEFT
        elif abs(sx0 - (bx0 + bx1) / 2.0) < (bx1 - bx0) * 0.3:
            align = 1  # CENTER
        else:
            align = 0  # LEFT (default)

        if lines_are_scattered(block):
            # 散乱ブロック: 行ごとに独立したアイテムとして処理
            scattered_lines = block["lines"]
            sl_merged: list = []  # [(merged_text, merged_bbox_list, span0, merged_lines)]
            si = 0
            while si < len(scattered_lines):
                line = scattered_lines[si]
                lt = "".join(_normalize_span_text(s) for s in line["spans"]).strip()
                merged_text = lt
                merged_bbox = list(line["bbox"])
                span0 = line["spans"][0]
                group_lines = [line]
                si2 = si + 1
                while si2 < len(scattered_lines):
                    next_line = scattered_lines[si2]
                    next_lt = "".join(_normalize_span_text(s) for s in next_line["spans"]).strip()
                    nb = next_line["bbox"]
                    # 次行の x0 が現行の x1 より右にあれば別列 → 結合しない
                    if nb[0] > merged_bbox[2] + 5:
                        break
                    if (merged_text and merged_text[-1] not in '.!?\u3002\uff01\uff1f'
                            and next_lt and next_lt[0].islower()):
                        merged_text = merged_text.rstrip() + ' ' + next_lt
                        merged_bbox = [
                            min(merged_bbox[0], nb[0]), min(merged_bbox[1], nb[1]),
                            max(merged_bbox[2], nb[2]), max(merged_bbox[3], nb[3]),
                        ]
                        group_lines.append(next_line)
                        si2 += 1
                    else:
                        break
                sl_merged.append((merged_text, merged_bbox, span0, group_lines))
                si = si2

            for (line_text, lbbox, span, grp_lines) in sl_merged:
                if not line_text or is_skip_text(line_text):
                    continue
                if span["size"] < MIN_TRANSLATE_FONTSIZE:
                    continue
                lx0, ly0, lx1, ly1 = lbbox
                # 行 bbox に垂直余白を追加（文字が収まるよう）
                render_bbox = [lx0, ly0, lx1, ly1 + span["size"] * 0.5]
                color = color_from_int(_dominant_color_int(grp_lines))
                # 散乱ブロック: per-span 色比率を記録
                cs = _build_color_spans(grp_lines)
                item = {
                    "text": line_text,
                    "font_size": span["size"],
                    "color": list(color),
                    "align": 0,  # LEFT
                    "bbox": render_bbox,
                    "redact_bboxes": [lbbox],
                    "stream_rank": block_rank,
                }
                if cs:
                    item["color_spans"] = cs
                insertion_items.append(item)
        else:
            # 通常ブロック: 1 つのアイテムとして処理
            color = color_from_int(_dominant_color_int(block["lines"]))
            redact_bboxes = [list(block_bbox_tuple)] + extra_redact_bboxes

            # per-span 色比率を記録
            cs = _build_color_spans(block["lines"])

            item = {
                "text": block_text,
                "font_size": block_fontsize,
                "color": list(color),
                "align": align,
                "bbox": list(block_bbox_tuple),
                "redact_bboxes": redact_bboxes,
                "stream_rank": block_rank,
            }
            if cs:
                item["color_spans"] = cs
            insertion_items.append(item)

    # ── 重複除去: 面積が 70% 以上重複する小ブロックを削除 ───────────────────
    if len(insertion_items) > 1:
        import fitz as _fitz
        iboxes = [_fitz.Rect(item["bbox"]) for item in insertion_items]
        keep = [True] * len(insertion_items)
        for ii in range(len(iboxes)):
            if not keep[ii]:
                continue
            for jj in range(ii + 1, len(iboxes)):
                if not keep[jj]:
                    continue
                inter = iboxes[ii] & iboxes[jj]
                if inter.is_empty:
                    continue
                ai = iboxes[ii].width * iboxes[ii].height
                aj = iboxes[jj].width * iboxes[jj].height
                a_inter = inter.width * inter.height
                min_a = min(ai, aj)
                if min_a > 0 and a_inter / min_a > 0.70:
                    if ai <= aj:
                        keep[ii] = False
                    else:
                        keep[jj] = False
        insertion_items = [item for idx, item in enumerate(insertion_items) if keep[idx]]

    # ── 隣接同列ブロックマージ ───────────────────────────────────────────────
    # y_gap < 6px かつ x 重複率 > 30% の連続アイテムを 1 つに統合する。
    _MERGE_Y_GAP   = 6.0
    _MERGE_X_RATIO = 0.30
    new_items: list = []
    mi = 0
    while mi < len(insertion_items):
        item = insertion_items[mi]
        item_bbox = fitz.Rect(item["bbox"])
        merged_redact = list(item["redact_bboxes"])
        while mi + 1 < len(insertion_items):
            ni = insertion_items[mi + 1]
            ni_bbox = fitz.Rect(ni["bbox"])
            ygap = ni_bbox.y0 - item_bbox.y1
            if ygap < 0 or ygap > _MERGE_Y_GAP:
                break
            xlo  = max(item_bbox.x0, ni_bbox.x0)
            xhi  = min(item_bbox.x1, ni_bbox.x1)
            xuni = max(item_bbox.x1, ni_bbox.x1) - min(item_bbox.x0, ni_bbox.x0)
            if xuni <= 0 or (xhi - xlo) / xuni < _MERGE_X_RATIO:
                break
            # マージ実行
            merged_bbox = fitz.Rect(
                min(item_bbox.x0, ni_bbox.x0), item_bbox.y0,
                max(item_bbox.x1, ni_bbox.x1), ni_bbox.y1,
            )
            prev_text = item["text"]  # preserve before overwrite
            merged_text = prev_text.rstrip('\n') + '\n' + ni["text"].lstrip('\n')
            merged_redact = merged_redact + list(ni["redact_bboxes"])
            item = dict(item)
            item["text"] = merged_text
            item["bbox"] = list(merged_bbox)
            item["redact_bboxes"] = merged_redact
            # color_spans のマージ (text format)
            cs_a = item.get("color_spans", [])
            cs_b = ni.get("color_spans", [])
            # Also synthesize color_spans when neither block has them
            # but their dominant colors differ (e.g. blue title + black bullets)
            colors_differ = (item.get("color", [0,0,0]) != ni.get("color", [0,0,0]))
            if cs_a or cs_b or colors_differ:
                # If a block had no color_spans, treat it as single-color
                if not cs_a:
                    cs_a = [{"text": prev_text.rstrip('\n'), "color": list(item.get("color", [0, 0, 0]))}]
                if not cs_b:
                    cs_b = [{"text": ni["text"].lstrip('\n'), "color": list(ni.get("color", [0, 0, 0]))}]
                # Concatenate
                merged_cs = list(cs_a) + list(cs_b)
                # Merge adjacent same-color
                compact = [dict(merged_cs[0])]
                for s in merged_cs[1:]:
                    if s["color"] == compact[-1]["color"]:
                        compact[-1]["text"] = compact[-1]["text"] + s["text"]
                    else:
                        compact.append(dict(s))
                # If only one segment, drop color_spans (use color fallback)
                if len(compact) > 1:
                    item["color_spans"] = compact
                elif "color_spans" in item:
                    del item["color_spans"]
            item_bbox = merged_bbox
            mi += 1
        new_items.append(item)
        mi += 1
    insertion_items = new_items

    # ── ブロック ID の付与 ────────────────────────────────────────────────────
    out_blocks = []
    for bidx, item in enumerate(insertion_items):
        block_id = f"p{page_num:02d}_b{bidx:03d}"
        out_block = {
            "id": block_id,
            "text": item["text"],
            "font_size": item["font_size"],
            "color": item["color"],
            "align": item["align"],
            "bbox": item["bbox"],
            "redact_bboxes": item["redact_bboxes"],
            "stream_rank": item["stream_rank"],
        }
        if item.get("color_spans"):
            out_block["color_spans"] = item["color_spans"]
        out_blocks.append(out_block)

    return {
        "page_num": page_num,
        "width": float(page_rect.width),
        "height": float(page_rect.height),
        "blocks": out_blocks,
        "image_obstacles": image_obstacles,
    }


# ── ページ指定パース ───────────────────────────────────────────────────────────

def parse_page_spec(spec: str, total_pages: int) -> List[int]:
    """
    "1,3,5-8" のようなページ指定文字列を 1-indexed ページ番号リストに展開する。
    全ページ指定の場合は None を渡すこと（呼び出し元で処理）。
    """
    pages: List[int] = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-", 1)
            lo = int(lo.strip())
            hi = int(hi.strip())
            pages.extend(range(lo, hi + 1))
        else:
            pages.append(int(part))
    # 重複除去・ソート・範囲クリップ
    pages = sorted(set(p for p in pages if 1 <= p <= total_pages))
    return pages


# ── エントリポイント ───────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="PDF テキストブロック抽出エージェント。parsed.json を出力する。"
    )
    parser.add_argument("--input",  required=True, help="入力 PDF ファイルパス")
    parser.add_argument("--output", default=None,  help="出力 parsed.json パス（省略時: <stem>.parsed.json）")
    parser.add_argument("--src",    default="en",  help="原文言語コード（デフォルト: en）")
    parser.add_argument("--tgt",    default="ja",  help="翻訳先言語コード（デフォルト: ja）")
    parser.add_argument("--pages",  default=None,  help='ページ指定（例: "1,3,5-8"）、省略時は全ページ')
    args = parser.parse_args()

    input_path = os.path.abspath(args.input)
    if not os.path.exists(input_path):
        print(f"ERROR: 入力ファイルが存在しません: {input_path}", file=sys.stderr)
        sys.exit(1)

    if args.output:
        output_path = os.path.abspath(args.output)
    else:
        stem = os.path.splitext(input_path)[0]
        output_path = stem + ".parsed.json"

    doc = fitz.open(input_path)
    total_pages = doc.page_count

    if args.pages:
        page_nums = parse_page_spec(args.pages, total_pages)
    else:
        page_nums = list(range(1, total_pages + 1))

    print(f"解析開始: {input_path}")
    print(f"  ページ数: {len(page_nums)} / {total_pages}, 出力: {output_path}")

    parsed_pages = []
    for page_num in page_nums:
        page = doc[page_num - 1]  # 0-indexed
        print(f"  ページ {page_num}/{total_pages} 解析中...", end="", flush=True)
        page_data = parse_page(page, page_num)
        parsed_pages.append(page_data)
        print(f" {len(page_data['blocks'])} ブロック抽出")

    doc.close()

    result = {
        "version": "1.0",
        "input_pdf": input_path,
        "source_lang": args.src,
        "target_lang": args.tgt,
        "pages": parsed_pages,
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    total_blocks = sum(len(p["blocks"]) for p in parsed_pages)
    print(f"完了: 合計 {total_blocks} ブロック → {output_path}")


if __name__ == "__main__":
    main()
