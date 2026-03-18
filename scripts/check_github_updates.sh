#!/bin/bash
# v12.1: Monitor GitHub repos for updates
# Controlla nuovi commit sui repo che usiamo e notifica via Telegram
# Cron: 1x/giorno alle 08:00 UTC

cd /root/polymarket_toolkit
source .env 2>/dev/null

STATE_FILE="logs/github_last_check.json"
LOG="logs/github_updates.log"

echo "=== $(date) ===" >> "$LOG"

# Repos da monitorare
REPOS=(
    "karpathy/autoresearch"
    "hyperspaceai/agi"
    "greyhaven-ai/autocontext"
    "666ghj/MiroFish"
    "MiroMindAI/MiroThinker"
    "polymarket/polymarket-cli"
    "polymarket/py-clob-client"
    "polymarket/py-builder-signing-sdk"
)

# Carica stato precedente (ultimo SHA visto per ogni repo)
if [ ! -f "$STATE_FILE" ]; then
    echo '{}' > "$STATE_FILE"
fi

UPDATES=""
UPDATED_STATE=$(cat "$STATE_FILE")

for REPO in "${REPOS[@]}"; do
    # Fetch ultimo commit via GitHub API (no auth needed per public repos)
    RESPONSE=$(curl -sf "https://api.github.com/repos/${REPO}/commits?per_page=1" 2>/dev/null)

    if [ -z "$RESPONSE" ]; then
        echo "  [WARN] Impossibile fetchare $REPO" >> "$LOG"
        continue
    fi

    LATEST_SHA=$(echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d[0]['sha'][:12])" 2>/dev/null)
    LATEST_MSG=$(echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d[0]['commit']['message'].split(chr(10))[0][:80])" 2>/dev/null)
    LATEST_DATE=$(echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d[0]['commit']['committer']['date'][:10])" 2>/dev/null)

    # Confronta con ultimo SHA salvato
    PREV_SHA=$(echo "$UPDATED_STATE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('${REPO}',''))" 2>/dev/null)

    if [ -n "$LATEST_SHA" ] && [ "$LATEST_SHA" != "$PREV_SHA" ]; then
        if [ -n "$PREV_SHA" ]; then
            # Nuovo commit trovato!
            echo "  [UPDATE] $REPO: $LATEST_SHA ($LATEST_DATE) $LATEST_MSG" >> "$LOG"
            UPDATES="${UPDATES}\n- <b>${REPO}</b>: ${LATEST_MSG} (${LATEST_DATE})"
        else
            echo "  [INIT] $REPO: $LATEST_SHA ($LATEST_DATE)" >> "$LOG"
        fi
        # Aggiorna stato
        UPDATED_STATE=$(echo "$UPDATED_STATE" | python3 -c "
import sys, json
d = json.load(sys.stdin)
d['${REPO}'] = '${LATEST_SHA}'
json.dump(d, sys.stdout)
" 2>/dev/null)
    else
        echo "  [OK] $REPO: nessun aggiornamento" >> "$LOG"
    fi
done

# Salva stato aggiornato
echo "$UPDATED_STATE" | python3 -m json.tool > "$STATE_FILE" 2>/dev/null

# Notifica Telegram se ci sono aggiornamenti
if [ -n "$UPDATES" ] && [ -n "$TELEGRAM_BOT_TOKEN" ] && [ -n "$TELEGRAM_CHAT_ID" ]; then
    MSG=$(printf "🔔 <b>GitHub Updates</b>\n${UPDATES}")

    curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d chat_id="$TELEGRAM_CHAT_ID" \
        -d parse_mode="HTML" \
        -d disable_web_page_preview=true \
        --data-urlencode "text=${MSG}" \
        >> "$LOG" 2>&1
fi

echo "=== DONE $(date) ===" >> "$LOG"
