[CmdletBinding()]
param(
    [ValidateSet('Stage', 'Activate', 'Verify')]
    [string] $Phase = 'Stage',
    [string] $HostName = '192.168.120.28',
    [string] $UserName = 'root',
    [string] $SshRunner = 'C:/Users/77528/.codex/skills/ssh-password-dialog/scripts/invoke-ssh.ps1'
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if ($Phase -eq 'Stage') {
    $deployPaths = @(
        'cloud_monitor/providers/huawei_network_scope.py',
        'cloud_monitor/providers/network_traffic.py',
        'cloud_monitor/resource_selection.py', 'cloud_monitor/store.py',
        'cloud_monitor/prometheus.py', 'cloud_monitor/diagnostics.py',
        'cloud_monitor/notification_rendering.py',
        'scripts/probe_aliyun_rds_eip.py', 'scripts/estimate_api_usage.py',
        'scripts/update_huawei_runtime_intranet.py', 'scripts/append_business_network_panels.py',
        'scripts/update_business_alert_details.py',
        'docs/policies/aliyun-rds-eip-discovery.json',
        'grafana/cloud-business-runtime-v1.json'
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
test -n "$monitor_id"
test -n "$gateway_id"
old_image=$(docker inspect --format '{{.Image}}' "$monitor_id")
stamp=$(date +%Y%m%d-%H%M%S)
backup=".deploy-backups/huawei-intranet-$stamp"
if test -f .deploy-backups/huawei-intranet-latest; then
    previous_backup=$(cat .deploy-backups/huawei-intranet-latest)
    case "$previous_backup" in .deploy-backups/huawei-intranet-*) ;; *) exit 1 ;; esac
    if ! test -f "$previous_backup/activated"; then backup="$previous_backup"; fi
fi
mkdir -p "$backup"
if ! test -f "$backup/containers-before.txt"; then
    docker image tag "$old_image" "cloud-resource-monitor:before-huawei-intranet-$stamp"
    docker inspect --format '{{.Id}} {{.Image}}' "$monitor_id" "$gateway_id" > "$backup/containers-before.txt"
    docker compose ps -q mysql > "$backup/mysql-before.txt"
fi
printf '%s\n' "$backup" > .deploy-backups/huawei-intranet-latest
export HUAWEI_SCOPE_BACKUP="$backup"
docker run --rm -i --user 0 --entrypoint python -v "$PWD:/workspace" -e HUAWEI_SCOPE_BACKUP "$old_image" - <<'PY'
import base64, json, os, shutil
from pathlib import Path
import yaml
root = Path('/workspace')
backup = root / os.environ['HUAWEI_SCOPE_BACKUP']
files = json.loads(base64.b64decode('__MANIFEST__'))
for name, encoded in files.items():
    destination = root / name
    if destination.exists():
        previous = backup / name
        previous.parent.mkdir(parents=True, exist_ok=True)
        if not previous.exists():
            shutil.copy2(destination, previous)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(base64.b64decode(encoded))
    destination.chmod(0o644)
config_path = root / 'config/network-traffic.yaml'
if not (backup / 'network-traffic.yaml').exists():
    shutil.copy2(config_path, backup / 'network-traffic.yaml')
before = yaml.safe_load(config_path.read_text())
after = json.loads(json.dumps(before))
for job in after['jobs']:
    if job['id'] in {'huawei-ecs-network', 'huawei-rds-network'}:
        assert job['kind'] == 'network_traffic'
        job['options'].update(scope='intranet_estimated', period=300)
        if job['options']['product'] == 'rds':
            job['options']['lookback_seconds'] = 1800
        job['interval_seconds'] = 300
for rule in after['rules']:
    if rule['id'] in {'huawei-ecs-network-in', 'huawei-ecs-network-out', 'huawei-rds-network-in', 'huawei-rds-network-out'}:
        rule['match']['scope'] = 'intranet_estimated'
        product = 'ECS' if '-ecs-' in rule['id'] else 'RDS'
        direction = '\u5165\u7ad9' if rule['id'].endswith('-in') else '\u51fa\u7ad9'
        rule['summary'] = product + ' \u5185\u7f51' + direction + '\u901f\u7387\uff08\u4f30\u7b97\uff09\u6301\u7eed\u504f\u9ad8'
for old, new in zip(before['jobs'], after['jobs']):
    if not old['id'].startswith('huawei-'):
        assert old == new
    assert old.get('enabled') == new.get('enabled')
config_path.write_text(yaml.safe_dump(after, allow_unicode=True, sort_keys=False), encoding='utf-8')
print(json.dumps({'uploaded_files':len(files), 'backup':str(backup.relative_to(root)),
    'aliyun_ecs_selection_counts':{j['id']:len(j['options'].get('instances', [])) for j in after['jobs']
        if j['id'].startswith('aliyun-') and j['options']['product']=='ecs'}}, ensure_ascii=False))
PY
docker compose config --quiet
docker compose build cloud-monitor
docker compose run --rm -T --interactive=false --no-deps cloud-monitor --config /app/config/config.yaml check --live
docker compose run --rm -T --no-deps --entrypoint python cloud-monitor - <<'PY'
import json
from collections import Counter
from cloud_monitor.config import load_config
from cloud_monitor.models import utcnow
from cloud_monitor.providers import make_collector, collect_job
from cloud_monitor.diagnostics import safe_collection_error
cfg = load_config('/app/config/config.yaml', live=True)
accounts = {a['id']:a for a in cfg['accounts']}
failed = False
for job in cfg['jobs']:
    if job['id'] not in {'huawei-ecs-network', 'huawei-rds-network'}:
        continue
    try:
        collector = make_collector(accounts[job['account']])
        now = utcnow()
        metrics = collect_job(collector, job, now)
        partial = getattr(collector, 'network_partial_failures', 0)
        valid = bool(metrics) and not partial and all(m.dimensions.get('scope')=='intranet_estimated'
            and m.dimensions.get('source')=='total_minus_eip' for m in metrics)
        failed |= not valid
        print(json.dumps({'job':job['id'],'status':'ok' if valid else 'error','samples':len(metrics),
            'resources':len({m.resource_id for m in metrics}),'directions':dict(Counter(m.metric for m in metrics)),
            'scopes':sorted({m.dimensions.get('scope') for m in metrics}),
            'sources':sorted({m.dimensions.get('source') for m in metrics}),
            'max_source_age_seconds':max(((now-m.observed_at).total_seconds() for m in metrics), default=None),
            'partial_failures':partial},ensure_ascii=False),flush=True)
    except Exception as exc:
        failed = True
        print(json.dumps({'job':job['id'],'status':'error','detail':safe_collection_error(exc)}),flush=True)
raise SystemExit(1 if failed else 0)
PY
if ! docker compose run --rm -T --interactive=false --user 0 --no-deps --entrypoint python -v "$PWD/scripts:/app/scripts:ro" cloud-monitor /app/scripts/probe_aliyun_rds_eip.py --config /app/config/config.yaml; then
    printf 'Alibaba RDS/EIP probe incomplete; inspect required_actions above.\n'
fi
printf 'Stage complete. Backup: %s\n' "$backup"
touch "$backup/staged-ok"
'@
    $remoteScript = $remoteScript.Replace('__MANIFEST__', $deployManifest)
} elseif ($Phase -eq 'Activate') {
    $remoteScript = @'
set -eu
cd /data/monitor/cloud-resource-monitor
test -f .deploy-backups/huawei-intranet-latest
backup=$(cat .deploy-backups/huawei-intranet-latest)
test -f "$backup/staged-ok"
docker compose config --quiet
if ! docker compose run --rm -T --interactive=false --user 0 --no-deps --entrypoint python -v "$PWD/scripts:/app/scripts:ro" cloud-monitor /app/scripts/probe_aliyun_rds_eip.py --config /app/config/config.yaml; then
    printf 'Alibaba RDS/EIP probe incomplete; inspect required_actions above.\n'
fi
docker compose up -d --no-deps --no-build --pull never --force-recreate cloud-monitor notification-gateway
touch "$backup/activated"
docker compose ps cloud-monitor notification-gateway mysql
'@
} else {
    $remoteScript = @'
set -eu
cd /data/monitor/cloud-resource-monitor
docker compose ps cloud-monitor notification-gateway mysql
backup=$(cat .deploy-backups/huawei-intranet-latest)
test "$(cat "$backup/mysql-before.txt")" = "$(docker compose ps -q mysql)"
curl -fsS http://127.0.0.1:18080/healthz
docker compose logs --no-color --since 10m cloud-monitor 2>&1 | grep -F 'network' | tail -30
if ! docker compose run --rm -T --interactive=false --user 0 --no-deps --entrypoint python -v "$PWD/scripts:/app/scripts:ro" cloud-monitor /app/scripts/probe_aliyun_rds_eip.py --config /app/config/config.yaml; then
    printf 'Alibaba RDS/EIP probe incomplete; inspect required_actions above.\n'
fi
'@
}
if ($Phase -in @('Activate', 'Verify')) {
    $remoteScript += @'

docker compose exec -T cloud-monitor python - <<'PY'
import json, time
from collections import Counter
from urllib.request import Request, ProxyHandler, build_opener
from prometheus_client.parser import text_string_to_metric_families
from cloud_monitor.config import load_config
from sqlalchemy import create_engine, select
from sqlalchemy.engine import URL
from cloud_monitor.store import JOBS, STATES, OUTBOX
cfg = load_config('/app/config/config.yaml')
target = {'huawei-ecs-network', 'huawei-rds-network'}
metric_token = cfg.get('api', {}).get('metrics', {}).get('token')
metric_headers = {'Authorization':'Bearer '+metric_token} if metric_token else {}
opener = build_opener(ProxyHandler({}))
jobs, samples, last_error = [], [], None
for attempt in range(30):
    try:
        with opener.open(Request('http://127.0.0.1:8080/metrics',headers=metric_headers),timeout=10) as response:
            exposition = response.read().decode()
        all_samples = [s for family in text_string_to_metric_families(exposition) for s in family.samples]
        samples = [s for s in all_samples if s.name == 'cloud_monitor_resource_value' and s.labels.get('job_id') in target
            and s.labels.get('metric') in {'network_in_bps','network_out_bps'}]
        jobs = [{'id':s.labels['job_id'],'status':s.labels['status']} for s in all_samples
            if s.name=='cloud_monitor_job_status' and s.labels.get('job_id') in target and s.value==1]
        ready = len(jobs)==2 and all(row['status']=='ok' for row in jobs) and len(samples)==72
        if ready:
            break
    except Exception as exc:
        ready = False
        last_error = type(exc).__name__
    time.sleep(2)
if not ready:
    print(json.dumps({'jobs':jobs,'network_series_count':len(samples),'last_error_type':last_error}))
    raise SystemExit('Huawei replacement not ready; inspect service logs')
assert all(s.labels.get('scope')=='intranet_estimated' and s.labels.get('source')=='total_minus_eip' for s in samples)
database = cfg['service']['database']
url = URL.create('mysql+pymysql',username=database['user'],password=database['password'],
    host=database['host'],port=database.get('port',3306),database=database['name'],query={'charset':'utf8mb4'})
engine = create_engine(url,hide_parameters=True,connect_args={'connect_timeout':10,'read_timeout':20})
with engine.connect() as connection:
    persisted_jobs = [dict(row) for row in connection.execute(select(JOBS.c.id,JOBS.c.status,JOBS.c.sample_count)
        .where(JOBS.c.id.in_(target))).mappings()]
    states = [json.loads(p) for p in connection.execute(select(STATES.c.payload)).scalars()]
    messages = [(status,json.loads(payload)) for status,payload in connection.execute(select(OUTBOX.c.status,OUTBOX.c.payload))]
engine.dispose()
old_alerts = [a for a in states if a and a.get('job_id') in target
    and a.get('metric',{}).get('metric') in {'network_in_bps','network_out_bps'}
    and a['metric'].get('dimensions',{}).get('scope')!='intranet_estimated']
assert not old_alerts
rules = {job+'-'+direction for job in target for direction in ('in','out')}
old_pending = sum(1 for status,event in messages if status in {'pending','failed'} and event.get('rule_id') in rules
    and event.get('dimensions',{}).get('scope')!='intranet_estimated')
assert not old_pending
counts = Counter((s.labels['product'],s.labels['metric'],s.labels['scope']) for s in samples)
print(json.dumps({'verification':'live_service','jobs':persisted_jobs,
    'network_series':[{'product':p,'metric':m,'scope':scope,'count':n} for (p,m,scope),n in sorted(counts.items())],
    'old_total_network_states':len(old_alerts),'old_total_pending_or_failed_messages':old_pending},ensure_ascii=False))
PY
python3 - <<'PY'
import json, time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen
dashboard = json.loads(Path('grafana/cloud-business-runtime-v1.json').read_text())
for panel_name in ('panel-8','panel-9','panel-12','panel-13'):
    panel = dashboard['spec']['elements'][panel_name]['spec']
    expr = panel['vizConfig']['spec']['options']['independentTime']['targets'][0]['expr']
    expr = expr.replace('$scrape_job','cloud-resource-monitor').replace('$instance','192.168.120.28:18080').replace('${account:regex}','.*')
    expected = 33 if panel_name in ('panel-8','panel-9') else 3
    for attempt in range(20):
        with urlopen('http://127.0.0.1:9090/api/v1/query?'+urlencode({'query':expr}),timeout=10) as response:
            body = json.load(response)
        result = body.get('data',{}).get('result',[])
        if body.get('status')=='success' and len(result)==expected:
            break
        time.sleep(2)
    assert len(result)==expected, panel_name+' unexpected series count'
    print(json.dumps({'verification':'actual_panel_query','panel':panel_name,'title':panel['title'],'series':len(result)},ensure_ascii=False))
PY
backup=$(cat .deploy-backups/huawei-intranet-latest)
test "$(cat "$backup/mysql-before.txt")" = "$(docker compose ps -q mysql)"
printf 'MySQL container preserved.\n'
'@
}
& $SshRunner -HostName $HostName -UserName $UserName -Command $remoteScript -TimeoutSeconds 900
exit $LASTEXITCODE
