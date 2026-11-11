"""Validate example config, dashboard PromQL and exposition with offline promtool."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import tempfile
from types import SimpleNamespace

import yaml

from cloud_monitor.config import validate_config
from cloud_monitor.engine import Engine
from cloud_monitor.models import Metric
from cloud_monitor.prometheus import render_metrics
from cloud_monitor.store import Store


ROOT = Path(__file__).resolve().parents[1]


def expressions(dashboard):
    queries = []
    for panel in dashboard["panels"]:
        queries.extend(target["expr"] for target in panel.get("targets", []) if "expr" in target)
    for variable in dashboard["templating"]["list"]:
        definition = variable.get("definition", "")
        if definition.startswith("label_values("):
            queries.append(definition[len("label_values("):].rsplit(",", 1)[0])
    return [re.sub(r"\$\{[a-z_]+:regex\}", ".*", query).replace("$instance", "demo:8080") for query in queries]


def example_metrics():
    now = datetime.now(timezone.utc)
    config = validate_config({
        "version": 1,
        "accounts": [{"id": "example", "provider": "aliyun", "enabled": True}],
        "jobs": [{"id": "example", "account": "example", "kind": "balance", "interval_seconds": 3600}],
        "rules": [{"id": "low-balance", "metric": "balance", "levels": [
            {"severity": "P1", "op": "le", "threshold": 1000}]}],
    })
    store = Store(":memory:")
    try:
        metric = Metric("aliyun", "example", "account-1", "Example account", "balance", 800, "CNY", now)
        store.save_samples("example", [metric])
        store.job_status("example", "ok", now, 1)
        Engine(store, config).evaluate(config["jobs"][0], metric, now)
        service = SimpleNamespace(config=config, store=store, jobs=config["jobs"],
                                  accounts={"example": config["accounts"][0]}, started=False, last_tick=now)
        return render_metrics(service, now)
    finally:
        store.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="prom/prometheus:v3.13.0", help="Locally installed Prometheus image")
    args = parser.parse_args()
    dashboard = json.loads((ROOT / "grafana/cloud-resource-monitor.json").read_text(encoding="utf-8"))
    queries = expressions(dashboard)
    with tempfile.TemporaryDirectory(prefix=".prometheus-check-", dir=ROOT) as directory:
        path = Path(directory)
        rules = {"groups": [{"name": "dashboard-validation", "rules": [
            {"record": f"dashboard_query_{index}", "expr": query} for index, query in enumerate(queries)
        ]}]}
        (path / "rules.yml").write_text(yaml.safe_dump(rules, allow_unicode=True), encoding="utf-8")
        config = yaml.safe_load((ROOT / "prometheus/scrape-config.yml").read_text(encoding="utf-8"))
        (path / "prometheus.yml").write_text(yaml.safe_dump(config), encoding="utf-8")
        command = ["docker", "run", "--rm", "--pull=never", "--network", "none", "-i",
                   "--mount", f"type=bind,source={path},target=/validation,readonly",
                   "--entrypoint", "/bin/promtool", args.image]
        for check in (["check", "config", "/validation/prometheus.yml"],
                      ["check", "rules", "--lint=none", "/validation/rules.yml"]):
            subprocess.run([*command, *check], check=True)
        subprocess.run([*command, "check", "metrics"], input=example_metrics(), check=True)
    print(f"Validated {len(queries)} dashboard queries, scrape configuration and exporter metrics offline.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
