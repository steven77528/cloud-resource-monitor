"""Keep runtime query copies and independent time settings consistent."""

from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from update_huawei_runtime_intranet import PANELS, update_runtime  # noqa: E402


@pytest.fixture
def dashboard():
    return json.loads((ROOT / "preview/validation/huawei-intranet-runtime-before.json").read_text(encoding="utf-8"))


def test_replaces_only_four_panels_and_is_idempotent(dashboard):
    original = deepcopy(dashboard)
    updated = update_runtime(dashboard)
    assert dashboard == original
    assert update_runtime(updated) == updated
    restored = deepcopy(updated)
    for name, (_, _, _, _, title) in PANELS.items():
        panel = updated["spec"]["elements"][name]["spec"]
        options = panel["vizConfig"]["spec"]["options"]
        old_options = original["spec"]["elements"][name]["spec"]["vizConfig"]["spec"]["options"]
        config = options["independentTime"]
        embedded = json.loads(options["getOption"].rpartition("})(context, ")[2][:-2])
        assert config == embedded
        assert panel["title"] == config["title"] == title
        assert config["targets"][0]["expr"] == panel["data"]["spec"]["queries"][0]["spec"]["query"]["spec"]["expr"]
        assert 'scope="all"' not in config["targets"][0]["expr"]
        assert 'scope="intranet_estimated"' in config["targets"][0]["expr"]
        assert config["unit"] == old_options["independentTime"]["unit"]
        assert config["thresholds"] == old_options["independentTime"]["thresholds"]
        assert options["getOption"].rpartition("})(context, ")[0] == old_options["getOption"].rpartition("})(context, ")[0]
        restored["spec"]["elements"][name] = original["spec"]["elements"][name]
    assert restored == original


def test_refuses_unexpected_dashboard_or_inconsistent_query(dashboard):
    dashboard["metadata"]["name"] = "another-dashboard"
    with pytest.raises(ValueError):
        update_runtime(dashboard)
    dashboard["metadata"]["name"] = "cloud-business-runtime-v1"
    dashboard["spec"]["elements"]["panel-8"]["spec"]["data"]["spec"]["queries"][0]["spec"]["query"]["spec"]["expr"] = "up"
    with pytest.raises(ValueError, match="differs"):
        update_runtime(dashboard)
