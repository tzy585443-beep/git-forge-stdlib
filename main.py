"""GitHub-compatible forge entrypoint.

Binds an ephemeral port, POSTs it to $MAKEFUN_ENV_URL/register, and polls
GET $MAKEFUN_ENV_URL/done until the suite answers {"done": true}. The HTTP
server speaks HTTP/1.1 keep-alive. This revision serves git smart HTTP;
REST, GraphQL, and MCP are attached beside it.
"""

from __future__ import annotations

import base64
import gzip
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gitstore  # noqa: E402
from store import get_store  # noqa: E402

REQUEST_TIMEOUT = 30
RETRY_INITIAL = 0.25
RETRY_MAX = 2.0
MAX_BODY = 128 * 1024 * 1024

JSON = "application/json; charset=utf-8"
NOT_FOUND = b'{"message":"Not Found"}'
UNAUTH = b'{"message":"Requires authentication"}'
BAD = b'{"message":"Problems parsing JSON"}'

REASONS = {
    200: "OK",
    201: "Created",
    202: "Accepted",
    204: "No Content",
    400: "Bad Request",
    401: "Unauthorized",
    404: "Not Found",
    405: "Method Not Allowed",
    409: "Conflict",
    413: "Payload Too Large",
    422: "Unprocessable Entity",
    500: "Internal Server Error",
}


def _retry_delay(attempt: int) -> float:
    return min(RETRY_MAX, RETRY_INITIAL * (2 ** min(attempt, 8)))


def _request(method: str, url: str, body: dict | None = None) -> tuple[int, bytes]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


def register(port: int) -> None:
    env = os.environ.get("MAKEFUN_ENV_URL", "").rstrip("/")
    url = f"{env}/register"
    attempt = 0
    while True:
        try:
            status, payload = _request("POST", url, {"port": port})
            if status in (200, 409):
                return
            if status in (400, 405):
                raise RuntimeError(f"register answered HTTP {status}: {payload.decode('utf-8', 'replace')}")
        except RuntimeError:
            raise
        except Exception:
            pass
        time.sleep(_retry_delay(attempt))
        attempt += 1


def wait_for_done() -> None:
    env = os.environ.get("MAKEFUN_ENV_URL", "").rstrip("/")
    url = f"{env}/done"
    attempt = 0
    while True:
        try:
            status, payload = _request("GET", url)
            if status == 200:
                body = json.loads(payload)
                if isinstance(body, dict) and body.get("done") is True:
                    return
        except Exception:
            pass
        time.sleep(_retry_delay(attempt))
        attempt += 1


def authorized(header: str) -> bool:
    expected = os.environ.get("MAKEFUN_SHOP_TOKEN", "")
    if not expected or not header:
        return False
    scheme, _, rest = header.partition(" ")
    scheme = scheme.lower()
    got = ""
    if scheme == "bearer":
        got = rest.strip()
    elif scheme == "basic":
        try:
            decoded = base64.b64decode(rest.strip()).decode()
        except Exception:
            return False
        got = decoded.split(":", 1)[1] if ":" in decoded else ""
    else:
        return False
    if len(got) != len(expected):
        return False
    diff = 0
    for left, right in zip(got.encode(), expected.encode()):
        diff |= left ^ right
    return diff == 0


def dispatch(method: str, path: str, query: str, body: bytes, header_map: dict[str, str]) -> tuple:
    """Return (status, body, content_type, extra_headers)."""
    if path == "/healthz" and method in ("GET", "HEAD"):
        return 200, b'{"ok":true}', JSON, ()
    if not authorized(header_map.get("authorization", "")):
        return 401, UNAUTH, JSON, ()
    if gitstore.parse_git_path(path) is not None:
        protocol = header_map.get("git-protocol", "")
        result = gitstore.handle_git(method, path, query, body, protocol)
        if result is not None:
            return result
    return 404, NOT_FOUND, JSON, ()


class ForgeServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 256
    allow_reuse_address = True
    # Keep worker threads from being counted against a slow shutdown.
    block_on_close = False

    def handle_error(self, request, client_address):
        return


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # One buffered write per response. Unbuffered writes dominate the read workload.
    rbufsize = 65536
    wbufsize = 65536
    disable_nagle_algorithm = True
    timeout = 300

    def log_message(self, fmt, *args):
        return

    def handle(self):
        try:
            self._serve()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, TimeoutError, OSError):
            return

    def _serve(self) -> None:
        rfile = self.rfile
        wfile = self.wfile
        while True:
            line = rfile.readline(65537)
            if not line or line == b"\r\n":
                if not line:
                    return
                continue
            if len(line) > 65536:
                self._write(wfile, 400, BAD, JSON, (), False, True)
                return
            parts = line.strip().split()
            if len(parts) != 3:
                return
            method_b, target, version = parts
            try:
                method = method_b.decode("ascii")
                target_s = target.decode("latin-1")
            except UnicodeError:
                return
            headers: dict[str, str] = {}
            while True:
                raw = rfile.readline(65537)
                if raw in (b"\r\n", b"\n", b""):
                    if not raw:
                        return
                    break
                name, sep, value = raw.partition(b":")
                if not sep:
                    continue
                headers[name.strip().lower().decode("latin-1", "replace")] = value.strip().decode("latin-1", "replace")
            if headers.get("expect", "").lower() == "100-continue":
                wfile.write(b"HTTP/1.1 100 Continue\r\n\r\n")
                wfile.flush()
            try:
                body = _read_body(rfile, headers)
            except ValueError:
                self._write(wfile, 413, b'{"message":"Payload Too Large"}', JSON, (), False, True)
                return
            encoding = headers.get("content-encoding", "")
            if encoding == "gzip" and body:
                try:
                    body = gzip.decompress(body)
                except OSError:
                    self._write(wfile, 400, BAD, JSON, (), False, True)
                    return
            mark = target_s.find("?")
            if mark >= 0:
                path, query = target_s[:mark], target_s[mark + 1 :]
            else:
                path, query = target_s, ""
            if len(path) > 1 and path.endswith("/"):
                path = path[:-1]
            close = _should_close(version, headers)
            head = method == "HEAD"
            try:
                status, payload, content_type, extra = dispatch(method, path, query, body, headers)
            except Exception:
                status, payload, content_type, extra = 500, b'{"message":"Internal Server Error"}', JSON, ()
            self._write(wfile, status, payload, content_type, extra, head, close)
            if close:
                return

    def _write(self, wfile, status: int, body: bytes, content_type: str, extra, head: bool, close: bool) -> None:
        reason = REASONS.get(status, "OK")
        payload = b"" if head or status == 204 else body
        length = 0 if status == 204 else len(body)
        conn = "close" if close else "keep-alive"
        lines = [
            f"HTTP/1.1 {status} {reason}",
            f"Content-Type: {content_type}",
            f"Content-Length: {length}",
            "X-GitHub-Request-Id: gf",
            f"Connection: {conn}",
        ]
        for key, value in extra:
            if value:
                lines.append(f"{key}: {value}")
        raw = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1", "replace")
        wfile.write(raw)
        if payload:
            wfile.write(payload)
        wfile.flush()


def _should_close(version: bytes, headers: dict[str, str]) -> bool:
    connection = headers.get("connection", "").lower()
    if "close" in connection:
        return True
    if version == b"HTTP/1.0":
        return "keep-alive" not in connection
    return False


def _read_body(rfile, headers: dict[str, str]) -> bytes:
    if headers.get("transfer-encoding", "").lower() == "chunked":
        return _read_chunked(rfile)
    length = int(headers.get("content-length") or "0")
    if length < 0 or length > MAX_BODY:
        raise ValueError("body too large")
    if length == 0:
        return b""
    data = rfile.read(length)
    if len(data) != length:
        raise ValueError("short body")
    return data


def _read_chunked(rfile) -> bytes:
    out = bytearray()
    while True:
        line = rfile.readline(64)
        if not line:
            break
        size = int(line.split(b";", 1)[0], 16)
        if size == 0:
            while True:
                trailer = rfile.readline(65537)
                if trailer in (b"\r\n", b"\n", b""):
                    break
            break
        if size < 0 or len(out) + size > MAX_BODY:
            raise ValueError("body too large")
        chunk = rfile.read(size)
        if len(chunk) != size:
            raise ValueError("short chunk")
        out += chunk
        rfile.read(2)
    return bytes(out)


def build_server() -> ForgeServer:
    return ForgeServer(("0.0.0.0", 0), Handler)


def main() -> None:
    env = os.environ.get("MAKEFUN_ENV_URL", "").strip()
    if not env:
        sys.stderr.write("[entry] MAKEFUN_ENV_URL is not set: there is no env to register with\n")
        sys.exit(1)
    # Fix login and trial time before the first request.
    get_store()
    server = build_server()
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
    thread.start()
    port = server.server_address[1]
    sys.stdout.write(f"[entry] listening on port {port}\n")
    sys.stdout.flush()
    try:
        register(port)
        sys.stdout.write("[entry] registered; waiting for the env to say done\n")
        sys.stdout.flush()
        wait_for_done()
    finally:
        server.shutdown()
        server.server_close()
    sys.stdout.write("[entry] done\n")
    sys.stdout.flush()


if __name__ == "__main__":
    try:
        main()
    except Exception as err:
        sys.stderr.write(f"[entry] {err}\n")
        sys.exit(1)
