import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest
import yaml


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "migrate_notifications.py"
SPEC = importlib.util.spec_from_file_location("migration_script", SCRIPT)
MIGRATION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MIGRATION)


@pytest.fixture
def paths(tmp_path):
    config_path = tmp_path / "config" / "config.yaml"
    notification_path = tmp_path / "config" / "notifications" / "notifications.yaml"
    env_path = tmp_path / ".env"
    config_path.parent.mkdir()
    return config_path, notification_path, env_path


def source_config(newline="\n"):
    return newline.join([
        "# Original main configuration", "version: 1", "accounts:",
        "  - id: test-account", '    access_key_secret: "synthetic-account-secret"',
        "# Notification section", "notifications:", "  sms:", "    provider: aliyun",
        '    access_key_secret: "synthetic-sms-secret"', "  dingtalk:",
        '    webhook: "https://example.invalid/robot?access_token=synthetic-dingtalk-secret"',
        '    secret: "synthetic-signing-secret"', '    keyword: "alerts"',
        "  email:", '    host: "smtp.example.invalid"', '    password: "synthetic-smtp-secret"',
        '    from_address: "sender@example.invalid"', '    to: ["ops@example.invalid"]',
        "    port: 465", "# API settings: preserve this comment", "api:",
        '  admin_token: "synthetic-admin-secret"  # preserve inline comment',
        "jobs: []", "# End of main configuration", "",
    ])


def write_source(paths, newline="\n"):
    config_path, _, env_path = paths
    source = source_config(newline)
    config_path.write_bytes(source.encode("utf-8"))
    env_path.write_bytes(("# Keep database settings" + newline + "MYSQL_PASSWORD=synthetic-db-secret"
                          + newline + "OTHER_VALUE=unchanged" + newline).encode("utf-8"))
    return source


def test_migration_moves_credentials_and_preserves_other_main_values(paths):
    source = write_source(paths)
    config_path, notification_path, env_path = paths
    original = yaml.safe_load(source)
    assert MIGRATION.migrate(*paths) is True
    actual = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    gateway = yaml.safe_load(notification_path.read_text(encoding="utf-8"))
    assert {key: value for key, value in actual.items() if key != "notifications"} == {
        key: value for key, value in original.items() if key != "notifications"
    }
    assert set(actual["notifications"]) == {"gateway"}
    assert actual["notifications"]["gateway"]["url"] == "http://notification-gateway:8081"
    assert actual["notifications"]["gateway"]["token"] == "${NOTIFICATION_GATEWAY_TOKEN}"
    for channel in ("dingtalk", "email"):
        assert gateway["notifications"][channel] == {**original["notifications"][channel], "enabled": True}
    assert gateway["notifications"]["sms"]["provider"] == "http"
    assert gateway["notifications"]["sms"]["enabled"] is False
    assert gateway["notifications"]["sms"]["url"] == ""
    assert gateway["routes"] == {"P0": "sms", "P1": "dingtalk", "P2": "email"}
    assert gateway["api"]["token"] == "${NOTIFICATION_GATEWAY_TOKEN}"
    tokens = [line.split("=", 1)[1] for line in env_path.read_text(encoding="utf-8").splitlines()
              if line.startswith("NOTIFICATION_GATEWAY_TOKEN=")]
    assert len(tokens) == 1 and len(tokens[0]) >= 24
    assert "synthetic-dingtalk-secret" not in config_path.read_text(encoding="utf-8")
    assert "synthetic-smtp-secret" not in config_path.read_text(encoding="utf-8")


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_migration_preserves_unrelated_source_text_comments_and_newlines(paths, newline):
    source = write_source(paths, newline)
    config_path, _, _ = paths
    MIGRATION.migrate(*paths)
    actual = config_path.read_bytes().decode("utf-8")
    prefix = source[:source.index("notifications:")]
    suffix = source[source.index("# API settings:"):]
    assert actual.startswith(prefix)
    assert actual.endswith(suffix)
    if newline == "\r\n":
        assert "\n" not in actual.replace("\r\n", "")


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_migration_only_appends_to_existing_environment_file(paths, newline):
    write_source(paths, newline)
    _, _, env_path = paths
    before = env_path.read_bytes()
    MIGRATION.migrate(*paths)
    after = env_path.read_bytes()
    assert after.startswith(before)
    assert after.count(b"MYSQL_PASSWORD=") == 1
    assert after.count(b"OTHER_VALUE=") == 1


def test_repeated_migration_is_exact_noop_and_existing_token_is_not_overwritten(paths):
    write_source(paths)
    env_path = paths[2]
    original_env = b"MYSQL_PASSWORD=synthetic-db-secret\r\nNOTIFICATION_GATEWAY_TOKEN=existing-private-token-123456\r\n"
    env_path.write_bytes(original_env)
    assert MIGRATION.migrate(*paths) is True
    assert env_path.read_bytes() == original_env
    first = [path.read_bytes() for path in paths]
    assert MIGRATION.migrate(*paths) is False
    assert [path.read_bytes() for path in paths] == first


def test_existing_gateway_config_is_not_overwritten_or_partially_migrated(paths):
    write_source(paths)
    config_path, notification_path, env_path = paths
    notification_path.parent.mkdir()
    notification_path.write_bytes(b"existing independent gateway config")
    original = [path.read_bytes() for path in paths]
    with pytest.raises(ValueError, match="refusing to overwrite"):
        MIGRATION.migrate(*paths)
    assert [path.read_bytes() for path in paths] == original


def test_existing_http_settings_and_disabled_other_channel_are_preserved(paths):
    config_path, notification_path, _ = paths
    sms = {"provider": "http", "enabled": True, "url": "https://sms.example.invalid/send",
           "encoding": "form", "extra_body": {"apikey": "synthetic-api-key", "mobile": "13800000000"},
           "content_field": "text", "success_json": {"code": 0}}
    config_path.write_text(yaml.safe_dump({"version": 1, "notifications": {
        "sms": sms, "dingtalk": {"enabled": False}, "email": {"enabled": False},
    }}), encoding="utf-8")
    assert MIGRATION.migrate(*paths) is True
    actual = yaml.safe_load(notification_path.read_text(encoding="utf-8"))
    assert actual["notifications"]["sms"] == {**sms, "enabled": False}
    assert actual["notifications"]["dingtalk"]["enabled"] is False
    assert actual["notifications"]["email"]["enabled"] is False


def test_cli_defaults_work_in_project_directory_without_printing_secrets(paths, tmp_path):
    write_source(paths)
    result = subprocess.run([sys.executable, str(SCRIPT)], cwd=tmp_path,
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0
    assert paths[1].exists()
    assert "No messages sent" in result.stdout
    assert "synthetic" not in result.stdout + result.stderr


def test_invalid_yaml_cli_reports_fixed_error_without_credentials(paths, tmp_path):
    paths[0].write_text('notifications: ["synthetic-broken-secret"\n', encoding="utf-8")
    result = subprocess.run([sys.executable, str(SCRIPT)], cwd=tmp_path,
                            capture_output=True, text=True, check=False)
    assert result.returncode == 2
    assert "synthetic-broken-secret" not in result.stdout + result.stderr
    assert not paths[1].exists()
    assert not paths[2].exists()


def test_path_aliases_are_rejected_before_writing(paths):
    write_source(paths)
    config_path, _, env_path = paths
    original = config_path.read_bytes()
    with pytest.raises(ValueError, match="paths must be different"):
        MIGRATION.migrate(config_path, config_path, env_path)
    assert config_path.read_bytes() == original
