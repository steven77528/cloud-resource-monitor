import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

from cloud_monitor.providers.asign import AsignCollector, validate_account, validate_job, validate_options
from cloud_monitor.providers.common import ProviderError
from cloud_monitor.providers.relay import RelayCollector


NOW = datetime(2026, 9, 11, 4, 0, tzinfo=timezone.utc)


def metrics_by_name(metrics):
    return {(m.metric, m.dimensions.get("scope", "")): m for m in metrics}


def snapshot_collector(tmp_path, row=None, **changes):
    data = {"version": 1, "app_id": "open-app", "endpoint": "https://oapi.asign.cn/user/getCompUser",
            "collected_at": (NOW - timedelta(minutes=5)).isoformat(), "response": {
                "code": 100000, "data": [row if row is not None else {
                    "account": "company-private", "userType": 1, "status": 1,
                    "name": "PRIVATE_NAME", "idCard": "PRIVATE_ID", "bankCard": "PRIVATE_BANK",
                    "identifyTime": "2021-01-01 12:00:00",
                }]}}
    data.update(changes)
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return AsignCollector({"id": "asign-open", "api_mode": "openapi_snapshot", "app_id": "open-app",
                           "sdk_result_file": str(path)})


COMPANY = {"resource_id": "company-main", "selector": {"account": "company-private"}}


def test_official_sdk_snapshot_preserves_source_date_and_redacts_personal_fields(tmp_path):
    collector = snapshot_collector(tmp_path)
    metrics = collector.collect("company_verification_snapshot", COMPANY, NOW)
    assert len(metrics) == 1
    assert metrics[0].metric == "company_verified"
    assert metrics[0].value == 1
    assert metrics[0].observed_at == NOW - timedelta(minutes=5)
    assert metrics[0].dimensions["source"] == "official_sdk_snapshot"
    assert "PRIVATE_" not in str(metrics[0].to_dict())
    assert "company-private" not in str(metrics[0].to_dict())
    assert "expiry" not in str(metrics[0].to_dict())


@pytest.mark.parametrize("row,reason", [
    ({"account": "other", "userType": 1, "status": 1}, "record_missing"),
    ({"account": "company-private", "userType": 2, "status": 1}, "company_identity_mismatch"),
    ({"account": "company-private", "userType": 1}, "invalid_response"),
    ({"account": "company-private", "userType": 1, "status": True}, "invalid_response"),
    ({"account": "company-private", "userType": 1, "status": 3}, "invalid_response"),
])
def test_company_requires_exact_corporate_identity_and_valid_status(tmp_path, row, reason):
    with pytest.raises(ProviderError) as error:
        snapshot_collector(tmp_path, row).collect("company_verification_snapshot", COMPANY, NOW)
    assert error.value.reason == reason


@pytest.mark.parametrize("changes,reason", [
    ({"app_id": "other-app"}, "source_identity_mismatch"),
    ({"endpoint": "https://sapi.asign.cn/user/getCompUser"}, "source_identity_mismatch"),
    ({"collected_at": (NOW - timedelta(days=3)).isoformat()}, "source_data_stale"),
    ({"collected_at": (NOW + timedelta(minutes=2)).isoformat()}, "source_time_in_future"),
    ({"collected_at": "2026-09-11T12:00:00"}, "invalid_timestamp"),
])
def test_snapshot_identity_and_freshness_are_enforced(tmp_path, changes, reason):
    with pytest.raises(ProviderError) as error:
        snapshot_collector(tmp_path, **changes).collect("company_verification_snapshot", COMPANY, NOW)
    assert error.value.reason == reason


def ledger_collector(tmp_path, packages=None, **changes):
    payload = {"version": 1, "account_id": "asign-ledger", "updated_at": (NOW - timedelta(hours=1)).isoformat(),
               "packages": packages or [{
        "id": "signatures-main", "unit": "signatures", "remaining": "15", "total": "100",
        "expires_at": (NOW + timedelta(days=7)).isoformat(),
    }]}
    payload.update(changes)
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return AsignCollector({"id": "asign-ledger", "api_mode": "ledger", "ledger_file": str(path)})


LEDGER_OPTIONS = {"package_ids": ["signatures-main"]}


def test_ledger_uses_explicit_units_and_commercial_expiry_not_authorization(tmp_path):
    metrics = metrics_by_name(ledger_collector(tmp_path).collect("package_ledger", LEDGER_OPTIONS, NOW))
    assert metrics["package_remaining", ""].value == 15
    assert metrics["package_remaining", ""].unit == "signatures"
    assert metrics["package_remaining_percent", ""].value == 15
    assert metrics["commercial_expiry_days", ""].value == 7
    assert metrics["ledger_age_seconds", ""].value == 3600
    assert all(m.dimensions["source"] == "ledger" for m in metrics.values())


@pytest.mark.parametrize("updated_at,expires_at", [
    ("2026-09-11 11:00:00", "2026-09-18 12:00:00"),
    ("2026-09-11T11:00:00", "2026-09-18T12:00:00"),
    ("2026-09-11T11:00:00+08:00", "2026-09-18T12:00:00+08:00"),
    ("2026-09-11T03:00:00Z", "2026-09-18T04:00:00Z"),
    ("2026-09-10T22:00:00-05:00", "2026-09-17T23:00:00-05:00"),
    ((NOW - timedelta(hours=1)).timestamp(), (NOW + timedelta(days=7)).timestamp()),
    (str((NOW - timedelta(hours=1)).timestamp()), str((NOW + timedelta(days=7)).timestamp())),
])
def test_ledger_times_default_to_beijing_and_preserve_explicit_instants(tmp_path, updated_at, expires_at):
    collector = ledger_collector(tmp_path, [{
        "id": "signatures-main", "unit": "signatures", "remaining": "15", "total": "100",
        "expires_at": expires_at,
    }], updated_at=updated_at)
    metrics = metrics_by_name(collector.collect("package_ledger", LEDGER_OPTIONS, NOW))
    assert metrics["ledger_age_seconds", ""].value == 3600
    assert metrics["commercial_expiry_days", ""].value == 7
    assert all(m.dimensions["source_updated_at"] == "2026-09-11T03:00:00+00:00" for m in metrics.values())
    assert metrics["commercial_expiry_days", ""].dimensions["expires_at"] == "2026-09-18T04:00:00+00:00"


def test_ledger_beijing_time_converts_across_utc_date_boundary(tmp_path):
    now = datetime(2026, 9, 10, 17, tzinfo=timezone.utc)
    collector = ledger_collector(tmp_path, [{
        "id": "signatures-main", "unit": "signatures", "remaining": "15",
        "expires_at": "2026-09-11 00:30:00",
    }], updated_at="2026-09-11 00:00:00")
    metrics = metrics_by_name(collector.collect("package_ledger", LEDGER_OPTIONS, now))
    assert metrics["ledger_age_seconds", ""].value == 3600
    assert metrics["commercial_expiry_days", ""].value == pytest.approx(-1 / 48)
    assert metrics["package_remaining", ""].dimensions["source_updated_at"] == "2026-09-10T16:00:00+00:00"


@pytest.mark.parametrize("updated_at,reason", [
    ("2026-09-09 11:59:59", "source_data_stale"),
    ("2026-09-11 12:01:01", "source_time_in_future"),
])
def test_ledger_naive_time_still_enforces_freshness(tmp_path, updated_at, reason):
    collector = ledger_collector(tmp_path, updated_at=updated_at)
    with pytest.raises(ProviderError) as error:
        collector.collect("package_ledger", LEDGER_OPTIONS, NOW)
    assert error.value.reason == reason
    assert error.value.field == "ledger.updated_at"


@pytest.mark.parametrize("field", ["updated_at", "expires_at"])
@pytest.mark.parametrize("value", ["2026-02-30 12:00:00", "2026-09-11 25:00:00", "CHANGE_ME_TIME", "", None])
def test_ledger_rejects_invalid_times(tmp_path, field, value):
    packages = [{"id": "signatures-main", "unit": "signatures", "remaining": "15"}]
    changes = {}
    if field == "expires_at":
        packages[0][field] = value
    else:
        changes[field] = value
    collector = ledger_collector(tmp_path, packages, **changes)
    with pytest.raises(ProviderError) as error:
        collector.collect("package_ledger", LEDGER_OPTIONS, NOW)
    assert error.value.reason == "invalid_timestamp"
    assert error.value.field == ("ledger.updated_at" if field == "updated_at" else "ledger.packages.expires_at")


def test_zero_ledger_total_does_not_create_zero_percent(tmp_path):
    collector = ledger_collector(tmp_path, [{"id": "signatures-main", "remaining": 0, "total": 0, "unit": "signatures"}])
    metrics = collector.collect("package_ledger", LEDGER_OPTIONS, NOW)
    assert not any(m.metric == "package_remaining_percent" for m in metrics)


def test_ledger_stale_cannot_look_like_current_api_data(tmp_path):
    collector = ledger_collector(tmp_path, updated_at=(NOW - timedelta(days=10)).isoformat())
    with pytest.raises(ProviderError, match="source_data_stale"):
        collector.collect("package_ledger", LEDGER_OPTIONS, NOW)


@pytest.mark.parametrize("account_id", [None, "another-contract"])
def test_ledger_requires_matching_account_identity(tmp_path, account_id):
    collector = ledger_collector(tmp_path, account_id=account_id)
    with pytest.raises(ProviderError, match="source_identity_mismatch"):
        collector.collect("package_ledger", LEDGER_OPTIONS, NOW)


def test_ledger_unit_changes_do_not_reuse_alert_or_sample_identity(tmp_path):
    first = ledger_collector(tmp_path).collect("package_ledger", LEDGER_OPTIONS, NOW)
    second = ledger_collector(tmp_path, packages=[{
        "id": "signatures-main", "unit": "credits", "remaining": "15", "total": "100",
        "expires_at": (NOW + timedelta(days=7)).isoformat(),
    }]).collect("package_ledger", LEDGER_OPTIONS, NOW)
    assert [metric.metric for metric in first] == [metric.metric for metric in second]
    assert all(left.key() != right.key() for left, right in zip(first, second))


@pytest.mark.parametrize("kind,options", [
    ("company_verification", {"resource_id": "safe", "selector": {"account": "a", "creditCode": "b"}}),
    ("package_ledger", {"package_ids": ["same", "same"]}),
    ("package_ledger", {"package_ids": ["safe"], "source_max_age_seconds": "86400"}),
])
def test_invalid_options_rejected(kind, options):
    with pytest.raises(ProviderError):
        validate_options(kind, options)


@pytest.mark.parametrize("changes", [
    {"endpoint": "https://sapi.asign.cn"}, {"app_id": "bad\r\nheader"},
    {"timeout_seconds": True},
])
def test_openapi_account_rejects_invalid_endpoint_headers_and_timeout(tmp_path, changes):
    collector = snapshot_collector(tmp_path)
    with pytest.raises(ProviderError):
        validate_account({**collector.account, **changes})


def test_removed_saas_account_mode_is_rejected():
    with pytest.raises(ProviderError) as error:
        validate_account({"id": "asign-retired", "api_mode": "saas", "app_id": "offline-app",
                          "token_file": "unused-offline.token"})
    assert error.value.reason == "invalid_configuration"
    assert error.value.field == "account.api_mode"


@pytest.mark.parametrize("kind", ["saas_authorizations", "saas_health"])
def test_removed_saas_kinds_are_rejected_before_any_request(kind):
    calls = []
    collector = AsignCollector({"id": "asign-open", "api_mode": "openapi", "app_id": "offline-app"},
                               transport=lambda *args, **kwargs: calls.append((args, kwargs)))
    with pytest.raises(ProviderError, match="invalid_configuration"):
        collector.collect(kind, {}, NOW)
    assert calls == []


@pytest.mark.parametrize("mode,kind,reason", [
    ("saas", "company_verification", "invalid_configuration"),
    ("openapi", "saas_authorizations", "unsupported_kind"),
    ("openapi", "saas_health", "unsupported_kind"),
])
def test_relay_rejects_removed_saas_configuration_before_any_request(tmp_path, mode, kind, reason):
    calls = []
    account = {"id": "asign-open", "provider": "asign", "api_mode": mode, "app_id": "offline-app",
               "relay": {"url": "https://relay.example.invalid/v1/monitor/collect",
                         "token_file": str(tmp_path / "not-read.token")}}
    collector = RelayCollector(account, transport=lambda *args, **kwargs: calls.append((args, kwargs)))
    options = {"operation": "company-verification", "collector": COMPANY if kind.startswith("company") else {}}
    with pytest.raises(ProviderError) as error:
        collector.collect(kind, options, NOW)
    assert error.value.reason == reason
    assert calls == []


@pytest.fixture(scope="module")
def openapi_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def native_collector(tmp_path, private_key, response=None, *, key_format="pem"):
    if key_format == "pem":
        content = private_key.private_bytes(serialization.Encoding.PEM,
                                            serialization.PrivateFormat.PKCS8,
                                            serialization.NoEncryption()).decode("ascii")
    else:
        raw = private_key.private_bytes(serialization.Encoding.DER,
                                        serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption())
        content = base64.b64encode(raw).decode("ascii")
    key_file = tmp_path / "offline-key.pem"
    key_file.write_text(content, encoding="utf-8")
    calls = []

    def transport(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return response if response is not None else {
            "code": 100000,
            "data": [{"account": "company-private", "userType": 1, "status": 1,
                      "idCard": "PRIVATE_ID", "bankCard": "PRIVATE_BANK", "name": "PRIVATE_NAME"}],
        }

    account = {"id": "asign-native", "api_mode": "openapi", "app_id": "offline-app",
               "private_key_file": str(key_file), "timeout_seconds": 12}
    return AsignCollector(account, transport=transport), calls, key_file


def multipart_fields(call):
    _, _, request = call
    message = BytesParser(policy=policy.default).parsebytes(
        b"Content-Type: " + request["headers"]["Content-Type"].encode("ascii")
        + b"\r\nMIME-Version: 1.0\r\n\r\n" + request["body"],
    )
    return {part.get_param("name", header="content-disposition"): part.get_payload(decode=True).decode("utf-8")
            for part in message.iter_parts()}


@pytest.mark.parametrize("key_format", ["pem", "der"])
def test_native_openapi_matches_sdk_multipart_and_rsa_sha1_contract(tmp_path, openapi_key, key_format):
    collector, calls, _ = native_collector(tmp_path, openapi_key, key_format=key_format)
    metrics = collector.collect("company_verification", COMPANY, NOW)
    assert calls[0][0:2] == ("POST", "https://oapi.asign.cn/user/getCompUser")
    assert calls[0][2]["timeout"] == 12
    fields = multipart_fields(calls[0])
    expected_payload = '{"account":"company-private"}'
    expected_stamp = str(round(NOW.timestamp() * 1000) + 60000)
    assert fields == {
        "appId": "offline-app", "bizData": expected_payload, "timestamp": expected_stamp,
        "Content-Type": "application/octet-stream", "type": "application/octet-stream",
    }
    assert calls[0][2]["headers"]["Accept"] == "*/*"
    # The supplied SDK actually uses SHA1.new despite its misleading SHA256 comment.
    expected_message = (expected_payload + hashlib.md5(expected_payload.encode()).hexdigest()
                        + "offline-app" + expected_stamp).encode("utf-8")
    signature = base64.b64decode(calls[0][2]["headers"]["sign"], validate=True)
    openapi_key.public_key().verify(signature, expected_message, padding.PKCS1v15(), hashes.SHA1())
    with pytest.raises(InvalidSignature):
        openapi_key.public_key().verify(signature, expected_message, padding.PKCS1v15(), hashes.SHA256())
    assert len(metrics) == 1 and metrics[0].value == 1
    assert metrics[0].observed_at == NOW
    assert metrics[0].dimensions["source"] == "official_api"
    serialized = str(metrics[0].to_dict())
    assert not any(value in serialized for value in ("PRIVATE_", "company-private", "offline-app", "expiry"))


def test_native_openapi_signs_utf8_company_name_without_ascii_escaping(tmp_path, openapi_key):
    name = "\u6d4b\u8bd5\u4f01\u4e1a"
    collector, calls, _ = native_collector(tmp_path, openapi_key, {
        "code": 100000, "data": [{"companyName": name, "userType": 1, "status": 0}],
    })
    options = {"resource_id": "company-name", "selector": {"companyName": name}}
    assert collector.collect("company_verification", options, NOW)[0].value == 0
    payload = multipart_fields(calls[0])["bizData"]
    assert payload == '{"companyName":"' + name + '"}'
    assert "\\u" not in payload


@pytest.mark.parametrize("response,reason", [
    ({"code": 100005, "msg": "PRIVATE_ERROR", "data": []}, "api_business_error"),
    ({"code": 100000, "data": []}, "record_missing"),
    ({"code": 100000, "data": [{"account": "other", "userType": 1, "status": 1}]}, "record_missing"),
    ({"code": 100000, "data": [{"account": "company-private", "userType": 2, "status": 1}]},
     "company_identity_mismatch"),
    ({"code": 100000, "data": [{"account": "company-private", "userType": 1}]}, "invalid_response"),
    ({"code": 100000, "data": [{"account": "company-private", "userType": 1, "status": 1}] * 2},
     "ambiguous_record"),
])
def test_native_failures_do_not_fabricate_unverified_company(tmp_path, openapi_key, response, reason):
    collector, _, _ = native_collector(tmp_path, openapi_key, response)
    with pytest.raises(ProviderError) as error:
        collector.collect("company_verification", COMPANY, NOW)
    assert error.value.reason == reason
    assert "PRIVATE_" not in str(error.value)


def test_native_reloads_private_key_from_file_for_rotation(tmp_path, openapi_key):
    collector, calls, key_file = native_collector(tmp_path, openapi_key)
    collector.collect("company_verification", COMPANY, NOW)
    replacement = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_file.write_bytes(replacement.private_bytes(serialization.Encoding.PEM,
                                                  serialization.PrivateFormat.PKCS8,
                                                  serialization.NoEncryption()))
    collector.collect("company_verification", COMPANY, NOW)
    assert calls[0][2]["headers"]["sign"] != calls[1][2]["headers"]["sign"]


def test_native_rejects_malformed_key_without_exposing_file_contents(tmp_path, openapi_key):
    collector, calls, key_file = native_collector(tmp_path, openapi_key)
    key_file.write_text("PRIVATE_BAD_KEY_MATERIAL", encoding="utf-8")
    with pytest.raises(ProviderError) as error:
        collector.collect("company_verification", COMPANY, NOW)
    assert error.value.reason == "invalid_key"
    assert "PRIVATE_BAD_KEY_MATERIAL" not in str(error.value)
    assert calls == []


def test_native_requires_rsa_private_key(tmp_path):
    collector, calls, _ = native_collector(tmp_path, ec.generate_private_key(ec.SECP256R1()))
    with pytest.raises(ProviderError, match="invalid_key"):
        collector.collect("company_verification", COMPANY, NOW)
    assert not calls


@pytest.mark.parametrize("mode,kind", [
    ("ledger", "company_verification"), ("openapi_snapshot", "company_verification"),
    ("openapi", "company_verification_snapshot"),
])
def test_config_job_validation_rejects_mismatched_api_mode(mode, kind):
    with pytest.raises(ProviderError, match="api_mode_mismatch"):
        validate_job({"api_mode": mode}, kind, COMPANY if kind.startswith("company") else {})
