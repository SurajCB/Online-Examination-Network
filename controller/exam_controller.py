import csv
import json
import os
import time
from collections import deque

from ryu.app.wsgi import ControllerBase, WSGIApplication, route
from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, DEAD_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.lib import hub
from ryu.lib.packet import arp, ether_types, ethernet, in_proto, ipv4, packet, tcp, udp
from ryu.ofproto import ofproto_v1_3
from webob import Response

EXAM_PORT = int(os.environ.get("EXAM_PORT", 5000))
SIGNAL_IP = "10.0.0.254"
SIGNAL_MAC = "00:00:00:00:00:fe"
SIGNAL_PORT = 6666
TOKEN = os.environ.get("EXAM_SDN_TOKEN", "exam-sdn-secret")

MONITOR_INTERVAL = float(os.environ.get("EXAM_MONITOR_INTERVAL", 5))
BLOCK_THRESHOLD = int(os.environ.get("EXAM_BLOCK_THRESHOLD", 3))
CONGESTION_MBPS = float(os.environ.get("EXAM_CONGESTION_MBPS", 5))
PRIORITIZE = os.environ.get("EXAM_PRIORITIZE", "1") == "1"
LOG_DIR = os.environ.get("EXAM_LOG_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs"))

PRIO_SIGNAL = 400
PRIO_BLOCK = 300
PRIO_EXAM = 100
PRIO_OTHER = 10
PRIO_L2 = 5
PRIO_MISS = 0

COOKIE_EXAM = 0xE1
COOKIE_OTHER = 0xE2
COOKIE_BLOCK = 0xE3
COOKIE_SIGNAL = 0xE4
COOKIE_L2 = 0xE5
COOKIE_ALL = 0xFFFFFFFFFFFFFFFF

QUEUE_BEST_EFFORT = 0
QUEUE_EXAM = 1

APP_NAME = "exam_app"


class ExamController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    _CONTEXTS = {"wsgi": WSGIApplication}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mac_to_port = {}
        self.datapaths = {}
        self.exam_active = False
        self.server_ip = None
        self.exam_port = EXAM_PORT
        self.allowed = set()
        self.blocked = {}
        self.active_students = {}
        self.submitted = {}
        self.run_id = None
        self.event_log = deque(maxlen=300)
        self.stats = {}
        self.prev = {}
        self.expired = {}
        self.port_stats = {}
        self.flow_tables = {}
        self.congested = {}
        self.last_congestion_log = {}
        os.makedirs(LOG_DIR, exist_ok=True)
        self.stats_file = os.path.join(LOG_DIR, "controller_stats.csv")
        if not os.path.exists(self.stats_file):
            with open(self.stats_file, "w", newline="") as f:
                csv.writer(f).writerow(["time", "dpid", "exam_active", "exam_flows", "other_flows",
                                        "exam_bytes", "other_bytes", "exam_mbps", "other_mbps", "congested"])
        kwargs["wsgi"].register(ExamRestController, {APP_NAME: self})
        hub.spawn(self._monitor)
        self.event("controller_start", prioritize=PRIORITIZE, congestion_mbps=CONGESTION_MBPS)

    def event(self, kind, **detail):
        entry = {"time": time.strftime("%H:%M:%S"), "event": kind}
        entry.update(detail)
        self.event_log.append(entry)
        self.logger.info("[%s] %s", kind.upper(), json.dumps(detail))

    def add_flow(self, dp, priority, match, actions, cookie, idle=0, hard=0, buffer_id=None):
        ofp = dp.ofproto
        parser = dp.ofproto_parser
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)] if actions else []
        kwargs = dict(datapath=dp, priority=priority, match=match, instructions=inst, cookie=cookie,
                      idle_timeout=idle, hard_timeout=hard, flags=ofp.OFPFF_SEND_FLOW_REM)
        if buffer_id is not None and buffer_id != ofp.OFP_NO_BUFFER:
            kwargs["buffer_id"] = buffer_id
        dp.send_msg(parser.OFPFlowMod(**kwargs))

    def delete_flows(self, dp, cookie, match=None):
        ofp = dp.ofproto
        parser = dp.ofproto_parser
        dp.send_msg(parser.OFPFlowMod(datapath=dp, cookie=cookie, cookie_mask=COOKIE_ALL, table_id=ofp.OFPTT_ALL,
                                      command=ofp.OFPFC_DELETE, out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY,
                                      match=match or parser.OFPMatch()))

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        dp = ev.msg.datapath
        ofp = dp.ofproto
        parser = dp.ofproto_parser
        self.add_flow(dp, PRIO_MISS, parser.OFPMatch(),
                      [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)], 0)
        to_ctrl = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)]
        self.add_flow(dp, PRIO_SIGNAL, parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP, ip_proto=in_proto.IPPROTO_UDP,
                                                       ipv4_dst=SIGNAL_IP, udp_dst=SIGNAL_PORT), to_ctrl, COOKIE_SIGNAL)
        self.add_flow(dp, PRIO_SIGNAL, parser.OFPMatch(eth_type=ether_types.ETH_TYPE_ARP, arp_tpa=SIGNAL_IP),
                      to_ctrl, COOKIE_SIGNAL)
        self.add_flow(dp, PRIO_SIGNAL, parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IPV6), [], COOKIE_SIGNAL)
        for ip in self.blocked:
            self.install_block(dp, ip)
        self.event("switch_connected", dpid=dp.id)

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def state_change_handler(self, ev):
        dp = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            self.datapaths[dp.id] = dp
        elif ev.state == DEAD_DISPATCHER and dp.id in self.datapaths:
            del self.datapaths[dp.id]
            self.event("switch_disconnected", dpid=dp.id)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg
        dp = msg.datapath
        ofp = dp.ofproto
        parser = dp.ofproto_parser
        in_port = msg.match["in_port"]
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None or eth.ethertype in (ether_types.ETH_TYPE_LLDP, ether_types.ETH_TYPE_IPV6):
            return
        self.mac_to_port.setdefault(dp.id, {})[eth.src] = in_port

        arp_pkt = pkt.get_protocol(arp.arp)
        if arp_pkt and arp_pkt.opcode == arp.ARP_REQUEST and arp_pkt.dst_ip == SIGNAL_IP:
            self.reply_signal_arp(dp, in_port, eth, arp_pkt)
            return

        ip_pkt = pkt.get_protocol(ipv4.ipv4)
        tcp_pkt = pkt.get_protocol(tcp.tcp)
        udp_pkt = pkt.get_protocol(udp.udp)

        if ip_pkt and udp_pkt and ip_pkt.dst == SIGNAL_IP and udp_pkt.dst_port == SIGNAL_PORT:
            payload = pkt.protocols[-1]
            if isinstance(payload, (bytes, bytearray)):
                self.handle_signal(bytes(payload), ip_pkt.src)
            return

        if ip_pkt and tcp_pkt and self.is_exam_request(ip_pkt, tcp_pkt):
            if ip_pkt.src in self.blocked or (self.allowed and ip_pkt.src not in self.allowed):
                if ip_pkt.src not in self.blocked:
                    self.block_ip(ip_pkt.src, "not on exam whitelist")
                return

        dst_port = self.mac_to_port[dp.id].get(eth.dst, ofp.OFPP_FLOOD)
        exam_flow = bool(ip_pkt and tcp_pkt and self.exam_port in (tcp_pkt.src_port, tcp_pkt.dst_port))
        actions = []
        if dst_port != ofp.OFPP_FLOOD and ip_pkt and PRIORITIZE and self.exam_active:
            actions.append(parser.OFPActionSetQueue(QUEUE_EXAM if exam_flow else QUEUE_BEST_EFFORT))
        actions.append(parser.OFPActionOutput(dst_port))

        if dst_port != ofp.OFPP_FLOOD:
            if ip_pkt:
                fields = dict(eth_type=ether_types.ETH_TYPE_IP, ipv4_src=ip_pkt.src, ipv4_dst=ip_pkt.dst,
                              ip_proto=ip_pkt.proto)
                if tcp_pkt:
                    fields.update(tcp_src=tcp_pkt.src_port, tcp_dst=tcp_pkt.dst_port)
                elif udp_pkt:
                    fields.update(udp_src=udp_pkt.src_port, udp_dst=udp_pkt.dst_port)
                if exam_flow:
                    self.add_flow(dp, PRIO_EXAM, parser.OFPMatch(**fields), actions, COOKIE_EXAM, idle=60)
                else:
                    self.add_flow(dp, PRIO_OTHER, parser.OFPMatch(**fields), actions, COOKIE_OTHER, idle=30)
            else:
                match = parser.OFPMatch(in_port=in_port, eth_src=eth.src, eth_dst=eth.dst, eth_type=eth.ethertype)
                self.add_flow(dp, PRIO_L2, match, actions, COOKIE_L2, idle=30)

        data = msg.data if msg.buffer_id == ofp.OFP_NO_BUFFER else None
        dp.send_msg(parser.OFPPacketOut(datapath=dp, buffer_id=msg.buffer_id, in_port=in_port,
                                        actions=actions, data=data))

    def is_exam_request(self, ip_pkt, tcp_pkt):
        return (self.exam_active and tcp_pkt.dst_port == self.exam_port
                and (self.server_ip is None or ip_pkt.dst == self.server_ip))

    def reply_signal_arp(self, dp, in_port, eth, arp_pkt):
        parser = dp.ofproto_parser
        ofp = dp.ofproto
        reply = packet.Packet()
        reply.add_protocol(ethernet.ethernet(dst=eth.src, src=SIGNAL_MAC, ethertype=ether_types.ETH_TYPE_ARP))
        reply.add_protocol(arp.arp(opcode=arp.ARP_REPLY, src_mac=SIGNAL_MAC, src_ip=SIGNAL_IP,
                                   dst_mac=arp_pkt.src_mac, dst_ip=arp_pkt.src_ip))
        reply.serialize()
        dp.send_msg(parser.OFPPacketOut(datapath=dp, buffer_id=ofp.OFP_NO_BUFFER, in_port=ofp.OFPP_CONTROLLER,
                                        actions=[parser.OFPActionOutput(in_port)], data=reply.data))

    def handle_signal(self, raw, src_ip):
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self.event("signal_rejected", src=src_ip, reason="malformed")
            return
        if data.get("token") != TOKEN:
            self.event("signal_rejected", src=src_ip, reason="bad token")
            return
        kind = data.get("event")
        if kind != "exam_start" and self.server_ip and src_ip != self.server_ip:
            self.event("signal_rejected", src=src_ip, reason="not exam server")
            return
        if kind == "exam_start":
            self.start_exam(data.get("server_ip", src_ip), int(data.get("port", EXAM_PORT)),
                            data.get("allowed_ips", []), data.get("run"))
        elif kind == "exam_end":
            self.end_exam()
        elif kind == "student_login":
            self.active_students[data.get("ip")] = data.get("student_id")
            self.event("student_login", student=data.get("student_id"), ip=data.get("ip"))
        elif kind == "student_submit":
            self.active_students.pop(data.get("ip"), None)
            self.submitted[data.get("student_id")] = {"score": data.get("score"), "total": data.get("total"),
                                                     "status": data.get("status")}
            self.event("student_submit", student=data.get("student_id"), score=data.get("score"),
                       status=data.get("status"))
        elif kind == "auth_fail":
            ip = data.get("ip")
            count = int(data.get("count", 1))
            self.event("auth_fail", ip=ip, count=count)
            if count >= BLOCK_THRESHOLD and ip not in self.blocked:
                self.block_ip(ip, "%d failed logins" % count)
        else:
            self.event("signal_unknown", kind=kind)

    def start_exam(self, server_ip, port, allowed_ips, run=None):
        new_run = run is not None and run != self.run_id
        if new_run:
            self.run_id = run
            self.submitted = {}
            self.active_students = {}
            for ip in list(self.blocked):
                self.unblock_ip(ip)
        changed = (new_run or not self.exam_active or self.server_ip != server_ip or self.exam_port != port
                   or self.allowed != set(allowed_ips))
        self.exam_active = True
        self.server_ip = server_ip
        self.exam_port = port
        self.allowed = set(allowed_ips)
        if changed:
            for dp in self.datapaths.values():
                self.delete_flows(dp, COOKIE_EXAM)
                self.delete_flows(dp, COOKIE_OTHER)
            self.event("exam_start", server=server_ip, port=port, allowed=sorted(self.allowed))

    def end_exam(self):
        if not self.exam_active:
            return
        self.exam_active = False
        self.allowed = set()
        for ip in list(self.blocked):
            self.unblock_ip(ip)
        for dp in self.datapaths.values():
            self.delete_flows(dp, COOKIE_EXAM)
            self.delete_flows(dp, COOKIE_OTHER)
        self.event("exam_end", submitted=len(self.submitted))

    def install_block(self, dp, ip):
        parser = dp.ofproto_parser
        fields = dict(eth_type=ether_types.ETH_TYPE_IP, ipv4_src=ip)
        if self.server_ip:
            fields["ipv4_dst"] = self.server_ip
        self.add_flow(dp, PRIO_BLOCK, parser.OFPMatch(**fields), [], COOKIE_BLOCK)

    def block_ip(self, ip, reason):
        self.blocked[ip] = {"reason": reason, "since": time.strftime("%H:%M:%S")}
        for dp in self.datapaths.values():
            self.install_block(dp, ip)
        self.event("blocked", ip=ip, reason=reason)

    def unblock_ip(self, ip):
        if self.blocked.pop(ip, None) is None:
            return False
        for dp in self.datapaths.values():
            parser = dp.ofproto_parser
            self.delete_flows(dp, COOKIE_BLOCK, parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP, ipv4_src=ip))
        self.event("unblocked", ip=ip)
        return True

    def _monitor(self):
        while True:
            for dp in list(self.datapaths.values()):
                parser = dp.ofproto_parser
                dp.send_msg(parser.OFPFlowStatsRequest(dp))
                dp.send_msg(parser.OFPPortStatsRequest(dp, 0, dp.ofproto.OFPP_ANY))
            hub.sleep(MONITOR_INTERVAL)

    @set_ev_cls(ofp_event.EventOFPFlowRemoved, MAIN_DISPATCHER)
    def flow_removed_handler(self, ev):
        msg = ev.msg
        acc = self.expired.setdefault(msg.datapath.id, {})
        acc[msg.cookie] = acc.get(msg.cookie, 0) + msg.byte_count

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def flow_stats_reply_handler(self, ev):
        dpid = ev.msg.datapath.id
        now = time.time()
        expired = self.expired.get(dpid, {})
        exam_bytes = expired.get(COOKIE_EXAM, 0)
        other_bytes = expired.get(COOKIE_OTHER, 0)
        exam_flows = other_flows = 0
        self.flow_tables[dpid] = sorted((self.describe_flow(stat) for stat in ev.msg.body),
                                        key=lambda f: (-f["priority"], f["kind"]))
        for stat in ev.msg.body:
            if stat.cookie == COOKIE_EXAM:
                exam_bytes += stat.byte_count
                exam_flows += 1
            elif stat.cookie == COOKIE_OTHER:
                other_bytes += stat.byte_count
                other_flows += 1
        prev = self.prev.get(dpid)
        exam_mbps = other_mbps = 0.0
        if prev:
            dt = max(now - prev[0], 1e-6)
            exam_mbps = max(0, exam_bytes - prev[1]) * 8 / dt / 1e6
            other_mbps = max(0, other_bytes - prev[2]) * 8 / dt / 1e6
        self.prev[dpid] = (now, exam_bytes, other_bytes)
        congested = self.exam_active and other_mbps > CONGESTION_MBPS
        if congested and now - self.last_congestion_log.get(dpid, 0) > 10:
            self.last_congestion_log[dpid] = now
            self.event("congestion", dpid=dpid, other_mbps=round(other_mbps, 2), exam_mbps=round(exam_mbps, 3),
                       action="exam traffic on priority queue" if PRIORITIZE else "none (baseline)")
        self.congested[dpid] = congested
        self.stats[dpid] = {"exam_flows": exam_flows, "other_flows": other_flows, "exam_bytes": exam_bytes,
                            "other_bytes": other_bytes, "exam_mbps": round(exam_mbps, 4),
                            "other_mbps": round(other_mbps, 4), "congested": congested}
        with open(self.stats_file, "a", newline="") as f:
            csv.writer(f).writerow([time.strftime("%H:%M:%S"), dpid, int(self.exam_active), exam_flows, other_flows,
                                    exam_bytes, other_bytes, round(exam_mbps, 4), round(other_mbps, 4),
                                    int(congested)])

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def port_stats_reply_handler(self, ev):
        dpid = ev.msg.datapath.id
        self.port_stats[dpid] = {
            stat.port_no: {"rx_bytes": stat.rx_bytes, "tx_bytes": stat.tx_bytes,
                           "rx_dropped": stat.rx_dropped, "tx_dropped": stat.tx_dropped}
            for stat in ev.msg.body if stat.port_no < 0xFFFFFF00
        }

    @staticmethod
    def describe_flow(stat):
        kinds = {COOKIE_EXAM: "exam", COOKIE_OTHER: "other", COOKIE_BLOCK: "block", COOKIE_SIGNAL: "signal",
                 COOKIE_L2: "l2", 0: "table-miss"}
        match = {}
        for key, value in getattr(stat.match, "_fields2", []):
            match[key] = value if isinstance(value, (int, str)) else str(value)
        actions = []
        for inst in stat.instructions:
            for act in getattr(inst, "actions", []):
                name = type(act).__name__
                if name == "OFPActionOutput":
                    port = act.port
                    actions.append("CONTROLLER" if port == 0xfffffffd else "FLOOD" if port == 0xfffffffb
                                   else "output:%d" % port)
                elif name == "OFPActionSetQueue":
                    actions.append("set_queue:%d" % act.queue_id)
                else:
                    actions.append(name.replace("OFPAction", "").lower())
        return {"priority": stat.priority, "kind": kinds.get(stat.cookie, hex(stat.cookie)), "match": match,
                "actions": actions or ["drop"], "packets": stat.packet_count, "bytes": stat.byte_count,
                "idle_timeout": stat.idle_timeout}

    def status(self):
        return {"exam_active": self.exam_active, "server_ip": self.server_ip, "exam_port": self.exam_port,
                "prioritize": PRIORITIZE, "allowed_ips": sorted(self.allowed), "blocked": self.blocked,
                "active_students": self.active_students, "submitted": self.submitted,
                "switches": sorted(self.datapaths.keys())}


def json_response(obj, status=200):
    return Response(status=status, content_type="application/json", charset="utf-8",
                    body=json.dumps(obj, indent=2, default=str).encode("utf-8"))


class ExamRestController(ControllerBase):
    def __init__(self, req, link, data, **config):
        super().__init__(req, link, data, **config)
        self.app = data[APP_NAME]

    @route("exam", "/exam/status", methods=["GET"])
    def get_status(self, req, **kwargs):
        return json_response(self.app.status())

    @route("exam", "/exam/stats", methods=["GET"])
    def get_stats(self, req, **kwargs):
        return json_response({"flows": self.app.stats, "ports": self.app.port_stats})

    @route("exam", "/exam/flows", methods=["GET"])
    def get_flows(self, req, **kwargs):
        return json_response(self.app.flow_tables)

    @route("exam", "/exam/events", methods=["GET"])
    def get_events(self, req, **kwargs):
        return json_response(list(self.app.event_log))

    @route("exam", "/exam/block/{ip}", methods=["POST"])
    def post_block(self, req, ip, **kwargs):
        self.app.block_ip(ip, "manual (REST)")
        return json_response({"blocked": ip})

    @route("exam", "/exam/unblock/{ip}", methods=["POST"])
    def post_unblock(self, req, ip, **kwargs):
        return json_response({"unblocked": ip, "was_blocked": self.app.unblock_ip(ip)})

    @route("exam", "/exam/start", methods=["POST"])
    def post_start(self, req, **kwargs):
        body = req.json if req.body else {}
        self.app.start_exam(body.get("server_ip", "10.0.0.1"), int(body.get("port", EXAM_PORT)),
                            body.get("allowed_ips", []))
        return json_response(self.app.status())

    @route("exam", "/exam/end", methods=["POST"])
    def post_end(self, req, **kwargs):
        self.app.end_exam()
        return json_response(self.app.status())
