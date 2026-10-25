import argparse
import csv
import os
import random
import select
import socket
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import protocol as P


class ExamClient:
    def __init__(self, args):
        self.args = args
        self.sock = None
        self.rtts = []
        self.connect_ms = None
        self.login_rtt = None
        self.result = None
        self.status = "not_started"
        self.t_begin = None

    def say(self, text):
        if not self.args.quiet:
            print(text, flush=True)

    def connect(self):
        delay = 0.5
        for attempt in range(1, self.args.retries + 1):
            try:
                t0 = time.time()
                sock = socket.create_connection((self.args.server, self.args.port), timeout=self.args.timeout)
                self.connect_ms = (time.time() - t0) * 1000
                P.tune_socket(sock)
                sock.settimeout(None)
                self.sock = sock
                self.say("[+] connected to %s:%d in %.1f ms" % (self.args.server, self.args.port, self.connect_ms))
                return True
            except OSError as exc:
                self.say("[!] connect attempt %d failed: %s" % (attempt, exc))
                time.sleep(delay)
                delay = min(delay * 2, 5)
        return False

    def request(self, msg):
        P.send_msg(self.sock, msg)
        reply = P.recv_msg(self.sock)
        if reply.get("echo_t") == msg["t"]:
            self.rtts.append((time.time() - msg["t"]) * 1000)
        return reply

    def login(self):
        reply = self.request(P.make(P.LOGIN, student_id=self.args.id, pin=self.args.pin))
        if reply["type"] == P.LOGIN_OK:
            self.login_rtt = self.rtts[-1] if self.rtts else None
            self.say("[+] welcome %s | %s | %d questions | %.0fs remaining%s"
                     % (reply["name"], reply["exam_title"], reply["total_questions"], reply["remaining"],
                        " (resumed)" if reply.get("resumed") else ""))
            return True
        self.say("[-] login failed: %s" % reply.get("reason", reply["type"]))
        self.status = "login_failed"
        return False

    def show_question(self, q):
        self.say("\n[Q%d/%d] (%s marks) %s   [%.0fs left]" % (q["index"], q["total"], q.get("marks", 1),
                                                           q["text"], q["remaining"]))
        for i, opt in enumerate(q["options"], 1):
            self.say("   %d) %s" % (i, opt))

    def handle_final(self, msg):
        if msg["type"] == P.TIME_UP:
            self.say("\n[!] %s" % msg.get("message", "time up"))
            msg = P.recv_msg(self.sock)
        if msg["type"] == P.RESULT:
            self.result = msg
            self.status = msg["status"]
            self.say("\n===== RESULT: %d / %d  (answered %d, %s) =====" % (msg["score"], msg["total"],
                                                                      msg["answered"], msg["status"]))
            return True
        return False

    def auto_answer(self, q):
        time.sleep(random.uniform(self.args.think_min, self.args.think_max))
        choice = random.randrange(len(q["options"]))
        self.say("    -> auto answer %d" % (choice + 1))
        return P.make(P.ANSWER, qid=q["qid"], choice=choice)

    def interactive_answer(self, q):
        while True:
            sys.stdout.write("answer [1-%d | s=skip | submit | time]: " % len(q["options"]))
            sys.stdout.flush()
            ready, _, _ = select.select([sys.stdin, self.sock], [], [])
            if self.sock in ready:
                return None
            line = sys.stdin.readline().strip().lower()
            if line == "submit":
                return P.make(P.SUBMIT)
            if line == "s":
                return P.make(P.ANSWER, qid=q["qid"], choice=None)
            if line == "time":
                reply = self.request(P.make(P.PING))
                if reply["type"] == P.PONG:
                    self.say("    %.0f seconds remaining" % reply["remaining"])
                    continue
                self.handle_final(reply)
                return "done"
            if line.isdigit() and 1 <= int(line) <= len(q["options"]):
                return P.make(P.ANSWER, qid=q["qid"], choice=int(line) - 1)
            self.say("    invalid input")

    def run(self):
        self.t_begin = time.time()
        if not self.connect():
            self.status = "connect_failed"
            return
        try:
            self.sock.settimeout(self.args.timeout + 600)
            if not self.login():
                return
            msg = P.recv_msg(self.sock)
            while True:
                if msg["type"] in (P.RESULT, P.TIME_UP):
                    self.handle_final(msg)
                    return
                if msg["type"] == P.ERROR:
                    self.say("[!] server: %s" % msg.get("reason"))
                    msg = P.recv_msg(self.sock)
                    continue
                if msg["type"] != P.QUESTION:
                    msg = P.recv_msg(self.sock)
                    continue
                self.show_question(msg)
                out = self.auto_answer(msg) if self.args.auto else self.interactive_answer(msg)
                if out == "done":
                    return
                if out is None:
                    msg = P.recv_msg(self.sock)
                    continue
                msg = self.request(out)
        except (P.ConnectionClosed, ConnectionResetError, BrokenPipeError) as exc:
            self.status = "disconnected"
            self.say("[!] connection lost: %s" % exc)
        except socket.timeout:
            self.status = "timeout"
            self.say("[!] server not responding")
        finally:
            if self.sock:
                self.sock.close()
            self.write_metrics()

    def write_metrics(self):
        if not self.args.metrics:
            return
        rtts = sorted(self.rtts)
        p95 = rtts[min(len(rtts) - 1, int(0.95 * len(rtts)))] if rtts else None
        row = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "label": self.args.label,
            "student_id": self.args.id,
            "status": self.status,
            "connect_ms": round(self.connect_ms, 3) if self.connect_ms else "",
            "login_rtt_ms": round(self.login_rtt, 3) if self.login_rtt else "",
            "avg_rtt_ms": round(statistics.mean(rtts), 3) if rtts else "",
            "p95_rtt_ms": round(p95, 3) if p95 else "",
            "max_rtt_ms": round(max(rtts), 3) if rtts else "",
            "requests": len(rtts),
            "total_s": round(time.time() - self.t_begin, 3),
            "score": self.result["score"] if self.result else "",
            "total": self.result["total"] if self.result else "",
        }
        os.makedirs(os.path.dirname(os.path.abspath(self.args.metrics)), exist_ok=True)
        new = not os.path.exists(self.args.metrics)
        with open(self.args.metrics, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(row.keys()))
            if new:
                w.writeheader()
            w.writerow(row)


def parse_args():
    ap = argparse.ArgumentParser(description="Online Examination TCP client")
    ap.add_argument("--server", default="10.0.0.1")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--id", required=True)
    ap.add_argument("--pin", required=True)
    ap.add_argument("--auto", action="store_true")
    ap.add_argument("--think-min", type=float, default=0.5)
    ap.add_argument("--think-max", type=float, default=1.5)
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--retries", type=int, default=5)
    ap.add_argument("--metrics", default=None)
    ap.add_argument("--label", default="default")
    ap.add_argument("--quiet", action="store_true")
    return ap.parse_args()


if __name__ == "__main__":
    ExamClient(parse_args()).run()
