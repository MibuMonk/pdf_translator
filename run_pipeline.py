#!/usr/bin/env python3
"""
PDF翻訳パイプライン オーケストレータ

使い方:
  python3 run_pipeline.py input.pdf --tgt ja
  python3 run_pipeline.py input.pdf --tgt ja --pages 1,3,5-8 --output out.pdf

処理フロー:
  1. parse_agent      → parsed.json
  2. architect_agent  → plan.json          (analyzes doc, designs workflow)
  3a. translate_agent → translated.json    ┐ parallel
  3b. space_planner   → layout_plan.json   ┘ (per architect plan)
  4. layout_agent     → <output>.pdf       (render: uses translated + layout_plan)
  5. qa_agent         → qa_report.json
"""
import argparse
import json
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

AGENTS = Path(__file__).parent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(cmd: list, label: str) -> None:
    """Run a subprocess step; exit on failure."""
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        print(f"  ✗ {label} failed (exit {result.returncode})")
        sys.exit(result.returncode)


def run_parallel(tasks: list) -> None:
    """
    Run multiple (cmd, label) tasks in parallel threads.
    Waits for all to complete; exits if any fails.
    """
    errors = []

    def _worker(cmd, label):
        print(f"\n{'='*60}")
        print(f"  {label}  [parallel]")
        print(f"{'='*60}")
        result = subprocess.run(cmd, check=False)
        if result.returncode != 0:
            errors.append(f"✗ {label} failed (exit {result.returncode})")

    threads = [threading.Thread(target=_worker, args=(cmd, label)) for cmd, label in tasks]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    if errors:
        for e in errors:
            print(e)
        sys.exit(1)


def load_plan(plan_path: Path) -> dict:
    """Load architect plan.json; return empty dict if missing."""
    if plan_path.exists():
        with open(plan_path, encoding="utf-8") as f:
            return json.load(f)
    return {}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="PDF翻訳パイプライン（architect駆動）")
    ap.add_argument("input",                       help="入力 PDF")
    ap.add_argument("--tgt",     default="ja",     help="翻訳先言語 (デフォルト: ja)")
    ap.add_argument("--src",     default="en",     help="原文言語 (デフォルト: en)")
    ap.add_argument("--output",  default=None,     help="出力 PDF")
    ap.add_argument("--pages",   default=None,     help="ページ指定 (例: 1,3,5-8)")
    ap.add_argument("--font",    default=None,     help="CJK フォントパス")
    ap.add_argument("--cache",   default=None,     help="翻訳キャッシュ .json パス")
    ap.add_argument("--context", default=None,     help="手動術語・背景知識ファイル (architect生成に優先)")
    ap.add_argument("--thumbs",  default=None,     help="QA サムネイル出力ディレクトリ")
    ap.add_argument("--skip-qa", action="store_true", help="QA ステップをスキップ")
    ap.add_argument("--skip-architect", action="store_true", help="Architect analysisをスキップ")
    ap.add_argument("--workdir", default=None,     help="中間ファイル保存先 (省略時: tempdir)")
    args = ap.parse_args()

    input_path = Path(args.input).resolve()
    stem = input_path.stem

    # Work directory
    if args.workdir:
        workdir = Path(args.workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        cleanup = False
    else:
        workdir = Path(tempfile.mkdtemp(prefix="pdf_pipeline_"))
        cleanup = True

    parsed_json     = workdir / f"{stem}.parsed.json"
    plan_json       = workdir / "plan.json"
    translated_json = workdir / f"{stem}.translated.json"
    layout_plan_json = workdir / "layout_plan.json"
    output_pdf      = (
        Path(args.output) if args.output
        else input_path.with_name(f"{stem}.{args.tgt}.pdf")
    )
    qa_report       = workdir / "qa_report.json"
    cache_path      = (
        args.cache
        or str(input_path.with_name(f"{stem}.{args.tgt}.transcache.json"))
    )

    print(f"入力: {input_path}")
    print(f"出力: {output_pdf}")
    print(f"作業: {workdir}")

    # ── Step 1: Parse ────────────────────────────────────────────────────
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
    run(parse_cmd, "Step 1/5: Parse")

    # ── Step 1b: Consolidate ─────────────────────────────────────────────
    consolidate_cmd = [
        sys.executable,
        str(AGENTS / "pdf_translator_parse" / "consolidator.py"),
        "--input",  str(parsed_json),
        "--output", str(parsed_json),   # overwrite in-place (same schema)
    ]
    run(consolidate_cmd, "Step 1b/5: Consolidate")

    # ── Step 2: Intel (document analysis) ────────────────────────────────
    if not args.skip_architect:
        arch_cmd = [
            sys.executable,
            str(AGENTS / "pdf_translator_architect" / "intel_agent.py"),
            "--input",  str(parsed_json),
            "--output", str(plan_json),
            "--src",    args.src,
            "--tgt",    args.tgt,
        ]
        run(arch_cmd, "Step 2/5: Architect")
    else:
        print("\n[skipped] Step 2/5: Architect")

    # Load architect plan (may be empty if skipped)
    plan = load_plan(plan_json)
    workflow = plan.get("workflow", {})
    batch_size = workflow.get("recommended_batch_size", 40)
    space_plan_needed = workflow.get("space_plan_needed", False)

    # Determine context: manual flag overrides architect-generated context
    context_file: str | None = args.context
    generated_context = plan.get("translation_context", "")
    if not context_file and generated_context:
        # Write architect-generated context to a temp file for translate_agent
        context_file = str(workdir / "architect_context.txt")
        Path(context_file).write_text(generated_context, encoding="utf-8")
        print(f"[architect] Using generated context ({len(generated_context)} chars)")

    # ── Step 3: Translate ∥ Space Plan ───────────────────────────────────
    trans_cmd = [
        sys.executable,
        str(AGENTS / "pdf_translator_translate" / "translate_agent.py"),
        "--input",  str(parsed_json),
        "--output", str(translated_json),
        "--cache",  cache_path,
        "--src",    args.src,
        "--tgt",    args.tgt,
        "--batch",  str(batch_size),
    ]
    if context_file:
        trans_cmd += ["--context", context_file]

    if space_plan_needed:
        space_cmd = [
            sys.executable,
            str(AGENTS / "pdf_translator_layout" / "space_planner.py"),
            "--input",   str(input_path),
            "--parsed",  str(parsed_json),
            "--output",  str(layout_plan_json),
        ]
        if args.pages:
            space_cmd += ["--pages", args.pages]

        run_parallel([
            (trans_cmd, "Step 3a/5: Translate"),
            (space_cmd, "Step 3b/5: Space Plan"),
        ])
    else:
        run(trans_cmd, "Step 3/5: Translate")

    # ── Step 4: Render (Layout) ───────────────────────────────────────────
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
    if layout_plan_json.exists():
        layout_cmd += ["--plan", str(layout_plan_json)]
    run(layout_cmd, "Step 4/5: Render")

    # ── Step 5: QA ───────────────────────────────────────────────────────
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
        run(qa_cmd, "Step 5/5: QA")

    # Cleanup
    if cleanup:
        import shutil
        shutil.rmtree(workdir, ignore_errors=True)

    print(f"\n✅ 完成！→ {output_pdf}")


if __name__ == "__main__":
    main()
