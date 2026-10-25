#!/usr/bin/env bash
cd "$(dirname "$0")/.."
sudo pkill -f 'exam_[s]erver.py|student_[p]ortal.py|admin_[p]ortal.py|app/[g]ateway.py|exam_[c]lient.py|[c]loudflared tunnel|[i]perf' 2>/dev/null || true
for br in $(sudo ovs-vsctl list-br 2>/dev/null); do sudo ovs-vsctl --if-exists del-br "$br"; done
for l in $(ip -o link show | awk -F': ' '{print $2}' | cut -d@ -f1 | grep -E '^(s[0-9]+-eth[0-9]+|root-eth[0-9]+)$'); do
  sudo ip link del "$l" 2>/dev/null || true
done
sudo pkill -f "mininet:" 2>/dev/null || true
rm -f logs/server_status.json logs/public_url.txt logs/console_url.txt 2>/dev/null
exit 0
