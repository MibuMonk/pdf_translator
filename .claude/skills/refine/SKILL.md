---
name: refine
description: Refine translation/layout results or develop the pipeline tool itself. Use when the user reports quality issues with output or wants to improve the pipeline code.
---

# Refine & Develop

This skill covers two scenarios:

## Result Refinement

User reports a specific quality issue (e.g. "page 3 layout is broken", "translation on page 5 is wrong").

Follow the Defect Response Protocol in CLAUDE.md:

1. **DEFINE** — Classify the defect using `docs/defect_taxonomy.md` codes (L1-L6, T1-T3)
2. **TEST** — Ensure test_agent can detect this defect. If not, fix test_agent first
3. **DIAGNOSE** — Read intermediate files (parsed.json, translated.json, layout_plan.json) to locate root cause
4. **FIX** — Implement the fix in the appropriate agent
5. **VERIFY** — Run `scripts/verify.sh <testcase> [pages]` and present results

## Tool Development

Developer wants to improve pipeline code, add features, or refactor agents.

Follow the coordinator role defined in CLAUDE.md:
- Define I/O contracts and requirements, delegate implementation to agents
- Do NOT directly edit agent implementation code — dispatch working agents
- All new I/O contracts must be defined before implementation
- Working agents own their internals; only output must meet the contract

## Key Resources

- Pipeline: `run_pipeline.py`
- Agents: `agents/`
- Contracts/schemas: `contracts/`
- Test data: `testdata/`
- Defect taxonomy: `docs/defect_taxonomy.md`
- Verify script: `scripts/verify.sh`
