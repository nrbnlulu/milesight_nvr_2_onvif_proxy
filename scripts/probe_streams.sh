#!/usr/bin/env bash
# ffprobe each RTSP path and print codec/resolution so main vs sub stream naming can be identified.
# Usage: scripts/probe_streams.sh <host:port> <user:pass> path...
set -u
target=${1:?host:port}
creds=${2:?user:pass}
shift 2
for path in "$@"; do
	info=$(timeout 15 ffprobe -v error -rtsp_transport tcp -select_streams v:0 \
		-show_entries stream=codec_name,width,height,avg_frame_rate -of csv=p=0 \
		"rtsp://$creds@$target/$path" 2>&1 | head -1)
	echo "$path -> ${info:-<no video>}"
done
