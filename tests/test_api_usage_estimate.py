import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.estimate_api_usage import estimate


SCRIPT = Path(__file__).parents[1] / "scripts" / "estimate_api_usage.py"


def account(account_id="ali", provider="aliyun", enabled=True):
    return {"id": account_id, "provider": provider, "enabled": enabled, "regions": ["cn-hangzhou"]}


def metrics_job(job_id="bandwidth", account_id="ali", **changes):
    result = {"id": job_id, "account": account_id, "kind": "metrics", "enabled": True,
              "interval_seconds": 60, "options": {"metrics": [
                  {"namespace": "acs_alb", "metric_name": "LoadBalancerInBits", "period": 60},
                  {"namespace": "acs_alb", "metric_name": "LoadBalancerOutBits", "period": 60},
              ]}}
    result.update(changes)
    return result


def test_current_enabled_alb_baseline_and_no_secrets_in_report():
    config = {"accounts": [account()], "jobs": [metrics_job()],
              "notifications": {"token": "secret-notification-token"}}
    config["accounts"][0]["access_key_secret"] = "secret-cloud-key"
    result = estimate(config)
    pool = result["cms_pools"][0]
    assert pool["expected_calls"] == 89280
    assert pool["remaining_calls"] == 910720
    assert pool["usage_percent"] == 8.928
    assert pool["status"] == "within_free_quota"
    serialized = json.dumps(result)
    assert "secret-notification-token" not in serialized
    assert "secret-cloud-key" not in serialized
    assert "access_key_secret" not in serialized


def test_accounts_and_jobs_must_both_be_enabled_and_huawei_is_separate():
    config = {"accounts": [account(), account("off", enabled=False), account("hw", "huawei")],
              "jobs": [metrics_job(), metrics_job("off-account", "off"),
                       metrics_job("off-job", enabled=False), metrics_job("hw-job", "hw")]}
    result = estimate(config)
    assert len(result["cms_pools"]) == 1
    assert result["cms_pools"][0]["expected_calls"] == 89280
    assert result["jobs"][1]["scheduled_runs"] == 0
    assert result["jobs"][2]["scheduled_runs"] == 0
    assert result["jobs"][3]["cms_expected_calls"] == 0
    assert result["jobs"][3]["other_calls"]["minimum_monthly_calls"] == 89280


def test_pagination_uses_entry_window_and_counts_requests_not_points():
    job = metrics_job(max_age_seconds=999999)
    job["options"]["metrics"] = [
        {"namespace": "n", "metric_name": "m", "period": 60, "max_age_seconds": 86400}]
    result = estimate({"accounts": [account()], "jobs": [job]})
    row = result["jobs"][0]
    assert row["metrics"][0]["query_window_seconds"] == 86460
    assert row["metrics"][0]["expected_points_per_series"] == 1441
    assert row["metrics"][0]["expected_pages"] == 2
    assert row["cms_minimum_calls"] == 44640
    assert row["cms_expected_calls"] == 89280


def test_distinct_entries_are_not_implicitly_batched_and_known_owners_merge():
    jobs = [metrics_job("one", "one", interval_seconds=5),
            metrics_job("two", "two", interval_seconds=5)]
    for job in jobs:
        job["options"]["metrics"] = job["options"]["metrics"][:1]
    config = {"accounts": [account("one"), account("two")], "jobs": jobs}
    result = estimate(config, owners={"one": "1234", "two": "1234"})
    pool = result["cms_pools"][0]
    assert len(result["cms_pools"]) == 1
    assert pool["accounts"] == ["one", "two"]
    assert pool["expected_calls"] == 1071360
    assert pool["status"] == "exceeds_free_quota"
    assert pool["remaining_calls"] == -71360
    assert len(estimate(config)["cms_pools"]) == 2


def test_native_rds_and_daily_metadata_are_not_cms_usage():
    job = {"id": "rds", "account": "ali", "kind": "rds_performance", "interval_seconds": 60,
           "options": {"instances": [{"resource_id": "a"}, {"resource_id": "b"}],
                       "metadata_cache_seconds": 86400}}
    result = estimate({"accounts": [account()], "jobs": [job]})
    assert result["cms_pools"][0]["expected_calls"] == 0
    assert len(result["rds_plans"]) == 1
    plan = result["rds_plans"][0]
    assert plan["performance_calls"] == 89280
    assert plan["metadata_calls"] == 62
    assert plan["cms_calls"] == 0
    assert result["jobs"][0]["other_calls"]["minimum_monthly_calls"] == 89342


def test_disabled_rds_job_is_only_a_plan_and_makes_no_scheduled_calls():
    job = {"id": "rds", "account": "ali", "kind": "rds_performance", "enabled": False,
           "interval_seconds": 60, "options": {"instances": [{"resource_id": "a"}]}}
    result = estimate({"accounts": [account()], "jobs": [job]})
    assert result["cms_pools"] == []
    assert result["jobs"][0]["other_calls"]["minimum_monthly_calls"] == 0
    assert result["rds_plans"][0]["enabled"] is False


def test_unknown_instance_count_remains_a_formula_and_override_is_a_scenario():
    config = {"accounts": [account()], "jobs": []}
    plan = estimate(config)["rds_plans"][0]
    assert plan["instance_count"] is None
    assert plan["performance_calls"] is None
    assert plan["metadata_calls"] is None
    assert plan["performance_formula"] == "N * 44640 DescribeDBInstancePerformance calls"
    plan = estimate(config, days=30, rds_instances={"ali": 3})["rds_plans"][0]
    assert plan["performance_calls"] == 129600
    assert plan["metadata_calls"] == 90


def test_local_sql_jobs_are_not_cloud_requests():
    job = {"id": "sql", "account": "ali", "kind": "rds_sql", "interval_seconds": 60}
    row = estimate({"accounts": [account()], "jobs": [job]})["jobs"][0]
    assert row["scheduled_runs"] == 44640
    assert row["cms_expected_calls"] == 0
    assert row["other_calls"]["minimum_monthly_calls"] == 0


def test_cli_reads_yaml_without_cloud_credentials_or_database(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("accounts:\n- id: ali\n  provider: aliyun\n  enabled: true\njobs: []\n", encoding="utf-8")
    process = subprocess.run([sys.executable, str(SCRIPT), str(config), "--rds-instances", "ali=2"],
                             text=True, capture_output=True, check=True, cwd=tmp_path)
    assert json.loads(process.stdout)["rds_plans"][0]["performance_calls"] == 89280
    assert process.stderr == ""


def test_invalid_yaml_does_not_echo_secret_source_lines(tmp_path):
    config = tmp_path / "broken.yaml"
    config.write_text("password: [top-secret-password\n", encoding="utf-8")
    process = subprocess.run([sys.executable, str(SCRIPT), str(config)], text=True, capture_output=True)
    assert process.returncode == 2
    assert "top-secret-password" not in process.stdout + process.stderr
    assert "Traceback" not in process.stderr


def test_cli_merges_monitoring_files_from_config_directory(tmp_path):
    directory = tmp_path / "config"
    directory.mkdir()
    config = directory / "config.yaml"
    config.write_text(
        "accounts:\n- id: ali\n  provider: aliyun\n  enabled: true\n"
        "monitoring_files: [rds-performance.yaml]\njobs: []\n", encoding="utf-8")
    (directory / "rds-performance.yaml").write_text(
        "jobs:\n- id: rds\n  account: ali\n  kind: rds_performance\n  enabled: false\n"
        "  interval_seconds: 60\n  options:\n    instances:\n    - resource_id: a\n"
        "    - resource_id: b\n    metadata_cache_seconds: 86400\nrules: []\n", encoding="utf-8")
    process = subprocess.run([sys.executable, str(SCRIPT), str(config)], cwd=tmp_path,
                             text=True, capture_output=True, check=True)
    result = json.loads(process.stdout)
    row = result["jobs"][0]
    plan = result["rds_plans"][0]
    assert row["id"] == "rds"
    assert row["enabled"] is False
    assert row["other_calls"]["minimum_monthly_calls"] == 0
    assert plan["performance_calls"] == 89280
    assert plan["metadata_calls"] == 62
    assert result["cms_pools"] == []


def test_cli_rejects_monitoring_file_outside_config_directory(tmp_path):
    directory = tmp_path / "config"
    directory.mkdir()
    config = directory / "config.yaml"
    config.write_text("accounts: []\njobs: []\nmonitoring_files: [../outside.yaml]\n", encoding="utf-8")
    (tmp_path / "outside.yaml").write_text("jobs: []\npassword: top-secret-outside\n", encoding="utf-8")
    process = subprocess.run([sys.executable, str(SCRIPT), str(config)], cwd=tmp_path,
                             text=True, capture_output=True)
    assert process.returncode == 2
    assert "top-secret-outside" not in process.stdout + process.stderr
    assert "Traceback" not in process.stderr


def cloudmonitor_job(count=2, **changes):
    job = {"id": "mysql-cloudmonitor", "account": "ali", "kind": "rds_cloudmonitor",
           "enabled": True, "interval_seconds": 60, "options": {"instances": [
               {"resource_id": f"rds-{index}", "engine": "MySQL", "region": "cn-hangzhou"}
               for index in range(count)]}}
    job.update(changes)
    return job


def test_batched_slow_queries_add_one_cms_request_per_minute_and_cache_metadata():
    result = estimate({"accounts": [account()], "jobs": [metrics_job(), cloudmonitor_job()]})
    pool = result["cms_pools"][0]
    row = result["jobs"][1]
    assert pool["expected_calls"] == 133920
    assert pool["usage_percent"] == 13.392
    assert pool["remaining_calls"] == 866080
    assert row["cms_expected_calls"] == 44640
    assert row["cloudmonitor_plan"]["groups"][0]["batch_instance_counts"] == [2]
    assert row["cloudmonitor_plan"]["conservative_points_per_series"] == 17
    assert row["other_calls"]["minimum_monthly_calls"] == 31
    assert row["cloudmonitor_plan"]["metadata_uses_cms_free_pool"] is False


def test_disabled_cloudmonitor_job_and_account_only_show_projected_calls():
    result = estimate({"accounts": [account(), account("off", enabled=False)],
                       "jobs": [metrics_job(), cloudmonitor_job(enabled=False),
                                cloudmonitor_job(id="off-job", account="off")]})
    assert result["cms_pools"][0]["expected_calls"] == 89280
    for row in result["jobs"][1:]:
        assert row["cms_expected_calls"] == 0
        assert row["other_calls"]["minimum_monthly_calls"] == 0
        assert row["cloudmonitor_plan"]["cms_expected_calls_if_enabled"] == 44640


def test_cloudmonitor_separates_regions_and_metrics_with_metadata_per_group():
    job = cloudmonitor_job()
    job["options"]["instances"].extend([
        {"resource_id": "third", "engine": "MySQL", "region": "cn-shanghai"},
        {"resource_id": "fourth", "engine": "PostgreSQL", "region": "cn-hangzhou"},
    ])
    row = estimate({"accounts": [account()], "jobs": [job]})["jobs"][0]
    assert row["cms_expected_calls"] == 133920
    assert row["other_calls"]["minimum_monthly_calls"] == 93
    assert {(group["region"], group["metric_name"]) for group in row["cloudmonitor_plan"]["groups"]} == {
        ("cn-hangzhou", "MySQL_SlowQueries"), ("cn-shanghai", "MySQL_SlowQueries"),
        ("cn-hangzhou", "PG_SharedBufferHitRate"),
    }


def test_cloudmonitor_never_batches_across_accounts_even_for_the_same_owner():
    config = {"accounts": [account("one"), account("two")],
              "jobs": [cloudmonitor_job(1, id="one", account="one"),
                       cloudmonitor_job(1, id="two", account="two")]}
    separate = estimate(config)
    assert len(separate["cms_pools"]) == 2
    assert [pool["expected_calls"] for pool in separate["cms_pools"]] == [44640, 44640]
    merged = estimate(config, owners={"one": "1234", "two": "1234"})
    assert len(merged["cms_pools"]) == 1
    assert merged["cms_pools"][0]["expected_calls"] == 89280


def test_cloudmonitor_fifty_instance_batch_boundary_and_metadata_not_per_batch():
    full = estimate({"accounts": [account()], "jobs": [cloudmonitor_job(50)]})["jobs"][0]
    split = estimate({"accounts": [account()], "jobs": [cloudmonitor_job(51)]})["jobs"][0]
    assert full["cms_expected_calls"] == 44640
    assert split["cms_expected_calls"] == 89280
    assert split["cloudmonitor_plan"]["groups"][0]["batch_instance_counts"] == [50, 1]
    assert split["other_calls"]["minimum_monthly_calls"] == 31


def test_cloudmonitor_counts_pagination_per_batch_and_uses_lookback_not_max_age():
    job = cloudmonitor_job(51)
    job["options"].update(lookback_seconds=3600, max_age_seconds=999999, metadata_cache_seconds=43200)
    row = estimate({"accounts": [account()], "jobs": [job]})["jobs"][0]
    assert row["cloudmonitor_plan"]["conservative_points_per_series"] == 62
    # 50 * 62 points needs three pages; the final single-instance batch needs one.
    assert row["cms_minimum_calls"] == 89280
    assert row["cms_expected_calls"] == 178560
    assert row["other_calls"]["minimum_monthly_calls"] == 62


def network_job(provider="aliyun", product="ecs", count=2, **changes):
    options = {"product": product}
    if count is not None:
        options["instances"] = [{"resource_id": f"private-network-instance-{index}", "region": "cn-hangzhou",
                                 **({"engine": "MySQL"} if product == "rds" else {})}
                                for index in range(count)]
    result = {"id": "network", "account": "ali" if provider == "aliyun" else "hw",
              "kind": "network_traffic", "enabled": True, "interval_seconds": 60, "options": options}
    result.update(changes)
    return result


def test_explicit_network_instances_add_both_directions_and_daily_metadata_without_inventory():
    result = estimate({"accounts": [account()], "jobs": [network_job()]})
    row, pool = result["jobs"][0], result["cms_pools"][0]
    assert row["metric_count"] == 4
    assert row["cms_minimum_calls"] == 178560
    assert row["cms_expected_calls"] == 178560
    assert pool["expected_calls"] == 178560
    assert pool["status"] == "within_free_quota"
    plan = row["network_plan"]
    assert plan["instance_count"] == 2
    assert plan["monitoring_api"] == "DescribeMetricList"
    assert plan["inventory_refreshes_if_enabled"] == 0
    assert plan["inventory_minimum_calls_if_enabled"] == 0
    assert plan["metadata_calls_if_enabled"] == 62
    assert row["other_calls"]["minimum_monthly_calls"] == 62
    assert row["other_calls"]["complete"] is True
    assert "private-network-instance" not in json.dumps(result)


@pytest.mark.parametrize("provider", ["aliyun", "huawei"])
def test_empty_ecs_selection_has_zero_monitoring_inventory_and_metadata_calls(provider):
    selected_account = account("ali" if provider == "aliyun" else "hw", provider)
    selected_account.pop("regions")
    job = network_job(provider, count=0, interval_seconds=300 if provider == "huawei" else 60)
    row = estimate({"accounts": [selected_account], "jobs": [job]})["jobs"][0]
    plan = row["network_plan"]
    assert plan["status"] == "estimated"
    assert plan["instance_count"] == 0
    assert plan["monitoring_expected_calls_if_enabled"] == 0
    assert plan["inventory_minimum_calls_if_enabled"] == 0
    assert plan["metadata_calls_if_enabled"] == 0
    assert row["metric_count"] == 0
    assert row["cms_expected_calls"] == 0
    assert row["other_calls"]["minimum_monthly_calls"] == 0
    assert row["other_calls"]["complete"] is True


def test_network_cms_pages_use_metric_period_and_max_age_for_each_direction():
    job = network_job(count=1, max_age_seconds=999999)
    job["options"].update(period=60, max_age_seconds=86400)
    row = estimate({"accounts": [account()], "jobs": [job]})["jobs"][0]
    assert row["network_plan"]["query_window_seconds"] == 86460
    assert row["network_plan"]["expected_pages_per_direction"] == 2
    assert row["cms_minimum_calls"] == 89280
    assert row["cms_expected_calls"] == 178560


def test_auto_network_inventory_marks_cms_pool_incomplete_and_keeps_known_baseline():
    result = estimate({"accounts": [account()], "jobs": [metrics_job(), network_job(count=None)]})
    row, pool = result["jobs"][1], result["cms_pools"][0]
    assert row["cms_expected_calls"] is None
    assert row["cms_minimum_calls"] == 0
    assert row["network_plan"]["instance_count"] is None
    assert row["network_plan"]["monitoring_formula"] == "N * 44640 * 2 * 1 DescribeMetricList calls"
    assert row["network_plan"]["inventory_api"] == "DescribeInstances"
    assert row["other_calls"]["minimum_monthly_calls"] == 8928
    assert row["other_calls"]["complete"] is False
    assert pool["expected_calls"] == 89280
    assert pool["minimum_calls"] == 89280
    assert pool["remaining_calls"] is None
    assert pool["usage_percent"] is None
    assert pool["status"] == "instance_count_required"
    assert pool["unestimated_jobs"] == ["network"]


def test_ali_rds_network_inventory_is_per_region_and_metadata_groups_are_not_per_instance():
    job = network_job(product="rds", count=None)
    job["options"].update(regions=["cn-hangzhou", "cn-shanghai"], engines=["MySQL"])
    row = estimate({"accounts": [account()], "jobs": [job]})["jobs"][0]
    assert row["network_plan"]["inventory_api"] == "DescribeDBInstances"
    assert row["other_calls"]["minimum_monthly_calls"] == 17856
    job = network_job(product="rds", count=3)
    job["options"]["instances"][-1]["region"] = "cn-shanghai"
    job["options"]["metadata_cache_seconds"] = 43200
    row = estimate({"accounts": [account()], "jobs": [job]})["jobs"][0]
    assert row["network_plan"]["metadata_calls_if_enabled"] == 248
    assert row["network_plan"]["inventory_minimum_calls_if_enabled"] == 0


def test_disabled_network_job_does_not_make_pool_incomplete_or_add_calls():
    config = {"accounts": [account(), account("off", enabled=False)],
              "jobs": [metrics_job(), network_job(count=None, enabled=False),
                       network_job(count=None, id="disabled-account-network", account="off")]}
    result = estimate(config)
    pool = result["cms_pools"][0]
    assert pool["expected_calls"] == 89280
    assert pool["status"] == "within_free_quota"
    assert "unestimated_jobs" not in pool
    for row in result["jobs"][1:]:
        assert row["cms_expected_calls"] == 0
        assert row["other_calls"]["minimum_monthly_calls"] == 0
        assert row["other_calls"]["complete"] is True


def test_huawei_explicit_network_has_two_ces_queries_per_instance_and_no_cms():
    row = estimate({"accounts": [account("hw", "huawei")],
                    "jobs": [network_job("huawei", interval_seconds=300)]})["jobs"][0]
    assert row["cms_expected_calls"] == 0
    assert row["other_calls"]["minimum_monthly_calls"] == 35712
    assert row["other_calls"]["complete"] is True
    assert row["network_plan"]["monitoring_api"] == "ShowMetricData"
    assert row["network_plan"]["period_seconds"] == 300
    assert row["network_plan"]["query_window_seconds"] == 1200
    assert row["network_plan"]["metadata_calls_if_enabled"] == 0
    assert row["network_plan"]["inventory_minimum_calls_if_enabled"] == 0


def test_huawei_auto_network_lists_one_ecs_metric_or_each_rds_region_engine():
    ecs = network_job("huawei", count=None, interval_seconds=300)
    ecs["options"]["regions"] = ["cn-north-1", "cn-east-3"]
    rds = network_job("huawei", "rds", count=None, id="rds-network")
    rds["options"]["regions"] = ["cn-north-1", "cn-east-3"]
    result = estimate({"accounts": [account("hw", "huawei")], "jobs": [ecs, rds]})
    assert result["cms_pools"] == []
    ecs_plan, rds_plan = [row["network_plan"] for row in result["jobs"]]
    assert ecs_plan["inventory_api"] == "ListMetrics"
    assert ecs_plan["inventory_minimum_calls_if_enabled"] == 17856
    assert ecs_plan["metadata_api"] == "ListServersDetails"
    assert ecs_plan["metadata_calls_if_enabled"] is None
    assert ecs_plan["metadata_cache_seconds"] == 300
    assert "ceil(N_region / 100)" in ecs_plan["metadata_formula"]
    assert "ListServersDetails" in result["jobs"][0]["other_calls"]["formula"]
    assert rds_plan["inventory_api"] == "ListInstances"
    assert rds_plan["metadata_api"] is None
    assert rds_plan["inventory_minimum_calls_if_enabled"] == 35712
    assert all(row["other_calls"]["complete"] is False for row in result["jobs"])


@pytest.mark.parametrize("product", ["ecs", "rds"])
def test_huawei_intranet_estimate_counts_total_eip_and_mapping_queries(product):
    job = network_job("huawei", product, interval_seconds=300)
    job["options"]["scope"] = "intranet_estimated"
    result = estimate({"accounts": [account("hw", "huawei")], "jobs": [job]}, days=1)
    row, plan = result["jobs"][0], result["jobs"][0]["network_plan"]
    assert result["cms_pools"] == []
    assert row["cms_expected_calls"] == 0
    assert row["metric_count"] == 4
    assert plan["period_seconds"] == 300
    assert plan["query_window_seconds"] == 1800
    assert plan["monitoring_minimum_calls_if_enabled"] == 2304
    assert plan["monitoring_expected_calls_if_enabled"] is None
    assert plan["monitoring_formula"].startswith("(N + E) * 288 * 2 ShowMetricData calls")
    assert plan["minimum_eip_count"] == 2
    assert plan["eip_count"] is None
    assert plan["status"] == "eip_count_required"
    assert plan["inventory_minimum_calls_if_enabled"] == 0
    assert plan["metadata_api"] == ("ListServersDetails" if product == "ecs" else "ListInstances")
    assert plan["metadata_minimum_calls_if_enabled"] == 288
    assert plan["metadata_calls_if_enabled"] is None
    assert plan["eip_inventory_api"] == "ListPublicips"
    assert plan["eip_inventory_minimum_calls_if_enabled"] == 288
    assert row["other_calls"]["minimum_monthly_calls"] == 2880
    assert row["other_calls"]["complete"] is False
    assert "private-network-instance" not in json.dumps(result)


@pytest.mark.parametrize(("product", "discovery_calls", "total_minimum"), [
    ("ecs", 576, 576), ("rds", 1152, 1152),
])
def test_huawei_intranet_auto_discovery_and_mapping_inventory_are_separate(product, discovery_calls, total_minimum):
    job = network_job("huawei", product, count=None, interval_seconds=300)
    job["options"].update(scope="intranet_estimated", regions=["cn-east-3", "cn-north-1"])
    row = estimate({"accounts": [account("hw", "huawei")], "jobs": [job]}, days=1)["jobs"][0]
    plan = row["network_plan"]
    assert plan["status"] == "instance_count_required"
    assert plan["inventory_minimum_calls_if_enabled"] == discovery_calls
    assert plan["metadata_minimum_calls_if_enabled"] == 0
    assert plan["eip_inventory_minimum_calls_if_enabled"] == 0
    assert plan["mapping_region_count"] is None
    assert plan["maximum_mapping_region_count"] == 2
    assert "sum(resource_pages_region) * 288" in plan["metadata_formula"]
    assert "sum(EIP_pages_region) * 288" in plan["eip_inventory_formula"]
    assert plan["monitoring_minimum_calls_if_enabled"] is None
    assert plan["monitoring_expected_calls_if_enabled"] is None
    assert row["other_calls"]["minimum_monthly_calls"] == total_minimum
    assert row["other_calls"]["complete"] is False
    if product == "ecs":
        assert "ceil(N_region / 100)" in plan["metadata_formula"]
        assert "name lookup" in plan["metadata_formula"]
    assert "ListPublicips" in row["other_calls"]["formula"]


def test_huawei_estimated_mapping_cache_follows_inventory_ttl_and_active_regions():
    job = network_job("huawei", "rds", count=2, interval_seconds=300)
    job["options"].update(scope="intranet_estimated", inventory_cache_seconds=500,
                          metadata_cache_seconds=86400, lookback_seconds=2400)
    job["options"]["instances"][-1]["region"] = "cn-east-3"
    row = estimate({"accounts": [account("hw", "huawei")], "jobs": [job]}, days=1)["jobs"][0]
    plan = row["network_plan"]
    assert plan["query_window_seconds"] == 2400
    assert plan["metadata_cache_seconds"] == 500
    assert plan["eip_inventory_cache_seconds"] == 500
    assert plan["eip_inventory_refreshes_if_enabled"] == 144
    assert plan["metadata_minimum_calls_if_enabled"] == 288
    assert plan["eip_inventory_minimum_calls_if_enabled"] == 288
    assert row["other_calls"]["minimum_monthly_calls"] == 2880


@pytest.mark.parametrize("empty", [False, True])
def test_huawei_estimated_disabled_and_empty_jobs_make_no_calls(empty):
    job = network_job("huawei", count=0 if empty else 2, interval_seconds=300, enabled=empty)
    job["options"]["scope"] = "intranet_estimated"
    row = estimate({"accounts": [account("hw", "huawei")], "jobs": [job]}, days=1)["jobs"][0]
    assert row["other_calls"]["minimum_monthly_calls"] == 0
    assert row["other_calls"]["complete"] is True
    if empty:
        plan = row["network_plan"]
        assert plan["status"] == "estimated"
        assert plan["monitoring_expected_calls_if_enabled"] == 0
        assert plan["metadata_calls_if_enabled"] == 0
        assert plan["metadata_minimum_calls_if_enabled"] == 0
        assert plan["eip_inventory_minimum_calls_if_enabled"] == 0
        assert plan["eip_count"] == 0


def test_network_inventory_cache_refreshes_on_next_scheduled_run():
    job = network_job(count=None, interval_seconds=70)
    row = estimate({"accounts": [account()], "jobs": [job]}, days=1)["jobs"][0]
    assert row["scheduled_runs"] == 1235
    # A 300-second TTL expires at the fifth 70-second interval, so discovery recurs every 350 seconds.
    assert row["network_plan"]["inventory_refreshes_if_enabled"] == 247
    assert row["other_calls"]["minimum_monthly_calls"] == 247


def test_network_estimator_rejects_unmapped_aliyun_postgresql_metrics():
    job = network_job(product="rds", count=None)
    job["options"]["engines"] = ["MySQL", "PostgreSQL"]
    with pytest.raises(ValueError, match="MySQL only"):
        estimate({"accounts": [account()], "jobs": [job]})
    job = network_job(product="rds", count=1)
    job["options"]["instances"][0]["engine"] = "PostgreSQL"
    with pytest.raises(ValueError, match="MySQL only"):
        estimate({"accounts": [account()], "jobs": [job]})


def test_network_estimator_cli_from_another_cwd_loads_shared_validation(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        "accounts:\n- id: ali\n  provider: aliyun\n  enabled: true\n"
        "jobs:\n- id: network\n  account: ali\n  kind: network_traffic\n"
        "  interval_seconds: 60\n  options:\n    product: ecs\n    instances:\n"
        "    - resource_id: private-offline-instance\n      region: cn-hangzhou\n", encoding="utf-8")
    process = subprocess.run([sys.executable, str(SCRIPT), str(config)], cwd=tmp_path,
                             text=True, capture_output=True, check=True)
    result = json.loads(process.stdout)
    assert result["jobs"][0]["cms_expected_calls"] == 89280
    assert "private-offline-instance" not in process.stdout
    assert process.stderr == ""
