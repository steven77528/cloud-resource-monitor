set -eu
cd /data/monitor/cloud-resource-monitor
python3 - <<'PY'
import collections
import datetime
import json
import re
import subprocess

NAME = 'cloud-resource-monitor-cloud-monitor-1'
TZ = datetime.timezone(datetime.timedelta(hours=8))

def emit(kind, data):
    print(kind + ' ' + json.dumps(data, ensure_ascii=False), flush=True)

def local(value):
    return datetime.datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(TZ).isoformat(timespec='seconds')

result = subprocess.run(['docker', 'logs', '--timestamps', '--since', '48h', NAME],
                        capture_output=True, text=True, timeout=120)
reasons = collections.Counter()
resources = collections.Counter()
events = []
runs = []
transport = collections.Counter()
for line in (result.stdout + '\n' + result.stderr).splitlines():
    stamp = re.match(r'\d{4}-\d\d-\d\dT\S+', line)
    if not stamp:
        continue
    at = local(stamp[0])
    match = re.search(r'network collection resource_id=([A-Za-z0-9-]+) region=([a-z0-9-]+) failed detail=(.*)', line)
    if match:
        rid, region, detail = match.groups()
        known = ('sources are stale or in the future', 'source has no datapoints',
                 'sources have no common timestamp for the current EIP binding',
                 'has no complete unambiguous EIP mapping',
                 'source has an invalid rate, unit or timestamp')
        reason = next((value for value in known if value in detail), 'other')
        query = re.search(r'query failed \(status=(unknown|\d{3}), code=([A-Za-z0-9_.-]+)\)', detail)
        if query:
            reason = 'query_failed status=' + query[1] + ' code=' + query[2]
        reasons[reason] += 1
        resources[(rid, reason)] += 1
        events.append({'at': at, 'resource': rid, 'region': region, 'reason': reason})
    match = re.search(r'collection job=([A-Za-z0-9_.-]+) (.*)', line)
    if match:
        job, detail = match.groups()
        status = re.search(r'status=([a-z_]+)', detail)
        runs.append({'at': at, 'job': job, 'status': status[1] if status else 'error' if 'failed' in detail else 'unknown'})
    for reason, pattern in (
        ('timeout', r'timed out|ReadTimeout|ConnectTimeout'),
        ('connection_reset', r'Connection reset|ConnectionResetError'),
        ('dns_error', r'NameResolutionError|Name or service not known'),
        ('http_503', r'HTTP Status: 503|http_status=503')):
        if re.search(pattern, line):
            transport[reason] += 1
emit('RESOURCE_FAILURE_REASONS', dict(reasons))
emit('TOP_RESOURCE_FAILURES', [{'resource': rid, 'reason': reason, 'n': n}
    for (rid, reason), n in resources.most_common(10)])
emit('RECENT_RESOURCE_FAILURES', events[-8:])
emit('TRANSPORT_LOG_LINES', dict(transport))
emit('LOG_RUNS', runs)

script = r'''
import collections, datetime, json, logging, time
import pymysql
from sqlalchemy.engine import make_url
from cloud_monitor.config import load_config
from cloud_monitor.diagnostics import safe_collection_error
from cloud_monitor.models import utcnow
from cloud_monitor.providers import make_collector, collect_job
from cloud_monitor.providers.huawei_network_scope import HuaweiIntranetCollector
logging.disable(logging.CRITICAL)
TZ = datetime.timezone(datetime.timedelta(hours=8))
def emit(kind, data):
    print(kind + ' ' + json.dumps(data, ensure_ascii=False), flush=True)
def local(value):
    return datetime.datetime.fromtimestamp(float(value), TZ).isoformat(timespec='seconds')
try:
    cfg = load_config('/app/config/config.yaml')
    accounts = {a['id']: a for a in cfg['accounts']}
    jobs = {j['id']: j for j in cfg['jobs']}
    job = jobs['huawei-ecs-network']
    emit('NETWORK_SETTINGS', {k: job.get('options', {}).get(k) for k in
        ('product', 'scope', 'period', 'lookback_seconds', 'max_age_seconds', 'region', 'regions')})
    emit('ALERT_SETTINGS', {'interval_seconds': job['interval_seconds'],
        'collection_error_samples': job.get('collection_error_samples', 1),
        'recovery_samples': cfg['service'].get('recovery_samples'),
        'notify_recovery': cfg['service'].get('notify_recovery'),
        'repeat_seconds': cfg['service'].get('repeat_seconds')})
    db = cfg['service']['database']
    if isinstance(db, dict):
        options = dict(host=db.get('host', 'mysql'), port=int(db.get('port', 3306)), user=db.get('user'),
            password=db.get('password'), database=db.get('name', 'cloud_monitor'))
    else:
        url = make_url(db)
        options = dict(host=url.host, port=url.port or 3306, user=url.username, password=url.password, database=url.database)
    connection = pymysql.connect(**options, charset='utf8mb4', connect_timeout=10, read_timeout=30,
        cursorclass=pymysql.cursors.DictCursor, autocommit=False)
    with connection.cursor() as cursor:
        cursor.execute('SET TRANSACTION READ ONLY')
        cursor.execute('START TRANSACTION WITH CONSISTENT SNAPSHOT')
        cursor.execute("SELECT status, channel, attempts, created_at, payload FROM outbox WHERE created_at >= %s ORDER BY created_at", (time.time()-7*86400,))
        counts = collections.Counter()
        phases = collections.Counter()
        attempts = collections.Counter()
        timeline = []
        for row in cursor.fetchall():
            payload = json.loads(row['payload'])
            if row['channel'] != 'email':
                continue
            at = local(row['created_at'])
            counts[(at[:10], payload.get('rule_id'), row['status'])] += 1
            if payload.get('rule_id') == 'collection-error':
                phases[(at[:10], payload.get('resource_id'), payload.get('phase'), row['status'])] += 1
                attempts[row['attempts']] += 1
                timeline.append({'at': at, 'job': payload.get('resource_id'), 'phase': payload.get('phase'), 'status': row['status']})
        emit('ALL_MAIL_DAILY', [{'day': day, 'rule': rule, 'status': status, 'n': n}
            for (day, rule, status), n in sorted(counts.items())])
        emit('COLLECTION_MAIL_DAILY', [{'day': day, 'job': jid, 'phase': phase, 'status': status, 'n': n}
            for (day, jid, phase, status), n in sorted(phases.items())])
        emit('COLLECTION_MAIL_ATTEMPTS', dict(attempts))
        emit('COLLECTION_TIMELINE', timeline)
        cursor.execute('SELECT status, channel, COUNT(*) AS n FROM outbox GROUP BY status, channel')
        emit('OUTBOX_TOTALS', cursor.fetchall())
    connection.rollback()
    connection.close()

    # Trace timestamps only in this independent read-only query process.
    original_series = HuaweiIntranetCollector._series
    original_estimate = HuaweiIntranetCollector._estimate
    age_counts = collections.Counter()
    failures = []
    def traced_series(self, region, namespace, metric, dimension, rid, units, start, end):
        result = original_series(self, region, namespace, metric, dimension, rid, units, start, end)
        self._probe_sources.append({'metric': metric, 'stamps': sorted(result)})
        return result
    def traced_estimate(self, options, instance, resource, eips, now):
        self._probe_sources = []
        try:
            return original_estimate(self, options, instance, resource, eips, now)
        except Exception as exc:
            sources = self._probe_sources
            common = set(sources[0]['stamps']).intersection(*(set(s['stamps']) for s in sources[1:])) if sources else set()
            record = {'resource': instance['resource_id'], 'name': instance.get('resource_name') or resource['name'],
                'at': local(now.timestamp()), 'reason': safe_collection_error(exc),
                'common_age_seconds': round(now.timestamp()-max(common)/1000, 1) if common else None,
                'latest_sources': [{'metric': s['metric'], 'latest': local(max(s['stamps'])/1000),
                    'age_seconds': round(now.timestamp()-max(s['stamps'])/1000, 1)} for s in sources]}
            failures.append(record)
            raise
        finally:
            sources = self._probe_sources
            common = set(sources[0]['stamps']).intersection(*(set(s['stamps']) for s in sources[1:])) if sources else set()
            age_counts[round(now.timestamp()-max(common)/1000) if common else 'no_common'] += 1
    HuaweiIntranetCollector._series = traced_series
    HuaweiIntranetCollector._estimate = traced_estimate
    if job.get('enabled', True) and accounts[job['account']].get('enabled', False):
        collector = make_collector(accounts[job['account']])
        started = time.monotonic()
        try:
            samples = collect_job(collector, job, utcnow())
            emit('LIVE_RESULT', {'samples': len(samples), 'failed_resources': getattr(collector, 'network_partial_failures', 0),
                'unavailable_resources': len(getattr(collector, 'network_estimate_unavailable', [])),
                'elapsed_seconds': round(time.monotonic()-started, 2)})
        except Exception as exc:
            emit('LIVE_RESULT', {'reason': safe_collection_error(exc)})
        emit('LIVE_COMMON_AGES', dict(age_counts))
        emit('LIVE_RESOURCE_FAILURES', failures)
    emit('COMPLETE', {'read_only': True, 'configuration_changed': False})
except Exception as exc:
    emit('INSPECTION_ERROR', {'type': type(exc).__name__})
    raise SystemExit(1)
'''
result = subprocess.run(['docker', 'exec', '-i', NAME, 'python', '-'], input=script,
                        capture_output=True, text=True, timeout=180)
print(result.stdout, end='', flush=True)
if result.returncode:
    emit('PROBE_EXIT', {'code': result.returncode})
    raise SystemExit(result.returncode)
PY
