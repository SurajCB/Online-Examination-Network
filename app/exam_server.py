import argparse
import csv
import json
import os
import random
import signal
import socket
import sys
import threading
import time
from collections import deque
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import protocol as P
from sdn_signal import SDNSignaller, SIGNAL_IP, SIGNAL_PORT

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


LOG_LINES = deque(maxlen=80)


def log(msg):
    line = "%s %s" % (datetime.now().strftime("%H:%M:%S.%f")[:-3], msg)
    LOG_LINES.append(line)
    print(line, flush=True)


class Session:
    def __init__(self, student, question_ids, duration):
        self.student_id = student["id"]
        self.name = student["name"]
        self.order = question_ids
        self.index = 0
        self.answers = {}
        self.start = time.time()
        self.deadline = self.start + duration
        self.end = None
        self.status = "active"
        self.connected = False
        self.ip = None
        self.score = None

    def remaining(self):
        return max(0.0, self.deadline - time.time())

    def finished(self):
        return self.status in ("completed", "timeout")


class ExamServer:
    def __init__(self, args):
        self.args = args
        with open(args.questions, encoding="utf-8") as f:
            bank = json.load(f)
        self.title = bank["title"]
        self.questions = {q["id"]: q for q in bank["questions"]}
        with open(args.students, encoding="utf-8") as f:
            self.students = {s["id"]: s for s in json.load(f)}
        self.lookup = {sid.strip().lower(): s for sid, s in self.students.items()}
        self.sessions = {}
        self.auth_failures = {}
        self.lock = threading.Lock()
        self.csv_lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(args.max_clients)
        self.active_clients = 0
        self.running = True
        self.started = time.time()
        self.run_id = "%d-%d" % (int(self.started), os.getpid())
        self.total_connections = 0
        self.sdn = SDNSignaller(args.signal_ip, args.signal_port, enabled=not args.no_sdn, log=log)
        self.server_ip = args.advertise_ip
        os.makedirs(os.path.dirname(args.results), exist_ok=True)
        if os.path.exists(args.results) and os.path.getsize(args.results) > 0:
            old = os.path.join(os.path.dirname(args.results), "old")
            os.makedirs(old, exist_ok=True)
            base, ext = os.path.splitext(os.path.basename(args.results))
            os.replace(args.results, os.path.join(old, "%s_%s%s" % (base, time.strftime("%Y%m%d_%H%M%S"), ext)))
        with open(args.results, "w", newline="") as f:
            csv.writer(f).writerow(["student_id", "name", "ip", "score", "total", "answered",
                                    "status", "start", "end", "time_used_s"])

    def question_order(self, student_id):
        ids = list(self.questions.keys())
        if self.args.shuffle:
            random.Random(student_id).shuffle(ids)
        return ids

    def snapshot(self):
        with self.lock:
            sessions = [{"id": s.student_id, "name": s.name, "ip": s.ip, "status": s.status,
                         "connected": s.connected, "question": min(s.index + 1, len(s.order)), "total": len(s.order),
                         "answered": len(s.answers), "remaining": round(s.remaining()) if not s.finished() else 0,
                         "score": s.score} for s in self.sessions.values()]
            data = {"title": self.title, "listen": "%s:%d" % (self.args.host, self.args.port),
                    "advertise": "%s:%d" % (self.server_ip, self.args.port), "running": self.running,
                    "uptime": round(time.time() - self.started), "duration": self.args.duration,
                    "max_clients": self.args.max_clients, "active_clients": self.active_clients,
                    "total_connections": self.total_connections, "questions": len(self.questions),
                    "registered": len(self.students), "run": self.run_id,
                    "results_file": os.path.basename(self.args.results), "auth_failures": dict(self.auth_failures),
                    "sessions": sessions, "log": list(LOG_LINES)[-60:], "updated": time.time()}
        return data

    def write_status(self):
        if not self.args.status_file:
            return
        tmp = self.args.status_file + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(self.snapshot(), f)
            os.chmod(tmp, 0o644)
            os.replace(tmp, self.args.status_file)
        except OSError:
            pass

    def status_loop(self):
        while self.running:
            self.write_status()
            time.sleep(1)

    def start(self):
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind((self.args.host, self.args.port))
        self.listener.listen(self.args.backlog)
        self.listener.settimeout(1.0)
        log("[SERVER] '%s' listening on %s:%d | %d questions | %ds per student | max %d clients"
            % (self.title, self.args.host, self.args.port, len(self.questions),
               self.args.duration, self.args.max_clients))
        allowed = sorted({s["ip"] for s in self.students.values() if s.get("ip")})
        start_info = dict(server_ip=self.server_ip, port=self.args.port, allowed_ips=allowed,
                          duration=self.args.duration, run=self.run_id)
        self.sdn.send("exam_start", **start_info)
        self.sdn.start_heartbeat(self.args.heartbeat, "exam_start", **start_info)
        threading.Thread(target=self.status_loop, daemon=True).start()
        while self.running:
            try:
                conn, addr = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if not self.slots.acquire(blocking=False):
                log("[SERVER] rejecting %s:%d - server full" % addr)
                try:
                    P.send_msg(conn, P.make(P.ERROR, reason="server busy, retry later"))
                finally:
                    conn.close()
                continue
            threading.Thread(target=self.client_thread, args=(conn, addr), daemon=True).start()

    def client_thread(self, conn, addr):
        with self.lock:
            self.active_clients += 1
            self.total_connections += 1
        log("[CONN] %s:%d connected (active=%d)" % (addr[0], addr[1], self.active_clients))
        session = None
        try:
            P.tune_socket(conn)
            session = self.authenticate(conn, addr)
            if session:
                self.run_exam(conn, addr, session)
        except P.ConnectionClosed:
            log("[CONN] %s:%d closed connection" % addr)
        except socket.timeout:
            log("[CONN] %s:%d idle timeout" % addr)
        except (P.ProtocolError, ConnectionResetError, BrokenPipeError) as exc:
            log("[CONN] %s:%d error: %s" % (addr[0], addr[1], exc))
        finally:
            if session:
                with self.lock:
                    session.connected = False
                    if not session.finished():
                        session.status = "disconnected"
                        log("[EXAM] %s disconnected mid-exam, can resume within %.0fs"
                            % (session.student_id, session.remaining()))
            with self.lock:
                self.active_clients -= 1
            self.slots.release()
            conn.close()

    def authenticate(self, conn, addr):
        ip = addr[0]
        conn.settimeout(self.args.login_timeout)
        for _ in range(self.args.max_login_attempts):
            msg = P.recv_msg(conn)
            if msg["type"] != P.LOGIN:
                P.send_msg(conn, P.make(P.ERROR, reason="login required"))
                continue
            sid = str(msg.get("student_id", "")).strip()
            student = self.lookup.get(sid.lower())
            if student:
                sid = student["id"]
            if not student or student.get("pin") != str(msg.get("pin", "")):
                self.record_auth_failure(ip, sid)
                P.send_msg(conn, P.make(P.LOGIN_FAIL, reason="invalid credentials", echo_t=msg.get("t")))
                continue
            if self.args.strict_seat and student.get("ip") and student["ip"] != ip:
                self.record_auth_failure(ip, sid)
                P.send_msg(conn, P.make(P.LOGIN_FAIL, reason="login from unassigned seat %s" % ip,
                                        echo_t=msg.get("t")))
                continue
            with self.lock:
                session = self.sessions.get(sid)
                if session and session.finished():
                    P.send_msg(conn, P.make(P.LOGIN_FAIL, reason="exam already submitted", echo_t=msg.get("t")))
                    return None
                if session and session.connected:
                    P.send_msg(conn, P.make(P.LOGIN_FAIL, reason="already logged in elsewhere",
                                            echo_t=msg.get("t")))
                    return None
                resumed = session is not None
                if not session:
                    session = Session(student, self.question_order(sid), self.args.duration)
                    self.sessions[sid] = session
                if session.remaining() <= 0:
                    session.connected = True
                    session.ip = ip
                    return session
                session.connected = True
                session.status = "active"
                session.ip = ip
                self.auth_failures.pop(ip, None)
            log("[AUTH] %s (%s) logged in from %s%s" % (sid, session.name, ip, " [RESUMED]" if resumed else ""))
            P.send_msg(conn, P.make(P.LOGIN_OK, name=session.name, exam_title=self.title,
                                    total_questions=len(session.order), duration=self.args.duration,
                                    remaining=round(session.remaining(), 1), resumed=resumed,
                                    answered=len(session.answers), echo_t=msg.get("t")))
            self.sdn.send("student_login", student_id=sid, ip=ip)
            return session
        log("[AUTH] %s exceeded login attempts" % ip)
        return None

    def record_auth_failure(self, ip, sid):
        with self.lock:
            count = self.auth_failures.get(ip, 0) + 1
            self.auth_failures[ip] = count
        log("[AUTH] failed login #%d from %s (id=%r)" % (count, ip, sid))
        self.sdn.send("auth_fail", ip=ip, count=count, student_id=sid)

    def send_question(self, conn, session, echo_t=None):
        qid = session.order[session.index]
        q = self.questions[qid]
        P.send_msg(conn, P.make(P.QUESTION, qid=qid, index=session.index + 1, total=len(session.order),
                                text=q["text"], options=q["options"], marks=q.get("marks", 1),
                                remaining=round(session.remaining(), 1), echo_t=echo_t))

    def run_exam(self, conn, addr, session):
        if session.remaining() <= 0:
            self.finish(conn, session, "timeout")
            return
        self.send_question(conn, session)
        while True:
            remaining = session.remaining()
            if remaining <= 0:
                self.finish(conn, session, "timeout")
                return
            conn.settimeout(remaining + self.args.grace)
            try:
                msg = P.recv_msg(conn)
            except socket.timeout:
                self.finish(conn, session, "timeout")
                return
            mtype = msg["type"]
            if mtype == P.PING:
                P.send_msg(conn, P.make(P.PONG, remaining=round(session.remaining(), 1), echo_t=msg.get("t")))
            elif mtype == P.ANSWER:
                if time.time() > session.deadline + self.args.grace:
                    self.finish(conn, session, "timeout")
                    return
                expected = session.order[session.index]
                if msg.get("qid") != expected:
                    P.send_msg(conn, P.make(P.ERROR, reason="answer for wrong question", expected=expected))
                    self.send_question(conn, session)
                    continue
                choice = msg.get("choice")
                session.answers[expected] = choice
                session.index += 1
                if session.index >= len(session.order):
                    self.finish(conn, session, "completed", echo_t=msg.get("t"))
                    return
                self.send_question(conn, session, msg.get("t"))
            elif mtype == P.SUBMIT:
                self.finish(conn, session, "completed", echo_t=msg.get("t"))
                return
            else:
                P.send_msg(conn, P.make(P.ERROR, reason="unexpected message %s" % mtype))

    def grade(self, session):
        score = 0
        total = 0
        details = []
        for qid in session.order:
            q = self.questions[qid]
            marks = q.get("marks", 1)
            total += marks
            given = session.answers.get(qid)
            correct = given is not None and int(given) == int(q["answer"])
            if correct:
                score += marks
            details.append({"qid": qid, "given": given, "correct": correct})
        return score, total, details

    def finish(self, conn, session, status, echo_t=None):
        with self.lock:
            if session.finished():
                return
            session.status = status
            session.end = time.time()
        score, total, details = self.grade(session)
        session.score = score
        if conn and status == "timeout":
            try:
                P.send_msg(conn, P.make(P.TIME_UP, message="time is over, answers auto-submitted"))
            except OSError:
                pass
        if conn:
            try:
                P.send_msg(conn, P.make(P.RESULT, score=score, total=total, answered=len(session.answers),
                                        status=status, details=details, echo_t=echo_t))
            except OSError:
                pass
        used = session.end - session.start
        log("[RESULT] %s %s score=%d/%d answered=%d/%d time=%.1fs"
            % (session.student_id, status.upper(), score, total, len(session.answers), len(session.order), used))
        with self.csv_lock, open(self.args.results, "a", newline="") as f:
            csv.writer(f).writerow([session.student_id, session.name, session.ip, score, total,
                                    len(session.answers), status,
                                    datetime.fromtimestamp(session.start).isoformat(timespec="seconds"),
                                    datetime.fromtimestamp(session.end).isoformat(timespec="seconds"),
                                    round(used, 2)])
        self.sdn.send("student_submit", student_id=session.student_id, ip=session.ip, score=score,
                      total=total, status=status)

    def shutdown(self, *_):
        if not self.running:
            return
        self.running = False
        log("[SERVER] shutting down")
        for session in list(self.sessions.values()):
            if not session.finished():
                self.finish(None, session, "timeout")
        self.sdn.send("exam_end", server_ip=self.server_ip, port=self.args.port)
        done = sum(1 for s in self.sessions.values() if s.finished())
        log("[SERVER] sessions=%d finished=%d" % (len(self.sessions), done))
        try:
            self.listener.close()
        except OSError:
            pass
        self.sdn.close()
        self.write_status()


def parse_args():
    ap = argparse.ArgumentParser(description="Online Examination TCP server")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--advertise-ip", default="10.0.0.1")
    ap.add_argument("--questions", default=os.path.join(BASE, "data", "questions.json"))
    ap.add_argument("--students", default=os.path.join(BASE, "data", "students.json"))
    ap.add_argument("--results", default=os.path.join(BASE, "results", "results.csv"))
    ap.add_argument("--duration", type=int, default=120)
    ap.add_argument("--grace", type=float, default=2.0)
    ap.add_argument("--max-clients", type=int, default=50)
    ap.add_argument("--backlog", type=int, default=64)
    ap.add_argument("--login-timeout", type=float, default=30.0)
    ap.add_argument("--max-login-attempts", type=int, default=3)
    ap.add_argument("--shuffle", action="store_true")
    ap.add_argument("--strict-seat", action="store_true")
    ap.add_argument("--no-sdn", action="store_true")
    ap.add_argument("--signal-ip", default=SIGNAL_IP)
    ap.add_argument("--signal-port", type=int, default=SIGNAL_PORT)
    ap.add_argument("--heartbeat", type=float, default=15.0)
    ap.add_argument("--status-file", default=os.path.join(BASE, "logs", "server_status.json"))
    return ap.parse_args()


def main():
    server = ExamServer(parse_args())
    signal.signal(signal.SIGINT, server.shutdown)
    signal.signal(signal.SIGTERM, server.shutdown)
    server.start()
    server.shutdown()


if __name__ == "__main__":
    main()
