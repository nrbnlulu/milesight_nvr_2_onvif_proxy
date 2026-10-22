#!/usr/bin/env bash
# Probe candidate Milesight NVR RTSP paths with a DESCRIBE and print the status line.
# Usage: scripts/probe_rtsp.sh <host:port> <user:pass> [path...]
set -u
target=${1:?host:port}
creds=${2:?user:pass}
shift 2
paths=("$@")
if [ ${#paths[@]} -eq 0 ]; then
	paths=(ch_1 ch_101 ch_102 ch_001 ch_1_1 ch_01 main "ch_1?stream=1")
fi
for path in "${paths[@]}"; do
	status=$(curl -s -m 6 -u "$creds" -X DESCRIBE "rtsp://$target/$path" -i 2>&1 | head -1)
	echo "$path -> ${status:-<no response>}"
done
