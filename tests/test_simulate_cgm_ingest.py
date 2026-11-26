"""
FILE: tests/test_simulate_cgm_ingest.py
RUOLO: N2 — verifica che il simulatore invii l'header X-Device-Key e il corpo corretto
       e gestisca 401/403/202 con codici di uscita diversi da zero.
"""

import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


def _load_simulator():
    path = Path(__file__).resolve().parent.parent / "scripts" / "simulate_cgm_patient.py"
    spec = importlib.util.spec_from_file_location("simulate_cgm_patient", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sim = _load_simulator()

RECORDS = [{"step_index": 0, "current_glucose": 6.5, "current_insulin": 1.0, "current_cho": 0.0}]


class _RecordingHandler(BaseHTTPRequestHandler):
    status_code = 202
    response_body = b'{"status": "queued"}'
    captured = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        type(self).captured.append(
            {"path": self.path, "headers": dict(self.headers), "body": raw}
        )
        self.send_response(type(self).status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(type(self).response_body)))
        self.end_headers()
        self.wfile.write(type(self).response_body)

    def log_message(self, *args):  # pragma: no cover - silenzia il server di test
        pass


@pytest.fixture()
def fake_server():
    _RecordingHandler.captured = []
    _RecordingHandler.status_code = 202
    _RecordingHandler.response_body = b'{"status": "queued"}'
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RecordingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, _RecordingHandler
    finally:
        server.shutdown()
        server.server_close()


def test_build_ingest_url_maps_predict_to_internal_endpoint():
    assert sim._build_ingest_url("http://127.0.0.1:8080/predict") == "http://127.0.0.1:8080/api/cgm/ingest"
    assert sim._build_ingest_url("https://localhost/api/cgm/ingest") == "https://localhost/api/cgm/ingest"
    assert sim._build_ingest_url("http://127.0.0.1:8080") == "http://127.0.0.1:8080/api/cgm/ingest"


def test_ingest_sends_device_key_header_and_body(fake_server, capsys):
    server, handler = fake_server
    url = f"http://127.0.0.1:{server.server_port}/api/cgm/ingest"
    rc = sim._ingest_records(url, "591", RECORDS, "device-key-secret-value", delay=0.0, insecure=False)
    assert rc == 0
    assert len(handler.captured) == 1
    req = handler.captured[0]
    assert req["path"] == "/api/cgm/ingest"
    assert req["headers"]["X-Device-Key"] == "device-key-secret-value"
    body = json.loads(req["body"].decode("utf-8"))
    assert body["patient_code"] == "591"
    assert body["glucose_mmol"] == 6.5
    assert "recorded_at" in body
    assert "Ingestione completata" in capsys.readouterr().out


def test_ingest_401_returns_nonzero(fake_server, capsys):
    server, handler = fake_server
    handler.status_code = 401
    handler.response_body = b'{"detail": "Dispositivo non registrato o revocato."}'
    url = f"http://127.0.0.1:{server.server_port}/api/cgm/ingest"
    rc = sim._ingest_records(url, "591", RECORDS, "wrong-key", delay=0.0, insecure=False)
    assert rc != 0
    err = capsys.readouterr().err
    assert "401" in err
    assert "device_cli" in err


def test_ingest_403_prints_port_forward_hint(fake_server, capsys):
    server, handler = fake_server
    handler.status_code = 403
    handler.response_body = b'{"detail": "Endpoint non accessibile dall\'esterno."}'
    url = f"http://127.0.0.1:{server.server_port}/api/cgm/ingest"
    rc = sim._ingest_records(url, "591", RECORDS, "any-key", delay=0.0, insecure=False)
    assert rc != 0
    err = capsys.readouterr().err
    assert "403" in err
    assert "port-forward" in err
