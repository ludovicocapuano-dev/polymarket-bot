#!/bin/bash
# Daily AutoOptimizer v2.0 — Multi-strategy + auto-apply + Telegram
# Schedulato via crontab 2x/giorno (6:23 + 20:43)

cd /root/polymarket_toolkit
source .env 2>/dev/null

LOG="logs/auto_optimizer_daily.log"
echo "=== $(date) ===" >> "$LOG"

# 1. Refresh on-chain weather trade data
python3 scripts/refresh_onchain_trades.py >> "$LOG" 2>&1

# 2. Run AutoOptimizer — all strategies with auto-apply
OPT_OUTPUT=$(timeout 180 python3 auto_optimizer.py --strategy all --max-iter 200 --auto-apply 2>&1)
echo "$OPT_OUTPUT" >> "$LOG"

# 3. Run Quant Metrics (PSR/DSR/binHR)
python3 scripts/run_quant_metrics.py >> "$LOG" 2>&1

# 4. Hyperspace Sync — pubblica risultati e cerca scoperte peer
echo "--- Hyperspace Sync ---" >> "$LOG"
timeout 60 python3 scripts/hyperspace_bridge.py --strategy weather >> "$LOG" 2>&1

echo "=== DONE $(date) ===" >> "$LOG"

# 4. Invia risultati su Telegram
if [ -n "$TELEGRAM_BOT_TOKEN" ] && [ -n "$TELEGRAM_CHAT_ID" ]; then
    # Estrai metriche chiave dall'output
    SUMMARY=$(echo "$OPT_OUTPUT" | grep -A 20 "OPTIMIZATION SUMMARY" | head -20)
    APPLIED=$(echo "$OPT_OUTPUT" | grep "AUTO-APPLIED" | head -5)
    IMPROVEMENTS=$(echo "$OPT_OUTPUT" | grep "NEW BEST" | wc -l)

    # Per-strategy best results
    BEST_LINES=$(echo "$OPT_OUTPUT" | grep -E "^\s+(Score|WR|PnL|Profit Factor|Improvement):" | tail -15)

    # Count strategies
    N_STRATEGIES=$(echo "$OPT_OUTPUT" | grep -c "AutoOptimizer v2\.")

    # Build message
    if [ -n "$APPLIED" ]; then
        EMOJI="🚀"
        STATUS="PARAMETRI AUTO-APPLICATI"
    elif [ "$IMPROVEMENTS" -gt 0 ]; then
        EMOJI="📈"
        STATUS="${IMPROVEMENTS} miglioramenti trovati"
    else
        EMOJI="📊"
        STATUS="Nessun miglioramento"
    fi

    if echo "$OPT_OUTPUT" | grep -q "No trades found"; then
        MSG="⚠️ <b>AutoOptimizer</b>: nessun trade trovato"
    else
        # Closed trades count per strategy
        CLOSED_INFO=$(echo "$OPT_OUTPUT" | grep "Closed trades by strategy" -A 10 | grep "^\s" | head -5)

        MSG="${EMOJI} <b>AutoOptimizer v2.0 $(date +%H:%M)</b>

<b>${STATUS}</b>
Strategie: ${N_STRATEGIES}

<b>Trade chiusi:</b>
<code>${CLOSED_INFO}</code>

${APPLIED:+<b>Auto-applied:</b>
<code>${APPLIED}</code>
}
${SUMMARY:+<b>Summary:</b>
<code>${SUMMARY}</code>}"
    fi

    curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d chat_id="$TELEGRAM_CHAT_ID" \
        -d parse_mode="HTML" \
        -d disable_web_page_preview=true \
        --data-urlencode "text=${MSG}" \
        >> "$LOG" 2>&1
fi
