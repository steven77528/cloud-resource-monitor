"""Read-only Huawei inventory and sampled network-scope investigation."""

import json
import logging
import math
import re
import time
from collections import Counter

from huaweicloudsdkcore.auth.credentials import BasicCredentials
from huaweicloudsdkcore.client import Client, ClientBuilder
from huaweicloudsdkcore.http.http_config import HttpConfig
from huaweicloudsdkcore.sdk_response import SdkResponse

from cloud_monitor.config import load_config


PATHS = {
    "ecs": "/v1/{project}/cloudservers/detail",
    "rds": "/v3/{project}/instances",
    "vpc": "/v1/{project}/publicips",
    "ces": "/V1.0/{project}/metric-data",
    "ces_metrics": "/V1.0/{project}/metrics",
}
UNITS = {"Byte/s": 8, "byte/s": 8, "Bytes/s": 8, "bytes/s": 8,
         "B/s": 8, "KiB/s": 8192, "KB/s": 8192, "KByte/s": 8192}


class RawResponse(SdkResponse):
    openapi_types = {}
    attribute_map = {}


def safe_error(exc):
    status = getattr(exc, "status_code", None)
    code = getattr(exc, "error_code", None)
    # Only provider-style identifiers are allowed; never include exception text.
    code = code if isinstance(code, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", code) else None
    return {"status": status if type(status) is int else None,
            "error_code": code, "exception": type(exc).__name__}


class Probe:
    def __init__(self, account, region, report):
        self.account, self.region, self.report = account, region, report
        self.project = account.get("project_ids", {}).get(region)
        if not isinstance(self.project, str) or not re.fullmatch(r"[a-fA-F0-9]{16,64}", self.project):
            raise ValueError("InvalidProject")
        if not re.fullmatch(r"[a-z]{2}-[a-z]+-\d+", region):
            raise ValueError("InvalidRegion")
        self.clients = {}

    def get(self, service, query):
        if service not in PATHS:
            raise ValueError("ForbiddenEndpoint")
        path = PATHS[service].format(project=self.project)
        if service not in self.clients:
            http = HttpConfig.get_default_config()
            http.timeout, http.retry_times = (5, 15), 0
            http.ignore_ssl_verification = False
            self.clients[service] = (ClientBuilder(Client)
                .with_credentials(BasicCredentials(self.account["access_key_id"],
                                                  self.account["access_key_secret"], self.project))
                .with_http_config(http)
                .with_endpoint("https://%s.%s.myhuaweicloud.com" % (service.split("_")[0], self.region)).build())
        trace = {"service": service}
        self.report["calls"].append(trace)
        try:
            response = self.clients[service].do_http_request(
                "GET", path, query_params=list(query.items()),
                header_params={"Accept": "application/json"}, response_type=RawResponse)
            trace["status"] = response.status_code
            body = json.loads(response.raw_content)
            if not isinstance(body, dict):
                raise ValueError("UnexpectedResponse")
            return body
        except Exception as exc:
            trace.update(safe_error(exc))
            return None

    def inventory(self, service):
        key = {"ecs": "servers", "rds": "instances", "vpc": "publicips"}[service]
        rows, seen, offset, marker = [], set(), 1 if service == "ecs" else 0, None
        state = {"complete": False, "pages": 0}
        self.report["inventory"][service] = state
        for _ in range(100):
            query = {"limit": 100}
            if service == "vpc":
                if marker:
                    query["marker"] = marker
            else:
                query["offset"] = offset
            body = self.get(service, query)
            if body is None:
                if service == "vpc" and self.report["calls"][-1].get("status") == 403:
                    state["blocked_permission"] = "vpc:publicIps:list"
                break
            page = body.get(key)
            if not isinstance(page, list) or any(not isinstance(row, dict) or not row.get("id") for row in page):
                state["error"] = "InvalidInventoryShape"
                break
            state["pages"] += 1
            ids = [row["id"] for row in page]
            if len(set(ids)) != len(ids) or seen.intersection(ids):
                state["error"] = "RepeatedInventoryPage"
                break
            rows.extend(page)
            seen.update(ids)
            total = body.get("count" if service == "ecs" else "total_count")
            if type(total) is int:
                state["reported_total"] = total
                if len(rows) >= total:
                    state["complete"] = len(rows) == total
                    break
            if len(page) < 100:
                state["complete"] = type(total) is not int or len(rows) == total
                break
            marker = ids[-1]
            offset += 1 if service == "ecs" else 100
        state["returned"] = len(rows)
        return rows


def resource(row, product):
    addresses = [addr for group in (row.get("addresses") or {}).values() for addr in group] if product == "ecs" else []
    ips = {addr.get("addr") for addr in addresses if addr.get("OS-EXT-IPS:type") == "floating"}
    if product == "rds":
        ips = set(row.get("public_ips") or [])
    ports = {addr.get("OS-EXT-IPS:port_id") for addr in addresses}
    for item in row.get("network_interfaces") or []:
        ports.add(item.get("port_id"))
        address = (item.get("association") or {}).get("public_ip_address")
        if isinstance(address, str) and address:
            ips.add(address)
    return {"id": row["id"], "name": row.get("name"), "product": product,
            "type": row.get("type"),
            "engine": (row.get("datastore") or {}).get("type") if product == "rds" else None,
            "public_ips": sorted(ip for ip in ips if isinstance(ip, str) and ip),
            "port_ids": sorted(port for port in ports if isinstance(port, str) and port), "eips": []}


def map_eips(resources, eips):
    ambiguous, mapped = [], set()
    addresses = Counter(eip.get("public_ip_address") for eip in eips if eip.get("public_ip_address"))
    for eip in eips:
        matches = []
        address, port = eip.get("public_ip_address"), eip.get("port_id")
        for index, item in enumerate(resources):
            by_port = bool(port and port in item["port_ids"])
            by_address = bool(address and address in item["public_ips"])
            if by_port or by_address:
                mismatch = ((by_port and bool(item["public_ips"]) and not by_address)
                            or (item["product"] == "ecs" and by_address and bool(port)
                                and bool(item["port_ids"]) and not by_port))
                matches.append((index, bool(mismatch)))
        if len(matches) != 1 or addresses[address] > 1 or (matches and matches[0][1]):
            if matches:
                ambiguous.append({"eip_id": eip["id"], "public_ip_address": address,
                                  "candidate_ids": [resources[index]["id"] for index, _ in matches]})
            continue
        index = matches[0][0]
        resources[index]["eips"].append({"id": eip["id"], "public_ip_address": address,
                                       "port_id": port})
        mapped.add(eip["id"])
    return {"mapped_eip_count": len(mapped), "unmapped_eip_count": len(eips) - len(mapped),
            "ambiguous": ambiguous}


def series(probe, namespace, metric, dimension, value, start, end, public=False):
    body = probe.get("ces", {"namespace": namespace, "metric_name": metric,
        "dim.0": dimension + "," + value, "period": 300, "filter": "average", "from": start, "to": end})
    if body is None:
        return None
    points = body.get("datapoints")
    if not isinstance(points, list):
        return None
    result = {}
    for point in points:
        if not isinstance(point, dict):
            return None
        factor = 1 if public and point.get("unit") in {"bit/s", "bits/s", "bps"} else UNITS.get(point.get("unit")) if not public else None
        value, stamp = point.get("average"), point.get("timestamp")
        if factor is None or type(value) not in (int, float) or not math.isfinite(value) or type(stamp) not in (int, float):
            return None
        if stamp in result or not start <= stamp <= end:
            return None
        result[stamp] = value * factor
    return result


def compare(probe, resources, complete):
    end = int(time.time() // 300 * 300) * 1000
    start = end - 3600000
    result, selected = [], Counter()
    if not complete:
        return {"status": "skipped_incomplete_inventory", "samples": []}
    for item in resources:
        product = item["product"]
        if selected[product] >= 3 or not item["eips"]:
            continue
        if item["public_ips"] and set(item["public_ips"]) != {eip["public_ip_address"] for eip in item["eips"]}:
            continue
        if product == "rds" and (item["engine"] not in {"MySQL", "PostgreSQL"} or item["type"] not in {"Single", "Ha", "Replica"}):
            continue
        selected[product] += 1
        sample = {"id": item["id"], "product": product, "estimated_only": True, "directions": {}}
        for direction in ("in", "out"):
            if product == "ecs":
                namespace, metric, dim = "SYS.ECS", "network_vm_bandwidth_" + direction, "instance_id"
            else:
                namespace, metric = "SYS.RDS", "rds004_bytes_in" if direction == "in" else "rds005_bytes_out"
                dim = "postgresql_cluster_id" if item["engine"] == "PostgreSQL" else "rds_cluster_id"
            total = series(probe, namespace, metric, dim, item["id"], start, end)
            publics = [series(probe, "SYS.VPC", "downstream_bandwidth" if direction == "in" else "upstream_bandwidth",
                              "publicip_id", eip["id"], start, end, True) for eip in item["eips"]]
            if total is None or any(point is None for point in publics):
                sample["directions"][direction] = {"status": "query_failed_or_invalid_unit"}
                continue
            stamps = sorted(set(total).intersection(*(set(point) for point in publics)))
            if not stamps:
                sample["directions"][direction] = {"status": "no_exact_timestamp_match"}
                continue
            differences = [total[stamp] - sum(point[stamp] for point in publics) for stamp in stamps]
            stamp = stamps[-1]
            sample["directions"][direction] = {"status": "ok", "matched_points": len(stamps),
                "timestamp": stamp, "total_bps": total[stamp], "public_bps": sum(point[stamp] for point in publics),
                "difference_bps": differences[-1], "minimum_difference_bps": min(differences),
                "negative_difference_points": sum(value < 0 for value in differences)}
        result.append(sample)
    return {"from": start, "to": end, "period": 300, "samples": result,
            "caveat": "Different measurement boundaries; difference is not verified private-network traffic."}


def unassociated_public_samples(probe):
    body = probe.get("ces_metrics", {"namespace": "SYS.VPC", "metric_name": "upstream_bandwidth", "limit": 1000})
    report = {"associated": False, "inventory_complete": False, "samples": []}
    if body is None:
        return report
    rows = body.get("metrics", [])
    ids = sorted({dim.get("value") for row in rows if isinstance(row, dict)
                  and row.get("namespace") == "SYS.VPC" and row.get("metric_name") == "upstream_bandwidth"
                  for dim in row.get("dimensions", []) if isinstance(dim, dict)
                  and dim.get("name") == "publicip_id" and isinstance(dim.get("value"), str)})
    report["publicip_sequences_in_page"] = len(ids)
    end = int(time.time() // 300 * 300) * 1000
    for eip_id in ids[:3]:
        sample = {"eip_id": eip_id}
        for direction, metric in (("in", "downstream_bandwidth"), ("out", "upstream_bandwidth")):
            points = series(probe, "SYS.VPC", metric, "publicip_id", eip_id, end - 3600000, end, True)
            sample[direction] = ({"timestamp": max(points), "public_bps": points[max(points)]}
                                 if points else {"status": "no_valid_sample"})
        report["samples"].append(sample)
    return report


def main():
    logging.disable(logging.CRITICAL)
    config = load_config("/app/config/config.yaml")
    reports = []
    for account in config.get("accounts", []):
        if account.get("provider") != "huawei" or not account.get("enabled", False):
            continue
        regions = set()
        for job in config.get("jobs", []):
            if job.get("account") != account["id"] or job.get("kind") != "network_traffic" or not job.get("enabled", True):
                continue
            options = job.get("options", {})
            if "instances" in options:
                regions.update(item["region"] for item in options["instances"])
            else:
                regions.update([options["region"]] if options.get("region") else options.get("regions", account.get("regions", [])))
        for region in sorted(regions):
            report = {"account": account["id"], "region": region, "calls": [], "inventory": {}}
            reports.append(report)
            try:
                probe = Probe(account, region, report)
                ecs, rds, eips = (probe.inventory(service) for service in ("ecs", "rds", "vpc"))
                resources = [resource(row, product) for product, rows in (("ecs", ecs), ("rds", rds)) for row in rows]
                report["mapping"] = map_eips(resources, eips)
                report["counts"] = {product: {"resources": sum(row["product"] == product for row in resources),
                    "with_public_address": sum(row["product"] == product and bool(row["public_ips"]) for row in resources)}
                    for product in ("ecs", "rds")}
                report["resources"] = resources
                complete = all(state["complete"] for state in report["inventory"].values()) and not report["mapping"]["ambiguous"]
                report["comparison"] = compare(probe, resources, complete)
                if report["inventory"]["vpc"].get("blocked_permission"):
                    report["unassociated_public"] = unassociated_public_samples(probe)
            except Exception as exc:
                report["error"] = safe_error(exc)
    print(json.dumps({"read_only": True, "reports": reports}, ensure_ascii=True, allow_nan=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"read_only": True, "error": safe_error(exc)}))
