"""Read-only resource inventory and network-rate specifications for CMS and CES."""

from __future__ import annotations

import json
import logging
from datetime import datetime


LOG = logging.getLogger(__name__)
OUTPUT_METRICS = frozenset({"network_in_bps", "network_out_bps"})
_ENGINES = frozenset({"MySQL", "PostgreSQL"})
_OPTIONS = frozenset({"product", "instances", "region", "regions", "engines",
                      "inventory_cache_seconds", "period", "lookback_seconds", "max_age_seconds",
                      "enterprise_project_id", "metadata_cache_seconds", "scope"})


def validate_network_traffic_options(options: dict, provider: str | None = None) -> None:
    """Validate inventory and timing choices without constructing cloud clients."""
    if (not isinstance(options, dict) or not isinstance(options.get("product"), str)
            or options["product"] not in {"ecs", "rds"}):
        raise ValueError("Network traffic product must be ecs or rds")
    if options.keys() - _OPTIONS:
        raise ValueError("Network traffic configuration contains an unsupported option")
    estimated = options.get("scope") == "intranet_estimated"
    if "scope" in options and (not estimated or provider not in {None, "huawei"}):
        raise ValueError("Explicit network scope only supports Huawei intranet_estimated")
    for field in ("inventory_cache_seconds", "metadata_cache_seconds", "period", "lookback_seconds", "max_age_seconds"):
        value = options.get(field, 300)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("Network traffic time settings must be positive integers")
    period = options.get("period", 300 if provider == "huawei" and (options["product"] == "ecs" or estimated) else 60)
    if period not in {60, 300}:
        raise ValueError("Network traffic period must be 60 or 300 seconds")
    if provider == "huawei" and options["product"] == "ecs" and period != 300:
        raise ValueError("Huawei ECS network metrics require a 300-second period")
    if estimated and period != 300:
        raise ValueError("Huawei network estimates require a 300-second period")
    if options.get("lookback_seconds", max(900, period * 3)) < period:
        raise ValueError("Network traffic lookback must cover at least one period")
    if options.get("max_age_seconds", max(300, period * 3)) < period:
        raise ValueError("Network traffic max_age_seconds must cover at least one period")
    if "region" in options and "regions" in options:
        raise ValueError("Network traffic accepts either region or regions")
    if "region" in options and not _text(options["region"]):
        raise ValueError("Network traffic region must be a nonempty string")
    if "regions" in options and (not isinstance(options["regions"], list) or not options["regions"]
                                  or not all(_text(region) for region in options["regions"])):
        raise ValueError("Network traffic regions must be a nonempty list of region strings")
    engines = options.get("engines", ["MySQL"] if provider == "aliyun" else ["MySQL", "PostgreSQL"])
    if (not isinstance(engines, list) or not engines
            or any(not isinstance(engine, str) or engine not in _ENGINES for engine in engines)):
        raise ValueError("Network traffic engines must be MySQL and/or PostgreSQL")
    if len(engines) != len(set(engines)):
        raise ValueError("Network traffic engines must not repeat")
    if provider == "aliyun" and options["product"] == "rds" and "PostgreSQL" in engines:
        raise ValueError("Alibaba RDS network traffic currently supports MySQL only")
    if "enterprise_project_id" in options and not _text(options["enterprise_project_id"]):
        raise ValueError("Network traffic enterprise_project_id must be a nonempty string")
    if "instances" not in options:
        return
    instances = options["instances"]
    if not isinstance(instances, list):
        raise ValueError("Explicit network traffic instances must be a list")
    if not instances and options["product"] != "ecs":
        raise ValueError("Explicit RDS network traffic instances must be a nonempty list")
    seen = set()
    for instance in instances:
        if not isinstance(instance, dict) or instance.keys() - {"resource_id", "resource_name", "region", "engine"}:
            raise ValueError("Network traffic instance contains unsupported fields")
        if any(not _text(instance.get(field)) for field in ("resource_id", "region")):
            raise ValueError("Network traffic instance requires resource_id and region")
        if "resource_name" in instance and not _text(instance["resource_name"]):
            raise ValueError("Network traffic resource_name must be a nonempty string")
        if options["product"] == "rds" and (not isinstance(instance.get("engine"), str)
                                           or instance["engine"] not in _ENGINES):
            raise ValueError("Network traffic RDS instance requires a MySQL or PostgreSQL engine")
        if provider == "aliyun" and options["product"] == "rds" and instance["engine"] != "MySQL":
            raise ValueError("Alibaba RDS network traffic currently supports MySQL only")
        identity = (instance["region"], instance["resource_id"])
        if identity in seen:
            raise ValueError("Network traffic instances must not repeat region and resource_id")
        seen.add(identity)


def _text(value) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value == value.strip()


def _get(value, key, default=None):
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


class NetworkTrafficCollector:
    def __init__(self, collector, provider: str):
        self.collector = collector
        self.provider = provider
        self._inventory_cache = {}
        self._metadata_cache = {}
        self._huawei_intranet_collector = None

    def collect(self, options: dict, now: datetime):
        self.collector.network_partial_failures = 0
        self.collector.network_estimate_unavailable = []
        self.collector.network_estimate_resources = None
        validate_network_traffic_options(options, self.provider)
        instances = options.get("instances")
        if instances is None:
            regions = ([options["region"]] if options.get("region") else
                       options.get("regions", self.collector.account.get("regions", [])))
            if not isinstance(regions, list) or not regions or not all(_text(region) for region in regions):
                raise ValueError("Network traffic requires account.regions or job regions")
            regions = list(dict.fromkeys(regions))
            key = json.dumps({"product": options["product"], "regions": regions,
                              "engines": options.get("engines", ["MySQL"] if self.provider == "aliyun"
                                                     else ["MySQL", "PostgreSQL"]),
                              "enterprise_project_id": options.get("enterprise_project_id")}, sort_keys=True)
            cached = self._inventory_cache.get(key)
            timestamp = now.timestamp()
            ttl = options.get("inventory_cache_seconds", 300)
            if cached is not None and 0 <= timestamp - cached[0] < ttl:
                instances = cached[1]
            else:
                instances = self._discover(options, regions)
                self._inventory_cache[key] = (timestamp, instances)
        if options.get("scope") == "intranet_estimated":
            self.collector.network_estimate_resources = [
                {"resource_id": instance["resource_id"], "region": instance["region"]}
                for instance in instances]
        if not instances:
            return []
        if options.get("scope") == "intranet_estimated":
            from .huawei_network_scope import HuaweiIntranetCollector
            if self._huawei_intranet_collector is None:
                self._huawei_intranet_collector = HuaweiIntranetCollector(self.collector)
            return self._huawei_intranet_collector.collect(options, instances, now)
        result, failures = [], []
        for instance in instances:
            try:
                specs = self._specs(options, instance)
                if self.provider == "aliyun":
                    for spec in specs:
                        spec.update(self._aliyun_metadata(spec, options, now))
                result.extend(self.collector._metrics({"metrics": specs, "product": options["product"]}, now))
            except Exception as exc:
                failures.append(exc)
        if failures and not result:
            raise failures[0]
        self.collector.network_partial_failures = len(failures)
        return result

    def _discover(self, options, regions):
        if self.provider == "aliyun":
            instances = self._discover_aliyun(options, regions)
        elif options["product"] == "ecs":
            instances = self._discover_huawei_ecs(regions)
        else:
            instances = self._discover_huawei_rds(options, regions)
        unique = {}
        for instance in instances:
            if not _text(instance.get("resource_id")):
                raise ValueError("Network inventory has no valid resource identifier")
            identity = (instance["region"], instance["resource_id"])
            if identity in unique:
                raise ValueError("Network inventory contains duplicate resources")
            unique[identity] = instance
        return list(unique.values())

    def _discover_aliyun(self, options, regions):
        result = []
        ecs = options["product"] == "ecs"
        engines = options.get("engines", ["MySQL"])
        for region in regions:
            rows = self.collector._pages(
                "ecs" if ecs else "rds", "DescribeInstances" if ecs else "DescribeDBInstances",
                "2014-05-26" if ecs else "2014-08-15", {"RegionId": region},
                ("Instances", "Instance") if ecs else ("Items", "DBInstance"),
                ("TotalCount",) if ecs else ("TotalRecordCount",), region)
            for row in rows:
                if not ecs and not _text(row.get("Engine")):
                    raise ValueError("RDS network inventory has no valid Engine")
                if not ecs and row.get("Engine") not in engines:
                    continue
                rid = row.get("InstanceId" if ecs else "DBInstanceId")
                instance = {"resource_id": rid, "region": region,
                            "resource_name": row.get("InstanceName" if ecs else "DBInstanceDescription") or rid}
                if not ecs:
                    instance["engine"] = row["Engine"]
                result.append(instance)
        return result

    def _discover_huawei_ecs(self, regions):
        from huaweicloudsdkces.v1 import ListMetricsRequest

        result = []
        for region in regions:
            regional = []
            start, seen, fetched = None, set(), 0
            for _ in range(10000):
                request = ListMetricsRequest(namespace="SYS.ECS", metric_name="network_vm_bandwidth_in",
                                             start=start, limit=1000)
                response = self.collector._call("ces", region, "list_metrics", request)
                rows = _get(response, "metrics")
                if not isinstance(rows, list):
                    raise ValueError("CES network inventory has no metrics list")
                for row in rows:
                    if (_get(row, "namespace") != "SYS.ECS"
                            or _get(row, "metric_name") != "network_vm_bandwidth_in"):
                        raise ValueError("CES network inventory returned an unexpected metric")
                    dimensions = _get(row, "dimensions")
                    if not isinstance(dimensions, list) or len(dimensions) != 1:
                        raise ValueError("CES ECS network inventory requires one instance_id dimension")
                    if _get(dimensions[0], "name") != "instance_id":
                        raise ValueError("CES ECS network inventory has an unexpected dimension")
                    rid = _get(dimensions[0], "value")
                    if not _text(rid):
                        raise ValueError("Network inventory has no valid resource identifier")
                    regional.append({"resource_id": rid, "resource_name": rid, "region": region})
                fetched += len(rows)
                metadata = _get(response, "meta_data")
                total = _get(metadata, "total")
                if total is not None and (type(total) is not int or total < fetched):
                    raise ValueError("CES network inventory has an invalid total")
                if total is not None and fetched >= total:
                    break
                marker = _get(metadata, "marker")
                if not marker:
                    if total is not None and fetched < total:
                        raise ValueError("CES network inventory pagination is incomplete")
                    break
                if not _text(marker):
                    raise ValueError("CES network inventory pagination marker is invalid")
                if not rows or marker in seen:
                    raise ValueError("CES network inventory pagination is empty or repeated")
                seen.add(marker)
                start = marker
            else:
                raise ValueError("CES network inventory pagination exceeded safety limit")
            names = self._huawei_ecs_names(region, [item["resource_id"] for item in regional])
            for item in regional:
                item["resource_name"] = names.get(item["resource_id"], item["resource_id"])
            result.extend(regional)
        return result

    def _huawei_ecs_names(self, region, resource_ids):
        from huaweicloudsdkecs.v2 import ListServersDetailsRequest

        names = {}
        # Exact-ID batches avoid listing unrelated servers and stay below the API's recommended limit.
        resource_ids = list(dict.fromkeys(resource_ids))
        for offset in range(0, len(resource_ids), 100):
            batch = resource_ids[offset:offset + 100]
            try:
                request = ListServersDetailsRequest(server_id=",".join(batch), limit=100)
                response = self.collector._call("ecs", region, "list_servers_details", request)
                rows = _get(response, "servers")
                if not isinstance(rows, list):
                    raise ValueError("ECS instance metadata has no servers list")
                resolved, seen = {}, set()
                for row in rows:
                    rid, name = _get(row, "id"), _get(row, "name")
                    if not _text(rid) or rid not in batch or rid in seen:
                        raise ValueError("ECS instance metadata returned unexpected or duplicate identifiers")
                    seen.add(rid)
                    if _text(name) and not _get(row, "fault"):
                        resolved[rid] = name
                names.update(resolved)
                if len(resolved) < len(batch):
                    LOG.warning("Huawei ECS instance names missing region=%s resources=%s; using resource IDs",
                                region, len(batch) - len(resolved))
            except Exception:
                # Metadata must not interrupt CES measurements or expose provider error payloads.
                LOG.warning("Huawei ECS instance name lookup failed region=%s; using resource IDs; "
                            "check ECS API access and ecs:cloudServers:list permission", region)
        return names

    def _discover_huawei_rds(self, options, regions):
        from huaweicloudsdkrds.v3 import ListInstancesRequest

        result = []
        for region in regions:
            for engine in options.get("engines", ["MySQL", "PostgreSQL"]):
                def fetch(offset, size):
                    request = ListInstancesRequest(datastore_type=engine, offset=offset, limit=size,
                                                   eps_id=options.get("enterprise_project_id"))
                    return self.collector._call("rds", region, "list_instances", request)

                for row in self.collector._pages(fetch, "instances", 100):
                    actual_engine = _get(_get(row, "datastore"), "type")
                    if not isinstance(actual_engine, str):
                        raise ValueError("RDS network inventory has no datastore.type")
                    if actual_engine.casefold() != engine.casefold():
                        continue
                    rid = _get(row, "id")
                    result.append({"resource_id": rid, "resource_name": _get(row, "name") or rid,
                                   "region": region, "engine": engine, "topology": _get(row, "type")})
        return result

    def _specs(self, options, instance):
        product = options["product"]
        if (self.provider == "huawei" and product == "rds" and instance["engine"] == "MySQL"
                and str(instance.get("topology", "")).casefold() in {"cluster", "enterprise"}):
            raise ValueError("Huawei MySQL cluster network metrics require node dimensions; use a separate node collector")
        period = options.get("period", 300 if self.provider == "huawei" and product == "ecs" else 60)
        common = {**instance, "product": product, "period": period,
                  "lookback_seconds": options.get("lookback_seconds", 1200 if self.provider == "huawei" else max(900, period * 3)),
                  "max_age_seconds": options.get("max_age_seconds", 900 if self.provider == "huawei" else max(300, period * 3)),
                  "sample_max_gap_seconds": period * 1.5,
                  "aggregation": "Average", "filter": "average"}
        rid = instance["resource_id"]
        if self.provider == "aliyun" and product == "ecs":
            namespace, names, dimension, multiplier, scope = (
                "acs_ecs_dashboard", ("IntranetInRate", "IntranetOutRate"), "instanceId", 1, "intranet")
        elif self.provider == "aliyun":
            namespace, names, dimension, multiplier, scope = (
                "acs_rds_dashboard", ("MySQL_NetworkInNew", "MySQL_NetworkOutNew"), "instanceId", 1, "all")
        elif self.provider == "huawei" and product == "ecs":
            namespace, names, dimension, multiplier, scope = (
                "SYS.ECS", ("network_vm_bandwidth_in", "network_vm_bandwidth_out"), "instance_id", 8, "all")
            common["source_units"] = {"Byte/s": 8, "B/s": 8, "byte/s": 8, "Bytes/s": 8, "bytes/s": 8}
        elif self.provider == "huawei":
            namespace, names, dimension, multiplier, scope = (
                "SYS.RDS", ("rds004_bytes_in", "rds005_bytes_out"), "rds_cluster_id", 8192, "all")
            if instance["engine"] == "PostgreSQL":
                dimension = "postgresql_cluster_id"
            common["source_units"] = {"Byte/s": 8, "B/s": 8, "byte/s": 8, "Bytes/s": 8, "bytes/s": 8,
                                      "KiB/s": 8192, "KB/s": 8192, "KByte/s": 8192}
        return [{**common, "namespace": namespace, "metric_name": name,
                 "dimensions": {dimension: rid}, "multiplier": multiplier, "scope": scope,
                 "output_metric": output} for name, output in zip(names, ("network_in_bps", "network_out_bps"))]

    def _aliyun_metadata(self, spec, options, now):
        key = (spec["region"], spec["namespace"], spec["metric_name"], spec["period"])
        cached = self._metadata_cache.get(key)
        if cached is not None and 0 <= now.timestamp() - cached[0] < options.get("metadata_cache_seconds", 86400):
            return cached[1]
        rows = list(self.collector._pages(
            "cms", "DescribeMetricMetaList", "2019-01-01",
            {"Namespace": spec["namespace"], "MetricName": spec["metric_name"]},
            ("Resources", "Resource"), ("TotalCount",), spec["region"]))
        matching = [row for row in rows if row.get("MetricName") == spec["metric_name"]
                    and row.get("Namespace") == spec["namespace"]]
        if len(matching) != 1:
            raise ValueError("CMS network metric metadata is missing or ambiguous")
        row = matching[0]
        if (_tokens(row.get("Dimensions")) != {"userId", "instanceId"}
                or str(spec["period"]) not in _tokens(row.get("Periods"))
                or "Average" not in _tokens(row.get("Statistics"))):
            raise ValueError("CMS network metric metadata does not match dimensions, period or statistic")
        unit = row.get("Unit")
        multipliers = {"bit/s": 1, "bits/s": 1, "bps": 1,
                       "Kbit/s": 1000, "Kbits/s": 1000, "kbit/s": 1000, "kbits/s": 1000,
                       "kbps": 1000, "Kbps": 1000,
                       "Mbit/s": 1000000, "Mbits/s": 1000000, "Mbps": 1000000,
                       "Byte/s": 8, "Bytes/s": 8, "byte/s": 8, "bytes/s": 8, "B/s": 8,
                       "KiB/s": 8192}
        if not isinstance(unit, str) or unit not in multipliers:
            raise ValueError("CMS network metric metadata has an unsupported rate unit")
        metadata = {"multiplier": multipliers[unit], "source_unit": unit}
        self._metadata_cache[key] = (now.timestamp(), metadata)
        return metadata


def _tokens(value):
    if not isinstance(value, str) or not value:
        raise ValueError("CMS network metadata requires comma-separated field definitions")
    result = [token.strip() for token in value.split(",")]
    if not all(result) or len(result) != len(set(result)):
        raise ValueError("CMS network metadata contains invalid field definitions")
    return set(result)
