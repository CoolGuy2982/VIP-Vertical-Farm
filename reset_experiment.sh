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
