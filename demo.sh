#!/usr/bin/env bash
# Demo: run sentinelops on the sample K8s events + app log.
# No cluster, no API keys, no real Slack webhook needed.
set -euo pipefail
cd "$(dirname "$0")"

echo "=== sentinelops demo (heuristic mode, dry-run Slack) ==="
echo
python3 sentinelops.py detect sample_data/events.jsonl --logs sample_data/app.log
