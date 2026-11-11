"""Per-job resource selection for alerts, independent of collection scope."""

import re

from .resource_selection import HEALTH_METRICS, network_resource_selected


SELECTORS = frozenset({"resource_id", "domain", "package_id"})
DOMAIN_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
DOMAIN_NAME = re.compile(rf"(?:\*\.)?{DOMAIN_LABEL}(?:\.{DOMAIN_LABEL})*")


def _domain(value):
    name = value.strip().rstrip(".").lower()
    try:
        return name.encode("idna").decode("ascii")
    except UnicodeError:
        return name


def certificate_domains(*values):
    """Keep all returned CN/SAN names without changing metric identity or display."""
    names = set()
    for value in values:
        if isinstance(value, str):
            names.update(_domain(name) for name in re.split(r"[,;\s]+", value) if name)
        elif isinstance(value, (list, tuple)):
            names.update(certificate_domains(*value))
    return sorted(names)


def validate_alerting(job):
    if "alerting" not in job:
        return
    label = f"{job['id']}.alerting"
    policy = job["alerting"]
    if not isinstance(policy, dict) or set(policy) - {"default_enabled", "resources"}:
        raise ValueError(f"{label} must contain only default_enabled and resources")
    if not isinstance(policy.get("default_enabled", True), bool):
        raise ValueError(f"{label}.default_enabled must be boolean")
    resources = policy.get("resources", [])
    if not isinstance(resources, list):
        raise ValueError(f"{label}.resources must be a list")
    seen = set()
    for index, entry in enumerate(resources):
        entry_label = f"{label}.resources[{index}]"
        if (not isinstance(entry, dict) or set(entry) - SELECTORS - {"enabled"}
                or len(set(entry) & SELECTORS) != 1):
            raise ValueError(f"{entry_label} needs exactly one of resource_id, domain, package_id and enabled")
        if not isinstance(entry.get("enabled"), bool):
            raise ValueError(f"{entry_label}.enabled must be boolean")
        selector = next(iter(set(entry) & SELECTORS))
        value = entry[selector]
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise ValueError(f"{entry_label}.{selector} must be a nonempty string without surrounding spaces")
        normalized = _domain(value) if selector == "domain" else value
        if selector == "domain" and (len(normalized) > 253 or not DOMAIN_NAME.fullmatch(normalized)):
            raise ValueError(f"{entry_label}.domain must be a domain name, optionally starting with *.")
        identity = (selector, normalized)
        if identity in seen:
            raise ValueError(f"{entry_label} duplicates a resource selector")
        seen.add(identity)


def resource_alerts_enabled(job, metric):
    if not network_resource_selected(job, metric):
        return False
    policy = job.get("alerting")
    if not policy:
        return True
    data = metric if isinstance(metric, dict) else metric.to_dict()
    if data["metric"] in HEALTH_METRICS:
        return True
    dimensions = data.get("dimensions", {})
    domains = certificate_domains(dimensions.get("domain"), dimensions.get("domains"))
    if job.get("kind") == "domains":
        domains = certificate_domains(*domains, data["resource_id"])
    values = {"resource_id": [data["resource_id"]], "domain": domains,
              "package_id": [dimensions.get("package_id") or data["resource_id"]]}
    enabled = []
    for entry in policy.get("resources", []):
        selector = next(key for key in SELECTORS if key in entry)
        expected = _domain(entry[selector]) if selector == "domain" else entry[selector]
        if expected in values[selector]:
            enabled.append(entry["enabled"])
    return all(enabled) if enabled else policy.get("default_enabled", True)
