import json
import os
import socket
import threading
import time

SIGNAL_IP = "10.0.0.254"
SIGNAL_PORT = 6666
TOKEN = os.environ.get("EXAM_SDN_TOKEN", "exam-sdn-secret")


class SDNSignaller:
    def __init__(self, target_ip=SIGNAL_IP, target_port=SIGNAL_PORT, enabled=True, log=print):
        self.target = (target_ip, target_port)
        self.enabled = enabled
        self.log = log
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._stop = threading.Event()

    def send(self, event, **data):
        if not self.enabled:
            return
        payload = {"event": event, "token": TOKEN, "ts": time.time()}
        payload.update(data)
        try:
            self.sock.sendto(json.dumps(payload).encode("utf-8"), self.target)
            self.log("[SDN] signal -> %s %s" % (event, {k: v for k, v in data.items() if k != "allowed_ips"}))
        except OSError as exc:
            self.log("[SDN] signal failed (%s): %s" % (event, exc))

    def start_heartbeat(self, interval, event, **data):
        def loop():
            while not self._stop.wait(interval):
                self.send(event, **data)
        threading.Thread(target=loop, daemon=True).start()

    def close(self):
        self._stop.set()
        self.sock.close()
