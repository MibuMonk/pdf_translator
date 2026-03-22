#!/usr/bin/env python3
"""
翻译校阅工具 - 好朋友视角

用法:
  uv run python3 review_translation.py 原文.pdf 译文.pdf
  uv run python3 review_translation.py 原文.pdf 译文.pdf --output report.md
  uv run python3 review_translation.py 原文.pdf 译文.pdf --pages 1,2,5-8

校阅重点（不吹毛求疵）:
  1. 遗漏检测 - 原文有、译文没有的主要内容块
  2. 翻译大问题 - 明显错译、乱码、未翻译保留的大段原文
  3. 视觉大问题 - 文字溢出/截断、空白页、排版严重错乱
"""

import sys
import os
import re
import json
import base64
import subprocess
import shutil
import argparse
from typing import Optional

import fitz  # PyMuPDF

CLAUDE_CLI = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")

# 每批一次性送给 Claude 审阅几页（原文+译文各一张图 + 文字）
BATCH_SIZE = 2


def render_page_b64(page: fitz.Page, scale: float = 1.5) -> str:
    """渲染页面为 base64 PNG（用于发给 Claude 做视觉审查）。"""
    mat = fitz.Matrix(scale, scale)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    return base64.b64encode(pix.tobytes("png")).decode()


def extract_text(page: fitz.Page) -> str:
    """提取页面纯文字（保留换行，去掉多余空行）。"""
    text = page.get_text("text")
    # 压缩连续空行为单个空行
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_page_ranges(spec: str, total: int) -> list[int]:
    """解析页码规格，如 '1,3,5-8'，返回 0-indexed 列表。"""
    result = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            result.extend(range(int(a) - 1, int(b)))
        else:
            result.append(int(part) - 1)
    return [p for p in result if 0 <= p < total]


def call_claude_review(orig_texts: list[str], tran_texts: list[str],
                       orig_b64s: list[str], tran_b64s: list[str],
                       page_nums: list[int]) -> str:
    """调用 Claude CLI 对一批页面进行校阅，返回文字报告。"""

    pages_label = ", ".join(str(p + 1) for p in page_nums)

    # 构建文字部分
    text_blocks = []
    for i, (orig, tran, pn) in enumerate(zip(orig_texts, tran_texts, page_nums)):
        text_blocks.append(
            f"=== 第 {pn+1} 页 ===\n"
            f"[原文]\n{orig or '(无文字内容)'}\n\n"
            f"[译文]\n{tran or '(无文字内容)'}"
        )
    texts_section = "\n\n".join(text_blocks)

    # 构建图片部分 —— 使用 Claude 的多模态能力
    # 但 claude CLI (-p) 不支持直接传图片，所以图片作为 base64 嵌入 prompt 是不可行的。
    # 退而求其次：只用文字进行审阅。
    # （如果将来 CLI 支持图片输入，可以在这里添加）

    prompt = f"""你是一位专业的演示文稿翻译校阅员（"好朋友"角色）。
请对以下幻灯片页面的原文和译文进行校阅。

校阅重点（不要吹毛求疵，只关注真正的问题）：
1. **遗漏**：原文有的重要内容块，译文是否也有？有没有整段/整块内容完全没翻译？
2. **大翻译问题**：有没有明显的错译、术语完全用错、语句完全无法理解的情况？
3. **疑似视觉问题**：译文中有没有超长的单行文字（可能溢出）、大段空白、或内容明显少于原文（可能有文字截断）？

输出格式：
- 如果某页**没有问题**，直接写 "第X页：✅ 无问题"
- 如果有问题，写 "第X页：⚠ [问题类型] 描述"
- 最后给一个总结（1-2句话）
- 不要对翻译风格、用词选择等细节做评论

待校阅页面：第 {pages_label} 页

{texts_section}
"""

    try:
        result = subprocess.run(
            [CLAUDE_CLI, "-p", prompt],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            return f"[Claude 调用失败: {result.stderr[:200]}]"
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        return "[审阅超时，跳过]"
    except Exception as e:
        return f"[错误: {e}]"


def review_pdf(orig_path: str, tran_path: str,
               output_path: Optional[str] = None,
               page_spec: Optional[str] = None):
    if not os.path.exists(orig_path):
        print(f"错误: 找不到原文件 {orig_path}")
        sys.exit(1)
    if not os.path.exists(tran_path):
        print(f"错误: 找不到译文件 {tran_path}")
        sys.exit(1)

    orig_pdf = fitz.open(orig_path)
    tran_pdf = fitz.open(tran_path)

    orig_n = len(orig_pdf)
    tran_n = len(tran_pdf)

    print(f"原文: {os.path.basename(orig_path)}（{orig_n} 页）")
    print(f"译文: {os.path.basename(tran_path)}（{tran_n} 页）")

    if orig_n != tran_n:
        print(f"⚠ 页数不一致！原文 {orig_n} 页，译文 {tran_n} 页。")

    # 确定要审阅的页面
    total = min(orig_n, tran_n)
    if page_spec:
        pages = parse_page_ranges(page_spec, total)
    else:
        pages = list(range(total))

    print(f"共审阅 {len(pages)} 页，每批 {BATCH_SIZE} 页...\n")

    report_lines = [
        f"# 翻译校阅报告",
        f"",
        f"- **原文**: {os.path.basename(orig_path)}（{orig_n} 页）",
        f"- **译文**: {os.path.basename(tran_path)}（{tran_n} 页）",
        f"- **审阅页数**: {len(pages)} 页",
        f"",
    ]

    if orig_n != tran_n:
        report_lines.append(f"> ⚠ **页数不一致**：原文 {orig_n} 页，译文 {tran_n} 页。\n")

    # 分批处理
    for batch_start in range(0, len(pages), BATCH_SIZE):
        batch_pages = pages[batch_start: batch_start + BATCH_SIZE]
        batch_label = ", ".join(str(p + 1) for p in batch_pages)
        print(f"审阅第 {batch_label} 页...", end=" ", flush=True)

        orig_texts = [extract_text(orig_pdf[p]) for p in batch_pages]
        tran_texts = [extract_text(tran_pdf[p]) for p in batch_pages]
        orig_b64s  = [render_page_b64(orig_pdf[p]) for p in batch_pages]
        tran_b64s  = [render_page_b64(tran_pdf[p]) for p in batch_pages]

        review = call_claude_review(orig_texts, tran_texts, orig_b64s, tran_b64s, batch_pages)
        print("完成")

        report_lines.append(f"## 第 {batch_label} 页")
        report_lines.append(review)
        report_lines.append("")

    # 输出报告
    report = "\n".join(report_lines)

    if output_path:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"\n报告已保存至: {output_path}")
    else:
        print("\n" + "=" * 60)
        print(report)

    orig_pdf.close()
    tran_pdf.close()


def main():
    parser = argparse.ArgumentParser(
        description="翻译校阅工具 - 以人类读者视角对比原文和译文",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("original", help="原文 PDF 路径")
    parser.add_argument("translated", help="译文 PDF 路径")
    parser.add_argument("--output", "-o", help="报告输出路径（.md），不指定则打印到终端")
    parser.add_argument("--pages", "-p", help="只审阅指定页面，如 '1,3,5-8'")
    args = parser.parse_args()

    if not os.path.exists(CLAUDE_CLI):
        print(f"错误: 找不到 claude CLI（{CLAUDE_CLI}）")
        sys.exit(1)

    review_pdf(args.original, args.translated, args.output, args.pages)


if __name__ == "__main__":
    main()
