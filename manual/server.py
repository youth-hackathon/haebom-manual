"""HTTP. 앞단 서버(front.py)와 저작 서버(author.py)가 같이 쓴다. 파이썬 표준 라이브러리만 쓴다.

    GET  /health                 {"ok": true, "role", "manuals": [...], "fallback"}   앱은 1.5초 안에 200을 기다린다
    POST /v1/guide/next          앱의 안내 요청 (docs/interface.md). 매뉴얼이 먼저 답하고, 아니면 artemis 서버로
    GET  /v1/usage               artemis 서버의 것을 그대로 전한다
    GET  /manuals, /manuals/<id> 매뉴얼 목록과 내용
    GET  /sessions               최근 세션마다 매뉴얼이 답한 횟수와 artemis가 답한 횟수

저작 서버에만 있는 것은 author.py에 있다(`extra`로 끼운다).
"""

from __future__ import annotations

import json
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from guide import Guide
from store import Manuals


def manual_row(m: dict) -> dict:
    return {"id": m["id"], "task": m.get("task", ""), "aliases": m.get("aliases", []), "status": m.get("status", "draft"),
            "steps": len(m.get("steps", [])), "devices": sorted({s.get("device_model") or "unknown" for s in m.get("steps", [])})}


def make_server(host: str, port: int, role: str, guide: Guide, manuals: Manuals, extra=None) -> ThreadingHTTPServer:
    """`extra(method, path, body)` may answer a request first: it returns (code, object) or None."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # the guide prints one line per request itself
            pass

        def _send(self, code: int, obj) -> None:
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            if not raw:
                return {}
            return json.loads(raw.decode("utf-8"))

        def _route(self, method: str) -> None:
            url = urlparse(self.path)
            path = url.path.rstrip("/") or "/"
            try:
                body = self._body() if method in ("POST", "DELETE") else None
            except ValueError:
                return self._send(400, {"error": "the request body is not JSON"})
            try:
                if extra is not None:
                    answered = extra(method, path, body)
                    if answered is not None:
                        return self._send(*answered)
                if method == "GET" and path == "/health":
                    manuals.reload()
                    return self._send(200, {"ok": True, "role": role, "fallback": guide.artemis_url,
                                            "manuals": [manual_row(m) for m in manuals.all()]})
                if method == "GET" and path == "/manuals":
                    manuals.reload()
                    return self._send(200, [manual_row(m) for m in manuals.all()])
                if method == "GET" and path.startswith("/manuals/"):
                    manuals.reload()
                    m = manuals.get(path.split("/")[2])
                    return self._send(200, m) if m else self._send(404, {"error": "no such manual"})
                if method == "GET" and path == "/sessions":
                    return self._send(200, guide.recent())
                if method == "GET" and path == "/v1/usage":
                    with urllib.request.urlopen(guide.artemis_url + self.path, timeout=10) as r:
                        return self._send(r.status, json.loads(r.read().decode("utf-8")))
                if method == "POST" and path in ("/v1/guide/next", "/guide/next"):
                    if not isinstance(body, dict):
                        return self._send(400, {"error": "the request must be a JSON object"})
                    return self._send(*guide.next(body))
                return self._send(404, {"error": f"no {method} {path}"})
            except Exception as exc:  # noqa: BLE001 - one bad request must not take the server down
                print(f"[{role}] {method} {path} failed: {exc!r}", flush=True)
                return self._send(500, {"error": str(exc)})

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def do_DELETE(self):
            self._route("DELETE")

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server
