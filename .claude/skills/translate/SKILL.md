---
name: translate
description: Translate a PDF file while preserving layout. Use when the user wants to translate a PDF to another language.
---

# Quick Translate

Run the translation pipeline and return the result. Do not iterate or dig into internals.

## Steps

1. Identify from the user's message:
   - **Input PDF path** (required)
   - **Target language** `--tgt` (required, e.g. zh, ja, en)
   - **Source language** `--src` (optional, default: auto-detect from context)
   - **Page range** `--pages` (optional, e.g. `1,3,5-8`)
   - **Context file** `--context` (optional, domain terminology)
   - **Font** `--font` (optional)

2. If input PDF path or target language is unclear, ask the user.

3. Run the pipeline:
   ```bash
   python3 run_pipeline.py <input.pdf> --tgt <lang> --src <lang> --skip-qa [options]
   ```

   > `/translate` always uses `--skip-qa` for speed. For full QA, use `/refine`.

4. Return the output PDF path to the user. Output is at `{stem}.{tgt}.pdf` next to the input file.

## Rules

- Do NOT inspect intermediate files (parsed.json, translated.json, etc.)
- Do NOT run test_agent separately or enter any iterative loop
- Do NOT modify any code
- If the pipeline fails, report the error to the user as-is
