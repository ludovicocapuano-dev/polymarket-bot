#!/bin/bash
# Daily AutoOptimizer + On-chain data refresh + Quant metrics
# Schedulato via crontab alle 6:23 AM

cd /root/polymarket_toolkit
source .env 2>/dev/null

LOG="logs/auto_optimizer_daily.log"
echo "=== $(date) ===" >> "$LOG"

# 1. Refresh on-chain weather trade data
python3 scripts/refresh_onchain_trades.py >> "$LOG" 2>&1

# 2. Run AutoOptimizer
python3 auto_optimizer.py --strategy weather --max-iter 200 >> "$LOG" 2>&1

# 3. Run Quant Metrics (PSR/DSR/binHR)
python3 scripts/run_quant_metrics.py >> "$LOG" 2>&1

echo "=== DONE $(date) ===" >> "$LOG"
