#!/usr/bin/env bash
cd "$(dirname "$0")/.."
if ! command -v cloudflared >/dev/null; then
  echo "cloudflared is not installed inside Ubuntu. See the setup guide (Part F)."
  exit 1
fi
mkdir -p logs
rm -f logs/public_url.txt logs/console_url.txt
: > logs/tunnel_student.log
: > logs/tunnel_console.log
python3 app/gateway.py --http-port 7000 > logs/gateway.log 2>&1 &
GW=$!
cloudflared tunnel --no-autoupdate --url http://localhost:7000 > logs/tunnel_student.log 2>&1 &
T1=$!
cloudflared tunnel --no-autoupdate --url http://localhost:9000 > logs/tunnel_console.log 2>&1 &
T2=$!
cleanup() { kill $GW $T1 $T2 2>/dev/null; rm -f logs/public_url.txt logs/console_url.txt; echo; echo "Public links stopped."; }
trap cleanup EXIT
trap 'exit 0' INT TERM
wait_url() {
  local log=$1 url=""
  for i in $(seq 1 60); do
    url=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$log" | head -1)
    [ -n "$url" ] && break
    sleep 1
  done
  echo "$url"
}
STUDENT=$(wait_url logs/tunnel_student.log)
CONSOLE=$(wait_url logs/tunnel_console.log)
if [ -z "$STUDENT" ] || [ -z "$CONSOLE" ]; then
  echo "A tunnel did not start. Check your internet (use a phone hotspot) and logs/tunnel_*.log"
  exit 1
fi
echo "$STUDENT" > logs/public_url.txt
echo "$CONSOLE" > logs/console_url.txt
PW=$(cat logs/console_password.txt 2>/dev/null || echo "start run_portal.sh first")
echo
echo "================================================================"
echo "  STUDENT PORTAL (share with the class):"
echo "    $STUDENT"
echo
echo "  SERVER CONSOLE (examiners only):"
echo "    $CONSOLE"
echo "    password: $PW"
echo "================================================================"
if command -v qrencode >/dev/null; then
  echo "Scan to join the exam:"
  qrencode -t ANSIUTF8 "$STUDENT"
  qrencode -o logs/student_qr.png -s 12 "$STUDENT"
  qrencode -o logs/console_qr.png -s 12 "$CONSOLE"
  echo "QR images saved: logs/student_qr.png, logs/console_qr.png"
fi
echo "Keep this terminal open. Press Ctrl+C to stop both public links."
wait $T1 $T2
