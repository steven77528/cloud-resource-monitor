[CmdletBinding()]
param(
    [string] $HostName = '192.168.120.28',
    [string] $UserName = 'root',
    [string] $SshRunner = 'C:/Users/77528/.codex/skills/ssh-password-dialog/scripts/invoke-ssh.ps1'
)

$ErrorActionPreference = 'Stop'
$remoteScript = @'
set -eu
cd /data/monitor/cloud-resource-monitor
umask 077
python3 - <<'PY'
import copy
import datetime
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time

import yaml

TARGETS = {
    'config/config.yaml': {'aliyun-slb-bandwidth'},
    'config/rds-performance.yaml': {
        'aliyun-mysql-civil-performance', 'aliyun-mysql-iot-performance',
        'aliyun-mysql-cloudmonitor', 'aliyun-yuanzhu-mysql-cloudmonitor',
        'aliyun-yuanzhu-mysql-changsha-readonly-performance',
        'aliyun-yuanzhu-mysql-national-readonly-performance',
        'aliyun-yuanzhu-mysql-modian-performance', 'aliyun-yuanzhu-mysql-wuhan-performance',
    },
    'config/network-traffic.yaml': {'huawei-ecs-network'},
}
root = Path.cwd()
stage = 'preflight'
changed = False
restarted = False
before = {}
backup = None

def emit(kind, value):
    print(kind + ' ' + json.dumps(value, ensure_ascii=True), flush=True)

def run(args, timeout=120):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError('Command failed')
    return result.stdout

def inspect(name):
    return json.loads(run(['docker', 'inspect', name]))[0]

def file_hashes():
    paths = [root / '.env', *sorted((root / 'config').rglob('*'))]
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths if path.is_file()}

def candidate(raw, target_ids):
    source = raw.decode('utf-8')
    data = yaml.safe_load(source)
    expected = copy.deepcopy(data)
    jobs = data.get('jobs', [])
    selected = [job for job in jobs if job.get('id') in target_ids]
    if len(selected) != len(target_ids) or {job['id'] for job in selected} != target_ids:
        raise ValueError('Target jobs must occur exactly once')
    for job in selected:
        if not job.get('enabled', True) or job.get('collection_error_samples', 1) not in (1, 2):
            raise ValueError('Target job settings changed since inspection')
        if job['interval_seconds'] != (300 if job['id'] == 'huawei-ecs-network' else 60):
            raise ValueError('Target interval changed since inspection')
    for job in expected['jobs']:
        if job.get('id') in target_ids:
            job['collection_error_samples'] = 2
    document = yaml.compose(source)
    job_nodes = next(value for key, value in document.value if key.value == 'jobs')
    edits = []
    newline = '\r\n' if '\r\n' in source else '\n'
    for node in job_nodes.value:
        fields = {key.value: (key, value) for key, value in node.value}
        if fields['id'][1].value not in target_ids:
            continue
        if node.flow_style:
            raise ValueError('Target jobs must use block mappings')
        if 'collection_error_samples' in fields:
            value = fields['collection_error_samples'][1]
            edits.append((value.start_mark.index, value.end_mark.index, '2'))
        else:
            # YAML source marks preserve comments, credentials and original formatting.
            key, value = fields['id']
            end = source.find('\n', value.end_mark.index)
            if end < 0:
                raise ValueError('Target ID must have a following line')
            edits.append((end + 1, end + 1, ' ' * key.start_mark.column
                          + 'collection_error_samples: 2' + newline))
    for start, end, value in sorted(edits, reverse=True):
        source = source[:start] + value + source[end:]
    if yaml.safe_load(source) != expected:
        raise ValueError('Unexpected configuration difference')
    return source.encode('utf-8')

def check_config(name):
    output = run(['docker', 'exec', name, 'python', '-m', 'cloud_monitor',
                  '--config', '/app/config/config.yaml', 'check', '--live'])
    result = json.loads(output)
    if result.get('valid') is not True:
        raise ValueError('Offline configuration check failed')
    emit('OFFLINE_CONFIG', {key: result[key] for key in ('valid', 'accounts', 'jobs', 'rules')})

try:
    ids = {service: run(['docker', 'compose', 'ps', '-q', service]).strip()
           for service in ('cloud-monitor', 'notification-gateway', 'mysql')}
    if not all(ids.values()):
        raise ValueError('Required running service missing')
    states = {service: inspect(cid) for service, cid in ids.items()}
    if not all(info['State']['Running'] for info in states.values()):
        raise ValueError('Required service not running')
    hashes = file_hashes()
    before = {name: (root / name).read_bytes() for name in TARGETS}
    main = yaml.safe_load(before['config/config.yaml'])
    if main.get('service', {}).get('recovery_samples', 2) != 2:
        raise ValueError('Recovery confirmation changed since inspection')
    if main.get('service', {}).get('notify_recovery', True) is not True:
        raise ValueError('Recovery notifications changed since inspection')
    network = yaml.safe_load(before['config/network-traffic.yaml'])
    ecs = next(job for job in network['jobs'] if job['id'] == 'huawei-ecs-network')
    if ecs.get('options', {}).get('max_age_seconds', 900) != 900:
        raise ValueError('Source freshness changed since inspection')
    after = {name: candidate(raw, TARGETS[name]) for name, raw in before.items()}
    check_config(ids['cloud-monitor'])
    run(['docker', 'compose', 'config', '--quiet'])
    stamp = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime('%Y%m%d-%H%M%S')
    backup = root / '.deploy-backups' / ('collection-confirmation-' + stamp)
    backup.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name in TARGETS:
        destination = backup / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / name, destination)
    (backup / 'config-hashes.json').write_text(json.dumps(hashes), encoding='utf-8')
    emit('BACKUP', {'path': str(backup.relative_to(root)), 'target_jobs': sum(map(len, TARGETS.values()))})
    stage = 'write_and_validate'
    changed = True
    for name, raw in after.items():
        # Keep the inode so existing read-only bind mounts see the updated file.
        (root / name).write_bytes(raw)
    check_config(ids['cloud-monitor'])
    current_hashes = file_hashes()
    if current_hashes.keys() != hashes.keys():
        raise ValueError('Configuration file inventory changed')
    if any(current_hashes[name] != digest for name, digest in hashes.items() if name not in TARGETS):
        raise ValueError('Unrelated configuration changed')
    if any((root / name).read_bytes() != raw for name, raw in after.items()):
        raise ValueError('Target configuration changed during deployment')
    emit('PRESERVED', {'credentials_and_other_settings': True, 'recovery_samples': 2,
                       'huawei_source_max_age_seconds': 900})
    stage = 'restart_collector'
    restarted = True
    emit('ACTIVATING', {'service': 'cloud-monitor', 'action': 'restart_existing_container'})
    run(['docker', 'compose', 'restart', '-t', '120', 'cloud-monitor'], timeout=180)
    stage = 'verify_running_service'
    for attempt in range(40):
        current = inspect(ids['cloud-monitor'])
        if (current['State']['Running'] and current['State'].get('Health', {}).get('Status') == 'healthy'
                and current['State']['StartedAt'] != states['cloud-monitor']['State']['StartedAt']):
            break
        if attempt in (0, 10, 20, 30):
            emit('WAITING', {'service': 'cloud-monitor', 'health': current['State'].get('Health', {}).get('Status')})
        time.sleep(3)
    else:
        raise RuntimeError('Collector did not become healthy after restart')
    if current['Image'] != states['cloud-monitor']['Image']:
        raise ValueError('Collector image changed unexpectedly')
    for service in ('notification-gateway', 'mysql'):
        if run(['docker', 'compose', 'ps', '-q', service]).strip() != ids[service]:
            raise ValueError('Unrelated container changed')
        current_other = inspect(ids[service])
        if current_other['State']['StartedAt'] != states[service]['State']['StartedAt']:
            raise ValueError('Unrelated container restarted')
    check_config(ids['cloud-monitor'])
    verified = []
    for name, target_ids in TARGETS.items():
        if (root / name).read_bytes() != after[name]:
            raise ValueError('Configuration changed after activation')
        for job in yaml.safe_load(after[name])['jobs']:
            if job['id'] in target_ids:
                verified.append({'job': job['id'], 'collection_error_samples': job['collection_error_samples'],
                                 'interval_seconds': job['interval_seconds']})
    (backup / 'activated').write_text('verified\n', encoding='utf-8')
    emit('DEPLOYED', {'at': datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat(timespec='seconds'),
        'jobs': verified, 'recovery_samples': 2, 'health': current['State']['Health']['Status'],
        'image_preserved': True, 'gateway_preserved': True, 'mysql_preserved': True,
        'backup': str(backup.relative_to(root))})
except Exception as exc:
    emit('DEPLOYMENT_ERROR', {'stage': stage, 'type': type(exc).__name__})
    if changed:
        try:
            for name, raw in before.items():
                (root / name).write_bytes(raw)
            if restarted:
                run(['docker', 'compose', 'restart', '-t', '120', 'cloud-monitor'], timeout=180)
            emit('ROLLED_BACK', {'configuration_restored': True, 'collector_restart_requested': restarted})
        except Exception as rollback_error:
            emit('ROLLBACK_ERROR', {'type': type(rollback_error).__name__})
    raise SystemExit(1)
PY
'@
& $SshRunner -HostName $HostName -UserName $UserName -Command $remoteScript -TimeoutSeconds 600
exit $LASTEXITCODE
