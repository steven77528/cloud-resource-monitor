"""Collection scope shared by persisted network samples and alert eligibility."""

import json


HEALTH_METRICS = frozenset({
    "collection_failed", "api_query_success", "api_query_duration_seconds",
    "api_last_success_timestamp_seconds", "api_signature_failed",
})


def network_estimate_key(resource_id, region):
    return json.dumps([region or "", resource_id], separators=(",", ":"), ensure_ascii=False)


def network_resource_selected(job, metric):
    options = job.get("options", {})
    if job.get("kind") != "network_traffic":
        return True
    scoped = options.get("scope") == "intranet_estimated"
    selected = options.get("product") == "ecs" and "instances" in options
    if not scoped and not selected:
        return True
    data = metric if isinstance(metric, dict) else metric.to_dict()
    if data["metric"] in HEALTH_METRICS:
        return True
    if data["account"] != job["account"]:
        return False
    dimensions = data.get("dimensions", {})
    if scoped and dimensions.get("scope") != "intranet_estimated":
        return False
    if not selected:
        return True
    region = dimensions.get("region")
    return any(instance["resource_id"] == data["resource_id"] and instance["region"] == region
               for instance in options["instances"])
