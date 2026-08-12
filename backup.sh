#!/bin/bash
# backup.sh
# =========
# Backs up risk_state.json and trade_journal.csv with a timestamp, so a
# server crash or accidental deletion doesn't lose your trading history.
# Keeps the last 30 days of backups, prunes older ones automatically.
#
# Run manually anytime: bash backup.sh
# Or let cron run it automatically once a day (see setup instructions).

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_DIR="$SCRIPT_DIR/backups"
DATE=$(date +%Y-%m-%d_%H-%M-%S)
KEEP_DAYS=30

mkdir -p "$BACKUP_DIR"

if [ -f "$SCRIPT_DIR/risk_state.json" ]; then
    cp "$SCRIPT_DIR/risk_state.json" "$BACKUP_DIR/risk_state_${DATE}.json"
fi

if [ -f "$SCRIPT_DIR/trade_journal.csv" ]; then
    cp "$SCRIPT_DIR/trade_journal.csv" "$BACKUP_DIR/trade_journal_${DATE}.csv"
fi

echo "Backed up to $BACKUP_DIR (timestamp: $DATE)"

find "$BACKUP_DIR" -name "risk_state_*.json" -mtime +$KEEP_DAYS -delete 2>/dev/null || true
find "$BACKUP_DIR" -name "trade_journal_*.csv" -mtime +$KEEP_DAYS -delete 2>/dev/null || true

echo "Pruned backups older than $KEEP_DAYS days."
echo "Current backup count: $(ls "$BACKUP_DIR" | wc -l) files"
