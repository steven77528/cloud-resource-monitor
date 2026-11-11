"""Move channel credentials into the gateway config without printing their values."""

import argparse
from copy import deepcopy
import os
from pathlib import Path
import secrets
import tempfile

import yaml


def atomic_write(path, text, mode=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        mode = path.stat().st_mode & 0o777
    handle, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def migrate(config_path, notification_path, env_path):
    if len({p.resolve() for p in (config_path, notification_path, env_path)}) != 3:
        raise ValueError("Migration paths must be different")
    with config_path.open(encoding="utf-8", newline="") as stream:
        source = stream.read()
    cfg = yaml.safe_load(source)
    channels = cfg.get("notifications", {})
    if "gateway" in channels:
        if not notification_path.exists():
            raise ValueError("Gateway is configured but its channel configuration is missing")
        return False
    if notification_path.exists():
        raise ValueError("Channel configuration already exists; refusing to overwrite it")
    root = yaml.compose(source)
    if not isinstance(root, yaml.MappingNode):
        raise ValueError("Configuration must be a YAML mapping")
    sections = [(key, value) for key, value in root.value if key.value == "notifications"]
    if len(sections) != 1 or not isinstance(channels, dict):
        raise ValueError("Exactly one notifications mapping is required")

    moved = deepcopy(channels)
    if moved.get("sms", {}).get("provider") != "http":
        moved["sms"] = {
            "provider": "http", "url": "", "encoding": "json", "headers": {},
            "content_field": "content", "extra_body": {}, "success_json": {"code": 0},
            "templates": {
                "firing": "[{severity}] {summary} | {account} | {resource_name} | {metric}={value}{unit}",
                "recovery": "[RECOVERED] {summary} | {account} | {resource_name} | {metric}={value}{unit}",
            },
            "max_content_length": 1000, "timeout_seconds": 15,
        }
    moved["sms"]["enabled"] = False
    for channel in ("dingtalk", "email"):
        moved.setdefault(channel, {}).setdefault("enabled", True)
    gateway = {"version": 1,
               "api": {"host": "0.0.0.0", "port": 8081, "token": "${NOTIFICATION_GATEWAY_TOKEN}"},
               "routes": {"P0": "sms", "P1": "dingtalk", "P2": "email"},
               "notifications": moved}
    notification_text = (
        "# Notification channels are reloaded on every request. Keep this file private.\n"
        "# SMS is disabled until its provider API is configured and tested.\n"
        + yaml.safe_dump(gateway, allow_unicode=True, sort_keys=False, width=110)
    )
    replacement = yaml.safe_dump({"notifications": {"gateway": {
        "url": "http://notification-gateway:8081", "token": "${NOTIFICATION_GATEWAY_TOKEN}",
        "timeout_seconds": 20,
    }}}, sort_keys=False)
    newline = "\r\n" if "\r\n" in source else "\n"
    replacement = replacement.replace("\n", newline)
    key, value = sections[0]

    def last_value_end(node):
        if isinstance(node, yaml.MappingNode):
            return max((last_value_end(child) for pair in node.value for child in pair),
                       default=node.end_mark.index)
        if isinstance(node, yaml.SequenceNode):
            return max((last_value_end(child) for child in node.value), default=node.end_mark.index)
        return node.end_mark.index

    # Mapping end marks include comments preceding the next root key; preserve those comments.
    end = source.find("\n", last_value_end(value))
    end = len(source) if end < 0 else end + 1
    updated = source[:key.start_mark.index] + replacement + source[end:]
    reloaded = yaml.safe_load(updated)
    if {k: v for k, v in reloaded.items() if k != "notifications"} != {
        k: v for k, v in cfg.items() if k != "notifications"
    }:
        raise ValueError("Migration changed unrelated configuration")

    env_text = ""
    if env_path.exists():
        with env_path.open(encoding="utf-8", newline="") as stream:
            env_text = stream.read()
    if not any(line.strip().startswith("NOTIFICATION_GATEWAY_TOKEN=") for line in env_text.splitlines()):
        env_newline = "\r\n" if "\r\n" in env_text else "\n"
        if env_text and not env_text.endswith("\n"):
            env_text += env_newline
        env_text += env_newline + "# Internal notification gateway authentication" + env_newline
        env_text += "NOTIFICATION_GATEWAY_TOKEN=" + secrets.token_urlsafe(32) + env_newline
        atomic_write(env_path, env_text)
    atomic_write(notification_path, notification_text, mode=config_path.stat().st_mode & 0o777)
    atomic_write(config_path, updated)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/config.yaml"))
    parser.add_argument("--notifications-config", type=Path,
                        default=Path("config/notifications/notifications.yaml"))
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args()
    try:
        changed = migrate(args.config, args.notifications_config, args.env_file)
    except (OSError, ValueError, TypeError, AttributeError, yaml.YAMLError):
        parser.exit(2, "Migration failed; check input paths and YAML. Existing channel files are not overwritten.\n")
    print("Notification configuration migrated; SMS disabled. No messages sent." if changed
          else "Notification configuration is already migrated; no changes made.")


if __name__ == "__main__":
    main()
