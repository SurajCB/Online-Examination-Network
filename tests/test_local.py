import csv
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "app"))

import protocol as P

PORT = 5599
FIXTURE = os.path.join(tempfile.mkdtemp(), "students.json")
with open(FIXTURE, "w") as _f:
    json.dump([{"id": "S10%d" % i, "name": n, "pin": str(1110 + i), "ip": "10.0.0.%d" % (10 + i)}
               for i, n in enumerate(["Aarav", "Diya", "Kabir", "Meera", "Rohan", "Sneha"], 1)], _f)
SIGNAL_PORT = 6699


class FakeController(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", SIGNAL_PORT))
        self.sock.settimeout(0.5)
        self.events = []
        self.running = True

    def run(self):
        while self.running:
            try:
                data, _ = self.sock.recvfrom(65535)
                self.events.append(json.loads(data.decode()))
            except socket.timeout:
                continue

    def kinds(self):
        return [e["event"] for e in self.events]


def login(sid, pin):
    s = socket.create_connection(("127.0.0.1", PORT), timeout=5)
    P.send_msg(s, P.make(P.LOGIN, student_id=sid, pin=pin))
    return s, P.recv_msg(s)


class ProtocolTests(unittest.TestCase):
    def test_roundtrip(self):
        a, b = socket.socketpair()
        msg = P.make(P.ANSWER, qid="Q1", choice=2)
        P.send_msg(a, msg)
        got = P.recv_msg(b)
        self.assertEqual(got["qid"], "Q1")
        self.assertEqual(got["choice"], 2)

    def test_fragmented_frame(self):
        a, b = socket.socketpair()
        raw = P.encode(P.make(P.PING))
        for i in range(len(raw)):
            a.send(raw[i:i + 1])
        self.assertEqual(P.recv_msg(b)["type"], P.PING)

    def test_bad_length_rejected(self):
        a, b = socket.socketpair()
        a.send(P.HEADER.pack(P.MAX_MESSAGE + 1))
        with self.assertRaises(P.ProtocolError):
            P.recv_msg(b)
        a.close()
        b.close()

    def test_closed_peer(self):
        a, b = socket.socketpair()
        a.close()
        with self.assertRaises(P.ConnectionClosed):
            P.recv_msg(b)


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.results = os.path.join(cls.tmp, "results.csv")
        cls.metrics = os.path.join(cls.tmp, "metrics.csv")
        cls.ctrl = FakeController()
        cls.ctrl.start()
        cls.server = subprocess.Popen(
            [sys.executable, os.path.join(BASE, "app", "exam_server.py"), "--host", "127.0.0.1", "--port", str(PORT),
             "--students", FIXTURE,
             "--duration", "8", "--grace", "0.5", "--results", cls.results, "--signal-ip", "127.0.0.1",
             "--signal-port", str(SIGNAL_PORT), "--advertise-ip", "127.0.0.1",
             "--status-file", os.path.join(cls.tmp, "status.json")],
            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", PORT), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.server.send_signal(2)
        cls.server.wait(timeout=10)
        cls.ctrl.running = False

    def client(self, sid, pin, *extra):
        return subprocess.Popen([sys.executable, os.path.join(BASE, "app", "exam_client.py"), "--server", "127.0.0.1",
                                 "--port", str(PORT), "--id", sid, "--pin", pin, "--auto", "--quiet",
                                 "--metrics", self.metrics, "--label", "local", *extra])

    def rows(self):
        with open(self.results) as f:
            return {r["student_id"]: r for r in csv.DictReader(f)}

    def test_1_exam_start_signal(self):
        time.sleep(0.3)
        self.assertIn("exam_start", self.ctrl.kinds())
        start = next(e for e in self.ctrl.events if e["event"] == "exam_start")
        self.assertEqual(len(start["allowed_ips"]), 6)

    def test_2_concurrent_clients(self):
        procs = [self.client("S10%d" % i, str(1110 + i), "--think-min", "0.05", "--think-max", "0.2")
                 for i in (1, 2, 3, 4)]
        for p in procs:
            self.assertEqual(p.wait(timeout=20), 0)
        rows = self.rows()
        for i in (1, 2, 3, 4):
            self.assertEqual(rows["S10%d" % i]["status"], "completed")
            self.assertEqual(rows["S10%d" % i]["answered"], "10")
        with open(self.metrics) as f:
            self.assertGreaterEqual(len(list(csv.DictReader(f))), 4)

    def test_3_duplicate_and_resubmit_rejected(self):
        s, reply = login("S101", "1111")
        s.close()
        self.assertEqual(reply["type"], P.LOGIN_FAIL)
        self.assertIn("already submitted", reply["reason"])

    def test_4_bad_pin_signals_controller(self):
        for _ in range(3):
            s, reply = login("S105", "9999")
            s.close()
            self.assertEqual(reply["type"], P.LOGIN_FAIL)
        time.sleep(0.3)
        fails = [e for e in self.ctrl.events if e["event"] == "auth_fail"]
        self.assertGreaterEqual(max(e["count"] for e in fails), 3)

    def test_5_resume_after_disconnect(self):
        s, reply = login("S105", "1115")
        self.assertEqual(reply["type"], P.LOGIN_OK)
        q = P.recv_msg(s)
        P.send_msg(s, P.make(P.ANSWER, qid=q["qid"], choice=0))
        P.recv_msg(s)
        s.close()
        time.sleep(0.3)
        s, reply = login("S105", "1115")
        self.assertEqual(reply["type"], P.LOGIN_OK)
        self.assertTrue(reply["resumed"])
        self.assertEqual(reply["answered"], 1)
        P.send_msg(s, P.make(P.SUBMIT))
        while True:
            msg = P.recv_msg(s)
            if msg["type"] == P.RESULT:
                break
        s.close()
        self.assertEqual(msg["answered"], 1)

    def test_6_timer_enforced(self):
        s, reply = login("S106", "1116")
        self.assertEqual(reply["type"], P.LOGIN_OK)
        P.recv_msg(s)
        s.settimeout(15)
        kinds = []
        while True:
            msg = P.recv_msg(s)
            kinds.append(msg["type"])
            if msg["type"] == P.RESULT:
                break
        s.close()
        self.assertEqual(kinds, [P.TIME_UP, P.RESULT])
        self.assertEqual(msg["status"], "timeout")


class PortalTests(unittest.TestCase):
    EXAM_PORT = 5598
    WEB_PORT = 8598
    GW_PORT = 7598
    CONSOLE_PORT = 9598

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.procs = [
            subprocess.Popen([sys.executable, os.path.join(BASE, "app", "exam_server.py"), "--host", "127.0.0.1", "--students", FIXTURE,
                              "--port", str(cls.EXAM_PORT), "--duration", "60", "--no-sdn",
                              "--results", os.path.join(cls.tmp, "r.csv"), "--status-file", os.path.join(cls.tmp, "status.json")],
                             stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT),
            subprocess.Popen([sys.executable, os.path.join(BASE, "app", "student_portal.py"), "--server", "127.0.0.1",
                              "--port", str(cls.EXAM_PORT), "--listen", "127.0.0.1", "--http-port", str(cls.WEB_PORT)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT),
            subprocess.Popen([sys.executable, os.path.join(BASE, "app", "gateway.py"), "--listen", "127.0.0.1", "--students", FIXTURE,
                              "--http-port", str(cls.GW_PORT), "--seat-map", "st2=127.0.0.1:%d" % cls.WEB_PORT],
                             stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT),
            subprocess.Popen([sys.executable, os.path.join(BASE, "app", "admin_portal.py"), "--listen", "127.0.0.1", "--students", FIXTURE,
                              "--http-port", str(cls.CONSOLE_PORT), "--controller", "http://127.0.0.1:1",
                              "--results-dir", cls.tmp],
                             env=dict(os.environ, EXAM_CONSOLE_PASSWORD="test-pass"),
                             stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT),
        ]
        for port in (cls.WEB_PORT, cls.GW_PORT, cls.CONSOLE_PORT):
            for _ in range(50):
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                    break
                except OSError:
                    time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        for p in cls.procs:
            p.terminate()
            p.wait(timeout=10)

    def call(self, path, body=None):
        import urllib.request
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.WEB_PORT, path), data=data,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())

    def test_page_served(self):
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:%d/" % self.WEB_PORT, timeout=5) as r:
            self.assertIn(b"Online Examination Portal", r.read())

    def test_full_exam_through_portal(self):
        self.assertEqual(self.call("/api/state")["stage"], "login")
        bad = self.call("/api/login", {"student_id": "S101", "pin": "0000"})
        self.assertEqual(bad["stage"], "login")
        self.assertIn("invalid", bad["error"])
        s = self.call("/api/login", {"student_id": "s101 ", "pin": "1111"})
        self.assertEqual(s["stage"], "exam")
        self.assertEqual(s["question"]["index"], 1)
        s = self.call("/api/answer", {"choice": 1})
        self.assertEqual(s["question"]["index"], 2)
        s = self.call("/api/answer", {"choice": None})
        self.assertEqual(s["question"]["index"], 3)
        s = self.call("/api/submit", {})
        self.assertEqual(s["stage"], "result")
        self.assertEqual(s["result"]["answered"], 2)
        self.assertEqual(s["result"]["score"], 1)
        self.assertIsNotNone(s["avg_rtt_ms"])
        self.assertEqual(self.call("/api/logout", {})["stage"], "login")

    def test_public_gateway(self):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.GW_PORT, timeout=5)
        conn.request("GET", "/")
        r = conn.getresponse()
        r.read()
        self.assertEqual(r.status, 302)
        self.assertEqual(r.getheader("Location"), "/seats")
        conn = http.client.HTTPConnection("127.0.0.1", self.GW_PORT, timeout=10)
        conn.request("GET", "/seats/api")
        seats = json.loads(conn.getresponse().read().decode())["seats"]
        self.assertEqual(len(seats), 7)
        self.assertEqual(next(x for x in seats if x["name"] == "st2")["state"], "free")
        conn = http.client.HTTPConnection("127.0.0.1", self.GW_PORT, timeout=5)
        conn.request("GET", "/seats/pick?seat=st2")
        r = conn.getresponse()
        r.read()
        cookie = r.getheader("Set-Cookie").split(";")[0]
        self.assertEqual(cookie, "seat=st2")
        conn = http.client.HTTPConnection("127.0.0.1", self.GW_PORT, timeout=5)
        conn.request("GET", "/", headers={"Cookie": cookie})
        page = conn.getresponse().read().decode()
        self.assertIn("Online Examination Portal", page)
        self.assertIn('window.SEAT_ID="S102"', page)
        self.assertNotIn("1112", page)

    def test_console_requires_password(self):
        import http.client

        def req(method, path, body=None, cookie=None):
            conn = http.client.HTTPConnection("127.0.0.1", self.CONSOLE_PORT, timeout=10)
            headers = {"Content-Type": "application/json"}
            if cookie:
                headers["Cookie"] = cookie
            conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
            r = conn.getresponse()
            return r.status, r.getheader("Set-Cookie"), r.read()

        self.assertIn(b"Server Console", req("GET", "/")[2])
        self.assertEqual(req("GET", "/api/overview")[0], 401)
        self.assertEqual(req("POST", "/api/block", {"ip": "10.0.0.5"})[0], 401)
        self.assertEqual(req("POST", "/api/login", {"password": "nope"})[0], 403)
        status, cookie, _ = req("POST", "/api/login", {"password": "test-pass"})
        self.assertEqual(status, 200)
        cookie = cookie.split(";")[0]
        for path in ("/api/overview", "/api/server", "/api/network", "/api/tests"):
            status, _, body = req("GET", path, cookie=cookie)
            self.assertEqual(status, 200, path)
        overview = json.loads(req("GET", "/api/overview", cookie=cookie)[2])
        self.assertFalse(overview["controller_ok"])
        self.assertEqual(len(overview["students"]), 6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
