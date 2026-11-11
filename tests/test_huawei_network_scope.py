from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from cloud_monitor.providers.huawei import HuaweiCollector, HuaweiCollectionError
from cloud_monitor.providers.huawei_network_scope import HuaweiIntranetCollector


REGION = "cn-east-3"
NOW = datetime(2026, 10, 27, 10, 6, tzinfo=timezone.utc)
STAMP = int(datetime(2026, 10, 27, 10, 0, tzinfo=timezone.utc).timestamp() * 1000)


def server(rid="ecs-1", address="198.51.100.1", port="port-1"):
    return {"id": rid, "name": rid, "addresses": {"vpc": [
        {"addr": address, "OS-EXT-IPS:type": "floating", "OS-EXT-IPS:port_id": port}]}}


def eip(rid="eip-1", address="198.51.100.1", port="port-1"):
    return {"id": rid, "public_ip_address": address, "port_id": port}


class FakeScope(HuaweiIntranetCollector):
    def __init__(self):
        super().__init__(HuaweiCollector({"id": "account", "regions": [REGION]}))
        self.servers = [server()]
        self.instances = [{"id": "rds-1", "name": "database", "type": "Ha",
                           "datastore": {"type": "MySQL"}, "public_ips": ["198.51.100.1"]}]
        self.eips = [eip()]
        self.stamp = STAMP
        self.overrides = {}
        self.calls = []

    def _request(self, service, region, query):
        self.calls.append((service, region, query))
        if service == "ecs":
            return {"servers": deepcopy(self.servers), "count": len(self.servers)}
        if service == "rds":
            return {"instances": deepcopy(self.instances), "total_count": len(self.instances)}
        if service == "vpc":
            return {"publicips": deepcopy(self.eips)}
        metric, rid = query["metric_name"], query["dim.0"].split(",", 1)[1]
        if (metric, rid) in self.overrides:
            value = self.overrides[(metric, rid)]
            if isinstance(value, Exception):
                raise value
            return {"datapoints": deepcopy(value)}
        incoming = metric in {"network_vm_bandwidth_in", "rds004_bytes_in", "downstream_bandwidth"}
        public = query["namespace"] == "SYS.VPC"
        return {"datapoints": [{"timestamp": self.stamp, "average": 100 if incoming else 200,
                                "unit": "bit/s" if public else "Byte/s"}]}


def selected(product="ecs", rid=None):
    result = {"resource_id": rid or product + "-1", "resource_name": "selected-name", "region": REGION}
    if product == "rds":
        result["engine"] = "MySQL"
    return result


def test_estimate_uses_same_direction_closed_window_and_stable_source_identity():
    scope = FakeScope()
    readings = scope.collect({"product": "ecs"}, [selected()], NOW)
    assert [reading.value for reading in readings] == [700, 1400]
    assert all(reading.resource_name == "selected-name" for reading in readings)
    assert all(reading.observed_at == datetime.fromtimestamp(STAMP / 1000, timezone.utc) for reading in readings)
    assert all(reading.dimensions["scope"] == "intranet_estimated" for reading in readings)
    assert all(reading.dimensions["source"] == "total_minus_eip" for reading in readings)
    keys = [reading.key() for reading in readings]
    for service, _, query in scope.calls:
        if service == "ces":
            assert query["period"] == 300 and query["filter"] == "average"
            assert query["to"] < int(NOW.timestamp() // 300) * 300000
    replacement = FakeScope()
    replacement.eips[0]["id"] = "replacement-eip"
    again = replacement.collect({"product": "ecs"}, [selected()], NOW)
    assert [reading.key() for reading in again] == keys


@pytest.mark.parametrize("unit,multiplier", [("byte/s", 8), ("KiB/s", 8192), ("KByte/s", 8192)])
def test_rds_converts_actual_units_without_changing_instance_identity(unit, multiplier):
    scope = FakeScope()
    scope.overrides[("rds004_bytes_in", "rds-1")] = [{"timestamp": STAMP, "average": 2, "unit": unit}]
    scope.overrides[("downstream_bandwidth", "eip-1")] = [{"timestamp": STAMP, "average": 1, "unit": "bit/s"}]
    readings = scope.collect({"product": "rds"}, [selected("rds")], NOW)
    assert readings[0].value == 2 * multiplier - 1
    assert readings[0].resource_id == "rds-1"
    assert readings[0].dimensions["engine"] == "MySQL"
    assert readings[0].dimensions["identity"]["engine"] == "MySQL"


def test_postgres_uses_correct_cluster_dimension():
    scope = FakeScope()
    scope.instances[0]["datastore"]["type"] = "PostgreSQL"
    instance = selected("rds")
    instance["engine"] = "PostgreSQL"
    assert len(scope.collect({"product": "rds"}, [instance], NOW)) == 2
    total_queries = [query for service, _, query in scope.calls if service == "ces" and query["namespace"] == "SYS.RDS"]
    assert all(query["dim.0"] == "postgresql_cluster_id,rds-1" for query in total_queries)


@pytest.mark.parametrize("change", ["missing", "port_conflict", "duplicate_address", "unbound"])
def test_uncertain_eip_mappings_never_become_total_private_traffic(change):
    scope = FakeScope()
    if change == "missing":
        scope.eips = []
    elif change == "port_conflict":
        scope.eips[0]["port_id"] = "other-port"
    elif change == "duplicate_address":
        scope.eips.append(eip(rid="eip-2"))
    else:
        scope.eips[0].pop("port_id")
    with pytest.raises(HuaweiCollectionError, match="EIP mapping"):
        scope.collect({"product": "ecs"}, [selected()], NOW)
    assert scope.collector.network_partial_failures == 1
    assert not any(service == "ces" for service, _, _ in scope.calls)


@pytest.mark.parametrize("point", [
    {"timestamp": STAMP + 60000, "average": 1, "unit": "bit/s"},
    {"timestamp": STAMP, "average": 1, "unit": "Byte/s"},
    {"timestamp": STAMP, "average": -1, "unit": "bit/s"},
    {"timestamp": STAMP, "average": float("nan"), "unit": "bit/s"},
])
def test_malformed_units_timestamps_and_source_values_remain_collection_errors(point):
    scope = FakeScope()
    scope.overrides[("downstream_bandwidth", "eip-1")] = [point]
    with pytest.raises(HuaweiCollectionError):
        scope.collect({"product": "ecs"}, [selected()], NOW)
    assert scope.collector.network_partial_failures == 1
    assert scope.collector.network_estimate_unavailable == []


def test_latest_common_negative_difference_is_not_replaced_by_an_older_good_point():
    scope = FakeScope()
    for metric, rid, unit, value in [("network_vm_bandwidth_in", "ecs-1", "Byte/s", 100),
                                   ("network_vm_bandwidth_out", "ecs-1", "Byte/s", 100),
                                   ("downstream_bandwidth", "eip-1", "bit/s", 900),
                                   ("upstream_bandwidth", "eip-1", "bit/s", 1)]:
        scope.overrides[(metric, rid)] = [{"timestamp": STAMP - 300000, "average": 1, "unit": unit},
                                          {"timestamp": STAMP, "average": value, "unit": unit}]
    assert scope.collect({"product": "ecs"}, [selected()], NOW) == []
    assert scope.collector.network_partial_failures == 0
    assert scope.collector.network_estimate_unavailable == [{
        "resource_id": "ecs-1", "region": REGION,
        "observed_at": datetime.fromtimestamp(STAMP / 1000, timezone.utc),
        "reason": "negative_difference"}]


@pytest.mark.parametrize("product", ["ecs", "rds"])
def test_finite_negative_difference_has_a_typed_quality_error_with_the_source_time(product):
    scope = FakeScope()
    scope.overrides[("downstream_bandwidth", "eip-1")] = [
        {"timestamp": STAMP, "average": 900, "unit": "bit/s"}]
    metadata, mappings = scope._inventory(product, REGION, {"product": product}, NOW)
    instance = selected(product)

    with pytest.raises(HuaweiCollectionError) as caught:
        scope._estimate({"product": product}, instance, metadata[instance["resource_id"]],
                        mappings[instance["resource_id"]], NOW)

    assert type(caught.value).__name__ == "NetworkEstimateUnavailable"
    assert str(caught.value) == "Huawei network estimate has a negative difference"
    assert caught.value.observed_at == datetime.fromtimestamp(STAMP / 1000, timezone.utc)


def test_real_outbound_negative_difference_is_unavailable_not_a_zero_sample(caplog):
    scope = FakeScope()
    scope.overrides[("network_vm_bandwidth_out", "ecs-1")] = [
        {"timestamp": STAMP, "average": 60701.44 / 8, "unit": "Byte/s"}]
    scope.overrides[("upstream_bandwidth", "eip-1")] = [
        {"timestamp": STAMP, "average": 638741.87, "unit": "bit/s"}]

    readings = scope.collect({"product": "ecs"}, [selected()], NOW)

    assert readings == []
    assert scope.collector.network_partial_failures == 0
    assert scope.collector.network_estimate_unavailable == [{
        "resource_id": "ecs-1", "region": REGION,
        "observed_at": datetime.fromtimestamp(STAMP / 1000, timezone.utc),
        "reason": "negative_difference"}]
    assert "negative_difference" in caplog.text and "ecs-1" in caplog.text and REGION in caplog.text


def test_unavailable_resource_keeps_valid_resources_without_an_api_failure():
    scope = FakeScope()
    scope.servers.append(server("ecs-2", "198.51.100.2", "port-2"))
    scope.eips.append(eip("eip-2", "198.51.100.2", "port-2"))
    scope.overrides[("downstream_bandwidth", "eip-1")] = [
        {"timestamp": STAMP, "average": 900, "unit": "bit/s"}]

    readings = scope.collect({"product": "ecs"}, [selected(), selected(rid="ecs-2")], NOW)

    assert len(readings) == 2 and {reading.resource_id for reading in readings} == {"ecs-2"}
    assert [reading.value for reading in readings] == [700, 1400]
    assert scope.collector.network_partial_failures == 0
    assert [row["resource_id"] for row in scope.collector.network_estimate_unavailable] == ["ecs-1"]


def test_unavailable_resource_does_not_hide_a_real_failure_when_no_valid_resource_remains():
    scope = FakeScope()
    scope.servers.append(server("ecs-2", "198.51.100.2", "port-2"))
    scope.overrides[("downstream_bandwidth", "eip-1")] = [
        {"timestamp": STAMP, "average": 900, "unit": "bit/s"}]

    with pytest.raises(HuaweiCollectionError, match="EIP mapping"):
        scope.collect({"product": "ecs"}, [selected(), selected(rid="ecs-2")], NOW)

    assert scope.collector.network_partial_failures == 1
    assert [row["resource_id"] for row in scope.collector.network_estimate_unavailable] == ["ecs-1"]


def test_valid_unavailable_and_failed_resources_are_counted_separately():
    scope = FakeScope()
    scope.servers.extend([server("ecs-2", "198.51.100.2", "port-2"),
                          server("ecs-3", "198.51.100.3", "port-3")])
    scope.eips.append(eip("eip-2", "198.51.100.2", "port-2"))
    scope.overrides[("downstream_bandwidth", "eip-1")] = [
        {"timestamp": STAMP, "average": 900, "unit": "bit/s"}]

    readings = scope.collect({"product": "ecs"},
                             [selected(), selected(rid="ecs-2"), selected(rid="ecs-3")], NOW)

    assert len(readings) == 2 and {reading.resource_id for reading in readings} == {"ecs-2"}
    assert scope.collector.network_partial_failures == 1
    assert [row["resource_id"] for row in scope.collector.network_estimate_unavailable] == ["ecs-1"]


def test_nonfinite_difference_from_finite_sources_remains_a_real_failure():
    scope = FakeScope()
    scope.servers[0]["addresses"]["vpc"].append(
        {"addr": "198.51.100.2", "OS-EXT-IPS:type": "floating", "OS-EXT-IPS:port_id": "port-2"})
    scope.eips.append(eip("eip-2", "198.51.100.2", "port-2"))
    for rid in ("eip-1", "eip-2"):
        scope.overrides[("downstream_bandwidth", rid)] = [
            {"timestamp": STAMP, "average": 1e308, "unit": "bit/s"}]

    with pytest.raises(HuaweiCollectionError, match="non-finite") as caught:
        scope.collect({"product": "ecs"}, [selected()], NOW)

    assert type(caught.value) is HuaweiCollectionError
    assert scope.collector.network_partial_failures == 1
    assert scope.collector.network_estimate_unavailable == []


def test_unavailable_and_partial_failure_attributes_are_reset_for_every_attempt():
    scope = FakeScope()
    scope.overrides[("downstream_bandwidth", "eip-1")] = [
        {"timestamp": STAMP, "average": 900, "unit": "bit/s"}]
    assert scope.collect({"product": "ecs"}, [selected()], NOW) == []
    assert len(scope.collector.network_estimate_unavailable) == 1

    scope.overrides[("downstream_bandwidth", "eip-1")] = []
    with pytest.raises(HuaweiCollectionError, match="no datapoints"):
        scope.collect({"product": "ecs"}, [selected()], NOW)
    assert scope.collector.network_estimate_unavailable == []
    assert scope.collector.network_partial_failures == 1

    scope.overrides.clear()
    assert len(scope.collect({"product": "ecs"}, [selected()], NOW)) == 2
    assert scope.collector.network_estimate_unavailable == []
    assert scope.collector.network_partial_failures == 0
    scope.collector.network_estimate_unavailable = [{"resource_id": "stale"}]
    scope.collector.network_partial_failures = 3
    assert scope.collect({"product": "ecs"}, [], NOW) == []
    assert scope.collector.network_estimate_unavailable == []
    assert scope.collector.network_partial_failures == 0


def test_resource_failure_log_uses_only_safe_error_details(caplog, monkeypatch):
    scope = FakeScope()

    def failed(*args):
        raise RuntimeError("Authorization: test-secret-token raw-private-response")

    monkeypatch.setattr(scope, "_estimate", failed)
    with pytest.raises(RuntimeError):
        scope.collect({"product": "ecs"}, [selected()], NOW)

    assert "RuntimeError" in caplog.text and "ecs-1" in caplog.text and REGION in caplog.text
    assert "test-secret-token" not in caplog.text and "raw-private-response" not in caplog.text


def test_multiple_eips_require_every_source_and_sum_before_subtraction():
    scope = FakeScope()
    scope.servers[0]["addresses"]["vpc"].append(
        {"addr": "198.51.100.2", "OS-EXT-IPS:type": "floating", "OS-EXT-IPS:port_id": "port-2"})
    scope.eips.append(eip("eip-2", "198.51.100.2", "port-2"))
    assert [reading.value for reading in scope.collect({"product": "ecs"}, [selected()], NOW)] == [600, 1200]
    scope.overrides[("downstream_bandwidth", "eip-2")] = []
    with pytest.raises(HuaweiCollectionError, match="no datapoints"):
        scope.collect({"product": "ecs"}, [selected()], NOW)


def test_one_resource_failure_keeps_other_resource_pair_and_marks_partial_failure():
    scope = FakeScope()
    scope.servers.append(server("ecs-2", "198.51.100.2", "port-2"))
    readings = scope.collect({"product": "ecs"}, [selected(), selected(rid="ecs-2")], NOW)
    assert len(readings) == 2
    assert {reading.resource_id for reading in readings} == {"ecs-1"}
    assert scope.collector.network_partial_failures == 1


def test_stale_sources_and_current_bucket_are_rejected():
    scope = FakeScope()
    with pytest.raises(HuaweiCollectionError, match="stale"):
        scope.collect({"product": "ecs", "lookback_seconds": 3600}, [selected()], NOW + timedelta(minutes=20))
    scope.overrides[("downstream_bandwidth", "eip-1")] = [
        {"timestamp": STAMP + 300000, "average": 1, "unit": "bit/s"}]
    with pytest.raises(HuaweiCollectionError, match="timestamp"):
        scope.collect({"product": "ecs"}, [selected()], NOW)


def test_empty_selection_never_queries_inventory_and_metadata_cache_expires():
    scope = FakeScope()
    assert scope.collect({"product": "ecs"}, [], NOW) == []
    assert scope.calls == []
    scope.collect({"product": "ecs"}, [selected()], NOW)
    scope.collect({"product": "ecs"}, [selected()], NOW + timedelta(seconds=60))
    assert sum(service == "vpc" for service, _, _ in scope.calls) == 1
    scope.collect({"product": "ecs"}, [selected()], NOW + timedelta(seconds=300))
    assert sum(service == "vpc" for service, _, _ in scope.calls) == 2


def test_incomplete_paginated_inventory_is_not_cached_or_used():
    scope = FakeScope()
    scope._request = lambda service, region, query: {"servers": [server()], "count": 2}
    with pytest.raises(HuaweiCollectionError, match="incomplete"):
        scope.collect({"product": "ecs"}, [selected()], NOW)
    assert scope._inventory_cache == {}


def test_known_eip_replacement_waits_for_sources_after_the_observed_change():
    scope = FakeScope()
    scope.collect({"product": "ecs"}, [selected()], NOW)
    scope.eips[0]["id"] = "replacement-eip"
    changed = NOW + timedelta(minutes=5)
    with pytest.raises(HuaweiCollectionError, match="binding change"):
        scope.collect({"product": "ecs"}, [selected()], changed)
    cutoff = int(datetime(2026, 10, 27, 10, 15, tzinfo=timezone.utc).timestamp() * 1000)
    assert scope._binding_cutoffs[("ecs", REGION, "ecs-1")] == cutoff
    scope.stamp = cutoff
    readings = scope.collect({"product": "ecs"}, [selected()], NOW + timedelta(minutes=15))
    assert len(readings) == 2
    assert all(reading.observed_at.timestamp() * 1000 == cutoff for reading in readings)


def test_replacement_rejects_older_common_points_even_after_a_closed_window_exists():
    scope = FakeScope()
    scope.collect({"product": "ecs"}, [selected()], NOW)
    scope.eips[0]["id"] = "replacement-eip"
    with pytest.raises(HuaweiCollectionError):
        scope.collect({"product": "ecs"}, [selected()], NOW + timedelta(minutes=5))
    scope.stamp = STAMP + 600000
    with pytest.raises(HuaweiCollectionError, match="current EIP binding"):
        scope.collect({"product": "ecs"}, [selected()], NOW + timedelta(minutes=15))


def test_initially_unmapped_then_mapped_is_a_known_change():
    scope = FakeScope()
    scope.eips = []
    with pytest.raises(HuaweiCollectionError, match="EIP mapping"):
        scope.collect({"product": "ecs"}, [selected()], NOW)
    assert scope._binding_cutoffs == {}
    scope.eips = [eip()]
    with pytest.raises(HuaweiCollectionError, match="binding change"):
        scope.collect({"product": "ecs"}, [selected()], NOW + timedelta(minutes=5))


def test_unbind_and_rebind_to_the_same_eip_advances_the_resource_cutoff():
    scope = FakeScope()
    scope.collect({"product": "ecs"}, [selected()], NOW)
    scope.eips = []
    with pytest.raises(HuaweiCollectionError, match="EIP mapping"):
        scope.collect({"product": "ecs"}, [selected()], NOW + timedelta(minutes=5))
    first_cutoff = scope._binding_cutoffs[("ecs", REGION, "ecs-1")]
    scope.eips = [eip()]
    with pytest.raises(HuaweiCollectionError, match="binding change"):
        scope.collect({"product": "ecs"}, [selected()], NOW + timedelta(minutes=10))
    assert scope._binding_history[("ecs", REGION, "ecs-1")] == ("eip-1",)
    assert scope._binding_cutoffs[("ecs", REGION, "ecs-1")] == first_cutoff + 300000


def test_binding_change_does_not_suppress_other_resources():
    scope = FakeScope()
    scope.servers.append(server("ecs-2", "198.51.100.2", "port-2"))
    scope.eips.append(eip("eip-2", "198.51.100.2", "port-2"))
    instances = [selected(), selected(rid="ecs-2")]
    assert len(scope.collect({"product": "ecs"}, instances, NOW)) == 4
    scope.eips[0]["id"] = "replacement-eip"
    readings = scope.collect({"product": "ecs"}, instances, NOW + timedelta(minutes=5))
    assert len(readings) == 2
    assert {reading.resource_id for reading in readings} == {"ecs-2"}
    assert scope.collector.network_partial_failures == 1
    assert ("ecs", REGION, "ecs-2") not in scope._binding_cutoffs


def test_disappearing_resource_then_reappearing_binding_is_guarded():
    scope = FakeScope()
    scope.collect({"product": "ecs"}, [selected()], NOW)
    scope.servers = []
    with pytest.raises(HuaweiCollectionError, match="EIP mapping"):
        scope.collect({"product": "ecs"}, [selected()], NOW + timedelta(minutes=5))
    assert scope._binding_history[("ecs", REGION, "ecs-1")] is None
    scope.servers = [server()]
    with pytest.raises(HuaweiCollectionError, match="binding change"):
        scope.collect({"product": "ecs"}, [selected()], NOW + timedelta(minutes=10))


def test_failed_inventory_refresh_does_not_change_binding_history():
    scope = FakeScope()
    scope.collect({"product": "ecs"}, [selected()], NOW)
    original = scope._request

    def request(service, region, query):
        if service == "vpc":
            raise HuaweiCollectionError("unavailable")
        return original(service, region, query)

    scope._request = request
    with pytest.raises(HuaweiCollectionError, match="unavailable"):
        scope.collect({"product": "ecs"}, [selected()], NOW + timedelta(minutes=5))
    assert scope._binding_cutoffs == {}
    assert scope._binding_history[("ecs", REGION, "ecs-1")] == ("eip-1",)
