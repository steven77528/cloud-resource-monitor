"""Read-only DouGong V2 queries with mandatory response signature verification."""

import base64
from datetime import timezone, timedelta
import json
import re
from uuid import uuid4

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from ..models import Metric
from .common import ProviderError, decimal_number, read_secret_file, request_json, require_text, timestamp
from .relay import trusted_relay_client


SUPPORTED_KINDS = {"balance", "settlements", "local_certificate"}
BASE_URL = "https://api.huifu.com"
SHANGHAI = timezone(timedelta(hours=8))


def canonical_data(data):
    if not isinstance(data, dict):
        raise ProviderError("invalid_response", "data")
    ordered = {key: data[key] for key in sorted(data) if data[key] is not None}
    return json.dumps(ordered, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _key(path, private=False):
    field = "private_key_file" if private else "huifu_public_key_file"
    content = read_secret_file(path, field)
    if not content.startswith("-----BEGIN"):
        label = "PRIVATE KEY" if private else "PUBLIC KEY"
        content = f"-----BEGIN {label}-----\n{content}\n-----END {label}-----"
    try:
        value = (serialization.load_pem_private_key(content.encode(), password=None) if private
                 else serialization.load_pem_public_key(content.encode()))
    except (ValueError, TypeError):
        raise ProviderError("invalid_key", field) from None
    if not isinstance(value, rsa.RSAPrivateKey if private else rsa.RSAPublicKey) or value.key_size < 2048:
        raise ProviderError("invalid_key", field)
    return value


def validate_account(account):
    require_text(account.get("id"), "id")
    if account.get("credential_mode", "api") == "local_certificate":
        return
    if account.get("credential_mode", "api") != "api":
        raise ProviderError("invalid_configuration", "credential_mode")
    if trusted_relay_client(account) is not None:
        require_text(account.get("huifu_id"), "huifu_id")
        return
    for field in ("id", "sys_id", "product_id", "huifu_id", "private_key_file", "huifu_public_key_file"):
        require_text(account.get(field), field)
    _key(account["private_key_file"], private=True)
    _key(account["huifu_public_key_file"])


def validate_job(account, kind, options):
    validate_options(kind, options)
    if kind != "local_certificate" and account.get("credential_mode", "api") != "api":
        raise ProviderError("invalid_configuration", "credential_mode")


def validate_options(kind, options):
    if kind not in SUPPORTED_KINDS:
        raise ProviderError("unsupported_collector")
    if kind == "balance":
        if options.get("amount_unit_confirmed") is not True:
            raise ProviderError("unit_confirmation_required", "amount_unit_confirmed")
        if not re.fullmatch(r"[A-Z]{3}", str(options.get("currency", ""))):
            raise ProviderError("invalid_configuration", "currency")
        require_text(options.get("source_amount_unit"), "source_amount_unit")
        if decimal_number(options.get("amount_multiplier"), "amount_multiplier") <= 0:
            raise ProviderError("invalid_configuration", "amount_multiplier")
        targets = options.get("accounts")
        if not isinstance(targets, list) or not targets:
            raise ProviderError("missing_configuration", "accounts")
        identities = set()
        for target in targets:
            if not isinstance(target, dict):
                raise ProviderError("invalid_configuration", "accounts")
            identity = tuple(require_text(target.get(field), "accounts." + field)
                             for field in ("acct_id", "acct_type"))
            if identity in identities:
                raise ProviderError("duplicate_resource", "accounts")
            identities.add(identity)
    elif kind == "settlements":
        require_text(options.get("orders_file"), "orders_file")
        for field, default in (("sla_seconds", 1800), ("manifest_max_age_seconds", 900)):
            if decimal_number(options.get(field, default), field) <= 0:
                raise ProviderError("invalid_configuration", field)
    else:
        for field in ("certificate_file", "resource_id"):
            require_text(options.get(field), field)


class HuifuCollector:
    def __init__(self, account, transport=None):
        self.account = account
        self.transport = transport or request_json
        self.query_performed = False

    def _post(self, path, data):
        relay = trusted_relay_client(self.account)
        self.query_performed = True
        if relay is not None:
            response = relay.request("POST", BASE_URL + path, body=data)
        else:
            private = _key(self.account.get("private_key_file"), private=True)
            public = _key(self.account.get("huifu_public_key_file"))
            sign = private.sign(canonical_data(data), padding.PKCS1v15(), hashes.SHA256())
            payload = {"sys_id": self.account["sys_id"], "product_id": self.account["product_id"],
                       "data": data, "sign": base64.b64encode(sign).decode("ascii")}
            response = self.transport("POST", BASE_URL + path, headers={"Content-Type": "application/json"},
                                      body=json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        result = response.get("data")
        if relay is not None and isinstance(result, str):
            try:
                result = json.loads(result)
            except (ValueError, TypeError):
                raise ProviderError("invalid_response", "data") from None
        if not isinstance(result, dict):
            raise ProviderError("invalid_response", "data")
        if relay is None:
            try:
                signature = base64.b64decode(response.get("sign", ""), validate=True)
                public.verify(signature, canonical_data(result), padding.PKCS1v15(), hashes.SHA256())
            except (ValueError, TypeError, InvalidSignature):
                raise ProviderError("signature_invalid") from None
        if result.get("resp_code") != "00000000":
            raise ProviderError("business_error", "resp_code")
        return result

    def _metric(self, resource_id, name, metric, value, unit, now, dimensions):
        return Metric("huifu", self.account["id"], resource_id, name, metric, float(value), unit, now, dimensions)

    def collect(self, kind, options, now):
        self.query_performed = False
        validate_job(self.account, kind, options)
        return getattr(self, "_" + kind)(options, now)

    def _balance(self, options, now):
        req_date = now.astimezone(SHANGHAI).strftime("%Y%m%d")
        request = {"req_date": req_date, "req_seq_id": uuid4().hex,
                   "huifu_id": self.account["huifu_id"], "acct_date": req_date}
        result = self._post("/v2/trade/acctpayment/balance/query", request)
        if result.get("req_date") != req_date or result.get("req_seq_id") != request["req_seq_id"]:
            raise ProviderError("response_identity_mismatch")
        try:
            rows = json.loads(result["acctInfo_list"])
        except (KeyError, ValueError, TypeError):
            raise ProviderError("invalid_response", "acctInfo_list") from None
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise ProviderError("invalid_response", "acctInfo_list")
        selected = {(item["acct_id"], item["acct_type"]): item for item in options["accounts"]}
        found, metrics = set(), []
        for row in rows:
            identity = (row.get("acct_id"), row.get("acct_type"))
            if row.get("huifu_id") != self.account["huifu_id"] or identity not in selected:
                continue
            if identity in found:
                raise ProviderError("duplicate_resource", "acctInfo_list")
            found.add(identity)
            resource_id = ":".join((self.account["huifu_id"], identity[1], identity[0]))
            name = selected[identity].get("resource_name", identity[0])
            currency = options["currency"]
            dimensions = {"product": "huifu_account", "scope": identity[1], "currency": currency,
                          "identity": {"acct_type": identity[1], "currency": currency},
                          "source_amount_unit": options["source_amount_unit"], "source": "official_api"}
            for source, metric in (("avl_bal", "huifu_available_balance"), ("balance_amt", "huifu_balance"),
                                   ("frz_bal", "huifu_frozen_balance")):
                value = decimal_number(row.get(source), source) * decimal_number(
                    options["amount_multiplier"], "amount_multiplier")
                metrics.append(self._metric(resource_id, name, metric, value, currency, now,
                                            {**dimensions, "raw_value": str(row[source])}))
            if row.get("acct_stat") not in {"N", "C", "F", "D"}:
                raise ProviderError("invalid_response", "acct_stat")
            metrics.append(self._metric(resource_id, name, "huifu_account_normal", int(row["acct_stat"] == "N"),
                                        "bool", now, dimensions))
        if found != set(selected):
            raise ProviderError("resource_missing", "accounts")
        return metrics

    def _settlements(self, options, now):
        try:
            manifest = json.loads(read_secret_file(options["orders_file"], "orders_file"))
        except (ValueError, TypeError):
            raise ProviderError("invalid_configuration", "orders_file") from None
        if not isinstance(manifest, dict) or not isinstance(manifest.get("orders"), list):
            raise ProviderError("invalid_configuration", "orders_file")
        updated = timestamp(manifest.get("updated_at"), "updated_at")
        age = (now - updated).total_seconds()
        if not -60 <= age <= decimal_number(options.get("manifest_max_age_seconds", 900),
                                            "manifest_max_age_seconds"):
            raise ProviderError("source_stale", "updated_at")
        metrics, seen = [], set()
        for order in manifest["orders"]:
            if not isinstance(order, dict):
                raise ProviderError("invalid_configuration", "orders")
            identifier = require_text(order.get("resource_id"), "orders.resource_id")
            if identifier in seen:
                raise ProviderError("duplicate_resource", "orders")
            seen.add(identifier)
            date = require_text(order.get("org_req_date"), "orders.org_req_date")
            if not re.fullmatch(r"[0-9]{8}", date):
                raise ProviderError("invalid_configuration", "orders.org_req_date")
            sequence = require_text(order.get("org_req_seq_id"), "orders.org_req_seq_id")
            started = timestamp(order.get("created_at"), "orders.created_at")
            if started > now:
                raise ProviderError("invalid_timestamp", "orders.created_at")
            result = self._post("/v2/trade/settlement/query", {
                "huifu_id": self.account["huifu_id"], "org_req_date": date, "org_req_seq_id": sequence})
            if result.get("org_req_date") != date or result.get("org_req_seq_id") != sequence:
                raise ProviderError("response_identity_mismatch")
            state, returned = result.get("trans_status"), result.get("re_exchange", "")
            if state not in {"S", "F", "P"} or returned not in {"", "N", "Y", None}:
                raise ProviderError("invalid_response", "trans_status")
            dimensions = {"product": "huifu_settlement", "source": "official_api", "scope": "tracked_orders"}
            for metric, value in (("huifu_settlement_failed", int(state == "F")),
                                  ("huifu_settlement_returned", int(returned == "Y")),
                                  ("huifu_settlement_overdue", int(state == "P" and
                                   (now - started).total_seconds() > decimal_number(
                                       options.get("sla_seconds", 1800), "sla_seconds")))):
                metrics.append(self._metric(identifier, order.get("resource_name", identifier), metric,
                                            value, "bool", now, dimensions))
        metrics.append(self._metric(self.account["id"], "Tracked settlements", "huifu_tracked_settlements",
                                    len(seen), "count", now, {"source": "order_manifest"}))
        return metrics

    def _local_certificate(self, options, now):
        content = read_secret_file(options["certificate_file"], "certificate_file")
        try:
            certificate = x509.load_pem_x509_certificate(content.encode())
        except ValueError:
            raise ProviderError("invalid_certificate", "certificate_file") from None
        expiry = certificate.not_valid_after_utc
        return [self._metric(options["resource_id"], options.get("resource_name", options["resource_id"]),
                             "huifu_certificate_expiry_days", (expiry - now).total_seconds() / 86400,
                             "days", now, {"source": "local_certificate", "expires_at": expiry.isoformat()})]
