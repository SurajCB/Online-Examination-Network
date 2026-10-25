#!/usr/bin/env bash
cd "$(dirname "$0")/.."
./scripts/cleanup_net.sh
sudo python3 topology/exam_topo.py --demo --slow-last "$@"
