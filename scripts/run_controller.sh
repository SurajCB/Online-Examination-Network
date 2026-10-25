#!/usr/bin/env bash
cd "$(dirname "$0")/.."
source ~/ryu-venv/bin/activate 2>/dev/null || true
export EXAM_PRIORITIZE=${EXAM_PRIORITIZE:-1}
export EXAM_CONGESTION_MBPS=${EXAM_CONGESTION_MBPS:-5}
export EXAM_BLOCK_THRESHOLD=${EXAM_BLOCK_THRESHOLD:-3}
ryu-manager --ofp-tcp-listen-port 6653 --wsapi-port 8080 controller/exam_controller.py "$@"
