#!/bin/bash

# =============================================
#   Highrise Bot - Keep Alive Forever Script
# =============================================
# شغّل السكريبت ده على السيرفر وهيفضل يشتغل دايماً
# حتى لو البوت وقع هيرجع يشتغل تلقائياً
# =============================================
# طريقة التشغيل:
#   chmod +x run_forever.sh
#   ./run_forever.sh
#
# للتشغيل في الخلفية (بدون ما تقفل الترمينال):
#   nohup ./run_forever.sh > bot_output.log 2>&1 &
# =============================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

RESTART_DELAY=5
MAX_RESTART_DELAY=60

current_delay=$RESTART_DELAY

echo "======================================"
echo "  HIGHRISE BOT - KEEP ALIVE STARTED"
echo "  $(date)"
echo "======================================"

while true; do
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting bot..."
    
    python main.py
    
    EXIT_CODE=$?
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Bot stopped with exit code: $EXIT_CODE"
    
    if [ $EXIT_CODE -eq 0 ]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] Bot exited cleanly. Restarting in ${current_delay}s..."
    else
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] Bot crashed! Restarting in ${current_delay}s..."
    fi
    
    sleep $current_delay
    
    if [ $current_delay -lt $MAX_RESTART_DELAY ]; then
        current_delay=$((current_delay * 2))
        if [ $current_delay -gt $MAX_RESTART_DELAY ]; then
            current_delay=$MAX_RESTART_DELAY
        fi
    else
        current_delay=$RESTART_DELAY
    fi
done
