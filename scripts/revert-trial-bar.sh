#!/bin/bash
# Auto-revert for the 1/100 score-bar trial (user: "till tomorrow").
# Restores strict 50/100 bar + normal spam caps from .trial-bak backups,
# then restarts launch_scanner. Backups are removed after restore.
set -e
cd /home/hatch/workspace/Forex-agent/Forex-agent-main
for f in config/defaults.yaml config/config.py daemon/launch_scanner/__init__.py tests/test_multichain_reliability.py; do
  if [ -f "$f.trial-bak" ]; then
    cp "$f.trial-bak" "$f"
    rm "$f.trial-bak"
    echo "reverted $f"
  else
    echo "no backup for $f (skipped)"
  fi
done
scripts/forex-daemons restart launch_scanner
echo "trial reverted: strict 50/100 bar restored"
