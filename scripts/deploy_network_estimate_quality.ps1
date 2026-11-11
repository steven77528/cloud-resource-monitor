[CmdletBinding()]
param(
    [string] $HostName = '192.168.120.28',
    [string] $UserName = 'root',
    [ValidatePattern('^$|^\.deploy-backups/network-quality-[0-9]{8}-[0-9]{6}$')]
    [string] $ResumeBackup = '',
    [string] $SshRunner = 'C:/Users/77528/.codex/skills/ssh-password-dialog/scripts/invoke-ssh.ps1'
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$deployPaths = @(
    'cloud_monitor/providers/huawei_network_scope.py',
    'cloud_monitor/providers/network_traffic.py',
    'cloud_monitor/resource_selection.py', 'cloud_monitor/store.py',
    'cloud_monitor/prometheus.py', 'cloud_monitor/diagnostics.py',
    'cloud_monitor/service.py', 'cloud_monitor/api.py',
    'tests/test_huawei_network_scope.py', 'tests/test_network_traffic.py',
    'tests/test_network_estimate_quality.py', 'tests/test_diagnostics.py',
    'tests/test_prometheus.py', 'tests/test_grafana_dashboard.py',
    'scripts/build_grafana_dashboard.py', 'scripts/deploy_network_estimate_quality.ps1',
    'grafana/cloud-resource-monitor.json', 'README.md',
    'docs/network-traffic.md', 'docs/troubleshooting-collection.md',
    'docs/huawei-network-scope-audit.md', 'docs/huawei-network-negative-difference-research.md'
)
$deployFiles = @{}
foreach ($deployPath in $deployPaths) {
    $deployFiles[$deployPath] = [Convert]::ToBase64String(
        [IO.File]::ReadAllBytes((Join-Path $projectRoot $deployPath)))
}
$deployManifest = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(
    (ConvertTo-Json -InputObject $deployFiles -Compress)))
$remoteScript = @'
set -eu
cd /data/monitor/cloud-resource-monitor
umask 077
monitor_id=$(docker compose ps -q cloud-monitor)
gateway_id=$(docker compose ps -q notification-gateway)
mysql_id=$(docker compose ps -q mysql)
test -n "$monitor_id"
test -n "$gateway_id"
test -n "$mysql_id"
old_image=$(docker inspect --format '{{.Image}}' "$monitor_id")
stamp=$(date +%Y%m%d-%H%M%S)
backup='__RESUME_BACKUP__'
if test -z "$backup"; then
backup=".deploy-backups/network-quality-$stamp"
mkdir -p "$backup"
docker image tag "$old_image" "cloud-resource-monitor:before-network-quality-$stamp"
fi
export MONITOR_QUALITY_BACKUP="$backup"
if test -z '__RESUME_BACKUP__'; then
python3 - <<'PY'
import base64, hashlib, json, os, shutil, subprocess
from pathlib import Path
root = Path.cwd()
backup = root / os.environ['MONITOR_QUALITY_BACKUP']
files = json.loads(base64.b64decode('__MANIFEST__'))
config_files = [root / '.env', *sorted((root / 'config').rglob('*'))]
fingerprints = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in config_files if p.is_file()}
(backup / 'config-hashes.json').write_text(json.dumps(fingerprints), encoding='utf-8')
for name, encoded in files.items():
    destination = root / name
    if name.startswith('cloud_monitor/') and destination.exists():
        running = subprocess.run(['docker', 'compose', 'exec', '-T', 'cloud-monitor',
                                  'sha256sum', '/app/' + name], check=True, capture_output=True, text=True)
        assert running.stdout.split()[0] == hashlib.sha256(destination.read_bytes()).hexdigest(), name
    if destination.exists():
        previous = backup / name
        previous.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(destination, previous)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(base64.b64decode(encoded))
    destination.chmod(0o644)
print(json.dumps({'uploaded_files': len(files), 'backup': str(backup.relative_to(root))}), flush=True)
PY
fi
test -f "$backup/config-hashes.json"
base_tag="cloud-resource-monitor:before-network-quality-${backup#.deploy-backups/network-quality-}"
test "$old_image" = "$(docker image inspect --format '{{.Id}}' "$base_tag")"
python3 - <<'PY'
import hashlib, subprocess
from pathlib import Path
running = subprocess.run(['docker', 'compose', 'exec', '-T', 'cloud-monitor', 'sha256sum',
                          '/app/pyproject.toml'], check=True, capture_output=True, text=True)
assert running.stdout.split()[0] == hashlib.sha256(Path('pyproject.toml').read_bytes()).hexdigest(), \
    'Dependency metadata changed; use the normal Dockerfile build'
print('Dependency metadata unchanged; retaining the existing installed dependencies', flush=True)
PY
docker compose config --quiet
docker build --pull=false --build-arg MONITOR_BASE="$base_tag" -t cloud-resource-monitor:1.0.0 -f - . <<'DOCKER'
ARG MONITOR_BASE=cloud-resource-monitor:1.0.0
FROM ${MONITOR_BASE}
COPY --chown=10001:10001 cloud_monitor/providers/huawei_network_scope.py /app/cloud_monitor/providers/huawei_network_scope.py
COPY --chown=10001:10001 cloud_monitor/providers/network_traffic.py /app/cloud_monitor/providers/network_traffic.py
COPY --chown=10001:10001 cloud_monitor/resource_selection.py /app/cloud_monitor/resource_selection.py
COPY --chown=10001:10001 cloud_monitor/store.py /app/cloud_monitor/store.py
COPY --chown=10001:10001 cloud_monitor/prometheus.py /app/cloud_monitor/prometheus.py
COPY --chown=10001:10001 cloud_monitor/diagnostics.py /app/cloud_monitor/diagnostics.py
COPY --chown=10001:10001 cloud_monitor/service.py /app/cloud_monitor/service.py
COPY --chown=10001:10001 cloud_monitor/api.py /app/cloud_monitor/api.py
DOCKER
docker compose run --rm -T --interactive=false --no-deps cloud-monitor --config /app/config/config.yaml check --live
docker compose run --rm -T --interactive=false --no-deps cloud-monitor --config /app/config/config.yaml diagnose --job huawei-ecs-network --job huawei-rds-network
activated_at=$(date +%s)
docker compose up -d --no-deps --no-build --pull never --force-recreate cloud-monitor
test "$gateway_id" = "$(docker compose ps -q notification-gateway)"
test "$mysql_id" = "$(docker compose ps -q mysql)"
export MONITOR_QUALITY_ACTIVATED_AT="$activated_at"
docker compose exec -T -e MONITOR_QUALITY_ACTIVATED_AT cloud-monitor python - <<'PY'
import json, os, time
from collections import Counter
from urllib.request import Request, ProxyHandler, build_opener
from prometheus_client.parser import text_string_to_metric_families
from cloud_monitor.config import load_config
cfg = load_config('/app/config/config.yaml')
targets = {'huawei-ecs-network', 'huawei-rds-network'}
token = cfg.get('api', {}).get('metrics', {}).get('token')
headers = {'Authorization': 'Bearer ' + token} if token else {}
opener = build_opener(ProxyHandler({}))
started = int(os.environ['MONITOR_QUALITY_ACTIVATED_AT'])
for attempt in range(16):
    try:
        with opener.open(Request('http://127.0.0.1:8080/metrics', headers=headers), timeout=5) as response:
            samples = [s for f in text_string_to_metric_families(response.read().decode()) for s in f.samples]
        updated = {s.labels['job_id'] for s in samples if s.name == 'cloud_monitor_job_last_run_timestamp_seconds'
                   and s.labels.get('job_id') in targets and s.value >= started}
        if updated == targets:
            break
    except Exception:
        pass
    time.sleep(3)
else:
    raise SystemExit('Updated network jobs not observed; inspect cloud-monitor status')
quality = [s for s in samples if s.name == 'cloud_monitor_network_estimate_available'
           and s.labels.get('job_id') in targets]
values = [s for s in samples if s.name == 'cloud_monitor_resource_value'
          and s.labels.get('job_id') in targets and s.labels.get('metric') in {'network_in_bps', 'network_out_bps'}]
unavailable = {(s.labels['job_id'], s.labels['region'], s.labels['resource_id']) for s in quality if s.value == 0}
assert all((s.labels['job_id'], s.labels['region'], s.labels['resource_id']) not in unavailable for s in values)
assert all(s.value >= 0 for s in values)
assert quality, 'No persisted network quality metrics'
with opener.open('http://127.0.0.1:8080/healthz', timeout=5) as response:
    assert response.status == 200
counts = Counter((s.labels['job_id'], int(s.value)) for s in quality)
print(json.dumps({'verification': 'running_service', 'jobs': [
    {'job': s.labels['job_id'], 'status': s.labels['status']} for s in samples
    if s.name == 'cloud_monitor_job_status' and s.labels.get('job_id') in targets and s.value == 1],
    'estimate_quality': [{'job': job, 'available': available, 'resources': count}
                         for (job, available), count in sorted(counts.items())],
    'exported_network_series': len(values), 'invalid_resource_series_excluded': True}), flush=True)
PY
python3 - <<'PY'
import hashlib, json, os
from pathlib import Path
root = Path.cwd()
backup = root / os.environ['MONITOR_QUALITY_BACKUP']
before = json.loads((backup / 'config-hashes.json').read_text())
assert all((root / name).is_file() and hashlib.sha256((root / name).read_bytes()).hexdigest() == digest
           for name, digest in before.items())
(backup / 'activated').write_text('verified\n')
print(json.dumps({'configuration_preserved': True, 'gateway_container_preserved': True,
                  'mysql_container_preserved': True, 'backup': str(backup.relative_to(root))}), flush=True)
PY
docker compose ps cloud-monitor notification-gateway mysql
'@
$remoteScript = $remoteScript.Replace('__MANIFEST__', $deployManifest)
$remoteScript = $remoteScript.Replace('__RESUME_BACKUP__', $ResumeBackup)
& $SshRunner -HostName $HostName -UserName $UserName -Command $remoteScript -TimeoutSeconds 900
exit $LASTEXITCODE
