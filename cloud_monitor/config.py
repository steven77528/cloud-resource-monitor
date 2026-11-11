from copy import deepcopy
import math
import os
from pathlib import Path
import re

import yaml

from .alerting import validate_alerting
from .providers import EXTERNAL_PROVIDERS, PROVIDERS, provider_module, uses_relay, validate_account_credentials


ROUTES = {"P0": "sms", "P1": "dingtalk", "P2": "email"}
OPS = {"lt", "le", "gt", "ge", "eq"}
ENV_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand(value):
    if isinstance(value, str):
        return ENV_REFERENCE.sub(lambda m: os.environ.get(m[1], ""), value)
    if isinstance(value, list):
        return [_expand(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand(item) for key, item in value.items()}
    return value


def _number(value, label, minimum=0):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number")
    if not math.isfinite(value) or value < minimum:
        raise ValueError(f"{label} must be finite and >= {minimum}")


def validate_config(config: dict, live: bool = False) -> dict:
    cfg = deepcopy(config)
    if not isinstance(cfg, dict) or cfg.get("version") != 1:
        raise ValueError("Configuration must be a mapping with version: 1")
    cfg.setdefault("service", {})
    cfg.setdefault("accounts", [])
    cfg.setdefault("jobs", [])
    cfg.setdefault("rules", [])
    cfg.setdefault("notifications", {})
    cfg.setdefault("api", {})
    metrics = cfg["api"].setdefault("metrics", {})
    if not isinstance(metrics, dict):
        raise ValueError("api.metrics must be a mapping")
    metrics.setdefault("enabled", True)
    if not isinstance(metrics["enabled"], bool):
        raise ValueError("api.metrics.enabled must be boolean")
    if "token" in metrics and (not isinstance(metrics["token"], str) or len(metrics["token"].strip()) < 24):
        raise ValueError("api.metrics.token must contain at least 24 characters when configured")
    from .notifiers import sms_enabled
    sms_enabled(cfg["notifications"])
    cfg["service"].setdefault("database", {})
    cfg["service"].setdefault("repeat_seconds", {"P0": 21600, "P1": 86400, "P2": 86400})
    cfg["service"].setdefault("retention_days", 30)
    cfg["service"].setdefault("retry_max_attempts", 8)
    cfg["service"].setdefault("recovery_samples", 2)
    cfg["service"].setdefault("notify_recovery", True)
    _number(cfg["service"]["retention_days"], "retention_days", 1)
    for field in ("recovery_samples", "retry_max_attempts"):
        value = cfg["service"][field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{field} must be a positive integer")
    for severity in ROUTES:
        _number(cfg["service"]["repeat_seconds"].get(severity), f"repeat_seconds.{severity}", 60)
    accounts = {}
    for account in cfg["accounts"]:
        if not isinstance(account, dict) or not account.get("id") or account["id"] in accounts:
            raise ValueError("Account IDs must be nonempty and unique")
        if account.get("provider") not in PROVIDERS:
            raise ValueError(f"Unsupported provider for account {account['id']}")
        if not isinstance(account.get("enabled", False), bool):
            raise ValueError(f"enabled must be boolean for account {account['id']}")
        if not isinstance(account.get("regions", []), list):
            raise ValueError(f"regions must be a list: {account['id']}")
        accounts[account["id"]] = account
    rule_ids = set()
    for rule in cfg["rules"]:
        if not rule.get("id") or rule["id"] in rule_ids or not rule.get("metric"):
            raise ValueError("Rule IDs must be unique and every rule needs a metric")
        rule_ids.add(rule["id"])
        if not rule.get("levels"):
            raise ValueError(f"Rule {rule['id']} has no levels")
        seen = set()
        for level in rule["levels"]:
            severity = level.get("severity")
            if severity not in ROUTES or severity in seen or level.get("op") not in OPS:
                raise ValueError(f"Invalid level in rule {rule['id']}")
            seen.add(severity)
            _number(level.get("threshold"), f"{rule['id']}.threshold", -1e100)
            _number(level.get("for_seconds", 0), f"{rule['id']}.for_seconds")
            count = level.get("for_samples", 1)
            if type(count) is not int or count < 1:
                raise ValueError(f"{rule['id']}.for_samples must be a positive integer")
    job_ids = set()
    enabled = []
    for job in cfg["jobs"]:
        if not job.get("id") or job["id"] in job_ids:
            raise ValueError("Job IDs must be nonempty and unique")
        job_ids.add(job["id"])
        if not isinstance(job.get("enabled", True), bool):
            raise ValueError(f"enabled must be boolean for job {job['id']}")
        if job.get("account") not in accounts:
            raise ValueError(f"Unknown account for job {job['id']}")
        if not job.get("kind"):
            raise ValueError(f"Missing kind for job {job['id']}")
        _number(job.get("interval_seconds"), f"{job['id']}.interval_seconds", 10)
        _number(job.get("max_age_seconds", max(900, job["interval_seconds"] * 2)), "max_age_seconds", 1)
        if type(job.get("collection_error_samples", 1)) is not int or job.get("collection_error_samples", 1) < 1:
            raise ValueError("collection_error_samples must be a positive integer")
        if not isinstance(job.get("options", {}), dict):
            raise ValueError(f"options must be a mapping: {job['id']}")
        validate_alerting(job)
        if job["kind"] == "network_traffic":
            from .providers.network_traffic import validate_network_traffic_options
            validate_network_traffic_options(job.get("options", {}), accounts[job["account"]]["provider"])
        if job.get("rules") is not None and not set(job["rules"]).issubset(rule_ids):
            raise ValueError(f"Unknown rule selected by job {job['id']}")
        account = accounts[job["account"]]
        if job.get("enabled", True) and account.get("enabled", False):
            enabled.append(job)
            if live:
                validate_account_credentials(account)
                _validate_job(job, account)
    if live:
        if not enabled:
            raise ValueError("No enabled jobs. Fill credentials and enable at least one account/job")
        validate_database(cfg["service"]["database"])
        if "gateway" in cfg["notifications"]:
            from .notification_client import validate_gateway
            validate_gateway(cfg["notifications"]["gateway"])
        else:
            from .notifiers import validate_notifications
            validate_notifications(cfg["notifications"], list(ROUTES.values()))
        receipts = cfg["api"].get("receipts", {})
        if receipts.get("enabled", False):
            for account_id, receipt_config in receipts.get("accounts", {}).items():
                if account_id not in accounts or len(receipt_config.get("token", "")) < 24:
                    raise ValueError("Receipt accounts must exist and use tokens of at least 24 characters")
        for job in enabled:
            if job["kind"] == "sms_receipts":
                if not receipts.get("enabled") or job["account"] not in receipts.get("accounts", {}):
                    raise ValueError(f"Enable and configure receipt endpoint for {job['id']}")
    return cfg


def read_config_data(path: str | Path) -> dict:
    with Path(path).open(encoding="utf-8") as stream:
        try:
            loaded = yaml.safe_load(stream)
        except yaml.YAMLError:
            raise ValueError("Invalid YAML syntax in configuration") from None
    if not isinstance(loaded, dict):
        raise ValueError("Configuration must be a mapping")
    files = loaded.get("monitoring_files", [])
    if not isinstance(files, list) or not all(isinstance(item, str) for item in files):
        raise ValueError("monitoring_files must be a list of relative YAML paths")
    root = Path(path).resolve().parent
    for name in files:
        if Path(name).is_absolute() or Path(name).drive:
            raise ValueError("Monitoring files must use relative paths")
        target = (root / name).resolve()
        if not target.is_relative_to(root) or target == Path(path).resolve():
            raise ValueError("Monitoring files must be inside the configuration directory")
        try:
            extra = yaml.safe_load(target.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            raise ValueError("Invalid YAML syntax in monitoring file") from None
        if not isinstance(extra, dict) or set(extra) - {"accounts", "jobs", "rules"}:
            raise ValueError("Monitoring files may contain only accounts, jobs and rules")
        for section in ("accounts", "jobs", "rules"):
            if not isinstance(extra.get(section, []), list):
                raise ValueError("Monitoring accounts, jobs and rules must be lists")
            loaded.setdefault(section, []).extend(extra.get(section, []))
    return loaded


def load_config(path: str | Path, live: bool = False) -> dict:
    return validate_config(_expand(read_config_data(path)), live=live)


def _validate_job(job, account):
    if uses_relay(account):
        from .providers.relay import validate_job
        validate_job(account, job["kind"], job.get("options", {}))
        return
    if account["provider"] in EXTERNAL_PROVIDERS:
        module = provider_module(account["provider"])
        if job["kind"] not in module.SUPPORTED_KINDS:
            raise ValueError(f"Unsupported collector {job['kind']} for {account['provider']}")
        if hasattr(module, "validate_job"):
            module.validate_job(account, job["kind"], job.get("options", {}))
        else:
            module.validate_options(job["kind"], job.get("options", {}))
        return
    from .providers.aliyun import SUPPORTED_KINDS as ALIYUN_KINDS
    from .providers.huawei import SUPPORTED_KINDS as HUAWEI_KINDS
    supported = ALIYUN_KINDS | {"sms_receipts"} if account["provider"] == "aliyun" else HUAWEI_KINDS
    kind, options = job["kind"], job.get("options", {})
    if kind not in supported:
        raise ValueError(f"Unsupported collector {kind} for {account['provider']}")
    if kind == "rds_performance":
        from .providers.rds_performance import validate_rds_performance_options
        validate_rds_performance_options(options)
    if kind == "rds_cloudmonitor":
        from .providers.rds_cloudmonitor import validate_rds_cloudmonitor_options
        validate_rds_cloudmonitor_options(options)
    if kind == "rds_sql":
        from .providers.rds_sql import validate_rds_sql_options
        validate_rds_sql_options(options)
    if kind == "network_traffic":
        from .providers.network_traffic import validate_network_traffic_options
        validate_network_traffic_options(options, provider=account["provider"])
    if kind in ("ecs_expiry", "rds_expiry", "metrics") and not account.get("regions"):
        raise ValueError(f"Configure regions for {job['id']}")
    if (kind == "network_traffic" and "instances" not in options and not account.get("regions")
            and not any(options.get(key) for key in ("region", "regions"))):
        raise ValueError(f"Configure regions for {job['id']}")
    if account["provider"] == "huawei":
        from .providers.huawei import endpoint_region
        if kind in ("ecs_expiry", "slb_expiry", "balance", "packages"):
            endpoint_region("bss", account.get("bss_region", "cn-north-1"))
        elif kind == "certificates":
            endpoint_region("scm", options.get("region", account.get("scm_region", "cn-north-4")))
    if account["provider"] == "huawei" and kind in ("rds_expiry", "metrics", "network_traffic"):
        if kind == "network_traffic":
            if "instances" in options:
                regions = {entry["region"] for entry in options["instances"]}
            elif options.get("regions"):
                regions = options["regions"]
            elif options.get("region"):
                regions = [options["region"]]
            else:
                regions = account["regions"]
        elif kind == "rds_expiry":
            regions = account["regions"]
        else:
            regions = {entry.get("region", options.get("region", account["regions"][0]))
                       for entry in options.get("metrics", [])}
        for region in regions:
            if not account.get("project_ids", {}).get(region):
                raise ValueError(f"Configure Huawei project_ids.{region} for {job['id']}")
    if kind == "packages" and account["provider"] == "aliyun" and not options.get("product_code"):
        raise ValueError(f"Configure actual package product_code for {job['id']}")
    if kind == "metrics":
        entries = options.get("metrics")
        if not isinstance(entries, list) or not entries:
            raise ValueError(f"Configure options.metrics for {job['id']}")
        for entry in entries:
            for field in ("namespace", "metric_name", "resource_id", "output_metric"):
                if not entry.get(field):
                    raise ValueError(f"Configure {job['id']}.metrics.{field}")
            if not entry.get("dimensions") or not all(entry["dimensions"].values()):
                raise ValueError(f"Configure exact monitoring dimensions for {job['id']}")
            if entry["output_metric"] not in {"network_in_bps", "network_out_bps"}:
                _number(entry.get("limit"), f"{job['id']}.limit", 1e-100)
            _number(entry.get("multiplier", 1), f"{job['id']}.multiplier", 1e-100)


def validate_database(database):
    if not isinstance(database, dict):
        raise ValueError("service.database must contain MySQL host/port/name/user/password")
    for key in ("host", "name", "user", "password"):
        value = database.get(key)
        if not isinstance(value, str) or not value or value.startswith("CHANGE_ME"):
            raise ValueError(f"Configure service.database.{key} for MySQL")
    if not isinstance(database.get("port", 3306), int) or not 1 <= database.get("port", 3306) <= 65535:
        raise ValueError("MySQL port must be an integer from 1 to 65535")
