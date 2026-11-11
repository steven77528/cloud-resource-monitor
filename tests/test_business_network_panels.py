"""Offline migration checks for direction-specific business network charts."""

from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "append_business_network_panels", ROOT / "scripts/append_business_network_panels.py"
)
updater = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(updater)

EXPECTED = (
    ("阿里云 ECS 内网入站速率", "in", "aliyun", "ecs", "intranet"),
    ("阿里云 ECS 内网出站速率", "out", "aliyun", "ecs", "intranet"),
    ("华为云 ECS 总网卡入站速率", "in", "huawei", "ecs", "all"),
    ("华为云 ECS 总网卡出站速率", "out", "huawei", "ecs", "all"),
    ("RDS 总网络入站吞吐量", "in", None, "rds", "all"),
    ("RDS 总网络出站吞吐量", "out", None, "rds", "all"),
)
EXPECTED_EIGHT = EXPECTED[:4] + (
    ("阿里云 RDS 总网络入站吞吐量", "in", "aliyun", "rds", "all"),
    ("阿里云 RDS 总网络出站吞吐量", "out", "aliyun", "rds", "all"),
    ("华为云 RDS 总网络入站吞吐量", "in", "huawei", "rds", "all"),
    ("华为云 RDS 总网络出站吞吐量", "out", "huawei", "rds", "all"),
)


def network_row(dashboard):
    return next(row for row in dashboard["spec"]["layout"]["spec"]["rows"]
                if row["spec"].get("title") == updater.SECTION_TITLE)


def network_names(dashboard):
    return [item["spec"]["element"]["name"]
            for item in network_row(dashboard)["spec"]["layout"]["spec"]["items"]]


def runtime_parts(panel):
    options = panel["spec"]["vizConfig"]["spec"]["options"]
    runtime, separator, encoded = options["getOption"].rpartition("})(context, ")
    assert separator and encoded.endswith(");")
    assert json.loads(encoded[:-2]) == options["independentTime"]
    return runtime, options["independentTime"]


@pytest.fixture
def base():
    dashboard = json.loads((ROOT / "grafana/cloud-business-dev-v1.json").read_text(encoding="utf-8"))
    for name in network_names(dashboard):
        del dashboard["spec"]["elements"][name]
    dashboard["spec"]["layout"]["spec"]["rows"].remove(network_row(dashboard))
    return dashboard


@pytest.fixture
def legacy(base):
    datasource = next(v["spec"]["query"]["datasource"] for v in base["spec"]["variables"]
                      if v["spec"].get("name") == "account")
    elements, row = updater.build_section(datasource)
    base["spec"]["elements"].update(elements)
    base["spec"]["layout"]["spec"]["rows"].append(row)
    return updater.enable_independent_time(base)


def assert_charts(dashboard, expected_charts=EXPECTED):
    items = network_row(dashboard)["spec"]["layout"]["spec"]["items"]
    assert len(items) == len(expected_charts)
    configs = []
    for index, (item, expected) in enumerate(zip(items, expected_charts)):
        title, direction, provider, product, scope = expected
        panel = dashboard["spec"]["elements"][item["spec"]["element"]["name"]]
        spec = panel["spec"]
        assert spec["title"] == title
        assert (item["spec"]["x"], item["spec"]["y"], item["spec"]["width"], item["spec"]["height"]) == (
            index % 2 * 12, index // 2 * 12, 12, 12)
        queries = spec["data"]["spec"]["queries"]
        assert len(queries) == 1 and queries[0]["spec"]["hidden"] is True
        query = queries[0]["spec"]["query"]["spec"]
        expression = query["expr"]
        assert f'metric="network_{direction}_bps"' in expression
        opposite = "out" if direction == "in" else "in"
        assert f'network_{opposite}_bps' not in expression
        assert f'product="{product}"' in expression
        assert f'scope="{scope}"' in expression
        assert 'unit="bit/s"' in expression
        if provider:
            assert expression.count(f'provider="{provider}"') == 2
        else:
            assert 'provider="' not in expression
        for guard in ("cloud_monitor_resource_fresh", "cloud_monitor_scheduler_alive", "up{"):
            assert guard in expression
        assert "rate(" not in expression and "sum(" not in expression
        _, config = runtime_parts(panel)
        assert config["title"] == title and config["id"] == spec["id"]
        assert config["dashboardUid"] == updater.DASHBOARD_UID
        assert config["unit"] == "bps"
        assert len(config["targets"]) == 1
        target = config["targets"][0]
        assert target["expr"] == expression
        assert target["legendFormat"] == query["legendFormat"]
        assert target["refId"] == queries[0]["spec"]["refId"]
        limits = [50_000_000, 100_000_000, 200_000_000] if product == "rds" else [100_000_000, 300_000_000, 800_000_000]
        assert [step["value"] for step in config["thresholds"]] == limits
        configs.append(config)
    all_ids = [p["spec"]["id"] for p in dashboard["spec"]["elements"].values()]
    assert len(set(all_ids)) == len(all_ids)
    assert len({c["id"] for c in configs}) == len(expected_charts)


def test_split_separates_both_query_sources_and_preserves_scope(legacy):
    assert_charts(updater.split_directions(legacy))


def test_split_preserves_other_panels_variables_and_original_ids(legacy):
    before = deepcopy(legacy)
    updated = updater.split_directions(legacy)
    assert legacy == before
    for name, panel in before["spec"]["elements"].items():
        if name not in network_names(before):
            assert updated["spec"]["elements"][name] == panel
    old_ids = [before["spec"]["elements"][n]["spec"]["id"] for n in network_names(before)]
    new_ids = [updated["spec"]["elements"][n]["spec"]["id"] for n in network_names(updated)]
    assert new_ids[::2] == old_ids
    for key, value in before.items():
        if key != "spec":
            assert updated[key] == value
    for key, value in before["spec"].items():
        if key not in ("elements", "layout"):
            assert updated["spec"][key] == value
    assert updated["spec"]["layout"]["spec"]["rows"][:-1] == before["spec"]["layout"]["spec"]["rows"][:-1]


def test_split_preserves_each_original_runtime_and_custom_style(legacy):
    originals = []
    for index, name in enumerate(network_names(legacy)):
        panel = legacy["spec"]["elements"][name]
        options = panel["spec"]["vizConfig"]["spec"]["options"]
        options["getOption"] = f"/* chart {index} */\n" + options["getOption"]
        options["renderer"] = f"custom-{index}"
        originals.append(deepcopy(panel))
    updated = updater.split_directions(legacy)
    for index, name in enumerate(network_names(updated)):
        panel = updated["spec"]["elements"][name]
        original = originals[index // 2]
        assert runtime_parts(panel)[0] == runtime_parts(original)[0]
        assert panel["spec"]["vizConfig"]["spec"]["options"]["renderer"] == f"custom-{index // 2}"
        assert panel["spec"]["vizConfig"]["spec"]["fieldConfig"] == original["spec"]["vizConfig"]["spec"]["fieldConfig"]


def test_repeat_split_and_independent_time_do_not_duplicate_or_restyle(legacy):
    updated = updater.split_directions(legacy)
    assert updater.split_directions(updated) == updated
    assert updater.enable_independent_time(updated) == updated


def test_append_to_dashboard_without_network_directly_builds_eight(base):
    updated = updater.append_section(base)
    assert_charts(updated, EXPECTED_EIGHT)
    with pytest.raises(ValueError):
        updater.append_section(updated)


def test_new_ids_skip_unrelated_panels(legacy):
    legacy["spec"]["elements"]["panel-200"] = deepcopy(legacy["spec"]["elements"]["panel-1"])
    legacy["spec"]["elements"]["panel-200"]["spec"]["id"] = 200
    updated = updater.split_directions(legacy)
    new_ids = [updated["spec"]["elements"][n]["spec"]["id"] for n in network_names(updated)]
    assert new_ids[1::2] == [201, 202, 203]


@pytest.mark.parametrize("qualifier", ["", "（含公网与内网）"])
def test_existing_huawei_title_is_preserved(legacy, qualifier):
    panel = legacy["spec"]["elements"]["panel-16"]["spec"]
    panel["title"] = "华为云 ECS 总网卡入出速率" + qualifier
    options = panel["vizConfig"]["spec"]["options"]
    runtime, separator, _ = options["getOption"].rpartition("})(context, ")
    config = options["independentTime"]
    config["title"] = panel["title"]
    options["getOption"] = runtime + separator + json.dumps(config, ensure_ascii=False) + ");"
    updated = updater.split_directions(legacy)
    titles = [updated["spec"]["elements"][name]["spec"]["title"] for name in network_names(updated)]
    assert titles[2:4] == ["华为云 ECS 总网卡入站速率" + qualifier, "华为云 ECS 总网卡出站速率" + qualifier]
    assert updater.split_directions(updated) == updated


@pytest.mark.parametrize("damage", ["wrong_uid", "missing_panel", "runtime_config", "mixed_directions"])
def test_invalid_source_is_rejected_without_mutation(legacy, damage):
    if damage == "wrong_uid":
        legacy["metadata"]["name"] = "another-dashboard"
    elif damage == "missing_panel":
        network_row(legacy)["spec"]["layout"]["spec"]["items"].pop()
    elif damage == "runtime_config":
        legacy["spec"]["elements"]["panel-15"]["spec"]["vizConfig"]["spec"]["options"]["independentTime"]["id"] = 999
    else:
        queries = legacy["spec"]["elements"]["panel-15"]["spec"]["data"]["spec"]["queries"]
        queries[1] = deepcopy(queries[0])
    before = deepcopy(legacy)
    with pytest.raises(ValueError):
        updater.split_directions(legacy)
    assert legacy == before


def test_canonical_export_is_eight_charts_and_idempotent():
    dashboard = json.loads((ROOT / "grafana/cloud-business-dev-v1.json").read_text(encoding="utf-8"))
    assert_charts(dashboard, EXPECTED_EIGHT)
    assert updater.split_directions(dashboard) == dashboard
    assert updater.split_rds_providers(dashboard) == dashboard


def test_rds_provider_split_filters_both_query_sources(legacy):
    six = updater.split_directions(legacy)
    updated = updater.split_rds_providers(six)
    assert_charts(updated, EXPECTED_EIGHT)
    for name in network_names(updated)[4:]:
        expression = updated["spec"]["elements"][name]["spec"]["data"]["spec"]["queries"][0]["spec"]["query"]["spec"]["expr"]
        assert 'cloud_monitor_scheduler_alive{job="$scrape_job",instance="$instance"}' in expression
        assert 'up{job="$scrape_job",instance="$instance"}' in expression
        assert 'account=~"${account:regex}"' in expression


def test_rds_split_keeps_ecs_and_all_unrelated_configuration(legacy):
    six = updater.split_directions(legacy)
    before = deepcopy(six)
    updated = updater.split_rds_providers(six)
    assert six == before
    rds_names = set(network_names(six)[4:])
    for name, panel in before["spec"]["elements"].items():
        if name not in rds_names:
            assert updated["spec"]["elements"][name] == panel
    old_items = network_row(before)["spec"]["layout"]["spec"]["items"]
    new_items = network_row(updated)["spec"]["layout"]["spec"]["items"]
    assert new_items[:4] == old_items[:4]
    assert network_names(updated)[4:6] == network_names(six)[4:6]
    for key, value in before.items():
        if key != "spec":
            assert updated[key] == value
    for key, value in before["spec"].items():
        if key not in ("elements", "layout"):
            assert updated["spec"][key] == value
    assert updated["spec"]["layout"]["spec"]["rows"][:-1] == before["spec"]["layout"]["spec"]["rows"][:-1]


def test_rds_split_preserves_each_source_runtime_style_and_thresholds(legacy):
    six = updater.split_directions(legacy)
    originals = []
    for index, name in enumerate(network_names(six)[4:]):
        panel = six["spec"]["elements"][name]
        options = panel["spec"]["vizConfig"]["spec"]["options"]
        options["getOption"] = f"/* RDS chart {index} */\n" + options["getOption"]
        options["renderer"] = f"rds-{index}"
        originals.append(deepcopy(panel))
    updated = updater.split_rds_providers(six)
    for index, name in enumerate(network_names(updated)[4:]):
        panel = updated["spec"]["elements"][name]
        original = originals[index % 2]
        assert runtime_parts(panel)[0] == runtime_parts(original)[0]
        assert panel["spec"]["vizConfig"]["spec"]["options"]["renderer"] == f"rds-{index % 2}"
        assert panel["spec"]["vizConfig"]["spec"]["fieldConfig"] == original["spec"]["vizConfig"]["spec"]["fieldConfig"]
        assert runtime_parts(panel)[1]["thresholds"] == runtime_parts(original)[1]["thresholds"]


def test_rds_split_migrates_three_or_six_and_is_idempotent(legacy):
    updated = updater.split_rds_providers(legacy)
    assert_charts(updated, EXPECTED_EIGHT)
    assert updater.split_rds_providers(updater.split_directions(legacy)) == updated
    assert updater.split_rds_providers(updated) == updated
    assert updater.split_directions(updated) == updated
    assert updater.enable_independent_time(updated) == updated


def test_rds_new_ids_skip_other_panels(legacy):
    six = updater.split_directions(legacy)
    six["spec"]["elements"]["panel-300"] = deepcopy(six["spec"]["elements"]["panel-1"])
    six["spec"]["elements"]["panel-300"]["spec"]["id"] = 300
    updated = updater.split_rds_providers(six)
    assert [updated["spec"]["elements"][name]["spec"]["id"] for name in network_names(updated)[6:]] == [301, 302]


@pytest.mark.parametrize("damage", ["missing_panel", "runtime_config", "conflicting_provider"])
def test_rds_split_rejects_inconsistent_source_without_mutation(legacy, damage):
    six = updater.split_directions(legacy)
    if damage == "missing_panel":
        network_row(six)["spec"]["layout"]["spec"]["items"].pop()
    elif damage == "runtime_config":
        six["spec"]["elements"]["panel-17"]["spec"]["vizConfig"]["spec"]["options"]["independentTime"]["id"] = 999
    else:
        panel = six["spec"]["elements"]["panel-17"]["spec"]
        query = panel["data"]["spec"]["queries"][0]["spec"]["query"]["spec"]
        query["expr"] = query["expr"].replace('product="rds"', 'provider="huawei",product="rds"')
        options = panel["vizConfig"]["spec"]["options"]
        runtime, separator, _ = options["getOption"].rpartition("})(context, ")
        options["independentTime"]["targets"][0]["expr"] = query["expr"]
        options["getOption"] = runtime + separator + json.dumps(options["independentTime"], ensure_ascii=False) + ");"
    before = deepcopy(six)
    with pytest.raises(ValueError):
        updater.split_rds_providers(six)
    assert six == before


@pytest.mark.skipif(os.environ.get("RUN_PROMTOOL_TESTS") != "1", reason="Opt in to local offline Docker promtool")
def test_rds_provider_queries_with_actual_promql_engine(legacy, tmp_path):
    updated = updater.split_rds_providers(legacy)
    scrape = {"job": "cloud-resource-monitor", "instance": "exporter:18080"}

    def series(metric, labels, value):
        encoded = ",".join(f"{key}={json.dumps(value)}" for key, value in sorted(labels.items()))
        return {"series": metric + "{" + encoded + "}", "values": str(value) + "+0x5"}

    inputs = [series("cloud_monitor_scheduler_alive", scrape, 1), series("up", scrape, 1)]
    for provider, multiplier in (("aliyun", 1), ("huawei", 10)):
        for direction, value in (("in", 100), ("out", 200)):
            for fresh in (True, False):
                labels = {**scrape, "provider": provider, "account": "same-account", "product": "rds",
                          "scope": "all", "unit": "bit/s", "metric": f"network_{direction}_bps",
                          "job_id": "rds-network", "metric_key": direction + ("-fresh" if fresh else "-stale")}
                inputs.append(series("cloud_monitor_resource_value", labels, value * multiplier if fresh else 999999))
                inputs.append(series("cloud_monitor_resource_fresh", labels, int(fresh)))
    checks = []
    for index, name in enumerate(network_names(updated)[4:]):
        expression = updated["spec"]["elements"][name]["spec"]["data"]["spec"]["queries"][0]["spec"]["query"]["spec"]["expr"]
        expression = expression.replace("$scrape_job", scrape["job"])
        expression = expression.replace("$instance", scrape["instance"]).replace("${account:regex}", ".*")
        checks.append({"expr": "sum(" + expression + ")", "eval_time": "5m", "exp_samples": [
            {"labels": "{}", "value": (100, 200, 1000, 2000)[index]},
        ]})
    fixture = tmp_path / "business-network-providers.yaml"
    fixture.write_text(yaml.safe_dump({"evaluation_interval": "1m", "tests": [
        {"interval": "1m", "input_series": inputs, "promql_expr_test": checks},
    ]}), encoding="utf-8")
    subprocess.run([
        "docker", "run", "--rm", "--pull=never", "--network", "none",
        "--mount", f"type=bind,source={tmp_path},target=/validation,readonly",
        "--entrypoint", "/bin/promtool", "prom/prometheus:v3.13.0",
        "test", "rules", "/validation/business-network-providers.yaml",
    ], check=True)
