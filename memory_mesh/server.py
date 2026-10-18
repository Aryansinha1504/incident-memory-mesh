"""Dependency-free HTTP API and static dashboard server."""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import HOST, MAX_REQUEST_BYTES, PORT
from .service import MemoryService
from .ui import ASSETS, BLOG_HTML, HTML

service = MemoryService()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def send_json(self, data: Any, status: int = 200) -> None:
        payload = json.dumps(data, default=str).encode()
        self.send_bytes(payload, "application/json; charset=utf-8", status)

    def send_bytes(self, payload: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError as exc:
            raise ValueError("Content-Length must be a valid integer") from exc
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise ValueError(f"request body must be {MAX_REQUEST_BYTES} bytes or fewer")
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("request body must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return payload

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/":
                self.send_bytes(HTML.encode(), "text/html; charset=utf-8")
                return
            if parsed.path == "/blog":
                self.send_bytes(BLOG_HTML.encode(), "text/html; charset=utf-8")
                return
            if parsed.path in ASSETS:
                path, content_type = ASSETS[parsed.path]
                self.send_bytes(path.read_bytes(), content_type)
                return
            query = parse_qs(parsed.query, keep_blank_values=True)
            tenant = query.get("tenant_id", ["Acme Corp"])[0]
            if parsed.path == "/api/dashboard":
                self.send_json(service.dashboard(tenant))
                return
            if parsed.path == "/api/memories":
                self.send_json(service.list_memories(
                    tenant_id=tenant, query=query.get("query", [""])[0],
                    type=query.get("type", ["all"])[0],
                    offset=int(query.get("offset", ["0"])[0]),
                    limit=int(query.get("limit", ["24"])[0]),
                ))
                return
            match = re.fullmatch(r"/api/incidents/([^/]+)", parsed.path)
            if match:
                self.send_json(service.get_incident_state(
                    tenant_id=tenant, incident_id=match.group(1),
                ))
                return
            self.send_json({"error": "not found"}, 404)
        except KeyError as exc:
            self.send_json({"error": exc.args[0]}, 404)
        except ValueError as exc:
            self.send_json({"error": str(exc)}, 400)
        except Exception as exc:
            self.send_json({"error": str(exc)}, 500)

    def do_POST(self) -> None:
        try:
            parsed = urlparse(self.path)
            decision_match = re.fullmatch(r"/api/incidents/([^/]+)/decision", parsed.path)
            if decision_match:
                data = self.body()
                for field in ("tenant_id", "user_id", "decision"):
                    if not data.get(field):
                        self.send_json({"error": f"{field} is required"}, 400)
                        return
                self.send_json(service.record_response_decision(
                    tenant_id=data["tenant_id"],
                    incident_id=decision_match.group(1),
                    user_id=data["user_id"],
                    decision=data["decision"],
                    note=data.get("note", ""),
                ))
                return
            if parsed.path == "/api/incidents/run":
                data = self.body()
                for field in ("tenant_id", "user_id", "description"):
                    if not data.get(field):
                        self.send_json({"error": f"{field} is required"}, 400)
                        return
                self.send_json(service.run_workflow(
                    tenant_id=data["tenant_id"], user_id=data["user_id"],
                    description=data["description"],
                ))
                return
            self.send_json({"error": "not found"}, 404)
        except KeyError as exc:
            self.send_json({"error": exc.args[0]}, 404)
        except ValueError as exc:
            self.send_json({"error": str(exc)}, 400)
        except Exception as exc:
            self.send_json({"error": str(exc)}, 500)


def serve() -> None:
    print(f"Memory Mesh running at http://{HOST}:{PORT}")
    print(f"Persistent database: {service.path}")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped. Saved incidents and memories are unchanged.")
    finally:
        server.server_close()
