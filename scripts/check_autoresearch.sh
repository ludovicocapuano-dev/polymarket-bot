#!/bin/bash
# Check karpathy/autoresearch for relevant updates
# Looks for commits related to: optimization loop, scoring, parameter search,
# evaluation metrics, experiment logging

cd /root/polymarket_toolkit
LOG="logs/autoresearch_updates.log"
STATE_FILE="logs/.autoresearch_last_sha"

# Get latest commit SHA
LATEST_SHA=$(gh api repos/karpathy/autoresearch/commits --jq '.[0].sha' 2>/dev/null)
if [ -z "$LATEST_SHA" ]; then
    echo "[AUTORESEARCH] Failed to fetch commits" >> "$LOG"
    exit 1
fi

# Check if we've seen this already
LAST_SHA=""
if [ -f "$STATE_FILE" ]; then
    LAST_SHA=$(cat "$STATE_FILE")
fi

if [ "$LATEST_SHA" = "$LAST_SHA" ]; then
    exit 0  # no new commits
fi

# Get new commits since last check (max 20)
echo "=== $(date) ===" >> "$LOG"

if [ -n "$LAST_SHA" ]; then
    COMMITS=$(gh api "repos/karpathy/autoresearch/compare/${LAST_SHA}...HEAD" \
        --jq '.commits[] | "\(.sha[0:8]) \(.commit.message | split("\n")[0])"' 2>/dev/null)
else
    COMMITS=$(gh api repos/karpathy/autoresearch/commits \
        --jq '.[0:10] | .[] | "\(.sha[0:8]) \(.commit.message | split("\n")[0])"' 2>/dev/null)
fi

# Filter for relevant keywords
RELEVANT_KW="optim|score|metric|eval|experiment|parameter|search|loop|train\.py|program\.md|backtest|sharpe|kelly|loss|improv"
RELEVANT=$(echo "$COMMITS" | grep -iE "$RELEVANT_KW" || true)

if [ -n "$RELEVANT" ]; then
    echo "[AUTORESEARCH] Relevant updates found:" >> "$LOG"
    echo "$RELEVANT" >> "$LOG"

    # Fetch the actual changed files for relevant commits
    for SHA in $(echo "$RELEVANT" | awk '{print $1}'); do
        FILES=$(gh api "repos/karpathy/autoresearch/commits/$SHA" \
            --jq '.files[].filename' 2>/dev/null | head -5)
        echo "  Files: $FILES" >> "$LOG"
    done

    echo "" >> "$LOG"
    echo "[AUTORESEARCH] Review these for AutoOptimizer improvements." >> "$LOG"
else
    echo "[AUTORESEARCH] ${#COMMITS} new commits, none relevant to optimizer" >> "$LOG"
fi

# Save state
echo "$LATEST_SHA" > "$STATE_FILE"
