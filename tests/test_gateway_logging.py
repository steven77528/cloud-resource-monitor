from contextlib import contextmanager
from http.client import HTTPConnection
import json
import logging
import re
import threading
import uuid

import pytest
import yaml

from cloud_monitor import notification_gateway as gateway
from cloud_monitor.notifiers import NotificationError


LOGGER = "cloud_monitor.notification_gateway"
TOKEN = "synthetic-secret-token-for-gateway-tests"
PHONE = "13800009999"
BODY = "private-notification-body-for-tests"
PRIVATE_ERROR = "private-supplier-error-for-tests"
EVENT_ID = "6984bece-0566-48f9-89fd-4829aba51d99"


@pytest.fixture
def logging_config(tmp_path, monkeypatch, caplog):
    config = {
        "version": 1,
        "api": {"token": TOKEN, "host": "127.0.0.1", "port": 8081},
        "routes": {"P0": "sms", "P1": "dingtalk", "P2": "email"},
        "notifications": {
            "sms": {"enabled": False, "provider": "http", "url": "https://example.test/send",
                    "extra_body": {"phone": PHONE}},
            "dingtalk": {"enabled": True, "webhook": "https://example.test/robot?token=" + TOKEN},
            "email": {"enabled": True, "host": "example.test", "from_address": "sender@example.test",
                      "to": ["private-recipient@example.test"]},
        },
    }
    path = tmp_path / "notifications.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")

    class FakeNotifier:
        error = None

        def __init__(self, config):
            pass

        def send(self, channel, event):
            if self.error:
                raise self.error

        def send_sms_test(self, content):
            if self.error:
                raise self.error
            return EVENT_ID

    monkeypatch.setattr(gateway, "Notifier", FakeNotifier)
    caplog.set_level(logging.INFO, logger=LOGGER)
    return path, config, FakeNotifier


@contextmanager
def running_gateway(path):
    server = gateway.make_server(path, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def request(port, path="/notify", *, payload=None, method="POST", token=TOKEN, raw_body=None):
    if payload is None:
        payload = {"event": {"id": EVENT_ID, "severity": "P1", "summary": BODY}}
    body = json.dumps(payload) if raw_body is None else raw_body
    connection = HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path, body=body, headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "X-Request-ID": TOKEN,
        })
        response = connection.getresponse()
        result = json.loads(response.read())
        return response.status, result
    finally:
        connection.close()


def gateway_records(caplog, kind="gateway_request"):
    records = [record for record in caplog.records
               if record.name == LOGGER and record.getMessage().startswith(kind + " ")]
    for record in records:
        assert record.exc_info is None
        assert record.stack_info is None
    return [dict(part.split("=", 1) for part in record.getMessage().split()[1:]) for record in records]


def assert_safe_logs(caplog):
    for private in (TOKEN, PHONE, BODY, PRIVATE_ERROR, "private-recipient@example.test", "example.test"):
        assert private not in caplog.text


def test_success_and_skip_are_correlated_without_logging_payload(logging_config, caplog):
    path, _, _ = logging_config
    with running_gateway(path) as port:
        assert request(port, "/notify?token=" + TOKEN)[0] == 200
        assert request(port, payload={"event": {"id": EVENT_ID, "severity": "P0", "summary": BODY}})[0] == 200
    entries = gateway_records(caplog)
    assert len(entries) == 2
    assert [entry["result"] for entry in entries] == ["accepted", "skipped"]
    assert [entry["channel"] for entry in entries] == ["dingtalk", "sms"]
    assert entries[1]["reason"] == "channel_disabled"
    for entry in entries:
        assert entry["endpoint"] == "notify"
        assert entry["status"] == "200"
        assert entry["event_id"] == EVENT_ID
        assert re.fullmatch(r"[0-9a-f]{32}", entry["request_id"])
        assert uuid.UUID(hex=entry["request_id"]).version == 4
    assert entries[0]["request_id"] != entries[1]["request_id"]
    assert_safe_logs(caplog)


@pytest.mark.parametrize("event_id,expected", [
    (TOKEN, "-"),
    (TOKEN + "\nstatus=200", "-"),
    ("6984BECE-0566-48F9-89FD-4829ABA51D99", EVENT_ID),
])
def test_only_canonical_event_identifiers_enter_logs(logging_config, caplog, event_id, expected):
    path, _, _ = logging_config
    with running_gateway(path) as port:
        assert request(port, payload={"event": {"id": event_id, "severity": "P1", "summary": BODY}})[0] == 200
    assert gateway_records(caplog)[0]["event_id"] == expected
    assert_safe_logs(caplog)


@pytest.mark.parametrize("options,status,endpoint,reason", [
    ({"token": "wrong-" + TOKEN}, 401, "notify", "authentication_failed"),
    ({"raw_body": "{" + BODY}, 400, "notify", None),
    ({"path": "/" + TOKEN + "?phone=" + PHONE}, 404, "other", None),
])
def test_rejections_log_safe_metadata(logging_config, caplog, options, status, endpoint, reason):
    path, _, _ = logging_config
    with running_gateway(path) as port:
        assert request(port, **options)[0] == status
    entries = gateway_records(caplog)
    assert len(entries) == 1
    assert entries[0]["status"] == str(status)
    assert entries[0]["endpoint"] == endpoint
    assert entries[0]["result"] == "rejected"
    assert entries[0]["event_id"] == "-"
    if reason:
        assert entries[0]["reason"] == reason
    assert re.fullmatch(r"[a-z_]+", entries[0]["reason"])
    assert_safe_logs(caplog)


@pytest.mark.parametrize("broken,reason", [("missing", "config_file_missing"), ("yaml", "config_yaml_invalid")])
def test_configuration_loss_after_startup_has_actionable_safe_log(logging_config, caplog, broken, reason):
    path, _, _ = logging_config
    with running_gateway(path) as port:
        if broken == "missing":
            path.unlink()
        else:
            path.write_text("api: [" + TOKEN, encoding="utf-8")
        status, response = request(port)
    assert status == 503
    assert response == {"error": gateway.CONFIG_ERROR}
    entry = gateway_records(caplog)[0]
    assert entry["reason"] == reason
    assert entry["field"] == "config"
    assert entry["result"] == "rejected"
    assert str(path) not in caplog.text
    assert_safe_logs(caplog)


@pytest.mark.parametrize("error_type", [NotificationError, RuntimeError])
def test_provider_errors_keep_delivery_context_and_hide_exception(logging_config, caplog, error_type):
    path, _, notifier = logging_config
    notifier.error = error_type(PRIVATE_ERROR + " token=" + TOKEN + " phone=" + PHONE)
    with running_gateway(path) as port:
        assert request(port)[0] == 502
    entry = gateway_records(caplog)[0]
    assert entry["reason"] == "delivery_failed"
    assert entry["status"] == "502"
    assert entry["channel"] == "dingtalk"
    assert entry["event_id"] == EVENT_ID
    assert_safe_logs(caplog)


@pytest.mark.parametrize("channel", ["sms", "dingtalk", "email"])
def test_manual_test_logs_normalized_endpoint_without_content(logging_config, caplog, channel):
    path, _, _ = logging_config
    with running_gateway(path) as port:
        assert request(port, "/notifications/" + channel + "/test?token=" + TOKEN,
                       payload={"content": BODY})[0] == 200
    entry = gateway_records(caplog)[0]
    assert entry["endpoint"] == channel + "_test"
    assert entry["channel"] == channel
    assert entry["result"] == "accepted"
    assert_safe_logs(caplog)


def test_health_polling_produces_no_access_logs(logging_config, caplog):
    path, _, _ = logging_config
    with running_gateway(path) as port:
        for _ in range(3):
            assert request(port, "/healthz", method="GET")[0] == 200
    assert gateway_records(caplog) == []
    assert_safe_logs(caplog)


def test_service_lifecycle_is_logged_without_configuration(logging_config, monkeypatch, caplog):
    path, _, _ = logging_config
    closed = []

    class FakeServer:
        server_address = ("127.0.0.1", 8081)

        def serve_forever(self):
            pass

        def server_close(self):
            closed.append(True)

    monkeypatch.setattr(gateway, "make_server", lambda _: FakeServer())
    monkeypatch.setattr(gateway.signal, "signal", lambda *args: None)
    assert gateway.main(["--config", str(path), "serve"]) == 0
    messages = [record.getMessage() for record in caplog.records if record.name == LOGGER]
    assert "gateway_started port=8081" in messages
    assert "gateway_stopped" in messages
    assert closed == [True]
    assert_safe_logs(caplog)


@pytest.mark.parametrize("broken,reason,field", [
    ("missing", "config_file_missing", "config"),
    ("yaml", "config_yaml_invalid", "config"),
    ("token", "api_token_invalid", "api.token"),
])
def test_check_reports_fixed_configuration_reason(logging_config, caplog, capsys, broken, reason, field):
    path, config, _ = logging_config
    if broken == "missing":
        path.unlink()
    elif broken == "yaml":
        path.write_text("api: [" + TOKEN, encoding="utf-8")
    else:
        config["api"]["token"] = PHONE
        path.write_text(yaml.safe_dump(config), encoding="utf-8")
    with pytest.raises(gateway.GatewayConfigError) as caught:
        gateway.load_gateway_config(path)
    assert caught.value.reason == reason
    assert caught.value.field == field
    assert str(caught.value) == gateway.CONFIG_ERROR
    assert gateway.main(["--config", str(path), "check"]) == 2
    entries = gateway_records(caplog, "gateway_startup_failed")
    assert len(entries) == 1
    assert entries[0]["reason"] == reason
    assert entries[0]["field"] == field
    output = capsys.readouterr()
    assert TOKEN not in output.out + output.err
    assert str(path) not in caplog.text
    assert_safe_logs(caplog)


@pytest.mark.parametrize("untrusted_exception", [False, True])
def test_channel_validation_logs_only_known_schema_fields(logging_config, caplog, monkeypatch, untrusted_exception):
    path, config, _ = logging_config
    if untrusted_exception:
        def invalid_validation(*args):
            raise ValueError("notifications.email." + TOKEN + " " + PRIVATE_ERROR)

        monkeypatch.setattr(gateway, "validate_notifications", invalid_validation)
        expected_field = "notifications.email"
    else:
        config["notifications"]["email"]["port"] = TOKEN
        path.write_text(yaml.safe_dump(config), encoding="utf-8")
        expected_field = "notifications.email.port"
    with running_gateway(path) as port:
        assert request(port, payload={"event": {"severity": "P2", "id": EVENT_ID, "summary": BODY}})[0] == 503
    entry = gateway_records(caplog)[0]
    assert entry["reason"] == "channel_config_invalid"
    assert entry["field"] == expected_field
    assert entry["channel"] == "email"
    assert entry["event_id"] == EVENT_ID
    assert_safe_logs(caplog)
