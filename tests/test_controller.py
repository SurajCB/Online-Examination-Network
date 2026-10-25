import json
import os
import sys
import tempfile
import unittest

os.environ.setdefault("EXAM_LOG_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "controller"))

from ryu.lib.packet import arp, ethernet, ether_types, in_proto, ipv4, packet, tcp, udp
from ryu.ofproto import ofproto_v1_3, ofproto_v1_3_parser

import exam_controller as C

SERVER_MAC = "00:00:00:00:00:01"
ST1_MAC = "00:00:00:00:00:11"
INTR_MAC = "00:00:00:00:00:66"


class FakeWSGI:
    def register(self, *args, **kwargs):
        pass


class FakeDP:
    def __init__(self, dpid):
        self.id = dpid
        self.ofproto = ofproto_v1_3
        self.ofproto_parser = ofproto_v1_3_parser
        self.sent = []

    def send_msg(self, msg):
        self.sent.append(msg)

    def flow_mods(self, cookie=None):
        return [m for m in self.sent if isinstance(m, ofproto_v1_3_parser.OFPFlowMod)
                and m.command == ofproto_v1_3.OFPFC_ADD and (cookie is None or m.cookie == cookie)]

    def packet_outs(self):
        return [m for m in self.sent if isinstance(m, ofproto_v1_3_parser.OFPPacketOut)]


class Ev:
    def __init__(self, msg):
        self.msg = msg


class Msg:
    def __init__(self, dp, in_port, data):
        self.datapath = dp
        self.match = {"in_port": in_port}
        self.data = data
        self.buffer_id = ofproto_v1_3.OFP_NO_BUFFER


def build(*protocols):
    p = packet.Packet()
    for proto in protocols:
        p.add_protocol(proto)
    p.serialize()
    return bytes(p.data)


def tcp_packet(src_mac, dst_mac, src_ip, dst_ip, sport, dport):
    return build(ethernet.ethernet(dst=dst_mac, src=src_mac, ethertype=ether_types.ETH_TYPE_IP),
                 ipv4.ipv4(src=src_ip, dst=dst_ip, proto=in_proto.IPPROTO_TCP),
                 tcp.tcp(src_port=sport, dst_port=dport, bits=tcp.TCP_SYN))


def signal_packet(payload, src_ip="10.0.0.1"):
    return build(ethernet.ethernet(dst=C.SIGNAL_MAC, src=SERVER_MAC, ethertype=ether_types.ETH_TYPE_IP),
                 ipv4.ipv4(src=src_ip, dst=C.SIGNAL_IP, proto=in_proto.IPPROTO_UDP),
                 udp.udp(src_port=40000, dst_port=C.SIGNAL_PORT),
                 json.dumps(payload).encode())


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.app = C.ExamController(wsgi=FakeWSGI())
        self.dp = FakeDP(1)
        self.app.datapaths[1] = self.dp
        self.app.mac_to_port[1] = {SERVER_MAC: 1, ST1_MAC: 3, INTR_MAC: 4}

    def packet_in(self, data, in_port):
        self.app.packet_in_handler(Ev(Msg(self.dp, in_port, data)))

    def start_exam(self):
        self.packet_in(signal_packet({"event": "exam_start", "token": C.TOKEN, "server_ip": "10.0.0.1",
                                      "port": 5000, "allowed_ips": ["10.0.0.11"]}), 1)

    def test_signal_arp_is_answered(self):
        data = build(ethernet.ethernet(dst="ff:ff:ff:ff:ff:ff", src=SERVER_MAC, ethertype=ether_types.ETH_TYPE_ARP),
                     arp.arp(opcode=arp.ARP_REQUEST, src_mac=SERVER_MAC, src_ip="10.0.0.1",
                             dst_mac="00:00:00:00:00:00", dst_ip=C.SIGNAL_IP))
        self.packet_in(data, 1)
        outs = self.dp.packet_outs()
        self.assertEqual(len(outs), 1)
        reply = packet.Packet(outs[0].data).get_protocol(arp.arp)
        self.assertEqual(reply.opcode, arp.ARP_REPLY)
        self.assertEqual(reply.src_mac, C.SIGNAL_MAC)

    def test_exam_start_signal_activates_policy(self):
        self.start_exam()
        self.assertTrue(self.app.exam_active)
        self.assertEqual(self.app.allowed, {"10.0.0.11"})
        self.assertEqual(self.dp.packet_outs(), [])

    def test_bad_token_ignored(self):
        self.packet_in(signal_packet({"event": "exam_start", "token": "wrong"}), 1)
        self.assertFalse(self.app.exam_active)

    def test_spoofed_signal_from_non_server_ignored(self):
        self.start_exam()
        self.packet_in(signal_packet({"event": "exam_end", "token": C.TOKEN}, src_ip="10.0.0.66"), 4)
        self.assertTrue(self.app.exam_active)

    def test_allowed_student_gets_priority_exam_flow(self):
        self.start_exam()
        self.packet_in(tcp_packet(ST1_MAC, SERVER_MAC, "10.0.0.11", "10.0.0.1", 40001, 5000), 3)
        flows = self.dp.flow_mods(C.COOKIE_EXAM)
        self.assertEqual(len(flows), 1)
        self.assertEqual(flows[0].priority, C.PRIO_EXAM)
        actions = flows[0].instructions[0].actions
        self.assertIsInstance(actions[0], ofproto_v1_3_parser.OFPActionSetQueue)
        self.assertEqual(actions[0].queue_id, C.QUEUE_EXAM)
        self.assertEqual(actions[-1].port, 1)

    def test_non_exam_traffic_best_effort(self):
        self.start_exam()
        self.packet_in(tcp_packet(ST1_MAC, SERVER_MAC, "10.0.0.11", "10.0.0.1", 40001, 80), 3)
        flows = self.dp.flow_mods(C.COOKIE_OTHER)
        self.assertEqual(len(flows), 1)
        self.assertEqual(flows[0].instructions[0].actions[0].queue_id, C.QUEUE_BEST_EFFORT)

    def test_unauthorized_host_blocked(self):
        self.start_exam()
        self.packet_in(tcp_packet(INTR_MAC, SERVER_MAC, "10.0.0.66", "10.0.0.1", 40002, 5000), 4)
        self.assertIn("10.0.0.66", self.app.blocked)
        blocks = self.dp.flow_mods(C.COOKIE_BLOCK)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].priority, C.PRIO_BLOCK)
        self.assertEqual(blocks[0].instructions, [])
        self.assertEqual(self.dp.packet_outs(), [])

    def test_auth_fail_threshold_blocks(self):
        self.start_exam()
        for n in (1, 2):
            self.packet_in(signal_packet({"event": "auth_fail", "token": C.TOKEN, "ip": "10.0.0.11", "count": n}), 1)
        self.assertNotIn("10.0.0.11", self.app.blocked)
        self.packet_in(signal_packet({"event": "auth_fail", "token": C.TOKEN, "ip": "10.0.0.11", "count": 3}), 1)
        self.assertIn("10.0.0.11", self.app.blocked)

    def test_exam_end_unblocks(self):
        self.start_exam()
        self.app.block_ip("10.0.0.66", "test")
        self.packet_in(signal_packet({"event": "exam_end", "token": C.TOKEN}), 1)
        self.assertFalse(self.app.exam_active)
        self.assertEqual(self.app.blocked, {})

    def test_new_exam_run_resets_state(self):
        def start(run):
            self.packet_in(signal_packet({"event": "exam_start", "token": C.TOKEN, "server_ip": "10.0.0.1",
                                          "port": 5000, "allowed_ips": ["10.0.0.11"], "run": run}), 1)
        start("run-1")
        self.packet_in(signal_packet({"event": "student_submit", "token": C.TOKEN, "student_id": "Harsha",
                                      "ip": "10.0.0.11", "score": 10, "total": 10, "status": "completed"}), 1)
        self.app.block_ip("10.0.0.66", "test")
        start("run-1")
        self.assertIn("Harsha", self.app.submitted)
        start("run-2")
        self.assertEqual(self.app.submitted, {})
        self.assertEqual(self.app.blocked, {})
        self.assertTrue(self.app.exam_active)

    def test_no_exam_mode_no_queue(self):
        self.packet_in(tcp_packet(ST1_MAC, SERVER_MAC, "10.0.0.11", "10.0.0.1", 40001, 5000), 3)
        actions = self.dp.flow_mods(C.COOKIE_EXAM)[0].instructions[0].actions
        self.assertEqual(len(actions), 1)

    def test_flow_table_snapshot(self):
        class M:
            pass
        msg = M()
        msg.datapath = self.dp
        p = ofproto_v1_3_parser
        msg.body = [
            p.OFPFlowStats(cookie=C.COOKIE_EXAM, priority=C.PRIO_EXAM, byte_count=500, packet_count=5, idle_timeout=60,
                           match=p.OFPMatch(eth_type=0x0800, ipv4_src="10.0.0.11", ip_proto=6, tcp_dst=5000),
                           instructions=[p.OFPInstructionActions(ofproto_v1_3.OFPIT_APPLY_ACTIONS,
                                                                 [p.OFPActionSetQueue(1), p.OFPActionOutput(1)])]),
            p.OFPFlowStats(cookie=C.COOKIE_BLOCK, priority=C.PRIO_BLOCK, byte_count=0, packet_count=0, idle_timeout=0,
                           match=p.OFPMatch(eth_type=0x0800, ipv4_src="10.0.0.66"), instructions=[]),
        ]
        self.app.flow_stats_reply_handler(Ev(msg))
        table = self.app.flow_tables[1]
        self.assertEqual(table[0]["kind"], "block")
        self.assertEqual(table[0]["actions"], ["drop"])
        self.assertEqual(table[1]["actions"], ["set_queue:1", "output:1"])
        self.assertEqual(table[1]["match"]["ipv4_src"], "10.0.0.11")


if __name__ == "__main__":
    unittest.main(verbosity=2)
