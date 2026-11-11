from pathlib import Path, PurePosixPath

import yaml


ROOT = Path(__file__).resolve().parents[1]


def _bind_mounts(service):
    mounts = []
    for volume in service.get("volumes", []):
        if isinstance(volume, dict):
            if volume.get("type") != "bind":
                continue
            source, target = volume["source"], volume["target"]
            read_only = volume.get("read_only", False)
        else:
            parts = volume.rsplit(":", 2)
            source, target = parts[:2]
            read_only = len(parts) == 3 and "ro" in parts[2].split(",")
        host_path = (ROOT / source).resolve()
        mounts.append((PurePosixPath(target), host_path, read_only))
    return sorted(mounts, key=lambda mount: len(mount[0].parts), reverse=True)


def _mounted_file(container_path, mounts):
    for target, source, read_only in mounts:
        if container_path.is_relative_to(target):
            host_path = source.joinpath(*container_path.relative_to(target).parts)
            assert read_only, f"Configuration mount must be read-only: {target}"
            assert host_path.is_file(), f"Missing configuration source: {host_path.relative_to(ROOT)}"
            return host_path
    raise AssertionError(f"Configuration is not bind-mounted: {container_path}")


def test_compose_mounts_all_monitoring_configuration_files_read_only():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    service = compose["services"]["cloud-monitor"]
    command = service["command"]
    config_path = PurePosixPath(command[command.index("--config") + 1])
    mounts = _bind_mounts(service)
    main_path = _mounted_file(config_path, mounts)
    monitoring_files = yaml.safe_load(main_path.read_text(encoding="utf-8")).get("monitoring_files", [])

    assert isinstance(monitoring_files, list)
    for name in monitoring_files:
        container_path = config_path.parent / name
        host_path = _mounted_file(container_path, mounts)
        expected_path = (main_path.parent / name).resolve()
        assert expected_path.is_relative_to(main_path.parent)
        assert host_path.resolve() == expected_path, f"Incorrect monitoring file mount: {name}"
