import importlib
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from memory_mesh import service as service_module


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.service = service_module.MemoryService(Path(cls.temp.name) / "http.db")
        # Avoid server.py's normal startup initialization touching the user's DB.
        with patch.object(service_module, "MemoryService", return_value=cls.service):
            cls.module = importlib.import_module("memory_mesh.server")
        cls.previous = cls.module.service
        cls.module.service = cls.service
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), cls.module.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:" + str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.module.service = cls.previous
        cls.temp.cleanup()

    def request(self, path, body=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        request = Request(self.base + path, data=data, headers={"Content-Type": "application/json"})
        try:
            response = urlopen(request, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            return response.status, response.headers, response.read()

    def test_assets_have_types_and_are_not_cached(self):
        for route, mime in (("/", "text/html"), ("/static/app.js", "text/javascript"), ("/static/app.css", "text/css"), ("/favicon.svg", "image/svg+xml")):
            status, headers, body = self.request(route)
            self.assertEqual(status, 200)
            self.assertIn(mime, headers["Content-Type"])
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertTrue(body)

    def test_static_routes_are_allowlisted(self):
        for route in ("/static/../service.py", "/memory_mesh.db", "/static/missing.js"):
            self.assertEqual(self.request(route)[0], 404)

    def test_http_workflow_decision_and_library(self):
        status, _, raw = self.request("/api/incidents/run", {"tenant_id": "HTTP test", "user_id": "analyst", "description": "Repeated admin login failures from a suspicious IP."})
        self.assertEqual(status, 200)
        incident = json.loads(raw)
        status, _, raw = self.request("/api/incidents/" + incident["id"] + "/decision", {"tenant_id": "HTTP test", "user_id": "analyst", "decision": "accepted"})
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(raw)["memories"]), 3)
        status, _, raw = self.request("/api/memories?tenant_id=HTTP%20test&type=procedural")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["total"], 1)
        self.assertEqual(self.request("/api/incidents/" + incident["id"] + "?tenant_id=Globex")[0], 404)

    def test_bad_input_and_missing_ids_return_useful_errors(self):
        for path in ("/api/memories?limit=nope", "/api/memories?offset=-1", "/api/memories?type=invalid", "/api/dashboard?tenant_id="):
            self.assertEqual(self.request(path)[0], 400)
        self.assertEqual(self.request("/api/incidents/run", raw=b"{")[0], 400)
        self.assertEqual(self.request("/api/incidents/run", raw=b"[]")[0], 400)
        self.assertEqual(self.request("/api/incidents/INCIDENT_ID/decision", {"tenant_id": "Acme Corp", "user_id": "analyst", "decision": "accepted"})[0], 404)


if __name__ == "__main__":
    unittest.main()
