"""Conservative private-network estimates from matched Huawei total and EIP rates."""

from __future__ import annotations

import json
import logging
import math
import re
from collections import Counter
from datetime import datetime, timezone

from .huawei import HuaweiCollectionError


SCOPE = "intranet_estimated"
LOG = logging.getLogger(__name__)
_PATHS = {"ecs": "/v1/{project}/cloudservers/detail",
          "rds": "/v3/{project}/instances", "vpc": "/v1/{project}/publicips",
          "ces": "/V1.0/{project}/metric-data"}
_BYTE_UNITS = {"Byte/s": 8, "byte/s": 8, "Bytes/s": 8, "bytes/s": 8, "B/s": 8}
_RDS_UNITS = {**_BYTE_UNITS, "KiB/s": 8192, "KB/s": 8192, "KByte/s": 8192}
_BIT_UNITS = {"bit/s": 1, "bits/s": 1, "bps": 1}


def _text(value):
    return isinstance(value, str) and bool(value.strip()) and value == value.strip()


def _error(message):
    return HuaweiCollectionError(message)


class NetworkEstimateUnavailable(HuaweiCollectionError):
    def __init__(self, observed_at: datetime):
        self.observed_at = observed_at
        super().__init__("Huawei network estimate has a negative difference")


class HuaweiIntranetCollector:
    def __init__(self, collector):
        self.collector = collector
        self._clients = {}
        self._inventory_cache = {}
        self._inventory_members = {}
        self._binding_history = {}
        self._binding_cutoffs = {}

    def _request(self, service, region, query):
        from huaweicloudsdkcore.auth.credentials import BasicCredentials
        from huaweicloudsdkcore.client import Client, ClientBuilder
        from huaweicloudsdkcore.http.http_config import HttpConfig
        from huaweicloudsdkcore.sdk_response import SdkResponse

        class RawResponse(SdkResponse):
            openapi_types = {}
            attribute_map = {}

        account = self.collector.account
        project = account.get("project_ids", {}).get(region)
        if (service not in _PATHS or not isinstance(region, str)
                or not re.fullmatch(r"[a-z]{2}-[a-z]+-\d+", region)
                or not isinstance(project, str) or not re.fullmatch(r"[a-fA-F0-9]{16,64}", project)):
            raise _error("Huawei network estimate has an invalid endpoint or project")
        key = (service, region)
        try:
            if key not in self._clients:
                if not account.get("access_key_id") or not account.get("access_key_secret"):
                    raise _error("Huawei credentials are required for network estimates")
                config = HttpConfig.get_default_config()
                config.timeout = (5, float(account.get("timeout_seconds", 20)))
                config.retry_times = 2
                config.ignore_ssl_verification = False
                self._clients[key] = (ClientBuilder(Client)
                    .with_credentials(BasicCredentials(account["access_key_id"],
                                                      account["access_key_secret"], project))
                    .with_http_config(config)
                    .with_endpoint(f"https://{service}.{region}.myhuaweicloud.com").build())
            response = self._clients[key].do_http_request(
                "GET", _PATHS[service].format(project=project),
                query_params=list(query.items()), header_params={"Accept": "application/json"},
                response_type=RawResponse)
            body = json.loads(response.raw_content)
            if not isinstance(body, dict):
                raise _error("Huawei network estimate response is not an object")
            return body
        except HuaweiCollectionError:
            raise
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            status = status if type(status) is int else "unknown"
            code = getattr(exc, "error_code", None)
            code = code if isinstance(code, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", code) else type(exc).__name__
            raise _error(f"Huawei {service} network estimate query failed (status={status}, code={code})") from None

    def _pages(self, service, region, options):
        field = {"ecs": "servers", "rds": "instances", "vpc": "publicips"}[service]
        rows, seen, offset, marker = [], set(), 1 if service == "ecs" else 0, None
        for _ in range(1000):
            query = {"limit": 100}
            if service == "vpc":
                if marker:
                    query["marker"] = marker
            else:
                query["offset"] = offset
            if service == "rds" and options.get("enterprise_project_id"):
                query["eps_id"] = options["enterprise_project_id"]
            body = self._request(service, region, query)
            page = body.get(field)
            if not isinstance(page, list) or any(not isinstance(row, dict) or not _text(row.get("id")) for row in page):
                raise _error("Huawei network estimate inventory has invalid resources")
            ids = [row["id"] for row in page]
            if len(set(ids)) != len(ids) or seen.intersection(ids):
                raise _error("Huawei network estimate inventory repeated a resource")
            seen.update(ids)
            rows.extend(page)
            total = body.get("count" if service == "ecs" else "total_count")
            if total is not None and (type(total) is not int or total < len(rows)):
                raise _error("Huawei network estimate inventory has an invalid total")
            if total is not None and len(rows) == total:
                return rows
            if len(page) < 100:
                if total is not None and len(rows) != total:
                    raise _error("Huawei network estimate inventory is incomplete")
                return rows
            marker = ids[-1]
            offset += 1 if service == "ecs" else 100
        raise _error("Huawei network estimate inventory exceeded pagination limit")

    def _inventory(self, product, region, options, now):
        key = (product, region, options.get("enterprise_project_id"))
        cached = self._inventory_cache.get(key)
        if cached and 0 <= now.timestamp() - cached[0] < options.get("inventory_cache_seconds", 300):
            return cached[1]
        resources = self._pages(product, region, options)
        eips = self._pages("vpc", region, options)
        metadata = {row["id"]: self._resource(row, product) for row in resources}
        mappings = self._map(metadata, eips)
        self._observe_bindings(key, mappings, now)
        result = (metadata, mappings)
        self._inventory_cache[key] = (now.timestamp(), result)
        return result

    def _observe_bindings(self, inventory_key, mappings, now):
        product, region, _ = inventory_key
        known = self._inventory_members.get(inventory_key, set())
        for rid in known | mappings.keys():
            identity = (product, region, rid)
            eips = mappings.get(rid)
            binding = tuple(sorted(eip["id"] for eip in eips)) if eips else None
            if identity in self._binding_history and self._binding_history[identity] != binding:
                cutoff = math.ceil(now.timestamp() / 300) * 300000
                self._binding_cutoffs[identity] = max(cutoff, self._binding_cutoffs.get(identity, cutoff))
            self._binding_history[identity] = binding
        self._inventory_members[inventory_key] = set(mappings)

    @staticmethod
    def _resource(row, product):
        addresses, ports = set(), set()
        if product == "rds":
            public_ips = row.get("public_ips") or []
            if not isinstance(public_ips, list) or not all(_text(ip) for ip in public_ips):
                raise _error("Huawei RDS estimate metadata has invalid public addresses")
            addresses.update(public_ips)
        else:
            groups = row.get("addresses") or {}
            interfaces = row.get("network_interfaces") or []
            if not isinstance(groups, dict) or not isinstance(interfaces, list):
                raise _error("Huawei ECS estimate metadata has invalid interfaces")
            for group in groups.values():
                if not isinstance(group, list):
                    raise _error("Huawei ECS estimate metadata has invalid addresses")
                for address in group:
                    if not isinstance(address, dict):
                        raise _error("Huawei ECS estimate metadata has invalid addresses")
                    if address.get("OS-EXT-IPS:type") == "floating":
                        if not _text(address.get("addr")):
                            raise _error("Huawei ECS estimate metadata has an invalid public address")
                        addresses.add(address["addr"])
                    if _text(address.get("OS-EXT-IPS:port_id")):
                        ports.add(address["OS-EXT-IPS:port_id"])
            for interface in interfaces:
                if not isinstance(interface, dict):
                    raise _error("Huawei ECS estimate metadata has an invalid interface")
                if _text(interface.get("port_id")):
                    ports.add(interface["port_id"])
                association = interface.get("association") or {}
                if not isinstance(association, dict):
                    raise _error("Huawei ECS estimate metadata has an invalid association")
                if _text(association.get("public_ip_address")):
                    addresses.add(association["public_ip_address"])
        return {"name": row.get("name"), "addresses": addresses, "ports": ports,
                "product": product, "engine": (row.get("datastore") or {}).get("type"),
                "topology": row.get("type")}

    @staticmethod
    def _map(metadata, eips):
        mappings = {rid: [] for rid in metadata}
        bad = set()
        public_counts = Counter(eip.get("public_ip_address") for eip in eips)
        for eip in eips:
            address, port = eip.get("public_ip_address"), eip.get("port_id")
            candidates = []
            for rid, resource in metadata.items():
                by_address = bool(address and address in resource["addresses"])
                by_port = bool(port and port in resource["ports"])
                if by_address or by_port:
                    conflict = ((by_port and bool(resource["addresses"]) and not by_address)
                                or (resource["product"] == "ecs" and by_address and port
                                    and resource["ports"] and not by_port))
                    candidates.append((rid, bool(conflict)))
            if not candidates:
                continue
            if (len(candidates) != 1 or public_counts[address] != 1 or not _text(address)
                    or not _text(port) or candidates[0][1]):
                bad.update(rid for rid, _ in candidates)
                continue
            mappings[candidates[0][0]].append({"id": eip["id"], "address": address})
        for rid, resource in metadata.items():
            if (rid in bad or not mappings[rid]
                    or (resource["addresses"] and resource["addresses"] != {eip["address"] for eip in mappings[rid]})):
                mappings[rid] = None
        return mappings

    def _series(self, region, namespace, metric, dimension, rid, units, start, end):
        body = self._request("ces", region, {"namespace": namespace, "metric_name": metric,
            "dim.0": f"{dimension},{rid}", "period": 300, "filter": "average", "from": start, "to": end})
        points = body.get("datapoints")
        if not isinstance(points, list) or not points:
            raise _error("Huawei network estimate source has no datapoints")
        result = {}
        for point in points:
            if not isinstance(point, dict):
                raise _error("Huawei network estimate source has an invalid datapoint")
            value, stamp, unit = point.get("average"), point.get("timestamp"), point.get("unit")
            if (type(value) not in (int, float) or not math.isfinite(value) or value < 0
                    or type(stamp) not in (int, float) or not math.isfinite(stamp)
                    or int(stamp) != stamp or not start <= stamp <= end or stamp in result
                    or not isinstance(unit, str) or unit not in units):
                raise _error("Huawei network estimate source has an invalid rate, unit or timestamp")
            converted = value * units[unit]
            if not math.isfinite(converted):
                raise _error("Huawei network estimate rate is not finite")
            result[int(stamp)] = converted
        return result

    def _estimate(self, options, instance, resource, eips, now):
        product, region, rid = options["product"], instance["region"], instance["resource_id"]
        if product == "rds":
            engine = resource["engine"]
            if (engine not in {"MySQL", "PostgreSQL"} or resource["topology"] not in {"Single", "Ha", "Replica"}
                    or (instance.get("engine") and instance["engine"] != engine)):
                raise _error("Huawei RDS topology or engine does not support network estimates")
        else:
            engine = None
        # Exclude the current aggregation bucket, and compare exact source timestamps.
        end = int(now.timestamp() // 300) * 300000 - 1
        start = end + 1 - options.get("lookback_seconds", 1800) * 1000
        cutoff = self._binding_cutoffs.get((product, region, rid))
        if cutoff is not None and end < cutoff:
            raise _error("Huawei network estimate is waiting for a closed window after an EIP binding change")
        namespace = "SYS.ECS" if product == "ecs" else "SYS.RDS"
        dimension = "instance_id" if product == "ecs" else "postgresql_cluster_id" if engine == "PostgreSQL" else "rds_cluster_id"
        units = _BYTE_UNITS if product == "ecs" else _RDS_UNITS
        totals, publics, names = {}, {}, {}
        for direction in ("in", "out"):
            names[direction] = ("network_vm_bandwidth_" + direction if product == "ecs"
                                else "rds004_bytes_in" if direction == "in" else "rds005_bytes_out")
            totals[direction] = self._series(region, namespace, names[direction], dimension, rid, units, start, end)
            publics[direction] = [self._series(region, "SYS.VPC",
                "downstream_bandwidth" if direction == "in" else "upstream_bandwidth",
                "publicip_id", eip["id"], _BIT_UNITS, start, end) for eip in eips]
        series = [totals[direction] for direction in ("in", "out")]
        series.extend(points for direction in ("in", "out") for points in publics[direction])
        common = set(series[0]).intersection(*(set(points) for points in series[1:]))
        if cutoff is not None:
            common = {stamp for stamp in common if stamp >= cutoff}
        if not common:
            raise _error("Huawei network estimate sources have no common timestamp for the current EIP binding")
        stamp = max(common)
        observed = datetime.fromtimestamp(stamp / 1000, timezone.utc)
        if not 0 <= (now - observed).total_seconds() <= options.get("max_age_seconds", 900):
            raise _error("Huawei network estimate sources are stale or in the future")
        values = {direction: totals[direction][stamp] - sum(points[stamp] for points in publics[direction])
                  for direction in ("in", "out")}
        if any(not math.isfinite(value) for value in values.values()):
            raise _error("Huawei network estimate has a negative or non-finite difference")
        if any(value < 0 for value in values.values()):
            raise NetworkEstimateUnavailable(observed)
        result = []
        for direction in ("in", "out"):
            identity = {"region": region, "namespace": namespace, "metric_name": names[direction],
                        "product": product, "scope": SCOPE}
            metadata = {**identity, "identity": identity, "kind": "network_traffic", "source": "total_minus_eip",
                        "period": 300, "filter": "average", "sample_max_gap_seconds": 450,
                        "publicip_ids": sorted(eip["id"] for eip in eips)}
            if engine:
                metadata["engine"] = engine
                identity["engine"] = engine
            result.append(self.collector._metric(rid, instance.get("resource_name") or resource["name"] or rid,
                "network_" + direction + "_bps", values[direction], "bit/s", observed, metadata))
        return result

    def collect(self, options, instances, now):
        self.collector.network_partial_failures = 0
        self.collector.network_estimate_unavailable = []
        if options.get("period", 300) != 300:
            raise ValueError("Huawei network estimates require a 300-second period")
        if not instances:
            return []
        inventories, result, failures = {}, [], []
        for instance in instances:
            region = instance["region"]
            if region not in inventories:
                try:
                    inventories[region] = self._inventory(options["product"], region, options, now)
                except Exception as exc:
                    inventories[region] = exc
            try:
                inventory = inventories[region]
                if isinstance(inventory, Exception):
                    raise inventory
                metadata, mappings = inventory
                rid = instance["resource_id"]
                if rid not in metadata or not mappings.get(rid):
                    raise _error("Huawei network estimate has no complete unambiguous EIP mapping")
                result.extend(self._estimate(options, instance, metadata[rid], mappings[rid], now))
            except NetworkEstimateUnavailable as exc:
                self.collector.network_estimate_unavailable.append({
                    "resource_id": instance["resource_id"], "region": region,
                    "observed_at": exc.observed_at, "reason": "negative_difference"})
                LOG.warning("network estimate resource_id=%s region=%s unavailable reason=negative_difference",
                            instance["resource_id"], region)
            except Exception as exc:
                from ..diagnostics import safe_collection_error

                failures.append(exc)
                LOG.warning("network collection resource_id=%s region=%s failed detail=%s",
                            instance["resource_id"], region, safe_collection_error(exc))
        self.collector.network_partial_failures = len(failures)
        if failures and not result:
            raise failures[0]
        return result
