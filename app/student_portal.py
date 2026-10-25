import argparse
import os
import select
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import protocol as P
from minihttp import HTTPServer, html_response, json_response, response

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


def local_ip_towards(host):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((host, 9))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "unknown"


class ExamBridge:
    def __init__(self, server, port, timeout):
        self.server = server
        self.port = port
        self.timeout = timeout
        self.lock = threading.Lock()
        self.seat = local_ip_towards(server)
        self.reset()

    def reset(self):
        self.sock = None
        self.stage = "login"
        self.student = None
        self.info = {}
        self.question = None
        self.result = None
        self.deadline = None
        self.timed_out = False
        self.error = None
        self.rtts = []

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None

    def request(self, msg):
        P.send_msg(self.sock, msg)
        reply = P.recv_msg(self.sock)
        if reply.get("echo_t") == msg["t"]:
            self.rtts.append((time.time() - msg["t"]) * 1000)
        return reply

    def process(self, msg):
        while True:
            mtype = msg["type"]
            if mtype == P.QUESTION:
                self.question = msg
                self.deadline = time.time() + msg.get("remaining", 0)
                self.stage = "exam"
                return
            if mtype == P.TIME_UP:
                self.timed_out = True
                msg = P.recv_msg(self.sock)
                continue
            if mtype == P.RESULT:
                self.result = msg
                self.stage = "result"
                self.close()
                return
            if mtype == P.ERROR:
                self.error = msg.get("reason", "server error")
                msg = P.recv_msg(self.sock)
                continue
            msg = P.recv_msg(self.sock)

    def guarded(self, fn):
        with self.lock:
            self.error = None
            try:
                fn()
            except socket.timeout:
                self.close()
                self.stage = "login"
                self.error = "The exam server did not respond. Log in again to resume."
            except (P.ConnectionClosed, P.ProtocolError, OSError) as exc:
                self.close()
                if self.stage != "result":
                    self.stage = "login"
                    self.error = "Connection to the exam server was lost (%s). Log in again to resume." % exc
            return self.state()

    def login(self, sid, pin):
        def run():
            if self.stage == "exam" and self.sock:
                if self.student and self.student["id"] == sid:
                    return
                self.error = "Another student is already taking the exam on this seat."
                return
            self.reset()
            try:
                t0 = time.time()
                self.sock = socket.create_connection((self.server, self.port), timeout=self.timeout)
                self.connect_ms = (time.time() - t0) * 1000
            except OSError as exc:
                self.sock = None
                self.error = ("Cannot reach the exam server at %s:%d (%s). If this seat is not assigned to the "
                              "exam, the network blocks it." % (self.server, self.port, exc))
                return
            P.tune_socket(self.sock)
            self.sock.settimeout(self.timeout)
            reply = self.request(P.make(P.LOGIN, student_id=sid, pin=pin))
            if reply["type"] != P.LOGIN_OK:
                self.close()
                self.error = reply.get("reason", "Login failed")
                return
            self.student = {"id": sid, "name": reply.get("name")}
            self.info = reply
            self.deadline = time.time() + reply.get("remaining", 0)
            self.process(P.recv_msg(self.sock))
        return self.guarded(run)

    def answer(self, choice):
        def run():
            if self.stage != "exam" or not self.question:
                self.error = "No active question."
                return
            self.process(self.request(P.make(P.ANSWER, qid=self.question["qid"], choice=choice)))
        return self.guarded(run)

    def submit(self):
        def run():
            if self.stage != "exam":
                return
            self.process(self.request(P.make(P.SUBMIT)))
        return self.guarded(run)

    def poll(self):
        def run():
            if self.stage == "exam" and self.sock:
                ready, _, _ = select.select([self.sock], [], [], 0)
                if ready:
                    self.process(P.recv_msg(self.sock))
        return self.guarded(run)

    def logout(self):
        with self.lock:
            self.close()
            self.reset()
            return self.state()

    def state(self):
        q = None
        if self.question and self.stage == "exam":
            q = {k: self.question.get(k) for k in ("qid", "index", "total", "text", "options", "marks")}
        rtts = self.rtts
        return {
            "stage": self.stage,
            "seat": self.seat,
            "server": "%s:%d" % (self.server, self.port),
            "student": self.student,
            "title": self.info.get("exam_title"),
            "total": self.info.get("total_questions"),
            "resumed": self.info.get("resumed", False),
            "question": q,
            "remaining": round(max(0.0, self.deadline - time.time()), 1) if self.deadline else None,
            "result": self.result,
            "timed_out": self.timed_out,
            "error": self.error,
            "last_rtt_ms": round(rtts[-1], 2) if rtts else None,
            "avg_rtt_ms": round(sum(rtts) / len(rtts), 2) if rtts else None,
        }


def make_handler(bridge):
    with open(os.path.join(WEB, "student.html"), encoding="utf-8") as f:
        page = f.read()

    def handler(req):
        if req.method == "GET" and req.path in ("/", "/index.html"):
            return html_response(page)
        if req.method == "GET" and req.path == "/api/state":
            return json_response(bridge.poll())
        if req.method != "POST":
            return response(404, "not found")
        body = req.json()
        if req.path == "/api/login":
            sid = str(body.get("student_id", "")).strip()
            pin = str(body.get("pin", "")).strip()
            if not sid or not pin:
                return json_response(dict(bridge.state(), error="Enter your username and password."))
            return json_response(bridge.login(sid, pin))
        if req.path == "/api/answer":
            choice = body.get("choice")
            if choice is not None:
                try:
                    choice = int(choice)
                except (TypeError, ValueError):
                    return json_response(dict(bridge.state(), error="Invalid option."))
            return json_response(bridge.answer(choice))
        if req.path == "/api/submit":
            return json_response(bridge.submit())
        if req.path == "/api/logout":
            return json_response(bridge.logout())
        return response(404, "not found")
    return handler


def main():
    ap = argparse.ArgumentParser(description="Student web portal (HTTP on raw sockets, TCP bridge to exam server)")
    ap.add_argument("--server", default="10.0.0.1")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--listen", default="0.0.0.0")
    ap.add_argument("--http-port", type=int, default=8000)
    ap.add_argument("--timeout", type=float, default=8.0)
    args = ap.parse_args()
    bridge = ExamBridge(args.server, args.port, args.timeout)
    httpd = HTTPServer(args.listen, args.http_port, make_handler(bridge))
    print("[PORTAL] student portal for seat %s on http://%s:%d -> exam server %s:%d"
          % (bridge.seat, bridge.seat if args.listen == "0.0.0.0" else args.listen, args.http_port,
             args.server, args.port), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.stop()


if __name__ == "__main__":
    main()
