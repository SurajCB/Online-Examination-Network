import json
import socket
import threading
from urllib.parse import parse_qs, urlsplit

REASONS = {200: "OK", 302: "Found", 204: "No Content", 400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed",
           409: "Conflict", 413: "Payload Too Large", 500: "Internal Server Error", 502: "Bad Gateway"}
MAX_HEADER = 16 * 1024
MAX_BODY = 256 * 1024


class Request:
    def __init__(self, method, target, headers, body, client):
        parts = urlsplit(target)
        self.method = method
        self.path = parts.path
        self.query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        self.headers = headers
        self.body = body
        self.client = client

    def json(self):
        if not self.body:
            return {}
        try:
            data = json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}


def response(status, body=b"", content_type="text/plain; charset=utf-8"):
    if isinstance(body, str):
        body = body.encode("utf-8")
    return status, content_type, body


def json_response(obj, status=200):
    return response(status, json.dumps(obj), "application/json; charset=utf-8")


def html_response(text):
    return response(200, text, "text/html; charset=utf-8")


class HTTPServer:
    def __init__(self, host, port, handler, log=print):
        self.addr = (host, port)
        self.handler = handler
        self.log = log
        self.running = True

    def serve_forever(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(self.addr)
        self.sock.listen(64)
        self.sock.settimeout(1.0)
        while self.running:
            try:
                conn, client = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self.handle, args=(conn, client), daemon=True).start()

    def stop(self):
        self.running = False
        try:
            self.sock.close()
        except OSError:
            pass

    def read_request(self, conn, client):
        conn.settimeout(10)
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(4096)
            if not chunk:
                return None
            data += chunk
            if len(data) > MAX_HEADER:
                raise ValueError(413)
        head, _, rest = data.partition(b"\r\n\r\n")
        lines = head.decode("iso-8859-1").split("\r\n")
        try:
            method, target, _ = lines[0].split(" ", 2)
        except ValueError:
            raise ValueError(400)
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        length = int(headers.get("content-length", "0") or 0)
        if length > MAX_BODY:
            raise ValueError(413)
        body = rest
        while len(body) < length:
            chunk = conn.recv(min(65536, length - len(body)))
            if not chunk:
                break
            body += chunk
        return Request(method.upper(), target, headers, body[:length], client)

    def handle(self, conn, client):
        try:
            try:
                req = self.read_request(conn, client)
                if req is None:
                    return
                result = self.handler(req)
                status, ctype, body = result[:3]
                extra = result[3] if len(result) > 3 else {}
            except ValueError as exc:
                code = exc.args[0] if exc.args and isinstance(exc.args[0], int) else 400
                status, ctype, body = response(code, REASONS.get(code, "Error"))
                extra = {}
            except Exception as exc:
                self.log("[HTTP] handler error: %r" % exc)
                status, ctype, body = json_response({"error": "internal error"}, 500)
                extra = {}
            head = ("HTTP/1.1 %d %s\r\nContent-Type: %s\r\nContent-Length: %d\r\n"
                    "Cache-Control: no-store\r\nConnection: close\r\n%s\r\n"
                    % (status, REASONS.get(status, "OK"), ctype, len(body),
                       "".join("%s: %s\r\n" % kv for kv in extra.items())))
            conn.sendall(head.encode("iso-8859-1") + body)
        except OSError:
            pass
        finally:
            conn.close()
