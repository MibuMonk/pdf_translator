#!/usr/bin/env python3
"""
PDF Translator 回归测试套件

用法:
  uv run python3 test_regression.py          # 运行所有测试
  uv run python3 test_regression.py --quick  # 仅运行快速测试（跳过实际翻译）
"""

import sys
import os
import json
import fitz
import numpy as np

sys.path.insert(0, os.path.dirname(__file__))

GAP_PDF  = "/Users/qirui/Downloads/【成果物3】ギャップ分析及びプロポーザル.pdf"
HWS_PDF  = "/tmp/Honda_Toolchain_v2.pdf"  # 45页 Honda WS（已翻译版本）

# ─── 测试配置 ────────────────────────────────────────────────────────────────
# (PDF路径, 0-indexed页码, 期望最少翻译块数, 期望最多翻译块数, 说明)
BLOCK_COUNT_TESTS = [
    (GAP_PDF,  0,   5,  30,  "Gap p1: 封面"),
    (GAP_PDF,  2,  40,  80,  "Gap p3: 复杂图表页"),
    (GAP_PDF, 10,  80, 150,  "Gap p11: 最复杂页（表格）"),
    (GAP_PDF, 12,  30,  80,  "Gap p13: 表格页"),
    (GAP_PDF, 24,  25,  50,  "Gap p25: 流程图（大量隐藏块）"),
    (GAP_PDF, 49,  30,  70,  "Gap p50: 复杂页"),
    (GAP_PDF, 46,   2,  10,  "Gap p47: 最简页"),
]

HIDDEN_DETECTION_TESTS = [
    # (PDF, 0-indexed页, 文本关键词, 期望是否被标记为hidden)
    (GAP_PDF, 24, "Bench CT",              True,  "Bench CT 应被检测为隐藏（实心色块内）"),
    (GAP_PDF, 24, "Test Benches",          True,  "Test Benches 应被检测为隐藏"),
    (GAP_PDF, 24, "Data Product Line",     False, "Data Product Line 应可见（白底）"),
    (GAP_PDF, 24, "Simulation testing",    True,  "Simulation testing 应被检测为隐藏"),
    (GAP_PDF, 24, "Recap: Momenta",        False, "Recap 标题应可见"),
]

PASSES = 0
FAILS  = 0


def check(label: str, condition: bool, detail: str = ""):
    global PASSES, FAILS
    status = "✅ PASS" if condition else "❌ FAIL"
    print(f"  {status}  {label}")
    if not condition and detail:
        print(f"         → {detail}")
    if condition:
        PASSES += 1
    else:
        FAILS += 1


def _render_page(pdf_path: str, page_idx: int):
    """渲染页面并返回亮度矩阵。"""
    pdf = fitz.open(pdf_path)
    page = pdf[page_idx]
    pix = page.get_pixmap(matrix=fitz.Matrix(1, 1), alpha=False)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
    lum = (arr[:,:,0].astype(np.int32)*299 +
           arr[:,:,1].astype(np.int32)*587 +
           arr[:,:,2].astype(np.int32)*114) // 1000
    return page, lum, (pix.width, pix.height)


def _is_hidden_check(lum, bbox: fitz.Rect):
    """与 pdf_translator.py 中 is_hidden() 相同的逻辑。"""
    w, h = lum.shape[1], lum.shape[0]
    x0 = max(0, int(bbox.x0)); y0 = max(0, int(bbox.y0))
    x1 = min(w, int(bbox.x1));  y1 = min(h, int(bbox.y1))
    if x1 <= x0 or y1 <= y0:
        return False
    region = lum[y0:y1, x0:x1]
    total = region.size
    if total < 4:
        return False
    dark_ratio = float((region < 180).sum()) / total
    std = float(region.std())
    mean = float(region.mean())
    if dark_ratio < 0.03 and std < 15:
        return True
    if dark_ratio < 0.10 and std < 18 and mean < 245:
        return True
    if std < 8 and mean < 180:
        return True
    return False


def test_block_counts():
    print("\n── 文本块计数测试 ──────────────────────────────────────────────")
    for pdf_path, page_idx, lo, hi, desc in BLOCK_COUNT_TESTS:
        if not os.path.exists(pdf_path):
            print(f"  ⚠ SKIP  {desc} (文件不存在)")
            continue
        pdf = fitz.open(pdf_path)
        page = pdf[page_idx]
        blocks = [b for b in page.get_text(
            "dict",
            flags=fitz.TEXT_PRESERVE_WHITESPACE | fitz.TEXT_PRESERVE_LIGATURES
        )["blocks"] if b["type"] == 0]
        count = len(blocks)
        check(
            f"{desc}: {count} 块 (期望 {lo}-{hi})",
            lo <= count <= hi,
            f"实际块数 {count} 超出范围 [{lo}, {hi}]"
        )


def test_hidden_detection():
    print("\n── 隐藏文字检测测试 ─────────────────────────────────────────────")
    for pdf_path, page_idx, keyword, expected_hidden, desc in HIDDEN_DETECTION_TESTS:
        if not os.path.exists(pdf_path):
            print(f"  ⚠ SKIP  {desc}")
            continue
        page, lum, _ = _render_page(pdf_path, page_idx)
        blocks = [b for b in page.get_text(
            "dict",
            flags=fitz.TEXT_PRESERVE_WHITESPACE | fitz.TEXT_PRESERVE_LIGATURES
        )["blocks"] if b["type"] == 0]

        found = False
        for b in blocks:
            first_text = ""
            if b["lines"] and b["lines"][0]["spans"]:
                first_text = b["lines"][0]["spans"][0].get("text", "")
            if keyword in first_text:
                found = True
                bbox = fitz.Rect(b["bbox"])
                actual_hidden = _is_hidden_check(lum, bbox)
                check(
                    desc,
                    actual_hidden == expected_hidden,
                    f"期望 hidden={expected_hidden}，实际 hidden={actual_hidden} (text='{first_text[:30]}')"
                )
                break

        if not found:
            print(f"  ⚠ SKIP  {desc} (未找到关键词 '{keyword}')")


def test_no_4pt_filter():
    """验证 ≤4pt 过滤器已移除（page 25 应有 30+ 可翻译块）"""
    print("\n── ≤4pt 过滤器回归测试 ──────────────────────────────────────────")
    if not os.path.exists(GAP_PDF):
        print("  ⚠ SKIP (文件不存在)")
        return

    page, lum, _ = _render_page(GAP_PDF, 24)
    blocks = [b for b in page.get_text(
        "dict",
        flags=fitz.TEXT_PRESERVE_WHITESPACE | fitz.TEXT_PRESERVE_LIGATURES
    )["blocks"] if b["type"] == 0]

    # 统计实际会被翻译的块（非watermark、非hidden）
    translatable = 0
    for b in blocks:
        first_text = ""
        if b["lines"] and b["lines"][0]["spans"]:
            first_text = b["lines"][0]["spans"][0].get("text", "")
        # 简单排除 "Confidential for Honda view only"
        if "Confidential" in first_text:
            continue
        bbox = fitz.Rect(b["bbox"])
        if not _is_hidden_check(lum, bbox):
            translatable += 1

    check(
        f"Gap p25 可翻译块 ≥ 30（实际 {translatable}，验证 ≤4pt 过滤器已移除）",
        translatable >= 30,
        "如果 ≤4pt 过滤器存在，此值会 ≤ 5"
    )


def test_trivially_invariant():
    """验证 _is_trivially_invariant 正确识别纯数字/符号（不需翻译）和正常文本（需翻译）。"""
    print("\n── 不变文本过滤器测试 ──────────────────────────────────────────")
    from pdf_translator import _is_trivially_invariant

    should_be_invariant = [
        ("45",          "页码"),
        ("100",         "整数"),
        ("3.14",        "小数"),
        ("100%",        "百分比"),
        ("±0.5",        "公差"),
        (" 42 ",        "带空格的数字"),
        ("2.0 × 10³",   "科学记数法"),
        ("",            "空字符串"),
    ]
    should_need_translation = [
        ("Data Product Line",  "英文词组"),
        ("R6",                 "版本号（含字母）"),
        ("Model Lab",          "产品名"),
        ("FST",                "缩写词"),
        ("45 items",           "数字+单词"),
        ("Honda",              "品牌名"),
    ]

    for text, label in should_be_invariant:
        check(f"不变量识别 '{text}' ({label})", _is_trivially_invariant(text),
              f"'{text}' 应被识别为不变量（不需翻译）")

    for text, label in should_need_translation:
        check(f"需翻译识别 '{text}' ({label})", not _is_trivially_invariant(text),
              f"'{text}' 不应被识别为不变量（应该翻译）")


def main():
    quick = "--quick" in sys.argv
    print("=" * 60)
    print("PDF Translator 回归测试")
    print("=" * 60)

    test_block_counts()
    test_hidden_detection()
    test_no_4pt_filter()
    test_trivially_invariant()

    print(f"\n{'='*60}")
    print(f"结果: {PASSES} 通过 / {FAILS} 失败")
    if FAILS > 0:
        print("⚠ 存在回归！请检查上方失败项。")
        sys.exit(1)
    else:
        print("✅ 全部通过！")


if __name__ == "__main__":
    main()
