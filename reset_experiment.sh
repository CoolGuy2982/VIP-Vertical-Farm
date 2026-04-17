#!/usr/bin/env bash
# reset_experiment.sh
# Clears all local experiment data so the AI grower starts fresh.
# Run from the project root: bash reset_experiment.sh

set -euo pipefail

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
    # NTP is likely blocked on this network — set time from the internet via HTTP
    echo "  NTP blocked, falling back to HTTP time sync..."
    HTTP_DATE=$(curl -sI --max-time 5 http://google.com | grep -i "^date:" | sed 's/[Dd]ate: //')
    if [[ -n "$HTTP_DATE" ]]; then
        sudo timedatectl set-time "$(date -d "$HTTP_DATE" '+%Y-%m-%d %H:%M:%S')" 2>/dev/null \
            && echo "  Clock set from HTTP headers: $HTTP_DATE" \
            || echo "  WARNING: Could not set clock from HTTP — set it manually with: sudo timedatectl set-time 'YYYY-MM-DD HH:MM:SS'"
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
