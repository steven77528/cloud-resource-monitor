"""Lazy provider dispatch shared by the scheduler and read-only diagnostics."""

from importlib import import_module


PROVIDERS = {
    "aliyun": "AliyunCollector", "huawei": "HuaweiCollector", "qiniu": "QiniuCollector",
    "huifu": "HuifuCollector", "asign": "AsignCollector", "wechat": "WechatCollector",
}
EXTERNAL_PROVIDERS = frozenset({"qiniu", "huifu", "asign", "wechat"})


def provider_module(provider):
    if provider not in PROVIDERS:
        raise ValueError("Unsupported provider")
    return import_module(f".{provider}", __name__)


def make_collector(account):
    if uses_relay(account):
        from .relay import RelayCollector
        return RelayCollector(account)
    return getattr(provider_module(account["provider"]), PROVIDERS[account["provider"]])(account)


def uses_relay(account):
    if account["provider"] not in EXTERNAL_PROVIDERS:
        return False
    local = account["provider"] == "asign" and account.get("api_mode") in {"ledger", "openapi_snapshot"}
    transport = account.get("transport", "local" if local else "relay")
    if transport == "local" and local:
        return False
    if transport != "relay":
        from .common import ProviderError
        raise ProviderError("relay_required", "account.transport")
    return True


def collect_job(collector, job, now):
    from .relay import RelayCollector
    if isinstance(collector, RelayCollector):
        return collector.collect_job(job, now)
    return collector.collect(job["kind"], job.get("options", {}), now)


def validate_account_credentials(account):
    if uses_relay(account):
        from .relay import validate_account
        validate_account(account)
        return
    if account["provider"] in EXTERNAL_PROVIDERS:
        provider_module(account["provider"]).validate_account(account)
        return
    for key in ("access_key_id", "access_key_secret"):
        value = account.get(key)
        if not isinstance(value, str) or not value or value.startswith("CHANGE_ME"):
            raise ValueError(f"Missing {key} for enabled account {account['id']}")
