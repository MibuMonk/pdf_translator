---
name: tune
description: Run self-supervised layout tuning on a PDF testcase. Use when the user wants to automatically improve layout quality via round-trip evaluation.
---

# Self-Supervised Layout Tuning

Runs the round-trip eval + auto-fix loop. Translates A→B→A, measures layout preservation, automatically fixes `layout_agent.py` and/or `translate_agent.py` based on defect categories, repeats until target score is reached.

## Steps

1. Identify from the user's message:
   - **PDF path** (required, e.g. `testdata/成果物4/source.pdf`)
   - **Mode**: `--identity` (API-free, layout-only) OR `--lang-b <lang>` (full roundtrip, e.g. `ja`, `zh`, `en`)
   - **Work dir** `--work-dir` (optional, default: `<pdf_parent>/work_rt_<lang-b>` or `work_rt_identity`)
   - **Target score** `--target-score` (optional, default: `0.90`)
   - **Max iterations** `--max-iters` (optional, default: `10`)

2. If PDF path is unclear, ask the user. If neither `--identity` nor `--lang-b` is specified, default to `--identity`.

3. Run in background:
   ```bash
   # Identity mode (no API, pure layout test):
   python3 scripts/roundtrip_tuner.py <pdf> --identity [--work-dir <dir>] --auto [--target-score X] [--max-iters N]

   # Full roundtrip mode:
   python3 scripts/roundtrip_tuner.py <pdf> --lang-b <lang> [--work-dir <dir>] --auto [--target-score X] [--max-iters N]
   ```

4. Set up a 20-minute cron check with CronCreate. Report to user when complete or when user asks for status.

## What the loop does

- **Iter 1**: Full round-trip eval (uses cached `rt_B.pdf` if it exists, skips pipeline if both PDFs cached). Categorizes orphan RT blocks as `untranslated` / `fragment` / `expansion`.
- **Iter 2+**: Layout-only re-run (skips parse/translate/space_planner, just re-renders with current `layout_agent.py`).
- **Auto-fix routing**: `untranslated` orphans → fix `translate_agent.py`; `fragment` orphans → fix `layout_agent.py`; `expansion` only → skip (no actionable fix).
- **Rollback**: if `claude` subprocess exits non-zero, the agent file is restored from backup automatically.

## Score formula

```
score = 1.0 - (
    0.3 * color_mismatch_pct / 100
    + 0.3 * line_overflow_pct / 100
    + 0.1 * min(abs(avg_font_delta) / 20, 1.0)
    + 0.3 * orphan_rt_rate        # orphan_rt / (matched + orphan_rt)
)
```

Orphan categories:
- `untranslated`: CJK text in output when target lang is non-CJK (translate_agent skipped blocks)
- `fragment`: ≤5 words and substring of matched content (layout_agent split a block)
- `expansion`: genuinely new content from translation (not fixable by layout/translate changes)

## Rules

- Always run in background. Do NOT block the main thread waiting.
- Set up 20-min cron check; tear it down after the loop finishes.
- Do NOT manually intervene in individual iterations.
- If loop exits without reaching target, report final diagnosis prompt to the user.
- Do NOT run `/tune` and `/translate` on the same testcase simultaneously.
