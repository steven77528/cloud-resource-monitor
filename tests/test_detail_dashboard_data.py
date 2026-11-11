"""Offline regressions against the four actual detail dashboard exports."""

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "update_detail_dashboard_data", ROOT / "scripts" / "update_detail_dashboard_data.py")
migration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migration)


def load_backup(uid):
    return json.loads((ROOT / "grafana" / "backups" / f"{uid}.before.json").read_text(encoding="utf-8"))


def panel(dashboard, panel_id):
    return dashboard["spec"]["elements"][f"panel-{panel_id}"]["spec"]


def expression(dashboard, panel_id):
    return panel(dashboard, panel_id)["data"]["spec"]["queries"][0]["spec"]["query"]["spec"]["expr"]


class DetailDashboardMigrationTests(unittest.TestCase):
    def test_only_target_panels_change_and_original_is_untouched(self):
        selections = {
            migration.CONTAINER_UID: {28},
            migration.NACOS_UID: {50, 51, 54, 55, 56, 57, 58, 59, 60, 61, 70, 94},
            **{uid: {14, 16, 18} for uid in migration.KAFKA_UIDS},
        }
        for uid, changed_ids in selections.items():
            with self.subTest(uid=uid):
                original = load_backup(uid)
                snapshot = deepcopy(original)
                updated = migration.update_dashboard(original)
                self.assertEqual(original, snapshot)
                self.assertEqual(updated, migration.update_dashboard(updated))
                changed = {f"panel-{value}" for value in changed_ids}
                before = deepcopy(original)
                after = deepcopy(updated)
                for key in changed:
                    before["spec"]["elements"].pop(key)
                    after["spec"]["elements"].pop(key)
                self.assertEqual(after, before)

    def test_container_text_labels_stay_rows_and_aliases_are_preserved(self):
        original = load_backup(migration.CONTAINER_UID)
        updated = migration.update_dashboard(original)
        before, after = panel(original, 28), panel(updated, 28)
        self.assertEqual(after["data"]["spec"]["queries"], before["data"]["spec"]["queries"])
        transforms = after["data"]["spec"]["transformations"]
        self.assertFalse(any(item["group"] == "reduce" for item in transforms))
        self.assertEqual(transforms[:-1], [item for item in before["data"]["spec"]["transformations"]
                                         if item["group"] == "renameByRegex"])
        organize = transforms[-1]["spec"]["options"]
        self.assertEqual(list(organize["indexByName"]), [
            "environment", "instance", "cadvisorVersion", "cadvisorRevision", "kernelVersion", "osVersion"])
        self.assertTrue(organize["excludeByName"]["Value"])
        self.assertTrue(organize["excludeByName"]["suite_instance_raw"])
        viz = after["vizConfig"]["spec"]
        self.assertTrue(viz["options"]["showHeader"])
        self.assertEqual(viz["fieldConfig"]["defaults"]["unit"], "none")
        self.assertEqual(viz["fieldConfig"]["defaults"]["mappings"],
                         before["vizConfig"]["spec"]["fieldConfig"]["defaults"]["mappings"])
        self.assertFalse(any(item.get("matcher", {}).get("options") == "Mean"
                             for item in viz["fieldConfig"]["overrides"]))

    def test_kafka_changes_only_subquery_resolution_preserving_signed_math(self):
        for uid in migration.KAFKA_UIDS:
            with self.subTest(uid=uid):
                original = load_backup(uid)
                updated = migration.update_dashboard(original)
                for panel_id in (14, 16, 18):
                    before, after = panel(original, panel_id), panel(updated, panel_id)
                    expected = deepcopy(before)
                    target = expected["data"]["spec"]["queries"][0]["spec"]["query"]["spec"]
                    target["expr"] = target["expr"].replace("[$__rate_interval:]", "[$__rate_interval:15s]")
                    self.assertEqual(after, expected)
                    self.assertIn("deriv(", expression(updated, panel_id))
                    self.assertNotIn("rate(kafka_", expression(updated, panel_id))
                self.assertEqual(panel(original, 19), panel(updated, 19))

    def test_nacos_no_samples_branch_requires_real_sum_and_count_rates(self):
        updated = migration.update_dashboard(load_backup(migration.NACOS_UID))
        result = expression(updated, 61)
        core = result[len(migration.WRAPPED_CORE_START):result.index(migration.WRAPPED_CORE_END)]
        self.assertIn('nacos_timer_seconds_sum{name="notifyRt",', core)
        self.assertIn('nacos_timer_seconds_count{name="notifyRt",', core)
        self.assertIn(" == 0) - 1) and on() sum(rate(nacos_timer_seconds_sum", core)
        self.assertNotIn("or vector(0)", core)
        self.assertNotIn("nacos_timer_seconds_sum{cluster", core)
        self.assertIn(" < Inf > -Inf", result)
        mappings = panel(updated, 61)["vizConfig"]["spec"]["fieldConfig"]["defaults"]["mappings"]
        self.assertIn({"type": "value", "options": {"-1": {"text": "当前窗口无通知样本"}}}, mappings)
        null_mappings = [item["options"]["result"]["text"] for item in mappings
                         if item["type"] == "special" and item["options"]["match"] == "null"]
        self.assertEqual(null_mappings, ["指标缺失或采样不足"])

    def test_nacos_invalid_push_sentinel_is_not_a_negative_millisecond_value(self):
        original = load_backup(migration.NACOS_UID)
        updated = migration.update_dashboard(original)
        self.assertIn(" >= 0) or max(nacos_monitor{", expression(updated, 70))
        self.assertIn(" == -1)", expression(updated, 70))
        self.assertIn('environment=~"${environment:regex}"} >= 0', expression(updated, 50))
        for panel_id in range(55, 61):
            self.assertEqual(expression(original, panel_id), expression(updated, panel_id))
            self.assertEqual(panel(updated, panel_id)["vizConfig"]["spec"]["fieldConfig"]["defaults"]["noValue"],
                             "指标未暴露或采样不足")

    def test_nacos_aggregate_legends_use_readable_titles_without_changing_numeric_queries(self):
        original = load_backup(migration.NACOS_UID)
        updated = migration.update_dashboard(original)
        for panel_id in (51, 54, 55, 56, 57, 58, 59, 60):
            with self.subTest(panel_id=panel_id):
                before = panel(original, panel_id)
                after = panel(updated, panel_id)
                before_query = before["data"]["spec"]["queries"][0]["spec"]["query"]["spec"]
                after_query = after["data"]["spec"]["queries"][0]["spec"]["query"]["spec"]
                self.assertEqual(after_query["legendFormat"], after["title"])
                self.assertEqual(after_query, {**before_query, "legendFormat": after["title"]})
                if panel_id in (51, 54):
                    expected = deepcopy(before)
                    expected["data"]["spec"]["queries"][0]["spec"]["query"]["spec"]["legendFormat"] = after["title"]
                    self.assertEqual(after, expected)

    def test_nacos_native_time_manager_retains_ranges_and_unrelated_configuration(self):
        original = load_backup(migration.NACOS_UID)
        updated = migration.update_dashboard(original)
        before = panel(original, 94)["vizConfig"]["spec"]["options"]
        after = panel(updated, 94)["vizConfig"]["spec"]["options"]
        runtime, _, encoded = after["getOption"].rpartition("})(context, ")
        self.assertEqual(json.loads(encoded[:-2]), after["nativeIndependentTimeManager"])
        self.assertIn("(record.emptyText || text.empty)", runtime)
        self.assertIn("emptyText: specification.emptyText", runtime)
        config_before = deepcopy(before["nativeIndependentTimeManager"])
        config_after = deepcopy(after["nativeIndependentTimeManager"])
        for item in config_after["panels"]:
            if item["id"] in (50, 55, 56, 57, 58, 59, 60):
                self.assertTrue(item.pop("emptyText"))
            if item["id"] == 50:
                self.assertEqual(item["title"], "各实例平均推送耗时")
                item["title"] = next(old["title"] for old in config_before["panels"] if old["id"] == 50)
        self.assertEqual(config_after, config_before)
        expected_runtime = before["getOption"].rpartition("})(context, ")[0]
        expected_runtime = expected_runtime.replace("text.timezone : text.empty +", "text.timezone : (record.emptyText || text.empty) +")
        expected_runtime = expected_runtime.replace("String(id), editing: false", "String(id), emptyText: specification.emptyText, editing: false")
        self.assertEqual(runtime, expected_runtime)

    def test_rejects_wrong_dashboard_schema_and_unknown_query_without_mutation(self):
        for invalid in ({}, {"apiVersion": "dashboard.grafana.app/v2", "kind": "Dashboard",
                             "metadata": {"name": "other"}, "spec": {"elements": {}}}):
            with self.assertRaises(ValueError):
                migration.update_dashboard(invalid)
        invalid = load_backup(migration.NACOS_UID)
        target = panel(invalid, 61)["data"]["spec"]["queries"][0]["spec"]["query"]["spec"]
        target["expr"] = target["expr"].replace("1000 *", "2000 *", 1)
        snapshot = deepcopy(invalid)
        with self.assertRaises(ValueError):
            migration.update_dashboard(invalid)
        self.assertEqual(snapshot, invalid)

    def test_cli_preserves_input_backup_and_refuses_same_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "before.json"
            output = Path(temporary) / "after.json"
            source.write_text(json.dumps(load_backup(migration.CONTAINER_UID)), encoding="utf-8")
            before = source.read_bytes()
            self.assertEqual(migration.main([str(source), str(output)]), 0)
            self.assertEqual(before, source.read_bytes())
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")),
                             migration.update_dashboard(json.loads(before)))
            with self.assertRaises(SystemExit) as error:
                migration.main([str(source), str(source)])
            self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
