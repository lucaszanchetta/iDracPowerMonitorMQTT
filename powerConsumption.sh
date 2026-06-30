#!/bin/bash

set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "usage: $0 <idrac-alias>" >&2
  exit 1
fi

exec /usr/bin/python3 /home/lucas/iDracPowerMonitorMQTT/powerMQTT.py \
  --host "$1" \
  --metric last_min_avg_watts \
  --plain
