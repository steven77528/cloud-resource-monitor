"""Estimate scheduled API usage offline without displaying secrets or contacting clouds."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys

import yaml


CMS_FREE_MONTHLY = 1_000_000
CMS_PAGE_SIZE = 1440
CMS_INSTANCE_BATCH_SIZE = 50
SECONDS_PER_DAY = 86400
RDS_CMS_METRICS = {"MySQL": "MySQL_SlowQueries", "PostgreSQL": "PG_SharedBufferHitRate"}


def _positive(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a positive number")
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{label} must be a positive number")
    return value


def _runs(seconds, interval):
    return math.ceil(seconds / _positive(interval, "interval"))


def _metric_estimate(entry):
    period = int(_positive(entry.get("period", 60), "metric period"))
    age = int(_positive(entry.get("max_age_seconds", max(300, period * 3)), "metric max age"))
    if period < 1 or age < 1:
        raise ValueError("Metric period and max age must be at least one second")
    # The runtime uses entry max_age + period, not the job's freshness threshold.
    window = age + period
    points = math.ceil(window / period)
    return {
        "namespace": entry.get("namespace"),
        "metric_name": entry.get("metric_name"),
        "period_seconds": period,
        "query_window_seconds": window,
        "expected_points_per_series": points,
        "expected_pages": max(1, math.ceil(points / CMS_PAGE_SIZE)),
    }


def _other_calls(job, account, monthly_runs):
    kind, options = job["kind"], job.get("options", {})
    provider = account["provider"]
    if kind in {"sms_receipts", "rds_sql"}:
        return {"minimum_monthly_calls": 0, "formula": "Local database queries; no cloud API calls"}
    if provider == "aliyun":
        regional = [options["region"]] if options.get("region") else account.get("regions", [])
        per_run = len(set(regional)) if kind in {"ecs_expiry", "rds_expiry"} else 1
        if kind == "certificates":
            per_run = len(options.get("statuses", [None, "EXPIRED"]))
            formula = f"{monthly_runs} * (certificate list pages + selected certificate count)"
        else:
            formula = f">= {monthly_runs * per_run}; add inventory pagination where applicable"
    elif provider == "huawei":
        per_run = 1
        if kind == "rds_expiry":
            regions = options.get("regions", account.get("regions", []))
            per_run = len(regions) * len(options.get("engines", ["MySQL", "PostgreSQL"]))
        if kind == "metrics":
            per_run = len(options.get("metrics", []))
            formula = f"{monthly_runs * per_run} CES ShowMetricData calls"
        else:
            formula = f">= {monthly_runs * per_run}; add inventory pagination where applicable"
    else:
        return {"minimum_monthly_calls": None, "formula": "Unknown provider; not estimated"}
    return {"minimum_monthly_calls": monthly_runs * per_run, "formula": formula}


def _rds_plan(account_id, instances, interval, cache_seconds, seconds, source):
    performance_per_instance = _runs(seconds, interval)
    metadata_per_instance = _runs(seconds, cache_seconds)
    return {
        "account": account_id,
        "source": source,
        "instance_count": instances,
        "interval_seconds": interval,
        "metadata_cache_seconds": cache_seconds,
        "performance_calls": None if instances is None else performance_per_instance * instances,
        "metadata_calls": None if instances is None else metadata_per_instance * instances,
        "performance_formula": f"N * {performance_per_instance} DescribeDBInstancePerformance calls",
        "metadata_formula": f"N * {metadata_per_instance} DescribeDBInstanceAttribute calls",
        "cms_calls": 0,
        "assumption": "One performance request per instance per run; warm metadata cache across runs",
    }


def _rds_cloudmonitor_plan(options, interval, seconds):
    instances = options.get("instances", [])
    if not isinstance(instances, list):
        raise ValueError("RDS CloudMonitor instances must be a list")
    window = _positive(options.get("lookback_seconds", 900), "lookback_seconds")
    cache = _positive(options.get("metadata_cache_seconds", SECONDS_PER_DAY), "metadata_cache_seconds")
    # Query boundaries align to minutes; allow both boundary points conservatively.
    points = math.floor(window / 60) + 2
    grouped = {}
    for instance in instances:
        engine = instance["engine"]
        if engine not in RDS_CMS_METRICS:
            raise ValueError("Unsupported RDS CloudMonitor engine")
        key = (instance["region"], engine)
        grouped[key] = grouped.get(key, 0) + 1
    groups = []
    for (region, engine), count in grouped.items():
        batch_sizes = [min(CMS_INSTANCE_BATCH_SIZE, count - offset)
                       for offset in range(0, count, CMS_INSTANCE_BATCH_SIZE)]
        groups.append({
            "region": region, "engine": engine, "metric_name": RDS_CMS_METRICS[engine],
            "instance_count": count, "batch_instance_counts": batch_sizes,
            "minimum_calls_per_run": len(batch_sizes),
            "expected_calls_per_run": sum(math.ceil(points * size / CMS_PAGE_SIZE) for size in batch_sizes),
        })
    runs = _runs(seconds, interval)
    return {
        "instance_count": len(instances), "groups": groups,
        "period_seconds": 60, "lookback_seconds": window,
        "conservative_points_per_series": points,
        "cms_minimum_calls_if_enabled": runs * sum(group["minimum_calls_per_run"] for group in groups),
        "cms_expected_calls_if_enabled": runs * sum(group["expected_calls_per_run"] for group in groups),
        "metadata_cache_seconds": cache,
        "metadata_calls_if_enabled": _runs(seconds, cache) * len(groups),
        "metadata_api": "DescribeMetricMetaList",
        "metadata_uses_cms_free_pool": False,
    }


def _cached_runs(seconds, interval, cache_seconds):
    refresh_interval = interval * max(1, math.ceil(_positive(cache_seconds, "cache seconds") / interval))
    return _runs(seconds, refresh_interval)


def _network_plan(account, options, interval, seconds):
    from cloud_monitor.providers.network_traffic import validate_network_traffic_options

    provider, product = account["provider"], options.get("product")
    if provider not in {"aliyun", "huawei"} or product not in {"ecs", "rds"}:
        raise ValueError("Network traffic requires Alibaba Cloud or Huawei ECS/RDS")
    validate_network_traffic_options(options, provider=provider)
    instances = options.get("instances")
    count = None if instances is None else len(instances)
    regions = ({instance["region"] for instance in instances} if instances is not None else
               set([options["region"]] if options.get("region") else
                   options.get("regions", account.get("regions", []))))
    if not regions and instances is None:
        raise ValueError("Network traffic requires at least one region")
    estimated = options.get("scope") == "intranet_estimated"
    period = options.get("period", 300 if provider == "huawei" and (product == "ecs" or estimated) else 60)
    spec = _metric_estimate({"period": period,
                             "max_age_seconds": options.get("max_age_seconds", max(300, period * 3))})
    pages = spec["expected_pages"] if provider == "aliyun" else 1
    runs = _runs(seconds, interval)
    monitoring_api = "DescribeMetricList" if provider == "aliyun" else "ShowMetricData"
    inventory_api = ("DescribeInstances" if product == "ecs" else "DescribeDBInstances") if provider == "aliyun" else (
        "ListMetrics" if product == "ecs" else "ListInstances")
    inventory_cache = _positive(options.get("inventory_cache_seconds", 300), "inventory_cache_seconds")
    inventory_refreshes = _cached_runs(seconds, interval, inventory_cache) if instances is None else 0
    inventory_groups = len(regions)
    if provider == "huawei" and product == "rds":
        inventory_groups *= len(set(options.get("engines", ["MySQL", "PostgreSQL"])))
    inventory_minimum = inventory_refreshes * inventory_groups
    metadata_cache = _positive(options.get("metadata_cache_seconds", SECONDS_PER_DAY), "metadata_cache_seconds")
    metadata_refreshes = _cached_runs(seconds, interval, metadata_cache) if provider == "aliyun" else 0
    metadata_calls = (metadata_refreshes * len(regions) * 2 if count is not None else None
                      ) if provider == "aliyun" else 0
    metadata_api = "DescribeMetricMetaList" if provider == "aliyun" else None
    metadata_formula = (f"G * {metadata_refreshes} DescribeMetricMetaList calls; G = active region/metric groups"
                        if provider == "aliyun" else "No metadata API queries")
    if provider == "huawei" and product == "ecs" and instances is None:
        metadata_api, metadata_cache, metadata_calls = "ListServersDetails", inventory_cache, None
        metadata_formula = (f"sum(ceil(N_region / 100)) * {inventory_refreshes} ListServersDetails calls; "
                            "N_region = CES-discovered instances per region")
    plan = {
        "instance_count": count, "directions_per_instance": 2,
        "status": "instance_count_required" if count is None else "estimated",
        "monitoring_api": monitoring_api, "period_seconds": spec["period_seconds"],
        "query_window_seconds": spec["query_window_seconds"] if provider == "aliyun" else
        options.get("lookback_seconds", 1200),
        "expected_pages_per_direction": pages,
        "monitoring_minimum_calls_if_enabled": None if count is None else runs * count * 2,
        "monitoring_expected_calls_if_enabled": None if count is None else runs * count * 2 * pages,
        "monitoring_formula": f"N * {runs} * 2 * {pages} {monitoring_api} calls",
        "inventory_api": inventory_api, "inventory_cache_seconds": inventory_cache,
        "inventory_refreshes_if_enabled": inventory_refreshes,
        "inventory_minimum_calls_if_enabled": inventory_minimum,
        "inventory_formula": f">= {inventory_refreshes} * {inventory_groups} {inventory_api} calls; add list pages",
        "metadata_api": metadata_api,
        "metadata_cache_seconds": metadata_cache if metadata_api else None,
        "metadata_calls_if_enabled": metadata_calls,
        "metadata_formula": metadata_formula,
        "metadata_uses_cms_free_pool": False,
        "assumption": "Two independent monitoring queries per instance; inventory pages are lower bounds; warm caches",
    }
    if estimated:
        mapping_refreshes = _cached_runs(seconds, interval, inventory_cache) if count != 0 else 0
        mapping_regions = None if instances is None else len(regions)
        mapping_minimum = mapping_refreshes * (mapping_regions or 0)
        metadata_api = "ListServersDetails" if product == "ecs" else "ListInstances"
        metadata_formula = (f">= {mapping_minimum} {metadata_api} calls for EIP mapping; "
                            f"sum(resource_pages_region) * {mapping_refreshes}; "
                            "only regions with selected instances; page size = 100")
        if product == "ecs" and instances is None:
            metadata_formula += (f" + sum(ceil(N_region / 100)) * {inventory_refreshes} "
                                 "ListServersDetails name lookup calls")
        plan.update({
            "status": "instance_count_required" if count is None else
                      "eip_count_required" if count else "estimated",
            "eip_count": 0 if count == 0 else None,
            "minimum_eip_count": count,
            "query_window_seconds": options.get("lookback_seconds", 1800),
            "monitoring_minimum_calls_if_enabled": None if count is None else runs * count * 4,
            "monitoring_expected_calls_if_enabled": 0 if count == 0 else None,
            "monitoring_formula": (f"(N + E) * {runs} * 2 ShowMetricData calls; "
                                   "E = mapped EIPs across selected instances; E >= N when every mapping is valid"),
            "metadata_api": metadata_api,
            "metadata_cache_seconds": inventory_cache,
            "metadata_calls_if_enabled": 0 if count == 0 else None,
            "metadata_minimum_calls_if_enabled": mapping_minimum,
            "metadata_formula": metadata_formula,
            "mapping_region_count": mapping_regions,
            "maximum_mapping_region_count": len(regions),
            "eip_inventory_api": "ListPublicips",
            "eip_inventory_cache_seconds": inventory_cache,
            "eip_inventory_refreshes_if_enabled": mapping_refreshes,
            "eip_inventory_minimum_calls_if_enabled": mapping_minimum,
            "eip_inventory_formula": (f">= {mapping_minimum} ListPublicips calls; "
                                      f"sum(EIP_pages_region) * {mapping_refreshes}; "
                                      "only regions with selected instances; page size = 100"),
            "assumption": ("Successful collection of every selected instance with complete EIP mappings; "
                           "two total and two queries per mapped EIP per run; mapping metadata and EIPs "
                           "are listed per active region even for explicit instances; caches are per job; "
                           "inventory pages and unknown EIP counts require live inventory for exact totals"),
        })
    return plan


def estimate(config, days=31, owners=None, rds_instances=None):
    """Return a whitelist-only report. Owners are optional, verified logical-account -> UID mappings."""
    seconds = _positive(days, "days") * SECONDS_PER_DAY
    owners, rds_instances = owners or {}, rds_instances or {}
    if not isinstance(config, dict):
        raise ValueError("Configuration must be a mapping")
    accounts = {account["id"]: account for account in config.get("accounts", [])}
    if not set(owners).issubset(accounts) or not set(rds_instances).issubset(accounts):
        raise ValueError("Overrides must reference existing accounts")
    for account_id in set(owners) | set(rds_instances):
        if accounts[account_id]["provider"] != "aliyun":
            raise ValueError("Owner and RDS planning overrides apply only to Alibaba Cloud accounts")
    for count in rds_instances.values():
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("RDS instance count must be a nonnegative integer")
    report = {"days": days, "accounts": [], "jobs": [], "cms_pools": [], "rds_plans": []}
    pools, rds_accounts = {}, set()
    for account_id, account in accounts.items():
        report["accounts"].append({
            "id": account_id, "provider": account["provider"],
            "enabled": account.get("enabled", False), "owner_uid": owners.get(account_id),
        })
    for job in config.get("jobs", []):
        account = accounts[job["account"]]
        enabled = bool(account.get("enabled", False) and job.get("enabled", True))
        interval = _positive(job["interval_seconds"], "job interval")
        runs = _runs(seconds, interval) if enabled else 0
        options = job.get("options", {})
        row = {
            "id": job["id"], "account": job["account"], "provider": account["provider"],
            "kind": job["kind"], "enabled": enabled, "interval_seconds": interval,
            "scheduled_runs": runs, "metric_count": len(options.get("metrics", [])),
            "cms_minimum_calls": 0, "cms_expected_calls": 0,
        }
        if account["provider"] == "aliyun" and job["kind"] == "metrics":
            entries = [_metric_estimate(entry) for entry in options.get("metrics", [])]
            row["metrics"] = entries
            row["cms_minimum_calls"] = runs * len(entries)
            row["cms_expected_calls"] = runs * sum(entry["expected_pages"] for entry in entries)
        elif account["provider"] == "aliyun" and job["kind"] == "rds_cloudmonitor":
            plan = _rds_cloudmonitor_plan(options, interval, seconds)
            row["cloudmonitor_plan"] = plan
            row["metric_count"] = len(plan["groups"])
            if enabled:
                row["cms_minimum_calls"] = plan["cms_minimum_calls_if_enabled"]
                row["cms_expected_calls"] = plan["cms_expected_calls_if_enabled"]
            row["other_calls"] = {
                "minimum_monthly_calls": plan["metadata_calls_if_enabled"] if enabled else 0,
                "formula": "DescribeMetricMetaList once per region/metric/cache lifetime, per job",
            }
        elif account["provider"] == "aliyun" and job["kind"] == "rds_performance":
            instances = options.get("instances")
            count = len(instances) if isinstance(instances, list) and instances else None
            plan = _rds_plan(job["account"], count, interval,
                             options.get("metadata_cache_seconds", SECONDS_PER_DAY), seconds, job["id"])
            plan["enabled"] = enabled
            report["rds_plans"].append(plan)
            if enabled:
                rds_accounts.add(job["account"])
            scheduled_calls = None if count is None else plan["performance_calls"] + plan["metadata_calls"]
            row["other_calls"] = {"minimum_monthly_calls": scheduled_calls if enabled else 0,
                                  "formula": "See rds_plans; disabled jobs make no calls"}
        elif job["kind"] == "network_traffic":
            plan = _network_plan(account, options, interval, seconds)
            row["network_plan"] = plan
            row["metric_count"] = None if plan["instance_count"] is None else plan["instance_count"] * 2
            metadata_calls = plan.get("metadata_minimum_calls_if_enabled", plan["metadata_calls_if_enabled"])
            inventory_calls = plan["inventory_minimum_calls_if_enabled"]
            other_minimum = (inventory_calls + (metadata_calls or 0)
                             + plan.get("eip_inventory_minimum_calls_if_enabled", 0))
            if account["provider"] == "aliyun":
                if enabled:
                    row["cms_minimum_calls"] = plan["monitoring_minimum_calls_if_enabled"] or 0
                    row["cms_expected_calls"] = plan["monitoring_expected_calls_if_enabled"]
                formula = "Inventory list pages + DescribeMetricMetaList metadata; monitoring is in CMS totals"
            else:
                other_minimum += plan["monitoring_minimum_calls_if_enabled"] or 0
                formula = plan["monitoring_formula"] + " + inventory list pages"
                if plan["metadata_api"]:
                    formula += " + " + plan["metadata_formula"]
                if plan.get("eip_inventory_api"):
                    formula += " + " + plan["eip_inventory_formula"]
            row["other_calls"] = {
                "minimum_monthly_calls": other_minimum if enabled else 0,
                "complete": not enabled or (plan["instance_count"] is not None
                                            and plan["monitoring_expected_calls_if_enabled"] is not None),
                "formula": formula,
            }
        else:
            row["other_calls"] = _other_calls(job, account, runs)
        report["jobs"].append(row)
        if account["provider"] == "aliyun" and enabled:
            owner = owners.get(job["account"])
            key = f"owner:{owner}" if owner else f"unverified:{job['account']}"
            pool = pools.setdefault(key, {
                "pool": key, "owner_uid": owner, "accounts": [],
                "minimum_calls": 0, "expected_calls": 0,
            })
            if job["account"] not in pool["accounts"]:
                pool["accounts"].append(job["account"])
            pool["minimum_calls"] += row["cms_minimum_calls"]
            pool["expected_calls"] += row["cms_expected_calls"] or 0
            if row["cms_expected_calls"] is None:
                pool.setdefault("unestimated_jobs", []).append(row["id"])
    for pool in pools.values():
        pool["free_monthly_calls"] = CMS_FREE_MONTHLY
        incomplete = bool(pool.get("unestimated_jobs"))
        pool["remaining_calls"] = None if incomplete else CMS_FREE_MONTHLY - pool["expected_calls"]
        pool["usage_percent"] = None if incomplete else round(pool["expected_calls"] / CMS_FREE_MONTHLY * 100, 4)
        pool["status"] = ("instance_count_required" if incomplete else
                          "exceeds_free_quota" if pool["remaining_calls"] < 0 else "within_free_quota")
        report["cms_pools"].append(pool)
    for account_id, account in accounts.items():
        if account["provider"] != "aliyun" or not account.get("enabled", False):
            continue
        if account_id in rds_instances or account_id not in rds_accounts:
            report["rds_plans"].append(_rds_plan(
                account_id, rds_instances.get(account_id), 60, SECONDS_PER_DAY, seconds,
                "planning_override" if account_id in rds_instances else "optional_plan",
            ))
    report["notes"] = [
        "CMS totals include Alibaba Cloud metrics, rds_cloudmonitor and network_traffic, not native RDS or Huawei APIs.",
        "Unverified logical accounts may share an owner and quota; supply --owner for confirmed grouping.",
        "Expected pages assume one exact time series and one point per period; extra series add pages.",
        "Counts exclude retries, restarts, manual diagnose/check/run and other systems using the same owner.",
        "Other API figures are lower bounds unless stated; resource discovery and detail calls can add usage.",
        "RDS plans are separate scenarios, not new CMS consumption; metadata counts assume a warm cache.",
        "RDS CloudMonitor batches at most 50 instances per region/engine within each job/account.",
        "DescribeMetricMetaList metadata queries do not consume the four-API CMS monthly free pool.",
        "Huawei has its own quotas; the Alibaba Cloud 1000000/month allowance does not apply to Huawei.",
        "Network auto-discovery needs instance counts; incomplete CMS pools show only known calls and no quota verdict.",
        "Network monitoring queries both directions separately; inventory caches refresh on the next scheduled run.",
    ]
    return report


def _mapping(values, integer=False):
    result = {}
    for value in values:
        key, separator, item = value.partition("=")
        if not separator or not key or not item or key in result:
            raise ValueError("Overrides require unique ACCOUNT=VALUE entries")
        result[key] = int(item) if integer else item
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("--days", type=int, default=31)
    parser.add_argument("--owner", action="append", default=[], metavar="ACCOUNT=UID")
    parser.add_argument("--rds-instances", action="append", default=[], metavar="ACCOUNT=COUNT")
    args = parser.parse_args(argv)
    try:
        # Direct script execution must work outside the project directory too.
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from cloud_monitor.config import read_config_data

        config = read_config_data(args.config)
        result = estimate(config, args.days, _mapping(args.owner), _mapping(args.rds_instances, True))
    except (OSError, ValueError, KeyError, TypeError, AttributeError, yaml.YAMLError):
        # YAML parser errors can include full source lines containing credentials.
        parser.exit(2, "Cannot estimate: invalid configuration, arguments, or unreadable configuration file.\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
