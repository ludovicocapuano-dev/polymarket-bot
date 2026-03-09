#!/bin/bash
# Daily AutoOptimizer + On-chain data refresh + Quant metrics
# Schedulato via crontab 2x/giorno (6:23 + 20:43)

cd /root/polymarket_toolkit
source .env 2>/dev/null

LOG="logs/auto_optimizer_daily.log"
echo "=== $(date) ===" >> "$LOG"

# 1. Refresh on-chain weather trade data
python3 scripts/refresh_onchain_trades.py >> "$LOG" 2>&1

# 2. Run AutoOptimizer — cattura output per Telegram
OPT_OUTPUT=$(timeout 120 python3 auto_optimizer.py --strategy weather --max-iter 200 2>&1)
echo "$OPT_OUTPUT" >> "$LOG"

# 3. Run Quant Metrics (PSR/DSR/binHR)
python3 scripts/run_quant_metrics.py >> "$LOG" 2>&1

echo "=== DONE $(date) ===" >> "$LOG"

# 4. Invia risultati su Telegram
if [ -n "$TELEGRAM_BOT_TOKEN" ] && [ -n "$TELEGRAM_CHAT_ID" ]; then
    # Estrai metriche chiave dall'output
    SCORE=$(echo "$OPT_OUTPUT" | grep -oP 'Score: \+[\d.]+' | head -1)
    WR=$(echo "$OPT_OUTPUT" | grep -oP 'WR: [\d.]+%' | tail -1)
    PNL=$(echo "$OPT_OUTPUT" | grep -oP 'PnL: \$\+?[-\d.]+' | tail -1)
    PF=$(echo "$OPT_OUTPUT" | grep -oP 'Profit Factor: [\d.]+' | tail -1)
    IMPROVEMENT=$(echo "$OPT_OUTPUT" | grep -oP 'Improvement: \+[\d.]+%' | head -1)
    BEST_PARAMS=$(echo "$OPT_OUTPUT" | grep -E '^\s+\w+:.*→' | head -5)
    BASELINE=$(echo "$OPT_OUTPUT" | grep -oP 'Baseline: WR=[\d.]+% PnL=\$\+?[-\d.]+ PF=[\d.]+' | head -1)
    CLOSED=$(echo "$OPT_OUTPUT" | grep -oP '\d+ closed' | head -1)

    # Emoji basato su improvement
    if echo "$OPT_OUTPUT" | grep -q "NEW BEST"; then
        EMOJI="📈"
        STATUS="MIGLIORAMENTO TROVATO"
    else
        EMOJI="📊"
        STATUS="Nessun miglioramento"
    fi

    # Controlla errori
    if echo "$OPT_OUTPUT" | grep -q "Not enough closed trades"; then
        MSG="⚠️ <b>AutoOptimizer</b>: dati insufficienti per ottimizzazione"
    else
        MSG="${EMOJI} <b>AutoOptimizer $(date +%H:%M)</b>

<b>${STATUS}</b>
${BASELINE:-}
${CLOSED:-}

<b>Best:</b>
${SCORE:-N/A} | ${WR:-N/A} | ${PNL:-N/A} | ${PF:-N/A}
${IMPROVEMENT:-}

${BEST_PARAMS:+<b>Parametri raccomandati:</b>
<code>${BEST_PARAMS}</code>}"
    fi

    curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d chat_id="$TELEGRAM_CHAT_ID" \
        -d parse_mode="HTML" \
        -d disable_web_page_preview=true \
        --data-urlencode "text=${MSG}" \
        >> "$LOG" 2>&1
fi
