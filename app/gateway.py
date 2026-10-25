import argparse
import html
import http.client
import json
import os
import sys
import threading
from http.cookies import SimpleCookie

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from minihttp import HTTPServer, html_response, json_response

APP = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(APP)
WEB = os.path.join(APP, "web")
PORTAL_PORT = 8000


class Seat:
    def __init__(self, name, host, port, student=None, ip=None):
        self.name = name
        self.host = host
        self.port = port
        self.student = student
        self.ip = ip or host


def build_seats(args):
    with open(args.students, encoding="utf-8") as f:
        roster = json.load(f)
    seats = []
    overrides = {}
    if args.seat_map:
        for item in args.seat_map.split(","):
            name, target = item.split("=")
            host, port = target.rsplit(":", 1)
            overrides[name.strip()] = (host.strip(), int(port))
    for i, s in enumerate(roster, 1):
        name = "st%d" % i
        host, port = overrides.get(name, (s["ip"], PORTAL_PORT))
        seats.append(Seat(name, host, port, s, s["ip"]))
    host, port = overrides.get("intr", ("10.0.0.66", PORTAL_PORT))
    seats.append(Seat("intr", host, port, None, "10.0.0.66"))
    return seats


def redirect(location, **extra):
    headers = {"Location": location}
    headers.update({k.replace("_", "-"): v for k, v in extra.items()})
    return 302, "text/plain; charset=utf-8", b"", headers


def forward(seat, method, path, body=b"", timeout=20):
    conn = http.client.HTTPConnection(seat.host, seat.port, timeout=timeout)
    try:
        headers = {"Content-Type": "application/json"} if body else {}
        conn.request(method, path, body=body or None, headers=headers)
        r = conn.getresponse()
        return r.status, r.getheader("Content-Type", "text/plain"), r.read()
    finally:
        conn.close()


def seat_status(seat):
    try:
        status, _, body = forward(seat, "GET", "/api/state", timeout=1.5)
        state = json.loads(body.decode())
        if state.get("stage") == "login":
            return "free"
        return "busy"
    except (OSError, ValueError, http.client.HTTPException):
        return "offline"


def all_status(seats):
    result = {}

    def check(seat):
        result[seat.name] = seat_status(seat)
    threads = [threading.Thread(target=check, args=(s,)) for s in seats]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return result


def make_handler(seats, title):
    by_name = {s.name: s for s in seats}
    with open(os.path.join(WEB, "seats.html"), encoding="utf-8") as f:
        picker = f.read()

    def chosen(req):
        cookie = SimpleCookie()
        try:
            cookie.load(req.headers.get("cookie", ""))
        except Exception:
            return None
        morsel = cookie.get("seat")
        return by_name.get(morsel.value) if morsel else None

    def handler(req):
        if req.path == "/seats/api":
            states = all_status(seats)
            return json_response({"title": title, "seats": [
                {"name": s.name, "ip": s.ip, "state": states[s.name],
                 "student": {"id": s.student["id"], "name": s.student["name"]}
                 if s.student else None} for s in seats]})
        if req.path == "/seats/pick":
            name = req.query.get("seat", "")
            if name == "auto":
                states = all_status(seats)
                free = [s.name for s in seats if s.student and states[s.name] == "free"]
                if not free:
                    return redirect("/seats?full=1")
                name = free[0]
            if name not in by_name:
                return redirect("/seats")
            return redirect("/", Set_Cookie="seat=%s; Path=/; Max-Age=7200; SameSite=Lax" % name)
        if req.path == "/seats/leave":
            return redirect("/seats", Set_Cookie="seat=; Path=/; Max-Age=0; SameSite=Lax")
        if req.path == "/seats":
            return html_response(picker)
        seat = chosen(req)
        if not seat:
            if req.path.startswith("/api/"):
                return json_response({"stage": "login", "seat": "-", "server": "-", "error": "Pick a seat first.",
                                      "student": None, "question": None, "result": None}, 409)
            return redirect("/seats")
        try:
            status, ctype, body = forward(seat, req.method, req.path, req.body)
        except (OSError, http.client.HTTPException) as exc:
            if req.path.startswith("/api/"):
                return json_response({"stage": "login", "seat": seat.ip, "server": "-",
                                      "error": "Seat %s is offline (%s). Is run_portal.sh running?" % (seat.name, exc),
                                      "student": None, "question": None, "result": None}, 502)
            return html_response("<p style='font-family:sans-serif;padding:24px'>Seat %s is offline. "
                                 "<a href='/seats/leave'>Pick another seat</a></p>" % html.escape(seat.name))
        if ctype.startswith("text/html"):
            who = ("for %s" % seat.student["name"]) if seat.student else "unregistered seat"
            bar = ("<div style=\"background:#1f5fae;color:#fff;font:13px system-ui,sans-serif;padding:8px 16px;"
                   "display:flex;gap:12px;justify-content:center;flex-wrap:wrap\"><span>Public gateway · seat "
                   "<b>%s</b> (%s) · %s</span><a href=\"/seats/leave\" style=\"color:#fff\">Change seat</a>"
                   "</div>" % (seat.name, seat.ip, html.escape(who)))
            hint = "<script>window.SEAT_ID=%s;</script>" % json.dumps(seat.student["id"] if seat.student else "")
            body = body.decode("utf-8").replace("<body>", "<body>" + bar + hint, 1).encode("utf-8")
        return status, ctype, body
    return handler


def main():
    ap = argparse.ArgumentParser(description="Public gateway: one URL for every seat portal")
    ap.add_argument("--listen", default="0.0.0.0")
    ap.add_argument("--http-port", type=int, default=7000)
    ap.add_argument("--students", default=os.path.join(BASE, "data", "students.json"))
    ap.add_argument("--seat-map", default=None)
    args = ap.parse_args()
    seats = build_seats(args)
    with open(os.path.join(BASE, "data", "questions.json"), encoding="utf-8") as f:
        title = json.load(f).get("title", "Online Examination")
    httpd = HTTPServer(args.listen, args.http_port, make_handler(seats, title))
    print("[GATEWAY] http://localhost:%d -> %s" % (args.http_port, ", ".join(
        "%s=%s:%d" % (s.name, s.host, s.port) for s in seats)), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.stop()


if __name__ == "__main__":
    main()
