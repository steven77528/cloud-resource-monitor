from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import pytest

from cloud_monitor.providers.common import ProviderError, request_json
from cloud_monitor.providers.relay import RelayCollector


@pytest.fixture
def relay_server(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    received = []
    state = {"redirect": False}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append({"path": self.path, "token": self.headers.get("Authorization"), "payload": payload})
            if state["redirect"]:
                self.send_response(307)
                self.send_header("Location", "/redirected")
                self.end_headers()
                return
            identity = {key: value for key, value in payload.items() if key != "request"}
            raw = json.dumps({**identity, "status": "ok", "signature_verified": None,
                "response": {"code": 0, "data": {"account_id": "finance-main", "currency": "CNY",
                    "cash_balance": 100000000, "present_balance": 0, "credit_line": 0,
                    "unpaid_money": 0}}}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1/monitor/collect", received, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def make_collector(tmp_path, url):
    token_file = tmp_path / "relay.token"
    token_file.write_text("offline-http-relay-test-token-001", encoding="ascii")
    return RelayCollector({"id": "merchant", "provider": "qiniu", "relay": {
        "url": url, "token_file": str(token_file)}})


def test_real_http_relay_transport(tmp_path, relay_server):
    url, received, _ = relay_server
    metrics = make_collector(tmp_path, url).collect("balance", {"operation": "balance-main", "collector": {}},
                                                  datetime.now(timezone.utc))
    assert {metric.metric: metric.value for metric in metrics}["qiniu_cash_balance"] == 1
    assert len(received) == 1
    assert received[0]["path"] == "/v1/monitor/collect"
    assert received[0]["token"] == "Bearer offline-http-relay-test-token-001"
    payload = received[0]["payload"]
    assert payload["version"] == 2 and payload["account"] == "merchant"
    assert payload["operation"] == "balance-main" and payload["kind"] == "balance"
    assert payload["request"]["url"].startswith("https://api.qiniu.com/")


def test_http_relay_does_not_follow_redirect(tmp_path, relay_server):
    url, received, state = relay_server
    state["redirect"] = True
    with pytest.raises(ProviderError, match="relay_request_failed"):
        make_collector(tmp_path, url).collect("balance", {"operation": "balance-main", "collector": {}},
                                              datetime.now(timezone.utc))
    assert len(received) == 1


def test_generic_provider_transport_still_requires_https(relay_server):
    url, received, _ = relay_server
    with pytest.raises(ProviderError, match="invalid_endpoint"):
        request_json("POST", url, body=b"{}")
    assert received == []


@pytest.mark.parametrize("url", ["http://relay.example.test:18443/v1/monitor/collect",
    "http://127.0.0.1/v1/monitor/collect", "http://127.0.0.1:18443/admin"])
def test_http_transport_opt_in_is_limited_to_relay_endpoint(url):
    with pytest.raises(ProviderError, match="invalid_endpoint"):
        request_json("POST", url, body=b"{}", allow_http_relay=True)
