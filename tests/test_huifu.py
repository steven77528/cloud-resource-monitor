import base64
from datetime import datetime, timedelta, timezone
import json

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID
import pytest

from cloud_monitor.providers.common import ProviderError
from cloud_monitor.providers.huifu import HuifuCollector, canonical_data, validate_account, validate_options


NOW = datetime(2026, 9, 11, tzinfo=timezone.utc)
OPTIONS = {"amount_unit_confirmed": True, "currency": "CNY", "source_amount_unit": "yuan",
           "amount_multiplier": "1", "accounts": [{"acct_id": "A1", "acct_type": "01"}]}


@pytest.fixture
def keys(tmp_path):
    merchant = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    platform = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = tmp_path / "merchant.pem"
    public = tmp_path / "platform.pem"
    private.write_bytes(merchant.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                               serialization.NoEncryption()))
    public.write_bytes(platform.public_key().public_bytes(serialization.Encoding.PEM,
                                                          serialization.PublicFormat.SubjectPublicKeyInfo))
    account = {"id": "merchant", "provider": "huifu", "sys_id": "system", "huifu_id": "merchant-id",
               "product_id": "test", "private_key_file": str(private), "huifu_public_key_file": str(public)}
    return account, merchant, platform


def row(**changes):
    return {"huifu_id": "merchant-id", "acct_id": "A1", "acct_type": "01", "acct_stat": "N",
            "avl_bal": "1234.56", "balance_amt": "1250.00", "frz_bal": "15.44", **changes}


def transport(keys, rows=None, mutate=None, bad_signature=False):
    account, merchant, platform = keys

    def request(method, url, *, headers, body):
        assert method == "POST" and url == "https://api.huifu.com/v2/trade/acctpayment/balance/query"
        assert headers == {"Content-Type": "application/json"}
        envelope = json.loads(body)
        data = envelope["data"]
        # Independent canonicalization follows the published first-level sort and compact JSON example.
        signed = json.dumps(dict(sorted(data.items())), ensure_ascii=False, separators=(",", ":")).encode()
        merchant.public_key().verify(base64.b64decode(envelope["sign"]), signed, padding.PKCS1v15(), hashes.SHA256())
        assert data["acct_date"] == data["req_date"] == "20260911"
        assert data["huifu_id"] == account["huifu_id"]
        result = {"req_date": data["req_date"], "req_seq_id": data["req_seq_id"], "resp_code": "00000000",
                  "acctInfo_list": json.dumps([row()] if rows is None else rows)}
        if mutate:
            mutate(result)
        signature = platform.sign(canonical_data(result), padding.PKCS1v15(), hashes.SHA256())
        return {"data": result, "sign": "bad" if bad_signature else base64.b64encode(signature).decode()}

    return request


def test_signed_balance_precise_units_and_only_selected_subaccount(keys):
    account, _, _ = keys
    validate_account(account)
    collector = HuifuCollector(account, transport(keys, [row(), row(acct_id="ignored", avl_bal="9999999")]))
    metrics = collector.collect("balance", OPTIONS, NOW)
    values = {item.metric: item for item in metrics}
    assert values["huifu_available_balance"].value == 1234.56
    assert values["huifu_frozen_balance"].value == 15.44
    assert values["huifu_account_normal"].value == 1
    assert all(item.resource_id == "merchant-id:01:A1" for item in metrics)
    assert values["huifu_available_balance"].dimensions["raw_value"] == "1234.56"
    assert "sign" not in json.dumps([item.to_dict() for item in metrics])


def test_currency_changes_do_not_collide(keys):
    collector = HuifuCollector(keys[0], transport(keys))
    cny = collector.collect("balance", OPTIONS, NOW)[0]
    usd = collector.collect("balance", {**OPTIONS, "currency": "USD"}, NOW)[0]
    assert cny.key() != usd.key()


@pytest.mark.parametrize("field,value", [("amount_unit_confirmed", False), ("amount_multiplier", "0"),
                                        ("amount_multiplier", "NaN"), ("currency", ""), ("accounts", [])])
def test_refuse_unconfirmed_or_invalid_units(field, value):
    with pytest.raises(ProviderError):
        validate_options("balance", {**OPTIONS, field: value})


@pytest.mark.parametrize("rows,reason", [([], "resource_missing"), ([row(), row()], "duplicate_resource"),
                                         ([row(huifu_id="other")], "resource_missing"),
                                         ([row(avl_bal=None)], "invalid_number"),
                                         ([row(avl_bal="NaN")], "invalid_number"),
                                         ([row(acct_stat="unknown")], "invalid_response")])
def test_bad_business_data_never_becomes_zero(keys, rows, reason):
    with pytest.raises(ProviderError, match=reason):
        HuifuCollector(keys[0], transport(keys, rows)).collect("balance", OPTIONS, NOW)


def test_signature_verification_is_mandatory(keys):
    with pytest.raises(ProviderError, match="signature_invalid"):
        HuifuCollector(keys[0], transport(keys, bad_signature=True)).collect("balance", OPTIONS, NOW)


@pytest.mark.parametrize("mutate,reason", [
    (lambda data: data.update(resp_code="99999999", resp_desc="secret bank payload"), "business_error"),
    (lambda data: data.update(req_seq_id="old-request"), "response_identity_mismatch"),
    (lambda data: data.update(acctInfo_list="not-json secret"), "invalid_response"),
])
def test_business_error_and_replay_are_safe(keys, mutate, reason):
    with pytest.raises(ProviderError, match=reason) as caught:
        HuifuCollector(keys[0], transport(keys, mutate=mutate)).collect("balance", OPTIONS, NOW)
    assert "secret" not in str(caught.value)


def test_local_expiry_requires_actual_certificate(keys, tmp_path):
    account, private, _ = keys
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "offline.test")])
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(private.public_key())
                   .serial_number(1).not_valid_before(NOW - timedelta(days=1))
                   .not_valid_after(NOW + timedelta(days=7)).sign(private, hashes.SHA256()))
    path = tmp_path / "certificate.pem"
    path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    options = {"certificate_file": str(path), "resource_id": "configured-certificate"}
    metrics = HuifuCollector(account).collect("local_certificate", options, NOW)
    assert metrics[0].value == 7
    assert metrics[0].dimensions["source"] == "local_certificate"
    with pytest.raises(ProviderError, match="invalid_certificate"):
        HuifuCollector(account).collect("local_certificate", {**options,
                                        "certificate_file": account["private_key_file"]}, NOW)


def test_settlement_signed_failure_return_and_timeout_are_distinct(keys, tmp_path):
    account, _, platform = keys
    path = tmp_path / "orders.json"
    path.write_text(json.dumps({"updated_at": NOW.isoformat(), "orders": [
        {"resource_id": "order-1", "org_req_date": "20260911", "org_req_seq_id": "req-1",
         "created_at": (NOW - timedelta(hours=1)).isoformat()}]}))

    def request(method, url, **kwargs):
        assert url.endswith("/v2/trade/settlement/query")
        data = {"resp_code": "00000000", "org_req_date": "20260911", "org_req_seq_id": "req-1",
                "trans_status": "P", "re_exchange": "Y"}
        return {"data": data, "sign": base64.b64encode(platform.sign(
            canonical_data(data), padding.PKCS1v15(), hashes.SHA256())).decode()}

    result = HuifuCollector(account, request).collect("settlements", {"orders_file": str(path)}, NOW)
    values = {item.metric: item.value for item in result}
    assert values["huifu_settlement_failed"] == 0
    assert values["huifu_settlement_returned"] == values["huifu_settlement_overdue"] == 1
    with pytest.raises(ProviderError, match="source_stale"):
        HuifuCollector(account, request).collect("settlements", {"orders_file": str(path)}, NOW + timedelta(hours=1))


def test_empty_settlement_manifest_does_not_claim_api_query_and_resets_between_polls(keys, tmp_path):
    account, _, platform = keys
    path = tmp_path / "orders.json"
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url))
        data = {"resp_code": "00000000", "org_req_date": "20260911", "org_req_seq_id": "req-1",
                "trans_status": "S", "re_exchange": ""}
        return {"data": data, "sign": base64.b64encode(platform.sign(
            canonical_data(data), padding.PKCS1v15(), hashes.SHA256())).decode()}

    collector = HuifuCollector(account, request)
    assert collector.query_performed is False
    manifest = {"updated_at": NOW.isoformat(), "orders": []}
    path.write_text(json.dumps(manifest))
    result = collector.collect("settlements", {"orders_file": str(path)}, NOW)
    assert [(metric.metric, metric.value) for metric in result] == [("huifu_tracked_settlements", 0)]
    assert collector.query_performed is False and calls == []

    manifest["orders"] = [{"resource_id": "order-1", "org_req_date": "20260911", "org_req_seq_id": "req-1",
                           "created_at": (NOW - timedelta(hours=1)).isoformat()}]
    path.write_text(json.dumps(manifest))
    collector.collect("settlements", {"orders_file": str(path)}, NOW)
    assert collector.query_performed is True and len(calls) == 1

    manifest["orders"] = []
    path.write_text(json.dumps(manifest))
    result = collector.collect("settlements", {"orders_file": str(path)}, NOW)
    assert [(metric.metric, metric.value) for metric in result] == [("huifu_tracked_settlements", 0)]
    assert collector.query_performed is False and len(calls) == 1


def test_query_performed_includes_failed_network_attempt_but_not_preparation_failure(keys):
    def unavailable(*args, **kwargs):
        raise ProviderError("transport_error")

    collector = HuifuCollector(keys[0], unavailable)
    with pytest.raises(ProviderError, match="transport_error"):
        collector.collect("balance", OPTIONS, NOW)
    assert collector.query_performed is True
    with pytest.raises(ProviderError, match="unit_confirmation_required"):
        collector.collect("balance", {**OPTIONS, "amount_unit_confirmed": False}, NOW)
    assert collector.query_performed is False
