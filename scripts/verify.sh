#!/bin/bash
# Usage: scripts/verify.sh <testcase> [pages] [--open]
#   scripts/verify.sh 成果物1              # full run
#   scripts/verify.sh 成果物1 16-21        # specific pages
#   scripts/verify.sh 成果物1 16-21 --open # also open PDF
#
# Runs pipeline → exports problem pages as PNG → prints test summary.

set -euo pipefail

TESTCASE="${1:?Usage: verify.sh <testcase> [pages] [--open]}"
PAGES="${2:-}"
OPEN_PDF=false

# Check for --open flag
for arg in "$@"; do
  if [[ "$arg" == "--open" ]]; then
    OPEN_PDF=true
  fi
done

TESTDIR="testdata/${TESTCASE}"
SOURCE="${TESTDIR}/source.pdf"
OUTDIR="/tmp/verify_${TESTCASE}_$(date +%s)"

if [[ ! -f "$SOURCE" ]]; then
  echo "ERROR: ${SOURCE} not found"
  exit 1
fi

# Build pipeline args
PIPE_ARGS=("$SOURCE" --tgt zh)
if [[ -n "$PAGES" && "$PAGES" != "--open" ]]; then
  PIPE_ARGS+=(--pages "$PAGES")
fi

echo "=== Running pipeline ==="
python3 run_pipeline.py "${PIPE_ARGS[@]}" 2>&1 | grep -E '^\[|^  [x✓]|Summary:|Page confidence:|Review needed:|FAIL|PASS|ERROR|Saved:'

OUTPUT_PDF="${TESTDIR}/source.zh.pdf"

if [[ ! -f "$OUTPUT_PDF" ]]; then
  echo "ERROR: Output PDF not generated"
  exit 1
fi

# Export PNGs for review-needed pages
echo ""
echo "=== Exporting PNGs ==="
mkdir -p "$OUTDIR"

# Extract review-needed pages from pipeline output, or use specified pages
if [[ -n "$PAGES" && "$PAGES" != "--open" ]]; then
  # Convert page range to pdftoppm args
  FIRST=$(echo "$PAGES" | sed 's/[,-].*//')
  LAST=$(echo "$PAGES" | sed 's/.*[,-]//')
  pdftoppm -f "$FIRST" -l "$LAST" -png -r 150 "$OUTPUT_PDF" "$OUTDIR/out"
  pdftoppm -f "$FIRST" -l "$LAST" -png -r 150 "$SOURCE" "$OUTDIR/src"
  echo "Exported pages ${FIRST}-${LAST} to ${OUTDIR}/"
else
  echo "Full run — export skipped (too many pages). Use pages arg to export specific pages."
fi

if $OPEN_PDF; then
  open "$OUTPUT_PDF"
fi

echo ""
echo "=== Done ==="
echo "Output PDF: ${OUTPUT_PDF}"
echo "PNGs: ${OUTDIR}/"
