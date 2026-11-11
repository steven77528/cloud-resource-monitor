import io
import json
import logging
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from aliyunsdkcore.acs_exception.exceptions import ServerException

from scripts.probe_aliyun_rds_eip import main, probe_config


def config(explicit=True):
    options = {"product": "rds"}
    if explicit:
        options["instances"] = [{"region": "cn-hangzhou", "resource_id": "rm-secret-id", "engine": "MySQL"}]
    return {"accounts": [{"id": "secret-account", "provider": "aliyun", "enabled": True,
                           "regions": ["cn-hangzhou"], "access_key_secret": "secret-key"}],
            "jobs": [{"id": "job", "account": "secret-account", "kind": "network_traffic", "options": options}]}


def endpoints(*rows):
    return {"DBInstanceNetInfos": {"DBInstanceNetInfo": list(rows)},
            "SecurityIPGroups": {"SecurityIPs": "secret-whitelist"}}


def eip_page(rows, total=None):
    return {"TotalCount": len(rows) if total is None else total, "EipAddresses": {"EipAddress": rows}}


class ProbeTests(unittest.TestCase):
    def run_probe(self, cfg, responses):
        calls, captured = [], []

        class FakeCollector:
            def __init__(self, account):
                captured.append(account)

            def _rpc(self, service, action, version, params, region):
                calls.append((service, action, version, params, region))
                response = responses.pop(0)
                if isinstance(response, Exception):
                    raise response
                return response

        report = probe_config(cfg, FakeCollector)
        self.assertFalse(responses)
        return report, calls, captured

    def test_all_endpoints_and_eip_pages_are_checked_without_printing_sensitive_fields(self):
        address = "47.75.1.2"
        rows = [{"AllocationId": f"eip-secret-{n}", "IpAddress": address,
                 "InstanceId": "bound-secret-id", "InstanceType": "IpAddress", "Status": "InUse",
                 "ServiceManaged": 1, "Description": "secret-description"} for n in range(101)]
        cfg = config()
        report, calls, captured = self.run_probe(cfg, [
            endpoints({"IPType": "Private", "IPAddress": "10.1.2.3"},
                      {"IPType": "Public", "IPAddress": address, "ConnectionStringType": "Normal"},
                      {"IPType": "Public", "IPAddress": address, "ConnectionStringType": "ReadWriteSplitting"}),
            eip_page(rows[:100], 101), eip_page(rows[100:], 101),
        ])
        resource = report["reports"][0]["resources"][0]
        self.assertEqual(resource["endpoint_count"], 3)
        self.assertEqual(resource["public_endpoint_count"], 2)
        self.assertEqual(resource["unique_public_ipv4_count"], 1)
        self.assertEqual(resource["eip_lookups"][0]["eip_count"], 101)
        self.assertEqual(resource["status"], "eip_address_match_binding_unverified")
        self.assertEqual([call[3].get("PageNumber") for call in calls[1:]], [1, 2])
        self.assertEqual(calls[1][:3], ("vpc", "DescribeEipAddresses", "2016-04-28"))
        self.assertEqual(calls[1][3]["EipAddress"], address)
        self.assertNotIn("AssociatedInstanceType", calls[1][3])
        self.assertEqual(captured[0]["endpoints"]["vpc"], "vpc.cn-hangzhou.aliyuncs.com")
        self.assertNotIn("endpoints", cfg["accounts"][0])
        serialized = json.dumps(report)
        for sensitive in (address, "10.1.2.3", "secret", "SecurityIPGroups", "IPAddress"):
            self.assertNotIn(sensitive, serialized)

    def test_permission_denial_is_not_an_empty_eip_result(self):
        report, _, _ = self.run_probe(config(), [
            endpoints({"IPType": "Public", "IPAddress": "47.75.1.2"}),
            ServerException("Forbidden.RAM", "secret-signature secret-whitelist", 403),
        ])
        resource = report["reports"][0]["resources"][0]
        self.assertEqual(report["status"], "incomplete")
        self.assertEqual(resource["status"], "eip_lookup_incomplete")
        self.assertEqual(resource["eip_lookups"][0]["status"], "permission_denied")
        self.assertEqual(report["required_actions"], ["vpc:DescribeEipAddresses"])
        self.assertNotIn("secret", json.dumps(report))

    def test_empty_success_is_limited_to_visible_scope(self):
        report, _, _ = self.run_probe(config(), [
            endpoints({"IPType": "Public", "IPAddress": "47.75.1.2"}), eip_page([]),
        ])
        resource = report["reports"][0]["resources"][0]
        self.assertEqual(resource["status"], "no_match_in_visible_scope")
        self.assertEqual(resource["eip_lookups"][0]["eip_count"], 0)
        self.assertEqual(report["status"], "complete_in_current_scope")

    def test_no_public_endpoint_does_not_query_eips(self):
        report, calls, _ = self.run_probe(config(), [endpoints({"IPType": "Private", "IPAddress": "10.1.2.3"})])
        self.assertEqual(report["reports"][0]["resources"][0]["status"], "no_public_endpoint_returned")
        self.assertEqual(len(calls), 1)

    def test_incomplete_page_and_mismatched_address_are_failures(self):
        cases = [eip_page([], 1),
                 eip_page([{"AllocationId": "eip-1", "IpAddress": "47.75.1.3"}]),
                 {"TotalCount": False, "EipAddresses": {"EipAddress": []}}]
        for response in cases:
            with self.subTest(response=response):
                report, _, _ = self.run_probe(config(), [
                    endpoints({"IPType": "Public", "IPAddress": "47.75.1.2"}), response,
                ])
                lookup = report["reports"][0]["resources"][0]["eip_lookups"][0]
                self.assertEqual(lookup["status"], "query_failed_or_invalid_response")
                self.assertNotIn("eip_count", lookup)

    def test_enabled_network_job_selection_and_inventory_engine_filter(self):
        cfg = config(explicit=False)
        cfg["jobs"].extend([{**cfg["jobs"][0], "id": "duplicate"},
                            {**cfg["jobs"][0], "id": "disabled", "enabled": False}])
        cfg["accounts"].append({"id": "disabled", "provider": "aliyun", "enabled": False})
        cfg["jobs"].append({**cfg["jobs"][0], "id": "disabled-account", "account": "disabled"})
        report, calls, _ = self.run_probe(cfg, [
            {"TotalRecordCount": 2, "Items": {"DBInstance": [
                {"DBInstanceId": "rm-secret-id", "Engine": "MySQL"},
                {"DBInstanceId": "pg-secret-id", "Engine": "PostgreSQL"}]}},
            endpoints(),
        ])
        self.assertEqual(len(report["reports"]), 1)
        self.assertEqual(report["reports"][0]["resource_count"], 1)
        self.assertEqual([call[1] for call in calls], ["DescribeDBInstances", "DescribeDBInstanceNetInfo"])

    def test_inventory_permission_failure_lists_existing_required_permission(self):
        report, _, _ = self.run_probe(config(explicit=False), [ServerException("Forbidden", "secret", 403)])
        self.assertEqual(report["required_actions"], ["rds:DescribeDBInstances"])
        self.assertEqual(report["reports"][0]["inventory_status"], "incomplete")
        self.assertEqual(report["status"], "incomplete")

    def test_authentication_error_does_not_request_more_permissions(self):
        report, _, _ = self.run_probe(config(), [ServerException("SignatureDoesNotMatch", "secret", 403)])
        self.assertEqual(report["required_actions"], [])
        self.assertEqual(report["reports"][0]["resources"][0]["status"], "query_failed_or_invalid_response")
        self.assertEqual(report["status"], "incomplete")

    def test_cli_uses_normal_loader_and_masks_config_errors(self):
        output = io.StringIO()
        with patch("scripts.probe_aliyun_rds_eip.load_config", side_effect=ValueError("secret-key")) as loader:
            with redirect_stdout(output):
                self.assertEqual(main(["--config", "custom-config.yaml"]), 1)
        loader.assert_called_once_with("custom-config.yaml")
        self.assertEqual(json.loads(output.getvalue()), {"read_only": True, "status": "configuration_failed"})

    def test_cli_restores_process_logging_state_on_success_and_failure(self):
        previous_logging_disable = logging.root.manager.disable
        try:
            for succeeds in (True, False):
                with self.subTest(succeeds=succeeds):
                    logging.disable(logging.ERROR)
                    loader_options = ({"return_value": {"accounts": [], "jobs": []}} if succeeds else
                                      {"side_effect": ValueError("secret-key")})
                    with patch("scripts.probe_aliyun_rds_eip.load_config", **loader_options):
                        with redirect_stdout(io.StringIO()):
                            self.assertEqual(main([]), 0 if succeeds else 1)
                    self.assertEqual(logging.root.manager.disable, logging.ERROR)
        finally:
            logging.disable(previous_logging_disable)


if __name__ == "__main__":
    unittest.main()
