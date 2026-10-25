#!/usr/bin/env python3
import argparse
import csv
import json
import os
import signal
import sys
import time
import urllib.request

from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import info, setLogLevel
from mininet.net import Mininet
from mininet.node import Node, OVSSwitch, RemoteController
from mininet.topo import Topo

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(BASE, "app")
RESULTS = os.path.join(BASE, "results")
LOGS = os.path.join(BASE, "logs")
REST = "http://127.0.0.1:8080"

CORE_BW = 10
EXAM_MIN_RATE = 8


class ExamTopo(Topo):
    def build(self, core_bw=CORE_BW, qos=True):
        s1 = self.addSwitch("s1", protocols="OpenFlow13")
        s2 = self.addSwitch("s2", protocols="OpenFlow13")
        s3 = self.addSwitch("s3", protocols="OpenFlow13")

        host_link = dict(bw=100, delay="1ms")
        srv = self.addHost("srv", ip="10.0.0.1/24", mac="00:00:00:00:00:01")
        bg = self.addHost("bg", ip="10.0.0.99/24", mac="00:00:00:00:00:99")
        self.addLink(srv, s1, **host_link)
        self.addLink(bg, s1, **host_link)

        core_link = {} if qos else dict(bw=core_bw)
        self.addLink(s1, s2, **core_link)
        self.addLink(s1, s3, **core_link)

        for i in range(1, 7):
            sw = s2 if i <= 3 else s3
            h = self.addHost("st%d" % i, ip="10.0.0.%d/24" % (10 + i), mac="00:00:00:00:00:%02d" % (10 + i))
            self.addLink(h, sw, **host_link)
        intr = self.addHost("intr", ip="10.0.0.66/24", mac="00:00:00:00:00:66")
        sink = self.addHost("sink", ip="10.0.0.98/24", mac="00:00:00:00:00:98")
        self.addLink(intr, s2, **host_link)
        self.addLink(sink, s2, **host_link)


def configure_qos(net, core_bw):
    max_rate = core_bw * 1000000
    exam_min = EXAM_MIN_RATE * 1000000
    for link in net.links:
        n1, n2 = link.intf1.node, link.intf2.node
        if isinstance(n1, OVSSwitch) and isinstance(n2, OVSSwitch):
            for intf in (link.intf1, link.intf2):
                cmd = ("ovs-vsctl -- set port %s qos=@q -- --id=@q create qos type=linux-htb "
                       "other-config:max-rate=%d queues=0=@q0,1=@q1 "
                       "-- --id=@q0 create queue other-config:min-rate=1000000 other-config:max-rate=%d "
                       "-- --id=@q1 create queue other-config:min-rate=%d other-config:max-rate=%d"
                       % (intf.name, max_rate, max_rate, exam_min, max_rate))
                intf.node.cmd(cmd)
                info("*** QoS on %s: q0 best-effort, q1 exam (min %d Mbps), cap %d Mbps\n"
                     % (intf.name, EXAM_MIN_RATE, core_bw))


def clear_qos(net):
    for sw in net.switches:
        sw.cmd("ovs-vsctl --all destroy qos -- --all destroy queue")


def rest(path, method="GET"):
    try:
        req = urllib.request.Request(REST + path, method=method)
        with urllib.request.urlopen(req, timeout=3) as r:
            return json.loads(r.read().decode())
    except Exception as exc:
        return {"error": str(exc)}


def py(script, *args):
    return ["python3", "-u", os.path.join(APP, script)] + [str(a) for a in args]


class Demo:
    def __init__(self, net, args):
        self.net = net
        self.args = args
        self.tag = args.tag or time.strftime("run_%Y%m%d_%H%M%S")
        self.results = os.path.join(RESULTS, "results_%s.csv" % self.tag)
        self.metrics = os.path.join(RESULTS, "metrics_%s.csv" % self.tag)
        self.checks = []
        with open(os.path.join(BASE, "data", "students.json"), encoding="utf-8") as f:
            self.students = json.load(f)

    def check(self, name, ok, detail=""):
        self.checks.append((name, ok, detail))
        info("*** [%s] %s %s\n" % ("PASS" if ok else "FAIL", name, detail))

    def logfile(self, name):
        return open(os.path.join(LOGS, "%s_%s.log" % (self.tag, name)), "w")

    def client(self, host, sid, pin, *extra):
        return host.popen(py("exam_client.py", "--server", "10.0.0.1", "--id", sid, "--pin", pin,
                             "--metrics", self.metrics, "--label", self.tag, "--quiet", *extra),
                          stdout=self.logfile("%s_%s" % (host.name, sid)))

    def run(self):
        net = self.net
        srv, intr, bg, sink = net.get("srv"), net.get("intr"), net.get("bg"), net.get("sink")

        info("\n*** T1: connectivity (pingall)\n")
        loss = net.pingAll(timeout="1")
        self.check("T1 connectivity", loss == 0, "loss=%s%%" % loss)

        info("\n*** Starting exam server on srv\n")
        server = srv.popen(py("exam_server.py", "--duration", self.args.duration, "--results", self.results,
                              "--heartbeat", 10), stdout=self.logfile("server"))
        time.sleep(3)
        status = rest("/exam/status")
        self.check("T2 exam_start signal reached controller", status.get("exam_active") is True,
                   "allowed=%s" % status.get("allowed_ips"))

        iperf_srv = iperf_cli = None
        if self.args.congestion:
            info("\n*** Starting background UDP flood bg -> sink (%s Mbps)\n" % self.args.bg_rate)
            iperf_srv = sink.popen(["iperf", "-s", "-u", "-i", "5"], stdout=self.logfile("iperf_sink"))
            time.sleep(1)
            iperf_cli = bg.popen(["iperf", "-c", "10.0.0.98", "-u", "-b", "%sM" % self.args.bg_rate,
                                  "-t", str(self.args.duration + 30), "-i", "5"], stdout=self.logfile("iperf_bg"))
            time.sleep(2)

        info("\n*** T3: %d concurrent student clients\n" % len(self.students))
        procs = {}
        for idx, s in enumerate(self.students, 1):
            host = net.get("st%d" % idx)
            if idx == len(self.students) and self.args.slow_last:
                extra = ["--auto", "--think-min", "8", "--think-max", "10"]
            else:
                extra = ["--auto", "--think-min", self.args.think_min, "--think-max", self.args.think_max]
            procs[s["id"]] = self.client(host, s["id"], s["pin"], *extra)

        info("\n*** T4: unauthorized host intr (10.0.0.66) tries to join\n")
        p = intr.popen(py("exam_client.py", "--id", self.students[0]["id"], "--pin", self.students[0]["pin"],
                                  "--retries", "2", "--timeout", "3"),
                       stdout=self.logfile("intruder"))
        p.wait()
        status = rest("/exam/status")
        self.check("T4 unauthorized host blocked by SDN", "10.0.0.66" in status.get("blocked", {}),
                   str(status.get("blocked", {}).get("10.0.0.66", "")))

        for sid, proc in procs.items():
            proc.wait()

        rows = []
        if os.path.exists(self.results):
            with open(self.results) as f:
                rows = list(csv.DictReader(f))
        done = {r["student_id"]: r for r in rows}
        self.check("T5 all students received results", all(s["id"] in done for s in self.students),
                   "%d/%d" % (len(done), len(self.students)))
        if self.args.slow_last:
            last = self.students[-1]["id"]
            self.check("T6 exam timer enforced (slow student timed out)",
                       done.get(last, {}).get("status") == "timeout", done.get(last, {}).get("status", "missing"))

        info("\n*** T7: brute-force login from st3 (3 wrong PINs)\n")
        st3 = net.get("st3")
        for _ in range(3):
            st3.popen(py("exam_client.py", "--id", self.students[min(2, len(self.students) - 1)]["id"], "--pin", "wrong-password",
                         "--retries", "1", "--quiet")).wait()
        time.sleep(1)
        status = rest("/exam/status")
        self.check("T7 brute-force source blocked", "10.0.0.13" in status.get("blocked", {}),
                   str(status.get("blocked", {}).get("10.0.0.13", "")))

        stats = rest("/exam/stats")
        flows = stats.get("flows", {})
        exam_bytes = sum(v.get("exam_bytes", 0) for v in flows.values())
        self.check("T8 controller monitored exam traffic", exam_bytes > 0, "exam_bytes=%d" % exam_bytes)

        for p in (iperf_cli, iperf_srv):
            if p:
                p.terminate()
        server.send_signal(signal.SIGINT)
        server.wait(timeout=10)
        time.sleep(1)
        status = rest("/exam/status")
        self.check("T9 exam_end clears SDN exam policy", status.get("exam_active") is False)

        self.report(rows)

    def report(self, rows):
        info("\n================ RESULTS (%s) ================\n" % self.tag)
        for r in rows:
            info("  %-5s %-6s %-10s %2s/%-2s %s\n" % (r["student_id"], r["name"], r["ip"], r["score"], r["total"],
                                                    r["status"]))
        perf = {}
        if os.path.exists(self.metrics):
            with open(self.metrics) as f:
                m = [r for r in csv.DictReader(f) if r["label"] == self.tag and r["avg_rtt_ms"]]
            if m:
                rtts = [float(r["avg_rtt_ms"]) for r in m]
                jain = sum(rtts) ** 2 / (len(rtts) * sum(x * x for x in rtts))
                perf = {"avg_rtt_ms": round(sum(rtts) / len(rtts), 2),
                        "max_rtt_ms": round(max(float(r["max_rtt_ms"]) for r in m), 2),
                        "jain": round(jain, 3), "clients": len(m)}
                info("  avg app RTT %.2f ms | max %.2f ms | Jain fairness %.3f\n"
                     % (perf["avg_rtt_ms"], perf["max_rtt_ms"], jain))
        info("================ TEST SUMMARY ================\n")
        for name, ok, detail in self.checks:
            info("  [%s] %s %s\n" % ("PASS" if ok else "FAIL", name, detail))
        passed = sum(1 for c in self.checks if c[1])
        info("  %d/%d checks passed\n" % (passed, len(self.checks)))
        info("  results: %s\n  metrics: %s\n  logs:    %s/%s_*.log\n" % (self.results, self.metrics, LOGS, self.tag))
        summary = {"tag": self.tag, "time": time.strftime("%Y-%m-%d %H:%M:%S"), "congestion": bool(self.args.congestion),
                   "checks": [{"name": n, "ok": bool(ok), "detail": d} for n, ok, d in self.checks],
                   "passed": passed, "total": len(self.checks), "performance": perf,
                   "results": [{k: r[k] for k in ("student_id", "name", "ip", "score", "total", "status")} for r in rows]}
        for path in (os.path.join(RESULTS, "test_summary_%s.json" % self.tag), os.path.join(RESULTS, "test_summary_latest.json")):
            with open(path, "w") as f:
                json.dump(summary, f, indent=2)
            os.chmod(path, 0o644)


ROOT_IP = "10.0.0.200"
PORTAL_PORT = 8000
ADMIN_PORT = 9000


def write_topology(net):
    hosts, links = [], []
    for link in net.links:
        a, b = link.intf1.node, link.intf2.node
        bw = (link.intf1.params or {}).get("bw")
        links.append({"a": a.name, "b": b.name, "bw": bw})
    for h in net.hosts:
        sw = next((l["b"] if l["a"] == h.name else l["a"] for l in links if h.name in (l["a"], l["b"])), None)
        hosts.append({"name": h.name, "ip": h.IP(), "switch": sw})
    hosts.append({"name": "desktop", "ip": ROOT_IP, "switch": "s2"})
    data = {"switches": [s.name for s in net.switches], "hosts": hosts, "links": links,
            "core_bw": CORE_BW, "exam_min_rate": EXAM_MIN_RATE}
    path = os.path.join(LOGS, "topology.json")
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(path, 0o644)


def connect_root_namespace(net):
    root = Node("root", inNamespace=False)
    link = net.addLink(root, net.get("s2"))
    root.setIP(ROOT_IP + "/24", intf=link.intf1)
    return root


def start_portals(net, args):
    procs = []
    srv = net.get("srv")
    procs.append(srv.popen(py("exam_server.py", "--duration", args.duration,
                              "--results", os.path.join(RESULTS, "results_portal.csv")),
                           stdout=open(os.path.join(LOGS, "portal_server.log"), "w")))
    time.sleep(2)
    seats = ["st%d" % i for i in range(1, 7)] + ["intr"]
    for name in seats:
        h = net.get(name)
        procs.append(h.popen(py("student_portal.py", "--server", "10.0.0.1", "--http-port", PORTAL_PORT),
                             stdout=open(os.path.join(LOGS, "portal_%s.log" % name), "w")))
    admin = None
    if not args.no_admin:
        import subprocess
        admin = subprocess.Popen(py("admin_portal.py", "--http-port", ADMIN_PORT),
                                 stdout=open(os.path.join(LOGS, "portal_admin.log"), "w"), stderr=subprocess.STDOUT)
    time.sleep(2)
    with open(os.path.join(BASE, "data", "students.json"), encoding="utf-8") as f:
        roster = {s["ip"]: s for s in json.load(f)}
    info("\n================ PORTALS READY ================\n")
    pw = ""
    try:
        with open(os.path.join(LOGS, "console_password.txt")) as f:
            pw = f.read().strip()
    except OSError:
        pass
    info("  Server Console     : http://localhost:%d   password: %s\n" % (ADMIN_PORT, pw or "see logs/console_password.txt"))
    for name in seats:
        ip = net.get(name).IP()
        s = roster.get(ip)
        who = "login %s / %s" % (s["id"], s["pin"]) if s else "unassigned seat - will be blocked"
        info("  %-5s http://%s:%d   %s\n" % (name, ip, PORTAL_PORT, who))
    info("  Open these in Firefox on the Ubuntu desktop, or run ./scripts/run_public.sh for public links.\n")
    info("===============================================\n")
    return procs[0], procs[1:], admin


def stop_portals(server, portals, admin):
    for p in portals:
        p.terminate()
    if admin:
        admin.terminate()
    server.send_signal(signal.SIGINT)
    try:
        server.wait(timeout=10)
    except Exception:
        server.kill()


def main():
    ap = argparse.ArgumentParser(description="Online Examination SDN topology")
    ap.add_argument("--controller-ip", default="127.0.0.1")
    ap.add_argument("--controller-port", type=int, default=6653)
    ap.add_argument("--core-bw", type=int, default=CORE_BW)
    ap.add_argument("--no-qos", action="store_true")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--cli", action="store_true")
    ap.add_argument("--congestion", action="store_true")
    ap.add_argument("--bg-rate", type=float, default=15)
    ap.add_argument("--duration", type=int, default=None)
    ap.add_argument("--portal", action="store_true")
    ap.add_argument("--no-admin", action="store_true")
    ap.add_argument("--think-min", type=float, default=0.5)
    ap.add_argument("--think-max", type=float, default=1.5)
    ap.add_argument("--slow-last", action="store_true")
    ap.add_argument("--tag", default=None)
    args = ap.parse_args()
    if args.duration is None:
        args.duration = 600 if args.portal else 60

    setLogLevel("info")
    os.makedirs(RESULTS, exist_ok=True)
    os.makedirs(LOGS, exist_ok=True)
    qos = not args.no_qos
    topo = ExamTopo(core_bw=args.core_bw, qos=qos)
    net = Mininet(topo=topo, switch=OVSSwitch, link=TCLink, autoSetMacs=False,
                  controller=lambda name: RemoteController(name, ip=args.controller_ip, port=args.controller_port))
    root = connect_root_namespace(net)
    net.start()
    write_topology(net)
    for h in net.hosts:
        for k in ("all", "default", "lo"):
            h.cmd("sysctl -qw net.ipv6.conf.%s.disable_ipv6=1" % k)
    if qos:
        configure_qos(net, args.core_bw)
    info("*** Waiting for switches to connect to the controller\n")
    net.waitConnected(timeout=15)
    portal = None
    try:
        if args.demo:
            Demo(net, args).run()
        if args.portal:
            portal = start_portals(net, args)
            CLI(net)
        elif args.cli or not args.demo:
            info("\n*** CLI ready. Examples:\n"
                 "  srv python3 %s/exam_server.py --duration 120 &\n"
                 "  st1 python3 %s/exam_client.py --id Harsha --pin 'Harsha@123'\n"
                 "  st2 python3 %s/exam_client.py --id Uday --pin 'Uday@123' --auto\n" % (APP, APP, APP))
            CLI(net)
    finally:
        if portal:
            stop_portals(*portal)
        if qos:
            clear_qos(net)
        net.stop()
        try:
            root.terminate()
        except Exception:
            pass


if __name__ == "__main__":
    main()
