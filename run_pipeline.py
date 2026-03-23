#!/usr/bin/env python3
"""
PDF翻訳パイプライン オーケストレータ

使い方:
  python3 run_pipeline.py input.pdf --tgt ja
  python3 run_pipeline.py input.pdf --tgt ja --pages 1,3,5-8 --output out.pdf

処理フロー:
  parse_agent  → parsed.json
  translate_agent → translated.json  (キャッシュ再利用)
  layout_agent → <output>.pdf
  qa_agent     → qa_report.json
"""
import argparse, os, subprocess, sys, tempfile
from pathlib import Path

AGENTS = Path(__file__).parent

def run(cmd: list, label: str):
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        print(f"  ✗ {label} failed (exit {result.returncode})")
        sys.exit(result.returncode)

def main():
    ap = argparse.ArgumentParser(description="PDF翻訳パイプライン")
    ap.add_argument("input",                    help="入力 PDF")
    ap.add_argument("--tgt",    default="ja",   help="翻訳先言語 (デフォルト: ja)")
    ap.add_argument("--src",    default="en",   help="原文言語 (デフォルト: en)")
    ap.add_argument("--output", default=None,   help="出力 PDF (省略時: <stem>.ja.pdf)")
    ap.add_argument("--pages",  default=None,   help="ページ指定 (例: 1,3,5-8)")
    ap.add_argument("--font",   default=None,   help="CJK フォントパス")
    ap.add_argument("--cache",  default=None,   help="翻訳キャッシュ .json パス")
    ap.add_argument("--context",default=None,   help="術語・背景知識ファイル")
    ap.add_argument("--thumbs", default=None,   help="QA サムネイル出力ディレクトリ")
    ap.add_argument("--skip-qa",action="store_true", help="QA ステップをスキップ")
    ap.add_argument("--workdir",default=None,   help="中間ファイル保存先 (省略時: tempdir)")
    args = ap.parse_args()

    input_path = Path(args.input).resolve()
    stem = input_path.stem

    # 作業ディレクトリ
    if args.workdir:
        workdir = Path(args.workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        cleanup = False
    else:
        _tmp = tempfile.mkdtemp(prefix="pdf_pipeline_")
        workdir = Path(_tmp)
        cleanup = True

    parsed_json     = workdir / f"{stem}.parsed.json"
    translated_json = workdir / f"{stem}.translated.json"
    output_pdf      = Path(args.output) if args.output else input_path.with_name(f"{stem}.{args.tgt}.pdf")
    qa_report       = workdir / "qa_report.json"
    cache_path      = args.cache or str(input_path.with_name(f"{stem}.{args.tgt}.transcache.json"))

    print(f"入力: {input_path}")
    print(f"出力: {output_pdf}")
    print(f"作業: {workdir}")

    # ── 1. Parse ──────────────────────────────────────────────────────
    parse_cmd = [
        sys.executable,
        str(AGENTS / "pdf_translator_parse" / "parse_agent.py"),
        "--input",  str(input_path),
        "--output", str(parsed_json),
        "--src",    args.src,
        "--tgt",    args.tgt,
    ]
    if args.pages:
        parse_cmd += ["--pages", args.pages]
    run(parse_cmd, "Step 1/4: Parse")

    # ── 2. Translate ──────────────────────────────────────────────────
    trans_cmd = [
        sys.executable,
        str(AGENTS / "pdf_translator_translate" / "translate_agent.py"),
        "--input",  str(parsed_json),
        "--output", str(translated_json),
        "--cache",  cache_path,
        "--src",    args.src,
        "--tgt",    args.tgt,
    ]
    if args.context:
        trans_cmd += ["--context", args.context]
    run(trans_cmd, "Step 2/4: Translate")

    # ── 3. Layout ─────────────────────────────────────────────────────
    layout_cmd = [
        sys.executable,
        str(AGENTS / "pdf_translator_layout" / "layout_agent.py"),
        "--input",  str(input_path),
        "--json",   str(translated_json),
        "--output", str(output_pdf),
    ]
    if args.font:
        layout_cmd += ["--font", args.font]
    if args.pages:
        layout_cmd += ["--pages", args.pages]
    run(layout_cmd, "Step 3/4: Layout")

    # ── 4. QA ────────────────────────────────────────────────────────
    if not args.skip_qa:
        qa_cmd = [
            sys.executable,
            str(AGENTS / "pdf_translator_qa" / "qa_agent.py"),
            "--json",   str(translated_json),
            "--pdf",    str(output_pdf),
            "--output", str(qa_report),
        ]
        if args.thumbs:
            qa_cmd += ["--thumbs", args.thumbs]
        run(qa_cmd, "Step 4/4: QA")

    # ── 後片付け ─────────────────────────────────────────────────────
    if cleanup:
        import shutil
        shutil.rmtree(workdir, ignore_errors=True)

    print(f"\n✅ 完成！→ {output_pdf}")

if __name__ == "__main__":
    main()
