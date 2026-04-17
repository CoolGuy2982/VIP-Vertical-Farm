#!/usr/bin/env bash
# reset_experiment.sh
# Clears all local experiment data so the AI grower starts fresh.
# Run from the project root: bash reset_experiment.sh

set -uo pipefail

echo "============================================"
echo "  VIP Vertical Farm — Reset Experiment Data"
echo "============================================"
echo ""
echo "This will delete all local logs and images."
read -r -p "Are you sure? (yes/no): " confirm
if [[ "$confirm" != "yes" ]]; then
    echo "Aborted."
    exit 0
fi

echo ""
echo "Syncing system clock..."
sudo timedatectl set-ntp true

# Try NTP sync via multiple servers
if sudo ntpdate -u pool.ntp.org 2>/dev/null; then
    echo "  Clock synced via pool.ntp.org"
elif sudo ntpdate -u time.google.com 2>/dev/null; then
    echo "  Clock synced via time.google.com"
elif sudo ntpdate -u time.cloudflare.com 2>/dev/null; then
    echo "  Clock synced via time.cloudflare.com"
else
    # NTP is likely blocked — get time from HTTP headers instead
    echo "  NTP blocked, falling back to HTTP time sync..."
    HTTP_DATE=""

    # Try wget
    if command -v wget &>/dev/null; then
        HTTP_DATE=$(wget -qS --spider --timeout=5 http://google.com 2>&1 | grep -i "Date:" | head -1 | sed 's/.*Date: //')
    fi

    # Try python3 as final fallback
    if [[ -z "$HTTP_DATE" ]] && command -v python3 &>/dev/null; then
        HTTP_DATE=$(python3 -c "
import urllib.request
try:
    r = urllib.request.urlopen('http://google.com', timeout=5)
    print(r.headers.get('Date', ''))
except: pass
" 2>/dev/null)
    fi

    if [[ -n "$HTTP_DATE" ]]; then
        PARSED=$(python3 -c "
from email.utils import parsedate_to_datetime
import sys
try:
    dt = parsedate_to_datetime('$HTTP_DATE')
    print(dt.strftime('%Y-%m-%d %H:%M:%S'))
except Exception as e:
    print('')
" 2>/dev/null)
        if [[ -n "$PARSED" ]]; then
            sudo timedatectl set-time "$PARSED" \
                && echo "  Clock set to: $PARSED" \
                || echo "  WARNING: Could not set clock. Set manually: sudo timedatectl set-time 'YYYY-MM-DD HH:MM:SS'"
        else
            echo "  WARNING: Could not parse HTTP date. Set manually: sudo timedatectl set-time 'YYYY-MM-DD HH:MM:SS'"
        fi
    else
        echo "  WARNING: All clock sync methods failed. Set manually: sudo timedatectl set-time 'YYYY-MM-DD HH:MM:SS'"
    fi
fi

echo ""
echo "Clearing log files..."
> data/logs/decisions.jsonl
> data/logs/sensor_log.jsonl
> data/logs/grower.log
rm -f data/logs/growth_summary.json
rm -f data/logs/milestones.jsonl
echo '[]' > data/logs/action_queue.json

echo "Clearing images..."
find data/images -name "*.jpg" -delete

echo ""
echo "Done. All local experiment data has been cleared."
echo "Remember to also clear Firestore and Firebase Storage via the console."
