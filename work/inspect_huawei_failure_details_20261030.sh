set -eu
python3 - <<'PY'
import collections, datetime, json, re, subprocess
name = 'cloud-resource-monitor-cloud-monitor-1'
tz = datetime.timezone(datetime.timedelta(hours=8))
def emit(kind, value):
    print(kind + ' ' + json.dumps(value, ensure_ascii=False), flush=True)
result = subprocess.run(['docker', 'logs', '--timestamps', '--since', '30h', name], capture_output=True, text=True, timeout=120)
for line in (result.stdout + '\n' + result.stderr).splitlines():
    if not re.search(r'collection job=(?:huawei-rds-expiry|huawei-elb-bandwidth|aliyun-mysql-iot-performance|aliyun-yuanzhu-mysql-national-readonly-performance) failed', line):
        continue
    stamp = line.split(' ', 1)[0]
    job = re.search(r'collection job=([A-Za-z0-9_.-]+)', line)[1]
    patterns = (r'Huawei (?:ces|rds|bss)\.[a-z_]+ failed \(status=(?:unknown|[0-9]{3}), code=[A-Za-z0-9_.-]+\)',
                'Missing time field in Huawei response', 'Invalid time field in Huawei response',
                'ClientException', 'RDS instance has unknown charge_info.charge_mode')
    reasons = [m.group(0) for pattern in patterns for m in re.finditer(pattern, line)]
    emit('HISTORICAL_FAILURE_DETAIL', {'at_utc': stamp, 'job': job, 'reasons': reasons})

script = r'''
import collections, datetime, hashlib, importlib, json, logging, pathlib, time
import pymysql
from cloud_monitor.config import load_config
from cloud_monitor.diagnostics import safe_collection_error
from cloud_monitor.models import utcnow
from cloud_monitor.providers import make_collector, collect_job
from cloud_monitor.providers.huawei_network_scope import HuaweiIntranetCollector
logging.disable(logging.CRITICAL)
tz = datetime.timezone(datetime.timedelta(hours=8))
def emit(kind, value):
    print(kind + ' ' + json.dumps(value, ensure_ascii=False), flush=True)
def stamp(t):
    return datetime.datetime.fromtimestamp(float(t), tz).isoformat(timespec='seconds')
cfg = load_config('/app/config/config.yaml')
accounts = {a['id']: a for a in cfg['accounts']}
jobs = {j['id']: j for j in cfg['jobs']}
for path in ('service.py', 'diagnostics.py', 'providers/aliyun.py', 'providers/rds_cloudmonitor.py', 'providers/rds_performance.py', 'providers/huawei.py', 'providers/network_traffic.py', 'providers/huawei_network_scope.py'):
    data = pathlib.Path('/app/cloud_monitor', path).read_bytes().replace(b'\r\n', b'\n')
    emit('SOURCE_HASH', {'file': path, 'sha256_lf': hashlib.sha256(data).hexdigest()})
db = cfg['service']['database']
conn = pymysql.connect(host=db.get('host', 'mysql'), port=int(db.get('port', 3306)), user=db.get('user'),
    password=db.get('password'), database=db.get('name', 'cloud_monitor'), charset='utf8mb4',
    connect_timeout=10, read_timeout=30, cursorclass=pymysql.cursors.DictCursor, autocommit=False)
with conn.cursor() as cur:
    cur.execute('SET TRANSACTION READ ONLY')
    cur.execute('START TRANSACTION WITH CONSISTENT SNAPSHOT')
    cur.execute("SELECT status, channel, attempts, created_at, payload FROM outbox WHERE created_at >= %s AND JSON_UNQUOTE(JSON_EXTRACT(payload, '$.rule_id')) = 'collection-error'", (time.time()-7*86400,))
    counts = collections.Counter()
    total = collections.Counter()
    attempts = collections.Counter()
    for row in cur.fetchall():
        event = json.loads(row['payload'])
        key = (event.get('resource_id'), event.get('phase'), row['status'])
        total[key] += 1
        counts[(stamp(row['created_at'])[:10], *key)] += 1
        attempts[row['attempts']] += 1
    emit('MAIL_7D_BY_JOB', [{'job': j, 'phase': p, 'status': s, 'n': n} for (j,p,s),n in sorted(total.items())])
    emit('MAIL_RECENT_DAYS', [{'day': d, 'job': j, 'phase': p, 'status': s, 'n': n}
        for (d,j,p,s),n in sorted(counts.items()) if d >= '2026-10-29'])
    emit('MAIL_ATTEMPTS', dict(attempts))
    cur.execute("SELECT id,status,updated_at,detail FROM jobs WHERE status='error'")
    emit('CURRENT_ERRORS', [{'job': r['id'], 'at': stamp(r['updated_at']), 'detail': r['detail']} for r in cur.fetchall()])
conn.rollback()
conn.close()

original_estimate = HuaweiIntranetCollector._estimate
def traced_estimate(self, options, instance, resource, eips, now):
    try:
        return original_estimate(self, options, instance, resource, eips, now)
    except Exception as exc:
        emit('RESOURCE_FAILURE', {'path': 'intranet_estimate', 'resource_id': instance['resource_id'],
            'name': instance.get('resource_name'), 'region': instance['region'], 'detail': safe_collection_error(exc)})
        raise
HuaweiIntranetCollector._estimate = traced_estimate
for job_id in ('huawei-rds-expiry', 'huawei-ecs-network'):
    job = jobs[job_id]
    if not job.get('enabled', True) or not accounts[job['account']].get('enabled', False):
        emit('LIVE_SKIP', {'job': job_id, 'reason': 'disabled'})
        continue
    options = job.get('options', {})
    emit('LIVE_JOB_OPTIONS', {'job': job_id, **{k:options[k] for k in ('product','scope','period','lookback_seconds','max_age_seconds','regions','region') if k in options}})
    collector = make_collector(accounts[job['account']])
    original_metrics = collector._metrics
    def traced_metrics(opts, now):
        try:
            return original_metrics(opts, now)
        except Exception as exc:
            emit('RESOURCE_FAILURE', {'path': 'ces_metrics', 'resources': [s['resource_id'] for s in opts.get('metrics', [])],
                'detail': safe_collection_error(exc)})
            raise
    collector._metrics = traced_metrics
    if job_id == 'huawei-rds-expiry':
        original_expiry = collector._expiry
        def traced_expiry(rid, name, expires, now, dimensions):
            emit('RDS_EXPIRY_FIELD', {'resource_id': rid, 'name': name, 'expiration_time_present': expires is not None,
                'expiration_time_type': type(expires).__name__, 'region': dimensions.get('region'), 'engine': dimensions.get('engine')})
            return original_expiry(rid, name, expires, now, dimensions)
        collector._expiry = traced_expiry
    for turn in range(1, 3 if job_id == 'huawei-ecs-network' else 2):
        started = time.monotonic()
        try:
            samples = collect_job(collector, job, utcnow())
            emit('LIVE_RESULT', {'job': job_id, 'round': turn, 'samples': len(samples),
                'failed_resources': getattr(collector, 'network_partial_failures', 0), 'elapsed': round(time.monotonic()-started, 2)})
        except Exception as exc:
            emit('LIVE_RESULT', {'job': job_id, 'round': turn, 'detail': safe_collection_error(exc),
                'elapsed': round(time.monotonic()-started, 2)})
'''
result = subprocess.run(['docker', 'exec', '-i', name, 'python', '-'], input=script, capture_output=True, text=True, timeout=180)
print(result.stdout, end='', flush=True)
if result.returncode:
    emit('FOCUSED_INSPECTION_EXIT', {'code': result.returncode})
PY
