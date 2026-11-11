"""Probe RDS public IPs against visible EIPs without changing cloud resources.

Run with ``python -m scripts.probe_aliyun_rds_eip --config config/config.yaml``.
Only enabled Alibaba RDS network jobs are inspected. Output contains counts,
stable hashed identifiers and statuses, never addresses or whitelist contents.
"""

from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import hashlib
import ipaddress
import json
import logging
import re

from cloud_monitor.config import load_config
from cloud_monitor.providers.aliyun import AliyunCollector


ACTIONS = {
    "DescribeDBInstances": ("rds", "2014-08-15", "rds:DescribeDBInstances"),
    "DescribeDBInstanceNetInfo": ("rds", "2014-08-15", "rds:DescribeDBInstanceNetInfo"),
    "DescribeEipAddresses": ("vpc", "2016-04-28", "vpc:DescribeEipAddresses"),
}
EIP_TYPES = {"EcsInstance", "SlbInstance", "Nat", "HaVip", "NetworkInterface", "IpAddress"}
EIP_STATES = {"Associating", "Unassociating", "InUse", "Available", "Releasing"}


def redacted(kind, value):
    return kind + "#" + hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]


def failure(exc, action):
    # SDK exception messages can contain signed URLs and whitelist data.
    code = str(getattr(exc, "error_code", "")).lower()
    status = getattr(exc, "http_status", None)
    status = int(status) if str(status) in {str(n) for n in range(400, 600)} else None
    authentication_failed = status == 401 or any(word in code for word in (
        "invalidaccesskeyid", "signaturedoesnotmatch", "invalidsecuritytoken", "incompletesignature",
        "signatureexpired"))
    denied = not authentication_failed and (status == 403 or any(word in code for word in (
        "forbidden", "unauthorized", "accessdenied", "nopermission", "unauthorizedoperation"))
    )
    result = {"status": "permission_denied" if denied else "query_failed_or_invalid_response",
              "action": ACTIONS[action][2]}
    if status is not None:
        result["http_status"] = status
    if denied:
        result["required_action"] = ACTIONS[action][2]
    return result


def count_value(value):
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,9}", value):
        return int(value)
    raise ValueError("InvalidCount")


class Probe:
    def __init__(self, account, region, report, collector_factory=AliyunCollector):
        if not isinstance(region, str) or not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)+", region):
            raise ValueError("InvalidRegion")
        scoped = deepcopy(account)
        scoped.setdefault("endpoints", {}).setdefault("vpc", f"vpc.{region}.aliyuncs.com")
        self.collector = collector_factory(scoped)
        self.region, self.report = region, report
        self.eip_cache = {}

    def rpc(self, action, params):
        service, version, permission = ACTIONS[action]
        counters = self.report["calls"].setdefault(permission, {"attempted": 0, "succeeded": 0})
        counters["attempted"] += 1
        response = self.collector._rpc(service, action, version, params, self.region)
        if not isinstance(response, dict):
            raise ValueError("InvalidResponse")
        counters["succeeded"] += 1
        return response

    def pages(self, action, params, container, rows_key, total_key, id_key):
        rows, seen, total = [], set(), None
        for page_number in range(1, 10001):
            body = self.rpc(action, {**params, "PageNumber": page_number, "PageSize": 100})
            page = body.get(container, {}).get(rows_key) if isinstance(body.get(container), dict) else None
            actual_total = count_value(body.get(total_key))
            if (not isinstance(page, list) or not all(isinstance(row, dict) for row in page)
                    or len(page) > 100 or (total is not None and actual_total != total)):
                raise ValueError("InvalidPage")
            total = actual_total
            ids = [row.get(id_key) for row in page]
            if any(not isinstance(value, str) or not value.strip() for value in ids):
                raise ValueError("InvalidResourceId")
            if len(set(ids)) != len(ids) or seen.intersection(ids):
                raise ValueError("RepeatedPage")
            rows.extend(page)
            seen.update(ids)
            if len(rows) > total:
                raise ValueError("InvalidTotal")
            if len(rows) == total:
                return rows
            if not page:
                raise ValueError("IncompletePagination")
        raise ValueError("PaginationLimit")

    def public_eips(self, address):
        if address not in self.eip_cache:
            try:
                rows = self.pages("DescribeEipAddresses", {"RegionId": self.region, "EipAddress": address},
                                  "EipAddresses", "EipAddress", "TotalCount", "AllocationId")
                if any(row.get("IpAddress") != address for row in rows):
                    raise ValueError("UnexpectedAddress")
                self.eip_cache[address] = {
                    "status": "address_match_binding_unverified" if rows else "no_match_in_visible_scope",
                    "eip_count": len(rows),
                    "eips": [{"eip_id": redacted("eip", row["AllocationId"]),
                              "bound_resource_id": redacted("resource", row["InstanceId"])
                              if row.get("InstanceId") else None,
                              "binding_type": row.get("InstanceType")
                              if row.get("InstanceType") in EIP_TYPES else "unknown",
                              "binding_status": row.get("Status")
                              if row.get("Status") in EIP_STATES else "unknown",
                              "service_managed": row.get("ServiceManaged")
                              if type(row.get("ServiceManaged")) is int
                              and row["ServiceManaged"] in {0, 1} else None} for row in rows],
                }
            except Exception as exc:
                self.eip_cache[address] = failure(exc, "DescribeEipAddresses")
        return {"public_address_id": redacted("address", address), **self.eip_cache[address]}

    def instance(self, instance_id):
        result = {"rds_id": redacted("rds", instance_id)}
        try:
            body = self.rpc("DescribeDBInstanceNetInfo", {"DBInstanceId": instance_id})
            container = body.get("DBInstanceNetInfos")
            endpoints = container.get("DBInstanceNetInfo") if isinstance(container, dict) else None
            if not isinstance(endpoints, list) or not all(isinstance(row, dict) for row in endpoints):
                raise ValueError("InvalidEndpoints")
        except Exception as exc:
            return {**result, **failure(exc, "DescribeDBInstanceNetInfo")}
        public = [row for row in endpoints if row.get("IPType") == "Public"]
        addresses, invalid, unsupported = set(), 0, 0
        for row in public:
            try:
                address = ipaddress.ip_address(row.get("IPAddress", ""))
                if address.version != 4:
                    unsupported += 1
                elif not address.is_global:
                    invalid += 1
                else:
                    addresses.add(str(address))
            except ValueError:
                invalid += 1
        lookups = [self.public_eips(address) for address in sorted(addresses)]
        if any(item["status"] in {"permission_denied", "query_failed_or_invalid_response"} for item in lookups):
            state = "eip_lookup_incomplete"
        elif invalid or unsupported:
            state = "public_address_lookup_incomplete"
        elif any(item["eip_count"] for item in lookups):
            state = "eip_address_match_binding_unverified"
        else:
            state = "no_match_in_visible_scope" if public else "no_public_endpoint_returned"
        return {**result, "status": state, "endpoint_count": len(endpoints),
                "public_endpoint_count": len(public), "unique_public_ipv4_count": len(addresses),
                "invalid_public_address_count": invalid, "unsupported_public_address_count": unsupported,
                "eip_lookups": lookups}


def plans(config):
    accounts = {account["id"]: account for account in config["accounts"]
                if account.get("enabled", False) and account.get("provider") == "aliyun"}
    result = {}
    for job in config["jobs"]:
        options = job.get("options", {})
        if (not job.get("enabled", True) or job.get("account") not in accounts
                or job.get("kind") != "network_traffic" or options.get("product") != "rds"):
            continue
        account = accounts[job["account"]]
        instances = options.get("instances")
        regions = ({item["region"] for item in instances} if instances is not None else
                   [options["region"]] if options.get("region") else
                   options.get("regions", account.get("regions", [])))
        if not regions:
            raise ValueError("MissingRegion")
        for region in regions:
            plan = result.setdefault((account["id"], region),
                                     {"account": account, "instance_ids": set(), "discover_engines": set()})
            if instances is not None:
                plan["instance_ids"].update(item["resource_id"] for item in instances if item["region"] == region)
            else:
                plan["discover_engines"].update(options.get("engines", ["MySQL"]))
    return result


def probe_config(config, collector_factory=AliyunCollector):
    report = {"read_only": True, "status": "ok", "reports": [], "required_actions": []}
    for (_, region), plan in sorted(plans(config).items()):
        regional = {"account_id": redacted("account", plan["account"]["id"]), "region": region,
                    "calls": {}, "inventory_status": "explicit_instances", "resources": []}
        report["reports"].append(regional)
        try:
            probe = Probe(plan["account"], region, regional, collector_factory)
        except Exception:
            regional["status"] = "invalid_configuration"
            continue
        instance_ids = set(plan["instance_ids"])
        if plan["discover_engines"]:
            try:
                rows = probe.pages("DescribeDBInstances", {"RegionId": region},
                                   "Items", "DBInstance", "TotalRecordCount", "DBInstanceId")
                if any(not isinstance(row.get("Engine"), str) or not row["Engine"] for row in rows):
                    raise ValueError("InvalidEngine")
                instance_ids.update(row["DBInstanceId"] for row in rows
                                    if row["Engine"] in plan["discover_engines"])
                regional["inventory_status"] = "complete_in_current_scope"
            except Exception as exc:
                regional["inventory_status"] = "incomplete"
                regional["inventory_error"] = failure(exc, "DescribeDBInstances")
        regional["resources"] = [probe.instance(instance_id) for instance_id in sorted(instance_ids)]
        regional["resource_count"] = len(instance_ids)
        regional["resource_status_counts"] = dict(Counter(row["status"] for row in regional["resources"]))
        regional["status"] = ("incomplete" if regional["inventory_status"] == "incomplete"
                              or any(row["status"] in {"permission_denied", "query_failed_or_invalid_response",
                                                       "eip_lookup_incomplete", "public_address_lookup_incomplete"}
                                     for row in regional["resources"]) else "complete_in_current_scope")
    permissions = set()
    for regional in report["reports"]:
        errors = [regional.get("inventory_error", {})]
        errors.extend(regional.get("resources", []))
        errors.extend(lookup for row in regional.get("resources", []) for lookup in row.get("eip_lookups", []))
        permissions.update(error["required_action"] for error in errors if error.get("required_action"))
    report["required_actions"] = sorted(permissions)
    report["status"] = ("no_enabled_rds_network_jobs" if not report["reports"] else
                        "incomplete" if any(row["status"] in {"incomplete", "invalid_configuration"}
                                            for row in report["reports"]) else "complete_in_current_scope")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/config.yaml", help="Normal monitor configuration path")
    args = parser.parse_args(argv)
    previous_logging_disable = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        try:
            report = probe_config(load_config(args.config))
        except Exception:
            report = {"read_only": True, "status": "configuration_failed"}
        print(json.dumps(report, ensure_ascii=True, allow_nan=False))
        return 1 if report["status"] in {"incomplete", "configuration_failed"} else 0
    finally:
        logging.disable(previous_logging_disable)


if __name__ == "__main__":
    raise SystemExit(main())
