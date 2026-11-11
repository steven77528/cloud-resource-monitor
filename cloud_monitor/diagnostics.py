"""Read-only collection checks and deliberately limited, credential-safe error details."""

import re

from .config import _validate_job
from .models import utcnow
from .providers.huawei import HuaweiCollectionError
from .providers import collect_job, make_collector, validate_account_credentials
from .providers.common import ProviderError
from .providers.aliyun import SmsStatisticsInsufficientData


_LOCAL_MESSAGES = frozenset({
    "CMS network metric metadata is missing or ambiguous",
    "CMS network metric metadata does not match dimensions, period or statistic",
    "CMS network metric metadata has an unsupported rate unit",
    "CES network rate has an unexpected unit",
    "CES returned a negative network rate",
    "Huawei MySQL cluster network metrics require node dimensions; use a separate node collector",
    "Huawei network estimate has an invalid endpoint or project",
    "Huawei credentials are required for network estimates",
    "Huawei network estimate response is not an object",
    "Huawei network estimate inventory has invalid resources",
    "Huawei network estimate inventory repeated a resource",
    "Huawei network estimate inventory has an invalid total",
    "Huawei network estimate inventory is incomplete",
    "Huawei network estimate inventory exceeded pagination limit",
    "Huawei RDS estimate metadata has invalid public addresses",
    "Huawei ECS estimate metadata has invalid interfaces",
    "Huawei ECS estimate metadata has invalid addresses",
    "Huawei ECS estimate metadata has an invalid public address",
    "Huawei ECS estimate metadata has an invalid interface",
    "Huawei ECS estimate metadata has an invalid association",
    "Huawei network estimate source has no datapoints",
    "Huawei network estimate source has an invalid datapoint",
    "Huawei network estimate source has an invalid rate, unit or timestamp",
    "Huawei network estimate rate is not finite",
    "Huawei RDS topology or engine does not support network estimates",
    "Huawei network estimate is waiting for a closed window after an EIP binding change",
    "Huawei network estimate sources have no common timestamp for the current EIP binding",
    "Huawei network estimate sources are stale or in the future",
    "Huawei network estimate has a negative or non-finite difference",
    "Huawei network estimates require a 300-second period",
    "Huawei network estimate has no complete unambiguous EIP mapping",
    "SMS statistics contains negative counts or duplicate dates",
    "SMS statistics contains invalid or out-of-range date",
    "SMS seven-day baseline is zero; percentage growth is undefined",
    "Huawei access_key_id and access_key_secret are required",
    "Huawei subscription resource_ids accepts at most 50 IDs per job",
    "Huawei RDS expiry requires at least one region",
    "Huawei RDS engines must be MySQL and/or PostgreSQL",
    "Huawei package service_type_codes must not be empty",
    "Huawei metrics collector needs explicit options.metrics entries",
    "Huawei CES requires 1-4 ordered dimensions as a mapping",
    "Unsupported Huawei CES period",
    "Unsupported Huawei CES filter",
    "CES lookback must cover a period and max_age_seconds must be positive",
    "Huawei normalized metric limit must be positive",
    "Set account.regions or job.options.region for regional resources",
    "balance_field must be AvailableAmount or AvailableCashAmount",
    "options.metrics must be a non-empty list",
    "Each metric needs one exact, non-empty dimensions mapping",
    "Metric limit and multiplier must be positive",
    "Metric period/max_age_seconds must be positive; max age <= 30 days",
    "Use the exact CloudMonitor statistic name in aggregation",
    "CloudMonitor Datapoints must be a JSON list",
    "CloudMonitor pagination token repeated or exceeded limit",
    "Resource has no identifier",
    "Pagination ended before total_count",
    "Pagination exceeded safety limit",
    "Configured subscription IDs absent from BSS response; check billing mode and region",
    "RDS instance has no datastore.type",
    "RDS instance has unknown charge_info.charge_mode",
    "Configured RDS IDs absent from selected MySQL/PostgreSQL inventory",
    "Balance currency absent from response",
    "Unsupported balance measure_id; expected currency units",
    "Configured certificate IDs absent or not issued/uploaded/expired",
    "Package resource item has no free_resource_id",
    "Package quota must have positive total and nonnegative remaining amount",
    "Configured package IDs absent or not active/exhausted/expired",
    "Huawei bss endpoint region is unsupported; set account.bss_region to cn-north-1 for mainland China",
    "Huawei scm endpoint region is unsupported; set account.scm_region to cn-north-4 for mainland China",
    "Huawei ces endpoint region is unsupported; check the resource region",
    "Huawei rds endpoint region is unsupported; check the resource region",
})
_HUAWEI_OPERATIONS = {
    "bss": {"list_pay_per_use_customer_resources", "show_customer_account_balances", "list_free_resource_infos"},
    "scm": {"list_certificates"},
    "rds": {"list_instances"},
    "ces": {"show_metric_data", "list_metrics"},
}
_HUAWEI_API = re.compile(
    r"Huawei (bss|scm|rds|ces)\.([a-z_]{1,60}) failed "
    r"\(status=(unknown|[1-5][0-9]{2}), code=([A-Za-z0-9_.-]{1,64})\)"
)
_HUAWEI_NETWORK_ESTIMATE_API = re.compile(
    r"Huawei (ecs|rds|vpc|ces) network estimate query failed "
    r"\(status=(unknown|[1-5][0-9]{2}), code=([A-Za-z0-9_.-]{1,64})\)"
)
_ERROR_CODES = frozenset({
    "unknown", "KeyError", "RuntimeError", "TypeError", "AttributeError", "ValueError",
    "ConnectionError", "TimeoutError", "OSError", "ImportError", "ModuleNotFoundError",
    "ConnectTimeout", "ReadTimeout", "ProxyError", "SSLError", "ClientRequestException",
    "ServerResponseException", "ServiceResponseException",
})
_REGION = r"[a-z]{2,3}-[a-z]{2,16}-[0-9]{1,2}"
_HUAWEI_REGION = re.compile(
    rf"Huawei (bss|scm|rds|ces) endpoint region ({_REGION}) is unsupported; "
    rf"set account\.\1_region to ({_REGION})"
)
_LOCAL_PATTERNS = (
    r"SMS daily statistics missing dates: [0-9]{4}-[0-9]{2}-[0-9]{2}(?:, [0-9]{4}-[0-9]{2}-[0-9]{2}){0,7}",
    r"Huawei CES metric requires (?:namespace|metric_name|region|resource_id|output_metric)",
    r"(?:account_type|CES period|lookback_seconds|max_age_seconds) must be an integer",
    rf"Huawei project_ids\[{_REGION}\] is required for (?:CES|RDS)",
    r"Response has no valid (?:data|instances|account_balances|certificates|free_resource_packages|free_resources|datapoints) list",
    r"Balance account_type=-?[0-9]{1,3} absent from response",
)


def safe_collection_error(exc: Exception) -> str:
    """Expose known local reasons, never arbitrary SDK messages or response fields."""
    if isinstance(exc, ProviderError):
        from .providers.wechat import WechatAPIError
        detail = "ProviderError: " + ProviderError(exc.reason, exc.field).args[0]
        if type(exc) is WechatAPIError:
            code = getattr(exc, "api_code", None)
            if type(code) is int and -1_000_000_000 <= code <= 1_000_000_000:
                detail += f" code={code}"
        return detail
    from .providers.rds_performance import RdsPerformanceError
    from .providers.rds_cloudmonitor import RdsCloudMonitorError
    if type(exc) is RdsPerformanceError:
        return f"RdsPerformanceError: {exc}"
    if type(exc) is RdsCloudMonitorError:
        return f"RdsCloudMonitorError: {exc}"
    name = type(exc).__name__
    if name == "ServerException" and type(exc).__module__.startswith("aliyunsdkcore."):
        code = exc.get_error_code()
        if code in {"Forbidden", "Forbidden.RAM", "NoPermission", "AccessDenied", "Throttling",
                    "Throttling.User", "Throttling.Api", "InvalidDBInstanceId.NotFound"}:
            return f"ServerException: code={code}"
    if type(exc) not in (ValueError, HuaweiCollectionError, SmsStatisticsInsufficientData):
        return name
    message = str(exc)
    detail = None
    sql_reasons = {
        "RDS SQL statistics are missing or invalid", "RDS SQL returned no statistics",
        "RDS SQL deadlock statistics are disabled or unavailable",
        "RDS SQL requires pg_read_all_stats and track_counts",
        "RDS SQL returned duplicate database statistics",
        "RDS SQL requires server TLS before authentication",
        "RDS SQL requires observable PostgreSQL activities",
    }
    if message in _LOCAL_MESSAGES | sql_reasons or any(re.fullmatch(pattern, message) for pattern in _LOCAL_PATTERNS):
        detail = message
    elif isinstance(exc, HuaweiCollectionError):
        match = _HUAWEI_API.fullmatch(message)
        if (match and match[2] in _HUAWEI_OPERATIONS[match[1]]
                and (match[4] in _ERROR_CODES or re.fullmatch(r"[A-Z][A-Z0-9]{1,11}\.[0-9]{1,8}", match[4]))):
            detail = message
        elif ((estimate_match := _HUAWEI_NETWORK_ESTIMATE_API.fullmatch(message))
              and (estimate_match[3] in _ERROR_CODES
                   or re.fullmatch(r"[A-Z][A-Z0-9]{1,11}\.[0-9]{1,8}", estimate_match[3]))):
            detail = message
        elif _HUAWEI_REGION.fullmatch(message):
            detail = message
        else:
            # Resource identifiers and response-derived labels are omitted, even in local errors.
            for prefix, reason in (
                ("CES has no datapoints for ", "CES has no datapoints; check region, metric and dimensions"),
                ("CES datapoint is stale or in the future for ", "CES datapoint is stale or in the future"),
                ("Missing or invalid numeric field: ", "Missing or invalid numeric field in Huawei response/configuration"),
                ("Invalid numeric field: ", "Invalid numeric field in Huawei response/configuration"),
                ("Non-finite numeric field: ", "Non-finite numeric field in Huawei response/configuration"),
                ("Invalid time field: ", "Invalid time field in Huawei response"),
                ("Missing time field: ", "Missing time field in Huawei response"),
            ):
                if message.startswith(prefix):
                    detail = reason
                    break
    else:
        for prefix, reason in (
            ("No CloudMonitor data for ", "No CloudMonitor data; check region, metric and dimensions"),
            ("Stale or future CloudMonitor data for ", "Stale or future CloudMonitor data"),
            ("Ambiguous CloudMonitor series for ", "Ambiguous CloudMonitor series; specify all dimensions"),
            ("Negative CloudMonitor measurement for ", "Negative CloudMonitor measurement"),
        ):
            if message.startswith(prefix):
                detail = reason
                break
    return f"{name}: {detail}" if detail else name


def _collector(account):
    return make_collector(account)


def _select_jobs(config, job_ids):
    accounts = {account["id"]: account for account in config["accounts"]}
    jobs = {job["id"]: job for job in config["jobs"]}
    if job_ids:
        selected = []
        for job_id in dict.fromkeys(job_ids):
            if job_id not in jobs:
                raise ValueError(f"Unknown diagnostic job: {job_id}")
            job = jobs[job_id]
            if not job.get("enabled", True) or not accounts[job["account"]].get("enabled", False):
                raise ValueError(f"Diagnostic job or its account is disabled: {job_id}")
            selected.append(job)
    else:
        selected = [job for job in jobs.values() if job.get("enabled", True)
                    and accounts[job["account"]].get("enabled", False)]
    if not selected:
        raise ValueError("No enabled jobs available for diagnostics")
    # Finish preflight for every selected job before making the first cloud request.
    for job in selected:
        if job["kind"] == "sms_receipts":
            continue
        account = accounts[job["account"]]
        validate_account_credentials(account)
        _validate_job(job, account)
    return accounts, selected


def diagnose_jobs(config, job_ids=None, collector_factory=None, now=None):
    """Yield one safe report per job without creating stores, alerts or notifications."""
    accounts, selected = _select_jobs(config, job_ids)
    factory = collector_factory or _collector
    now = now or utcnow()
    for job in selected:
        report = {"job": job["id"], "account": job["account"], "status": "no_data", "samples": 0}
        if job["kind"] == "sms_receipts":
            yield {**report, "skipped": True, "detail": "sms_receipts requires stored receipt data; skipped"}
            continue
        collector = None
        try:
            collector = factory(accounts[job["account"]])
            metrics = collect_job(collector, job, now)
            report.update(status="ok" if metrics else "no_data", samples=len(metrics))
            if job["kind"] == "network_traffic" and getattr(collector, "network_partial_failures", 0):
                report.update(status="error", error_type="NetworkPartialFailure",
                              detail=f"Network collection partially failed; failed_resources={collector.network_partial_failures}")
        except SmsStatisticsInsufficientData as exc:
            report.update(status="insufficient_data", detail=safe_collection_error(exc))
        except Exception as exc:
            report.update(status="error", error_type=type(exc).__name__, detail=safe_collection_error(exc))
        if job["kind"] == "network_traffic" and collector is not None:
            unavailable = getattr(collector, "network_estimate_unavailable", [])
            if unavailable:
                report["unavailable_resources"] = len(unavailable)
                if report["status"] != "error":
                    report.update(status="degraded", detail=
                                  f"Network estimate unavailable; unavailable_resources={len(unavailable)}")
        yield report
