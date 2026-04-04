#!/bin/bash
# Daily AutoOptimizer v2.0 — Multi-strategy + auto-apply + Telegram
# Schedulato via crontab 2x/giorno (6:23 + 20:43)

cd /root/polymarket_toolkit
source .env 2>/dev/null

LOG="logs/auto_optimizer_daily.log"
echo "=== $(date) ===" >> "$LOG"

# 1. Refresh on-chain weather trade data
python3 scripts/refresh_onchain_trades.py >> "$LOG" 2>&1

# 2. Meta-evolve scoring genome (1x/day, before optimization)
HOUR=$(date +%H)
if [ "$HOUR" = "00" ] || [ "$HOUR" = "06" ]; then
    echo "--- Meta-Optimizer: evolving scoring genome ---" >> "$LOG"
    timeout 120 python3 auto_optimizer.py --strategy all --meta-evolve --max-iter 0 >> "$LOG" 2>&1
fi

# 3. Run AutoOptimizer — all strategies with auto-apply
OPT_OUTPUT=$(timeout 180 python3 auto_optimizer.py --strategy all --max-iter 200 --auto-apply 2>&1)
echo "$OPT_OUTPUT" >> "$LOG"

# 3. Run Quant Metrics (PSR/DSR/binHR)
python3 scripts/run_quant_metrics.py >> "$LOG" 2>&1

# 4. Hyperspace Sync — pubblica risultati e cerca scoperte peer
echo "--- Hyperspace Sync ---" >> "$LOG"
timeout 60 python3 scripts/hyperspace_bridge.py --strategy weather >> "$LOG" 2>&1

# 5. Crowd Sport Scan — Delphi multi-agent prediction (top 5 markets)
echo "--- Crowd Sport Delphi Scan ---" >> "$LOG"
timeout 600 python3 scripts/mirofish_sport_bridge.py --limit 5 >> "$LOG" 2>&1

# 6. XGBoost model retrain (weekly — checks age internally)
echo "--- XGBoost Retrain Check ---" >> "$LOG"
timeout 300 python3 -c "
from strategies.xgboost_predictor import XGBoostPredictor, collect_training_data, FeatureExtractor, MIN_TRAINING_SAMPLES
import numpy as np
predictor = XGBoostPredictor()
if predictor.needs_retrain:
    print('Retraining XGBoost model...')
    fe = FeatureExtractor()
    features, labels = collect_training_data(feature_extractor=fe)
    if len(features) >= MIN_TRAINING_SAMPLES:
        predictor.train(features, labels)
        print(f'Retrained: test_acc={predictor.test_accuracy:.1%}, n={len(labels)}')
    else:
        print(f'Insufficient data: {len(features)} samples (need {MIN_TRAINING_SAMPLES})')
else:
    age_days = (__import__('time').time() - predictor.last_train_time) / 86400
    print(f'Model fresh ({age_days:.1f}d old) — skip retrain')
" >> "$LOG" 2>&1

# 7. AutoEvolve — autonomous code evolution (1x/day, subtraction-biased, all strategies)
echo "--- AutoEvolve: code evolution loop (all strategies) ---" >> "$LOG"
EVOLVE_OUTPUT=$(timeout 300 python3 scripts/auto_evolve.py --target all --auto-apply 2>&1)
echo "$EVOLVE_OUTPUT" >> "$LOG"
EVOLVE_STATUS=$(echo "$EVOLVE_OUTPUT" | grep -oP "Run #\d+ COMPLETE — \K\w+" || echo "SKIPPED")
echo "  AutoEvolve status: $EVOLVE_STATUS" >> "$LOG"

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
