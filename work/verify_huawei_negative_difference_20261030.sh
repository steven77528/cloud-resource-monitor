set -eu
docker exec -i cloud-resource-monitor-cloud-monitor-1 python - <<'PY'
import datetime, json, logging, math
from cloud_monitor.config import load_config
from cloud_monitor.diagnostics import safe_collection_error
from cloud_monitor.models import utcnow
from cloud_monitor.providers import make_collector
from cloud_monitor.providers.huawei_network_scope import HuaweiIntranetCollector

logging.disable(logging.CRITICAL)
tz = datetime.timezone(datetime.timedelta(hours=8))
def emit(kind, value):
    print(kind + ' ' + json.dumps(value, ensure_ascii=False), flush=True)
cfg = load_config('/app/config/config.yaml')
job = next(j for j in cfg['jobs'] if j['id'] == 'huawei-ecs-network')
account = next(a for a in cfg['accounts'] if a['id'] == job['account'])
if not job.get('enabled', True) or not account.get('enabled', False):
    raise SystemExit('Diagnostic target disabled')
options = job['options']
collector = make_collector(account)
probe = HuaweiIntranetCollector(collector)
now = utcnow()
metadata, mappings = probe._inventory('ecs', 'cn-east-3', options, now)
rid = '821b48c2-5199-41d6-823b-cfc2e262e159'
resource = metadata[rid]
eips = mappings[rid]
records = []
original = probe._series
def traced_series(region, namespace, metric, dimension, resource_id, units, start, end):
    points = original(region, namespace, metric, dimension, resource_id, units, start, end)
    records.append({'namespace': namespace, 'metric': metric, 'points': points})
    return points
probe._series = traced_series
instance = {'resource_id': rid, 'resource_name': resource['name'], 'region': 'cn-east-3'}
try:
    samples = probe._estimate(options, instance, resource, eips, now)
    emit('VERIFY_RESULT', {'at': now.astimezone(tz).isoformat(), 'samples': len(samples)})
except Exception as exc:
    emit('VERIFY_RESULT', {'at': now.astimezone(tz).isoformat(), 'detail': safe_collection_error(exc)})
if len(records) >= 4:
    common = set(records[0]['points']).intersection(*(set(r['points']) for r in records[1:]))
    for stamp in sorted(common):
        rows = []
        for direction in ('in', 'out'):
            total = next(r['points'][stamp] for r in records if r['namespace']=='SYS.ECS' and r['metric']=='network_vm_bandwidth_'+direction)
            public = sum(r['points'][stamp] for r in records if r['namespace']=='SYS.VPC' and r['metric']==('downstream_bandwidth' if direction=='in' else 'upstream_bandwidth'))
            rows.append({'direction': direction, 'total_bps': total, 'public_bps': public,
                'difference_bps': total-public, 'finite': math.isfinite(total-public)})
        emit('MATCHED_SOURCE_POINT', {'resource': resource['name'], 'source_time': datetime.datetime.fromtimestamp(stamp/1000,tz).isoformat(),
            'eip_count': len(eips), 'is_latest': stamp==max(common), 'rates': rows})
PY
