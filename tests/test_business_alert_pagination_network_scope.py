"""The live pagination panel keeps its runtime while Huawei labels change."""

from copy import deepcopy
import json
from pathlib import Path
import re

import pytest

from scripts.update_business_alert_details import update_alert_details


ROOT = Path(__file__).resolve().parents[1]


def original():
    return json.loads((ROOT / "preview/validation/huawei-intranet-alerts-before.json").read_text(encoding="utf-8"))


def options(dashboard):
    return dashboard["spec"]["elements"]["panel-2"]["spec"]["vizConfig"]["spec"]["options"]


def embedded(script):
    start = script.index("})(context, ") + len("})(context, ")
    config, length = json.JSONDecoder().raw_decode(script[start:])
    return script[:start], config, script[start + length:]


def test_only_huawei_network_text_and_specific_fallbacks_change_in_both_copies():
    before = original()
    unchanged = deepcopy(before)
    result = update_alert_details(before)
    assert before == unchanged
    old, new = options(before), options(result)
    prefix, config, suffix = embedded(new["getOption"])
    old_prefix, old_config, old_suffix = embedded(old["getOption"])
    assert prefix == old_prefix
    assert suffix == old_suffix
    assert config == new["alertDetailsPagination"]
    old_content = old_config["mappings"].pop("告警内容")
    new_content = deepcopy(config)["mappings"].pop("告警内容")
    comparable = deepcopy(config)
    comparable["mappings"].pop("告警内容")
    assert comparable == old_config
    old_patterns = {mapping["options"]["pattern"] for mapping in old_content}
    new_by_pattern = {mapping["options"]["pattern"]: mapping for mapping in new_content}
    changed = 0
    for mapping in old_content:
        pattern = mapping["options"]["pattern"]
        revised = new_by_pattern[pattern]
        if mapping != revised:
            assert pattern.startswith(("^network_in_bps", "^network_out_bps"))
            assert "huawei" in pattern and "aliyun" not in pattern
            old_text = mapping["options"]["result"]["text"]
            new_text = revised["options"]["result"]["text"]
            assert "内网" in new_text and "（估算）" in new_text
            assert old_text.split("：", 1)[1] == new_text.split("：", 1)[1]
            restored = deepcopy(revised)
            restored["options"]["result"]["text"] = old_text
            assert restored == mapping
            changed += 1
    added = [mapping for mapping in new_content if mapping["options"]["pattern"] not in old_patterns]
    assert changed == 28
    assert len(added) == 4
    for mapping in added:
        pattern = mapping["options"]["pattern"].replace("\\\\", "\\")
        assert "huawei" in pattern and "aliyun" not in pattern and "-rds-network" in pattern
        direction = "in" if pattern.startswith("^network_in_bps") else "out"
        assert re.search(pattern, f"network_{direction}_bps|huawei-other-rds-network|unknown|P0|Mbps：1")
        assert not re.search(pattern, f"network_{direction}_bps|aliyun-other-rds-network|unknown|P0|Mbps：1")
    restored_dashboard = deepcopy(result)
    restored_options = options(restored_dashboard)
    restored_options["alertDetailsPagination"] = deepcopy(old["alertDetailsPagination"])
    restored_options["getOption"] = old["getOption"]
    assert restored_dashboard == before
    assert update_alert_details(result) == result


def test_disagreeing_pagination_configurations_are_rejected():
    dashboard = original()
    options(dashboard)["alertDetailsPagination"]["pageSize"] = 11
    with pytest.raises(ValueError, match="must agree"):
        update_alert_details(dashboard)
