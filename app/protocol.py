import json
import socket
import struct
import time

HEADER = struct.Struct("!I")
MAX_MESSAGE = 64 * 1024

LOGIN = "LOGIN"
LOGIN_OK = "LOGIN_OK"
LOGIN_FAIL = "LOGIN_FAIL"
QUESTION = "QUESTION"
ANSWER = "ANSWER"
SUBMIT = "SUBMIT"
RESULT = "RESULT"
TIME_UP = "TIME_UP"
PING = "PING"
PONG = "PONG"
ERROR = "ERROR"


class ProtocolError(Exception):
    pass


class ConnectionClosed(Exception):
    pass


def make(msg_type, **fields):
    msg = {"type": msg_type, "t": time.time()}
    msg.update(fields)
    return msg


def encode(msg):
    body = json.dumps(msg, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_MESSAGE:
        raise ProtocolError("message too large")
    return HEADER.pack(len(body)) + body


def send_msg(sock, msg):
    sock.sendall(encode(msg))


def recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionClosed("peer closed connection")
        buf.extend(chunk)
    return bytes(buf)


def recv_msg(sock):
    (length,) = HEADER.unpack(recv_exact(sock, HEADER.size))
    if length == 0 or length > MAX_MESSAGE:
        raise ProtocolError("invalid frame length %d" % length)
    try:
        msg = json.loads(recv_exact(sock, length).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("malformed message: %s" % exc)
    if not isinstance(msg, dict) or "type" not in msg:
        raise ProtocolError("message missing type")
    return msg


def tune_socket(sock):
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
