import argparse
import csv
import glob
import json
import os
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
from http.cookies import SimpleCookie

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from minihttp import HTTPServer, html_response, json_response, response

APP = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(APP)
WEB = os.path.join(APP, "web")
LOGS = os.path.join(BASE, "logs")
WORDS = ["router", "packet", "switch", "socket", "kernel", "subnet", "beacon", "gateway", "bridge", "vector"]


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def read_text(path):
    try:
        with open(path) as f:
            return f.read().strip() or None
    except OSError:
        return None


def console_password():
    env = os.environ.get("EXAM_CONSOLE_PASSWORD")
    if env:
        return env
    path = os.path.join(LOGS, "console_password.txt")
    existing = read_text(path)
    if existing:
        return existing
    pw = "%s-%04d" % (secrets.choice(WORDS), secrets.randbelow(10000))
    os.makedirs(LOGS, exist_ok=True)
    with open(path, "w") as f:
        f.write(pw + "\n")
    os.chmod(path, 0o644)
    return pw


class Console:
    def __init__(self, controller, students_file, results_dir, password):
        self.controller = controller.rstrip("/")
        self.students_file = students_file
        self.results_dir = results_dir
        self.password = password
        self.tokens = set()
        self.failures = {}
        self.lock = threading.Lock()

    def rest(self, path, method="GET"):
        req = urllib.request.Request(self.controller + path, method=method, data=b"" if method == "POST" else None)
        with urllib.request.urlopen(req, timeout=1.5) as r:
            return json.loads(r.read().decode("utf-8"))

    def controller_data(self, *paths):
        out, error = {}, None
        for p in paths:
            try:
                out[p] = self.rest(p)
            except (urllib.error.URLError, OSError, ValueError) as exc:
                error = "SDN controller not reachable at %s (%s)" % (self.controller, getattr(exc, "reason", exc))
                break
        return out, error

    def latest_results(self, server=None):
        if server and server.get("fresh") and server.get("results_file"):
            newest = os.path.join(self.results_dir, server["results_file"])
            if not os.path.exists(newest):
                return {}, None
        else:
            files = glob.glob(os.path.join(self.results_dir, "results*.csv"))
            if not files:
                return {}, None
            newest = max(files, key=os.path.getmtime)
        rows = {}
        with open(newest, newline="") as f:
            for row in csv.DictReader(f):
                if row.get("student_id") and row.get("status"):
                    rows[row["student_id"]] = row
        return rows, os.path.basename(newest)

    def server_status(self):
        st = read_json(os.path.join(LOGS, "server_status.json"))
        if st:
            st["fresh"] = st.get("running") and time.time() - st.get("updated", 0) < 5
        return st

    def students(self, status, server):
        roster = read_json(self.students_file, [])
        results, results_file = self.latest_results(server)
        sessions = {s["id"]: s for s in (server or {}).get("sessions", [])} if server and server.get("fresh") else {}
        blocked = status.get("blocked", {}) or {}
        active = status.get("active_students", {}) or {}
        submitted = status.get("submitted", {}) or {}
        out = []
        for s in roster:
            row = results.get(s["id"])
            sess = sessions.get(s["id"])
            if s.get("ip") in blocked:
                state = "blocked"
            elif sess and sess["status"] in ("completed", "timeout"):
                state = sess["status"]
            elif sess and sess["status"] in ("active", "disconnected"):
                state = "in_exam" if sess["connected"] else "disconnected"
            elif row:
                state = row["status"]
            elif s["id"] in submitted:
                state = submitted[s["id"]].get("status", "completed")
            elif s.get("ip") in active:
                state = "in_exam"
            else:
                state = "not_joined"
            score = None
            if sess and sess.get("score") is not None:
                score = "%s/%s" % (sess["score"], sess["total"])
            elif row:
                score = "%s/%s" % (row["score"], row["total"])
            out.append({"id": s["id"], "name": s["name"], "ip": s.get("ip"), "state": state, "score": score,
                        "progress": "%s/%s" % (sess["answered"], sess["total"]) if sess else None,
                        "remaining": sess.get("remaining") if sess else None})
        return out, results_file

    def links(self):
        return {"student": read_text(os.path.join(LOGS, "public_url.txt")),
                "console": read_text(os.path.join(LOGS, "console_url.txt"))}

    def overview(self):
        data, error = self.controller_data("/exam/status", "/exam/stats", "/exam/events")
        status = data.get("/exam/status", {})
        stats = data.get("/exam/stats", {})
        server = self.server_status()
        students, results_file = self.students(status, server)
        flows = stats.get("flows", {}) if isinstance(stats, dict) else {}
        return {
            "controller_ok": error is None, "controller_error": error,
            "exam_active": status.get("exam_active", False),
            "server": "%s:%s" % (status.get("server_ip"), status.get("exam_port")) if status.get("server_ip") else None,
            "prioritize": status.get("prioritize"), "switches": status.get("switches", []),
            "allowed": status.get("allowed_ips", []), "blocked": status.get("blocked", {}) or {},
            "students": students, "results_file": results_file,
            "events": list(data.get("/exam/events", []))[-60:],
            "traffic": [dict(dpid=int(k), **v) for k, v in sorted(flows.items(), key=lambda kv: int(kv[0]))],
            "server_up": bool(server and server.get("fresh")), "links": self.links(),
        }

    def server_page(self):
        st = self.server_status()
        return {"status": st, "up": bool(st and st.get("fresh"))}

    def network_page(self):
        data, error = self.controller_data("/exam/flows", "/exam/stats", "/exam/status")
        return {"controller_ok": error is None, "controller_error": error,
                "topology": read_json(os.path.join(LOGS, "topology.json")),
                "flows": data.get("/exam/flows", {}), "stats": data.get("/exam/stats", {}),
                "status": data.get("/exam/status", {})}

    def tests_page(self):
        ov = self.overview()
        flows, _ = self.controller_data("/exam/flows")
        tables = flows.get("/exam/flows", {})
        kinds = {f["kind"] for t in tables.values() for f in t}
        server = self.server_status() or {}
        checks = [
            ("Ryu controller reachable (REST :8080)", ov["controller_ok"], ov["controller_error"] or "ok"),
            ("All 3 OpenFlow switches connected", len(ov["switches"]) >= 3, "%d connected" % len(ov["switches"])),
            ("Exam server running (TCP :5000)", ov["server_up"], server.get("listen", "no status file")),
            ("App → SDN signal received (exam mode ON)", ov["exam_active"], ov["server"] or "exam not started"),
            ("Seat whitelist installed", len(ov["allowed"]) >= 1, "%d seats allowed" % len(ov["allowed"])),
            ("Table-miss + signal rules on switches", "table-miss" in kinds and "signal" in kinds,
             ", ".join(sorted(kinds)) or "no flows yet"),
            ("Exam traffic flows installed (priority 100)", "exam" in kinds, "appears once a student logs in"),
            ("Controller is monitoring traffic", bool(ov["traffic"]), "%d switches reporting" % len(ov["traffic"])),
            ("Student public link running", bool(ov["links"]["student"]), ov["links"]["student"] or "run_public.sh not started"),
        ]
        return {"live": [{"name": n, "ok": bool(ok), "detail": d} for n, ok, d in checks],
                "last_run": read_json(os.path.join(self.results_dir, "test_summary_latest.json"))}

    def client_ip(self, req):
        return req.headers.get("cf-connecting-ip") or req.client[0]

    def authed(self, req):
        cookie = SimpleCookie()
        try:
            cookie.load(req.headers.get("cookie", ""))
        except Exception:
            return False
        m = cookie.get("console")
        return bool(m and m.value in self.tokens)

    def login(self, req):
        ip = self.client_ip(req)
        with self.lock:
            fails, until = self.failures.get(ip, (0, 0))
            if time.time() < until:
                return json_response({"error": "Too many attempts. Wait %d s." % int(until - time.time())}, 429)
        pw = str(req.json().get("password", ""))
        if not secrets.compare_digest(pw, self.password):
            with self.lock:
                fails += 1
                self.failures[ip] = (fails, time.time() + 30 if fails >= 5 else 0)
            return json_response({"error": "Wrong password"}, 403)
        token = secrets.token_urlsafe(24)
        with self.lock:
            self.tokens.add(token)
            self.failures.pop(ip, None)
        return (200, "application/json; charset=utf-8", b'{"ok": true}',
                {"Set-Cookie": "console=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=28800" % token})


def make_handler(console):
    with open(os.path.join(WEB, "console.html"), encoding="utf-8") as f:
        page = f.read()
    pages = {"/api/overview": console.overview, "/api/server": console.server_page,
             "/api/network": console.network_page, "/api/tests": console.tests_page}

    def handler(req):
        if req.method == "GET" and req.path in ("/", "/index.html"):
            return html_response(page)
        if req.method == "POST" and req.path == "/api/login":
            return console.login(req)
        if not console.authed(req):
            return json_response({"error": "login required"}, 401)
        if req.method == "GET" and req.path == "/api/session":
            return json_response({"ok": True})
        if req.method == "POST" and req.path == "/api/logout":
            return (200, "application/json; charset=utf-8", b'{"ok": true}',
                    {"Set-Cookie": "console=; Path=/; Max-Age=0"})
        if req.method == "GET" and req.path in pages:
            return json_response(pages[req.path]())
        if req.method == "POST" and req.path in ("/api/block", "/api/unblock"):
            ip = str(req.json().get("ip", "")).strip()
            parts = ip.split(".")
            if len(parts) != 4 or not all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
                return json_response({"error": "Enter a valid IPv4 address"}, 400)
            try:
                console.rest("/exam/%s/%s" % (req.path.rsplit("/", 1)[1], ip), method="POST")
            except (urllib.error.URLError, OSError) as exc:
                return json_response({"error": "controller unreachable: %s" % exc}, 502)
            return json_response(console.overview())
        return response(404, "not found")
    return handler


def main():
    ap = argparse.ArgumentParser(description="Server Console: examiner website for the whole D1 system")
    ap.add_argument("--controller", default="http://127.0.0.1:8080")
    ap.add_argument("--listen", default="0.0.0.0")
    ap.add_argument("--http-port", type=int, default=9000)
    ap.add_argument("--students", default=os.path.join(BASE, "data", "students.json"))
    ap.add_argument("--results-dir", default=os.path.join(BASE, "results"))
    args = ap.parse_args()
    console = Console(args.controller, args.students, args.results_dir, console_password())
    httpd = HTTPServer(args.listen, args.http_port, make_handler(console))
    print("[CONSOLE] server console on http://localhost:%d  password: %s" % (args.http_port, console.password),
          flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.stop()


if __name__ == "__main__":
    main()
