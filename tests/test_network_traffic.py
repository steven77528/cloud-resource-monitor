from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
from huaweicloudsdkces.v1.model.datapoint import Datapoint
from huaweicloudsdkces.v1.model.show_metric_data_response import ShowMetricDataResponse

from cloud_monitor.providers.aliyun import AliyunCollector
from cloud_monitor.providers.huawei import HuaweiCollectionError, HuaweiCollector
from cloud_monitor.providers.network_traffic import NetworkTrafficCollector, validate_network_traffic_options


NOW = datetime(2026, 10, 20, tzinfo=timezone.utc)
REGION = "cn-east-3"


class AliyunNetwork(AliyunCollector):
    def __init__(self):
        super().__init__({"id": "test-aliyun", "regions": [REGION]})
        self.calls = []
        self.value = 10
        self.observed = NOW - timedelta(seconds=60)
        self.inventory = [{"InstanceId": "ecs-1", "InstanceName": "server", "InstanceChargeType": "PostPaid"}]
        self.rds_inventory = [{"DBInstanceId": "mysql-1", "Engine": "MySQL", "PayType": "Postpaid"},
                              {"DBInstanceId": "pg-1", "Engine": "PostgreSQL", "PayType": "Postpaid"}]
        self.metadata_unit = None
        self.no_data = False

    def _rpc(self, service, action, version, params, region=None):
        self.calls.append((service, action, params, region))
        if action in {"DescribeInstances", "DescribeDBInstances"}:
            ecs = action == "DescribeInstances"
            rows = self.inventory if ecs else self.rds_inventory
            # Deliberately use short pages to check the TotalCount contract.
            page = rows[params["PageNumber"] - 1:params["PageNumber"]]
            return ({"Instances": {"Instance": page}, "TotalCount": len(rows)} if ecs else
                    {"Items": {"DBInstance": page}, "TotalRecordCount": len(rows)})
        if action == "DescribeMetricMetaList":
            return {"Resources": {"Resource": [{"Namespace": params["Namespace"],
                    "MetricName": params["MetricName"], "Dimensions": "userId,instanceId",
                    "Periods": "60,300", "Statistics": "Average,Maximum",
                    "Unit": self.metadata_unit or ("Kbits/s" if "ecs" in params["Namespace"] else "Bytes/s")}]},
                    "TotalCount": 1}
        assert action == "DescribeMetricList"
        return {"Datapoints": [] if self.no_data else [{"timestamp": self.observed.timestamp() * 1000,
                                                        "Average": self.value}]}


class HuaweiNetwork(HuaweiCollector):
    def __init__(self):
        super().__init__({"id": "test-huawei", "regions": [REGION], "project_ids": {REGION: "test"}})
        self.calls = []
        self.value = 10
        self.observed = NOW - timedelta(seconds=300)
        self.unit = None
        self.no_data = False
        self.empty_inventory = False
        self.rds_type = "Ha"
        self.ecs_names = {"ecs-1": "application-server", "ecs-2": "database-server"}

    def _call(self, service, region, operation, request):
        self.calls.append((service, operation, request, region))
        if operation == "list_servers_details":
            assert service == "ecs"
            return {"servers": [{"id": rid, "name": self.ecs_names.get(rid)}
                                for rid in request.server_id.split(",")]}
        if operation == "list_metrics":
            if self.empty_inventory:
                return {"metrics": [], "meta_data": {"total": 0}}
            rid = "ecs-1" if request.start is None else "ecs-2"
            return {"metrics": [{"namespace": "SYS.ECS", "metric_name": "network_vm_bandwidth_in",
                                 "dimensions": [{"name": "instance_id", "value": rid}]}],
                    "meta_data": {"total": 2, "marker": "next-page"}}
        if operation == "list_instances":
            if self.empty_inventory:
                return {"instances": [], "total_count": 0}
            return {"instances": [{"id": request.datastore_type + str(request.offset),
                                   "name": "database", "datastore": {"type": request.datastore_type},
                                   "charge_info": {"charge_mode": "postPaid"}, "type": self.rds_type}],
                    "total_count": 2}
        assert operation == "show_metric_data"
        return {"datapoints": [] if self.no_data else [{"timestamp": self.observed.timestamp() * 1000,
                    "average": self.value, "unit": self.unit or ("Byte/s" if request.namespace == "SYS.ECS" else "KiB/s")} ]}


def test_aliyun_ecs_discovers_postpaid_paginates_and_normalizes_both_directions():
    collector = AliyunNetwork()
    collector.inventory.append({"InstanceId": "ecs-2", "InstanceChargeType": "PrePaid"})
    metrics = collector.collect("network_traffic", {"product": "ecs"}, NOW)
    assert len(metrics) == 4
    assert {metric.resource_id for metric in metrics} == {"ecs-1", "ecs-2"}
    assert {metric.metric for metric in metrics} == {"network_in_bps", "network_out_bps"}
    assert all(metric.value == 10000 and metric.unit == "bit/s" for metric in metrics)
    assert all(metric.observed_at == collector.observed for metric in metrics)
    assert all(metric.dimensions["scope"] == "intranet" for metric in metrics)
    assert all(metric.dimensions["source_unit"] == "Kbits/s" for metric in metrics)
    assert all(metric.dimensions["sample_max_gap_seconds"] == 90 for metric in metrics)
    assert len({metric.key() for metric in metrics}) == 4
    assert [call[2]["PageNumber"] for call in collector.calls if call[1] == "DescribeInstances"] == [1, 2]
    assert sum(call[1] == "DescribeMetricMetaList" for call in collector.calls) == 2


def test_aliyun_mysql_inventory_includes_postpaid_without_claiming_pg_support():
    collector = AliyunNetwork()
    metrics = collector.collect("network_traffic", {"product": "rds"}, NOW)
    assert len(metrics) == 2
    assert {metric.resource_id for metric in metrics} == {"mysql-1"}
    assert all(metric.value == 80 and metric.dimensions["engine"] == "MySQL" for metric in metrics)
    assert all(metric.dimensions["scope"] == "all" for metric in metrics)
    with pytest.raises(ValueError, match="MySQL only"):
        collector.collect("network_traffic", {"product": "rds", "engines": ["PostgreSQL"]}, NOW)


def test_inventory_and_metadata_cache_expire_independently_and_refresh_after_clock_reversal():
    collector = AliyunNetwork()
    options = {"product": "ecs", "metadata_cache_seconds": 600}
    collector.collect("network_traffic", options, NOW)
    collector.collect("network_traffic", options, NOW + timedelta(seconds=60))
    assert sum(call[1] == "DescribeInstances" for call in collector.calls) == 1
    assert sum(call[1] == "DescribeMetricMetaList" for call in collector.calls) == 2
    collector.observed = NOW + timedelta(seconds=300)
    collector.collect("network_traffic", options, NOW + timedelta(seconds=300))
    assert sum(call[1] == "DescribeInstances" for call in collector.calls) == 2
    assert sum(call[1] == "DescribeMetricMetaList" for call in collector.calls) == 2
    collector.observed = NOW + timedelta(seconds=600)
    collector.collect("network_traffic", options, NOW + timedelta(seconds=600))
    assert sum(call[1] == "DescribeMetricMetaList" for call in collector.calls) == 4
    collector.observed = NOW
    collector.collect("network_traffic", options, NOW)
    assert sum(call[1] == "DescribeMetricMetaList" for call in collector.calls) == 6


@pytest.mark.parametrize("provider", [AliyunNetwork, HuaweiNetwork])
def test_explicit_ecs_inventory_collects_only_selected_instances_in_both_directions(provider):
    collector = provider()
    options = {"product": "ecs", "instances": [
        {"resource_id": "selected-1", "resource_name": "Selected first", "region": REGION},
        {"resource_id": "selected-2", "resource_name": "Selected second", "region": REGION},
    ]}
    metrics = collector.collect("network_traffic", options, NOW)
    assert len(metrics) == 4
    assert {(metric.resource_id, metric.metric) for metric in metrics} == {
        (resource_id, direction) for resource_id in ("selected-1", "selected-2")
        for direction in ("network_in_bps", "network_out_bps")}
    assert {metric.resource_name for metric in metrics} == {"Selected first", "Selected second"}
    assert not any(call[1] in {"DescribeInstances", "list_metrics", "list_servers_details"}
                   for call in collector.calls)
    if isinstance(collector, AliyunNetwork):
        assert [call[2]["Dimensions"][0]["instanceId"] for call in collector.calls
                if call[1] == "DescribeMetricList"] == ["selected-1"] * 2 + ["selected-2"] * 2
    else:
        assert [call[2].dim_0 for call in collector.calls if call[1] == "show_metric_data"] == [
            "instance_id,selected-1"] * 2 + ["instance_id,selected-2"] * 2


@pytest.mark.parametrize("provider", [AliyunNetwork, HuaweiNetwork])
def test_explicit_empty_ecs_inventory_makes_no_requests_or_failures(provider):
    collector = provider()
    collector.account.pop("regions", None)
    collector.account.pop("project_ids", None)
    collector.network_partial_failures = 3
    collector.network_estimate_unavailable = [{"resource_id": "old"}]
    assert collector.collect("network_traffic", {"product": "ecs", "instances": []}, NOW) == []
    assert collector.calls == []
    assert collector.network_partial_failures == 0
    assert collector.network_estimate_unavailable == []


@pytest.mark.parametrize("provider", [AliyunNetwork, HuaweiNetwork])
def test_explicit_inventory_skips_discovery_and_missing_data_is_an_error(provider):
    collector = provider()
    options = {"product": "ecs", "instances": [{"resource_id": "missing", "region": REGION}]}
    collector.no_data = True
    with pytest.raises((ValueError, HuaweiCollectionError), match="data"):
        collector.collect("network_traffic", options, NOW)
    assert not any(call[1] in {"DescribeInstances", "list_metrics", "list_servers_details"} for call in collector.calls)


@pytest.mark.parametrize("provider", [AliyunNetwork, HuaweiNetwork])
def test_empty_discovery_is_no_data_without_fabricating_zero(provider):
    collector = provider()
    collector.inventory = []
    collector.empty_inventory = True
    collector.network_estimate_unavailable = [{"resource_id": "old"}]
    assert collector.collect("network_traffic", {"product": "ecs"}, NOW) == []
    assert not any(call[1] in {"DescribeMetricList", "show_metric_data"} for call in collector.calls)
    assert collector.network_estimate_unavailable == []


@pytest.mark.parametrize("provider", [AliyunNetwork, HuaweiNetwork])
def test_network_quality_is_reset_before_option_validation(provider):
    collector = provider()
    collector.network_partial_failures = 3
    collector.network_estimate_unavailable = [{"resource_id": "old"}]
    collector.network_estimate_resources = [{"resource_id": "old", "region": REGION}]
    with pytest.raises(ValueError, match="product"):
        collector.collect("network_traffic", {"product": "invalid"}, NOW)
    assert collector.network_partial_failures == 0
    assert collector.network_estimate_unavailable == []
    assert collector.network_estimate_resources is None
    assert collector.calls == []


@pytest.mark.parametrize("provider", [AliyunNetwork, HuaweiNetwork])
def test_a_normal_network_collection_clears_old_estimate_quality(provider):
    collector = provider()
    collector.network_estimate_unavailable = [{"resource_id": "old"}]
    collector.network_estimate_resources = [{"resource_id": "old", "region": REGION}]
    assert collector.collect("network_traffic", {"product": "ecs"}, NOW)
    assert collector.network_estimate_unavailable == []
    assert collector.network_estimate_resources is None


@pytest.mark.parametrize("instances", [[], [{"resource_id": "ecs-1", "region": REGION}]])
def test_confirmed_estimate_discovery_records_current_resources_even_when_empty(instances):
    owner = HuaweiNetwork()
    collector = NetworkTrafficCollector(owner, "huawei")
    collector._discover = Mock(return_value=instances)
    collector._huawei_intranet_collector = Mock()
    collector._huawei_intranet_collector.collect.return_value = []

    assert collector.collect({"product": "ecs", "scope": "intranet_estimated"}, NOW) == []

    assert owner.network_estimate_resources == instances
    collector._discover.assert_called_once()


def test_explicit_empty_estimate_selection_is_a_confirmed_empty_resource_list():
    owner = HuaweiNetwork()
    owner.network_estimate_resources = [{"resource_id": "old", "region": REGION}]
    assert owner.collect("network_traffic", {
        "product": "ecs", "scope": "intranet_estimated", "instances": []}, NOW) == []
    assert owner.network_estimate_resources == []
    assert owner.calls == []


def test_estimate_discovery_failure_keeps_the_resource_snapshot_unknown():
    owner = HuaweiNetwork()
    owner.network_estimate_resources = [{"resource_id": "old", "region": REGION}]
    collector = NetworkTrafficCollector(owner, "huawei")
    collector._discover = Mock(side_effect=HuaweiCollectionError("inventory failed"))

    with pytest.raises(HuaweiCollectionError, match="inventory failed"):
        collector.collect({"product": "ecs", "scope": "intranet_estimated"}, NOW)

    assert owner.network_estimate_resources is None


def test_estimate_measurement_failure_retains_the_confirmed_resource_snapshot():
    owner = HuaweiNetwork()
    collector = NetworkTrafficCollector(owner, "huawei")
    instances = [{"resource_id": "ecs-1", "region": REGION}]
    collector._discover = Mock(return_value=instances)
    collector._huawei_intranet_collector = Mock()
    collector._huawei_intranet_collector.collect.side_effect = HuaweiCollectionError("CES failed")

    with pytest.raises(HuaweiCollectionError, match="CES failed"):
        collector.collect({"product": "ecs", "scope": "intranet_estimated"}, NOW)

    assert owner.network_estimate_resources == instances


def test_estimate_resource_snapshot_is_refreshed_from_cached_and_updated_inventories():
    owner = HuaweiNetwork()
    collector = NetworkTrafficCollector(owner, "huawei")
    instances = [{"resource_id": "ecs-1", "resource_name": "private-name", "region": REGION}]
    collector._discover = Mock(side_effect=[instances, []])
    collector._huawei_intranet_collector = Mock()
    collector._huawei_intranet_collector.collect.return_value = []
    options = {"product": "ecs", "scope": "intranet_estimated", "inventory_cache_seconds": 300}

    collector.collect(options, NOW)
    assert owner.network_estimate_resources == [{"resource_id": "ecs-1", "region": REGION}]
    owner.network_estimate_resources = None
    collector.collect(options, NOW + timedelta(seconds=60))
    assert owner.network_estimate_resources == [{"resource_id": "ecs-1", "region": REGION}]
    assert collector._discover.call_count == 1
    collector.collect(options, NOW + timedelta(seconds=300))
    assert owner.network_estimate_resources == []
    assert collector._discover.call_count == 2


@pytest.mark.parametrize("provider", [AliyunNetwork, HuaweiNetwork])
@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), None, True])
def test_invalid_network_measurements_fail(provider, value):
    collector = provider()
    collector.value = value
    with pytest.raises((ValueError, HuaweiCollectionError)):
        collector.collect("network_traffic", {"product": "ecs"}, NOW)


@pytest.mark.parametrize("provider", [AliyunNetwork, HuaweiNetwork])
@pytest.mark.parametrize("age", [1800, -120])
def test_stale_and_future_measurements_fail(provider, age):
    collector = provider()
    collector.observed = NOW - timedelta(seconds=age)
    with pytest.raises((ValueError, HuaweiCollectionError)):
        collector.collect("network_traffic", {"product": "ecs"}, NOW)


def test_huawei_ecs_paginates_discovery_uses_five_minutes_and_labels_total_traffic():
    collector = HuaweiNetwork()
    metrics = collector.collect("network_traffic", {"product": "ecs"}, NOW)
    assert len(metrics) == 4
    assert all(metric.value == 80 and metric.unit == "bit/s" for metric in metrics)
    assert all(metric.dimensions["scope"] == "all" and metric.dimensions["period"] == 300 for metric in metrics)
    assert all(metric.dimensions["sample_max_gap_seconds"] == 450 for metric in metrics)
    assert len({metric.key() for metric in metrics}) == 4
    assert [call[2].start for call in collector.calls if call[1] == "list_metrics"] == [None, "next-page"]
    assert all(call[2].period == 300 for call in collector.calls if call[1] == "show_metric_data")
    assert {metric.resource_id: metric.resource_name for metric in metrics} == collector.ecs_names
    assert [call[2].server_id for call in collector.calls if call[1] == "list_servers_details"] == ["ecs-1,ecs-2"]
    with pytest.raises(ValueError, match="300-second"):
        collector.collect("network_traffic", {"product": "ecs", "period": 60}, NOW)


def test_huawei_ecs_names_refresh_with_inventory_without_changing_metric_identity():
    collector = HuaweiNetwork()
    first = collector.collect("network_traffic", {"product": "ecs"}, NOW)
    collector.ecs_names["ecs-1"] = "renamed-server"
    cached = collector.collect("network_traffic", {"product": "ecs"}, NOW + timedelta(seconds=60))
    assert {metric.resource_name for metric in cached} == {"application-server", "database-server"}
    assert sum(call[1] == "list_servers_details" for call in collector.calls) == 1
    collector.observed = NOW
    refreshed = collector.collect("network_traffic", {"product": "ecs"}, NOW + timedelta(seconds=300))
    assert {metric.resource_name for metric in refreshed} == {"renamed-server", "database-server"}
    assert sum(call[1] == "list_servers_details" for call in collector.calls) == 2
    assert {metric.key() for metric in first} == {metric.key() for metric in refreshed}


def test_huawei_ecs_batches_names_and_keeps_successful_batches_on_failure(caplog):
    collector = HuaweiNetwork()
    original = collector._call
    ids = [f"ecs-{i}" for i in range(101)]

    def call(service, region, operation, request):
        if operation == "list_metrics":
            return {"metrics": [{"namespace": "SYS.ECS", "metric_name": "network_vm_bandwidth_in",
                                 "dimensions": [{"name": "instance_id", "value": rid}]} for rid in ids],
                    "meta_data": {"total": len(ids)}}
        if operation == "list_servers_details" and request.server_id == "ecs-100":
            collector.calls.append((service, operation, request, region))
            raise RuntimeError("secret-provider-payload")
        return original(service, region, operation, request)

    collector.ecs_names = {rid: f"server-{rid}" for rid in ids}
    collector._call = call
    metrics = collector.collect("network_traffic", {"product": "ecs"}, NOW)
    assert len(metrics) == 202
    assert {metric.resource_name for metric in metrics if metric.resource_id == "ecs-99"} == {"server-ecs-99"}
    assert {metric.resource_name for metric in metrics if metric.resource_id == "ecs-100"} == {"ecs-100"}
    requests = [call[2] for call in collector.calls if call[1] == "list_servers_details"]
    assert [request.server_id.split(",") for request in requests] == [ids[:100], ids[100:]]
    assert all(request.limit == 100 for request in requests)
    assert collector.network_partial_failures == 0
    assert "ecs:cloudServers:list" in caplog.text
    assert "secret-provider-payload" not in caplog.text


@pytest.mark.parametrize("servers", [
    None,
    [{"id": "unrelated", "name": "wrong-server"}],
    [{"id": "ecs-1", "name": "first"}, {"id": "ecs-1", "name": "second"}],
])
def test_huawei_ecs_invalid_metadata_falls_back_to_ids_without_losing_rates(servers, caplog):
    collector = HuaweiNetwork()
    original = collector._call

    def call(service, region, operation, request):
        if operation == "list_servers_details":
            return {"servers": servers}
        return original(service, region, operation, request)

    collector._call = call
    metrics = collector.collect("network_traffic", {"product": "ecs"}, NOW)
    assert len(metrics) == 4
    assert all(metric.resource_name == metric.resource_id for metric in metrics)
    assert "name lookup failed" in caplog.text


def test_huawei_ecs_sdk_metadata_matches_by_id_and_handles_deleted_servers(caplog):
    from huaweicloudsdkecs.v2 import ListServersDetailsResponse
    from types import SimpleNamespace

    collector = HuaweiNetwork()
    original = collector._call

    def call(service, region, operation, request):
        if operation == "list_servers_details":
            return ListServersDetailsResponse(servers=[
                SimpleNamespace(id="ecs-2", name="database", fault=None),
                SimpleNamespace(id="ecs-1", name=None, fault={"code": 404}),
            ])
        return original(service, region, operation, request)

    collector._call = call
    metrics = collector.collect("network_traffic", {"product": "ecs"}, NOW)
    assert {metric.resource_id: metric.resource_name for metric in metrics} == {"ecs-1": "ecs-1", "ecs-2": "database"}
    assert "names missing" in caplog.text


def test_huawei_ecs_names_stay_in_their_own_region():
    collector = HuaweiNetwork()
    original = collector._call

    def call(service, region, operation, request):
        if operation == "list_servers_details":
            return {"servers": [{"id": rid, "name": f"server-{region}"}
                                for rid in request.server_id.split(",")]}
        return original(service, region, operation, request)

    collector._call = call
    metrics = collector.collect("network_traffic", {"product": "ecs", "regions": [REGION, "cn-north-4"]}, NOW)
    assert len(metrics) == 8
    assert all(metric.resource_name == f"server-{metric.dimensions['region']}" for metric in metrics)


def test_huawei_rds_paginates_postpaid_mysql_and_pg_and_uses_correct_dimensions():
    collector = HuaweiNetwork()
    metrics = collector.collect("network_traffic", {"product": "rds"}, NOW)
    assert len(metrics) == 8
    assert all(metric.value == 81920 and metric.unit == "bit/s" for metric in metrics)
    requests = [call[2] for call in collector.calls if call[1] == "show_metric_data"]
    assert {request.dim_0.split(",")[0] for request in requests} == {"rds_cluster_id", "postgresql_cluster_id"}
    assert all(request.period == 60 for request in requests)
    assert [call[2].offset for call in collector.calls if call[1] == "list_instances"] == [0, 1, 0, 1]


@pytest.mark.parametrize("engine", ["MySQL", "PostgreSQL"])
@pytest.mark.parametrize("source_unit, expected", [
    ("byte/s", 80), ("Byte/s", 80), ("B/s", 80), ("Bytes/s", 80), ("bytes/s", 80),
    ("KiB/s", 81920), ("KB/s", 81920), ("KByte/s", 81920),
])
def test_huawei_rds_converts_each_returned_rate_unit_for_both_directions(engine, source_unit, expected):
    collector = HuaweiNetwork()
    collector.unit = source_unit
    metrics = collector.collect("network_traffic", {"product": "rds", "engines": [engine]}, NOW)
    assert len(metrics) == 4
    assert {metric.metric for metric in metrics} == {"network_in_bps", "network_out_bps"}
    assert all(metric.value == expected and metric.unit == "bit/s" for metric in metrics)
    assert all(metric.dimensions["engine"] == engine for metric in metrics)
    assert all(metric.dimensions["source_unit"] == source_unit for metric in metrics)


def test_huawei_rds_mixed_instance_and_direction_units_do_not_share_a_conversion_factor():
    collector = HuaweiNetwork()
    original = collector._call
    units = {
        ("MySQL0", "rds004_bytes_in"): "byte/s",
        ("MySQL0", "rds005_bytes_out"): "KiB/s",
        ("MySQL1", "rds004_bytes_in"): "KiB/s",
        ("MySQL1", "rds005_bytes_out"): "byte/s",
    }

    def call(service, region, operation, request):
        response = original(service, region, operation, request)
        if operation == "show_metric_data":
            resource_id = request.dim_0.split(",")[1]
            response["datapoints"][0]["unit"] = units[resource_id, request.metric_name]
        return response

    collector._call = call
    metrics = collector.collect("network_traffic", {"product": "rds", "engines": ["MySQL"]}, NOW)
    assert len(metrics) == 4
    values = {(metric.resource_id, metric.metric): metric.value for metric in metrics}
    assert values == {
        ("MySQL0", "network_in_bps"): 80,
        ("MySQL0", "network_out_bps"): 81920,
        ("MySQL1", "network_in_bps"): 81920,
        ("MySQL1", "network_out_bps"): 80,
    }


@pytest.mark.parametrize("engine", ["MySQL", "PostgreSQL"])
@pytest.mark.parametrize("source_unit, expected", [("byte/s", 80), ("KiB/s", 81920)])
def test_huawei_rds_uses_sdk_datapoint_unit(engine, source_unit, expected):
    collector = HuaweiNetwork()
    original = collector._call

    def call(service, region, operation, request):
        response = original(service, region, operation, request)
        if operation == "show_metric_data":
            return ShowMetricDataResponse(datapoints=[Datapoint(
                timestamp=int(collector.observed.timestamp() * 1000), average=10, unit=source_unit,
            )])
        return response

    collector._call = call
    metrics = collector.collect("network_traffic", {"product": "rds", "engines": [engine]}, NOW)
    assert len(metrics) == 4
    assert {metric.metric for metric in metrics} == {"network_in_bps", "network_out_bps"}
    assert all(metric.value == expected for metric in metrics)
    assert all(metric.dimensions["source_unit"] == source_unit for metric in metrics)


@pytest.mark.parametrize("product", ["ecs", "rds"])
@pytest.mark.parametrize("source_unit", [None, "", "Kb/s", "kb/s", "MByte/s", "Count"])
def test_huawei_rejects_missing_empty_unknown_and_bit_units(product, source_unit):
    collector = HuaweiNetwork()
    original = collector._call

    def call(service, region, operation, request):
        response = original(service, region, operation, request)
        if operation == "show_metric_data":
            response["datapoints"][0]["unit"] = source_unit
        return response

    collector._call = call
    with pytest.raises(HuaweiCollectionError, match="unit"):
        collector.collect("network_traffic", {"product": product}, NOW)


@pytest.mark.parametrize("product", ["ecs", "rds"])
def test_huawei_rejects_absent_datapoint_unit(product):
    collector = HuaweiNetwork()
    original = collector._call

    def call(service, region, operation, request):
        response = original(service, region, operation, request)
        if operation == "show_metric_data":
            del response["datapoints"][0]["unit"]
        return response

    collector._call = call
    with pytest.raises(HuaweiCollectionError, match="unit"):
        collector.collect("network_traffic", {"product": product}, NOW)


@pytest.mark.parametrize("source_unit", ["Byte/s", "B/s", "byte/s", "Bytes/s", "bytes/s"])
def test_huawei_ecs_converts_explicit_byte_rate_aliases(source_unit):
    collector = HuaweiNetwork()
    collector.unit = source_unit
    metrics = collector.collect("network_traffic", {"product": "ecs"}, NOW)
    assert len(metrics) == 4
    assert all(metric.value == 80 and metric.unit == "bit/s" for metric in metrics)
    assert all(metric.dimensions["source_unit"] == source_unit for metric in metrics)


def test_huawei_unknown_units_and_mysql_cluster_dimensions_fail():
    collector = HuaweiNetwork()
    collector.unit = "MByte/s"
    with pytest.raises(HuaweiCollectionError, match="unit"):
        collector.collect("network_traffic", {"product": "ecs"}, NOW)
    collector.rds_type = "Cluster"
    with pytest.raises(ValueError, match="node dimensions"):
        collector.collect("network_traffic", {"product": "rds", "engines": ["MySQL"]}, NOW)


@pytest.mark.parametrize("provider", [AliyunNetwork, HuaweiNetwork])
def test_missing_instance_keeps_healthy_metrics_and_reports_partial_failure(provider):
    collector = provider()
    if isinstance(collector, AliyunNetwork):
        collector.inventory.append({"InstanceId": "ecs-2"})
        original = collector._rpc

        def rpc(service, action, version, params, region=None):
            response = original(service, action, version, params, region)
            if action == "DescribeMetricList" and params["Dimensions"][0]["instanceId"] == "ecs-2":
                response["Datapoints"] = []
            return response

        collector._rpc = rpc
    else:
        original = collector._call

        def call(service, region, operation, request):
            response = original(service, region, operation, request)
            if operation == "show_metric_data" and request.dim_0 == "instance_id,ecs-2":
                response["datapoints"] = []
            return response

        collector._call = call
    options = {"product": "ecs"}
    metrics = collector.collect("network_traffic", options, NOW)
    assert len(metrics) == 2 and {metric.resource_id for metric in metrics} == {"ecs-1"}
    assert collector.network_partial_failures == 1
    if isinstance(collector, AliyunNetwork):
        collector._rpc = original
    else:
        collector._call = original
    assert len(collector.collect("network_traffic", options, NOW)) == 4
    assert collector.network_partial_failures == 0


def test_unsupported_mysql_topology_keeps_pg_metrics_and_reports_partial_failure():
    collector = HuaweiNetwork()
    collector.rds_type = "Enterprise"
    metrics = collector.collect("network_traffic", {"product": "rds"}, NOW)
    assert len(metrics) == 4
    assert {metric.dimensions["engine"] for metric in metrics} == {"PostgreSQL"}
    assert collector.network_partial_failures == 2


def test_huawei_inventory_repeated_marker_is_an_error():
    collector = HuaweiNetwork()
    original = collector._call

    def call(service, region, operation, request):
        result = original(service, region, operation, request)
        if operation == "list_metrics":
            result["meta_data"]["total"] = 3
        return result

    collector._call = call
    with pytest.raises(ValueError, match="repeated"):
        collector.collect("network_traffic", {"product": "ecs"}, NOW)


def test_cms_metadata_paginates_and_expired_invalid_metadata_is_not_reused():
    collector = AliyunNetwork()
    original = collector._rpc

    def rpc(service, action, version, params, region=None):
        response = original(service, action, version, params, region)
        if action == "DescribeMetricMetaList":
            if params["PageNumber"] == 1:
                response["Resources"]["Resource"][0]["MetricName"] = "other_metric"
            response["TotalCount"] = 2
        return response

    collector._rpc = rpc
    options = {"product": "ecs", "metadata_cache_seconds": 60}
    collector.collect("network_traffic", options, NOW)
    assert sum(call[1] == "DescribeMetricMetaList" for call in collector.calls) == 4
    collector.metadata_unit = "Bytes"
    with pytest.raises(ValueError, match="unit"):
        collector.collect("network_traffic", options, NOW + timedelta(seconds=60))


def test_partial_inventory_pagination_does_not_cache_an_incomplete_list():
    collector = AliyunNetwork()
    original = collector._rpc

    def rpc(service, action, version, params, region=None):
        response = original(service, action, version, params, region)
        if action == "DescribeInstances":
            response["TotalCount"] = 2
        return response

    collector._rpc = rpc
    with pytest.raises(ValueError, match="pagination"):
        collector.collect("network_traffic", {"product": "ecs"}, NOW)
    collector._rpc = original
    assert len(collector.collect("network_traffic", {"product": "ecs"}, NOW)) == 2


def test_scope_is_part_of_network_metric_identity():
    collector = AliyunNetwork()
    spec = {"namespace": "acs_ecs_dashboard", "metric_name": "IntranetInRate",
            "resource_id": "ecs-1", "region": REGION, "dimensions": {"instanceId": "ecs-1"},
            "output_metric": "network_in_bps", "scope": "intranet", "product": "ecs"}
    first = collector._metrics({"metrics": [spec]}, NOW)[0]
    second = collector._metrics({"metrics": [dict(spec, scope="all")]}, NOW)[0]
    assert first.key() != second.key()


@pytest.mark.parametrize("unit", ["Count", "Bytes", "MB", None, float("nan")])
def test_cms_metadata_rejects_unknown_rate_units(unit):
    collector = AliyunNetwork()
    collector.metadata_unit = unit or "Count"
    with pytest.raises(ValueError, match="unit"):
        collector.collect("network_traffic", {"product": "ecs"}, NOW)


@pytest.mark.parametrize("changes", [{"Dimensions": "userId,instanceId,nodeId"}, {"Periods": "300"},
                                     {"Statistics": "Sum"}])
def test_cms_metadata_rejects_incompatible_dimension_period_or_statistic(changes):
    collector = AliyunNetwork()
    original = collector._rpc

    def rpc(service, action, version, params, region=None):
        response = original(service, action, version, params, region)
        if action == "DescribeMetricMetaList":
            response["Resources"]["Resource"][0].update(changes)
        return response

    collector._rpc = rpc
    with pytest.raises(ValueError, match="metadata"):
        collector.collect("network_traffic", {"product": "ecs"}, NOW)


@pytest.mark.parametrize("options", [{}, {"product": "vpc"}, {"product": []},
    {"product": "ecs", "engines": [{}]}, {"product": "rds", "instances": []},
    {"product": "ecs", "instances": None}, {"product": "ecs", "instances": {}},
    {"product": "ecs", "instances": "selected-1"}, {"product": "ecs", "instances": False},
    {"product": "ecs", "inventory_cache_seconds": False}, {"product": "ecs", "period": 60.1},
    {"product": "ecs", "max_age_seconds": 1}, {"product": "ecs", "regions": []},
    {"product": "rds", "instances": [{"resource_id": "db", "region": REGION}]},
    {"product": "ecs", "instances": [{"resource_id": "id", "region": REGION}] * 2}])
def test_invalid_configuration_fails_without_cloud_requests(options):
    with pytest.raises(ValueError):
        validate_network_traffic_options(options)
