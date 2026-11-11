set -eu
python3 - <<'PY'
import collections
import datetime
import hashlib
import json
import re
import subprocess

NAME = 'cloud-resource-monitor-cloud-monitor-1'
TZ = datetime.timezone(datetime.timedelta(hours=8))

def output(kind, value):
    print(kind + ' ' + json.dumps(value, ensure_ascii=False), flush=True)

def run(args):
    return subprocess.run(args, capture_output=True, text=True, timeout=150)

def safe_error(message):
    fields = {}
    for key in ('error_type', 'action', 'api_code', 'http_status', 'failed_resources', 'metric'):
        match = re.search(r'\b' + key + r'=([A-Za-z0-9_.-]{1,96})(?:\s|;|$)', message)
        if match:
            fields[key] = match[1]
    for name, pattern in (
        ('transport_error', r'SDK\.HttpError|ConnectionError|ConnectTimeout|ReadTimeout|TimeoutError'),
        ('tls_error', r'CERTIFICATE_VERIFY_FAILED|SSLError|SSL:|TLSV1_|UNEXPECTED_EOF|SSLEOFError'),
        ('timeout', r'timed out|[Tt]imeout|[Tt]ime-out'),
        ('dns_error', r'NameResolutionError|Name or service not known|Temporary failure in name resolution'),
        ('connection_reset', r'Connection reset|ConnectionResetError|ECONNRESET'),
        ('connection_refused', r'Connection refused|ECONNREFUSED'),
        ('http_503', r'HTTP Status: 503|ServiceUnavailable|http_status=503'),
        ('throttling', r'Throttl|TooManyRequests|RequestLimitExceeded'),
        ('permission', r'Forbidden|Unauthorized|InvalidAccessKeyId|SignatureDoesNotMatch|SCM\.4002'),
        ('no_cloudmonitor_data', r'No CloudMonitor data|no datapoints'),
        ('stale_data', r'[Ss]tale|is stale|in the future|No fresh'),
        ('sms_insufficient', r'missing dates|insufficient|statistics.*(?:incomplete|missing)|SmsStatisticsInsufficientData'),
        ('invalid_metric', r'invalid or negative metric value|invalid metric|Non-finite'),
        ('partial_network', r'partially failed|failed_resources='),
        ('relay_error', r'RelayError|relay_request|relay_unavailable|upstream_unavailable'),
    ):
        if re.search(pattern, message):
            fields[name] = True
    return fields

inspect = run(['docker', 'inspect', NAME])
if inspect.returncode:
    output('FATAL', {'step': 'container_inspect', 'code': inspect.returncode})
    raise SystemExit(1)
info = json.loads(inspect.stdout)[0]
labels = info.get('Config', {}).get('Labels', {})
state = info['State']
output('CONTAINER', {'name': NAME, 'created': info['Created'], 'started': state['StartedAt'],
    'restart_count': info['RestartCount'], 'oom_killed': state['OOMKilled'],
    'health': state.get('Health', {}).get('Status'), 'image_id': info['Image'],
    'project_dir': labels.get('com.docker.compose.project.working_dir'),
    'log_options': info['HostConfig']['LogConfig']})

log_result = run(['docker', 'logs', '--timestamps', '--since', '168h', NAME])
lines = (log_result.stdout + '\n' + log_result.stderr).splitlines()
totals = collections.Counter()
daily = collections.Counter()
failures = []
sdk = []
first = last = None
last_ok = {}
pending = collections.defaultdict(list)
other = collections.Counter()
for line in lines:
    tm = re.match(r'(\d{4}-\d\d-\d\dT\S+)', line)
    if not tm:
        continue
    stamp = tm[1]
    try:
        at = datetime.datetime.fromisoformat(re.sub(r'(\.\d{6})\d+', r'\1', stamp).replace('Z', '+00:00'))
        local = at.astimezone(TZ).isoformat(timespec='seconds')
    except ValueError:
        continue
    first = first or local
    last = local
    match = re.search(r'collection job=([A-Za-z0-9_.-]+) (.*)', line)
    if match:
        job, rest = match.groups()
        status_match = re.search(r'status=([a-z_]+)', rest)
        status = status_match[1] if status_match else 'error' if 'failed' in rest else 'unknown'
        totals[(job, status)] += 1
        daily[(local[:10], job, status)] += 1
        if status == 'error':
            event = {'at': local, 'job': job, **safe_error(rest), 'previous_ok': last_ok.get(job)}
            failures.append(event)
            pending[job].append(event)
        elif status in ('ok', 'no_data', 'insufficient_data'):
            for event in pending.pop(job, []):
                event['next_healthy'] = local
            last_ok[job] = local
    if any(word in line for word in ('HTTP Status:', 'Error:', 'SDK.HttpError', 'SSLError', 'timed out', 'TLSV1_', 'UNEXPECTED_EOF')):
        event = {'at': local, **safe_error(line)}
        status_match = re.search(r'HTTP Status:\s*([1-5][0-9]{2})', line)
        code_match = re.search(r'Error:\s*([A-Za-z][A-Za-z0-9_.-]{0,64})(?=\s|,|;|$)', line)
        if status_match:
            event['http_status'] = status_match[1]
        if code_match:
            event['api_code'] = code_match[1]
        if len(event) > 1:
            sdk.append(event)
    for keyword in ('maximum number of running instances reached', 'was missed by', 'monitor started',
                    'MySQL connection or operation failed', 'scheduler lock was lost', 'Traceback'):
        if keyword in line:
            other[keyword] += 1

output('LOG_WINDOW', {'first': first, 'last': last, 'lines': len(lines), 'docker_exit': log_result.returncode})
output('JOB_COUNTS', [{'job': job, 'status': status, 'count': count} for (job, status), count in sorted(totals.items())])
output('ERROR_COUNTS_BY_DAY', [{'day': day, 'job': job, 'status': status, 'count': count}
    for (day, job, status), count in sorted(daily.items()) if status == 'error'])
output('FAILURE_EVENTS', failures[-100:])
output('SDK_ERRORS', sdk[-100:])
output('OTHER_LOG_SIGNALS', dict(other))

script = r'''
import collections, datetime, hashlib, json, logging, pathlib, re, time
import pymysql
from sqlalchemy.engine import make_url
from cloud_monitor.config import load_config
logging.disable(logging.CRITICAL)
tz = datetime.timezone(datetime.timedelta(hours=8))
def emit(kind, value):
    print(kind + ' ' + json.dumps(value, ensure_ascii=False), flush=True)
def stamp(value):
    return datetime.datetime.fromtimestamp(float(value), tz).isoformat(timespec='seconds')
try:
    cfg = load_config('/app/config/config.yaml')
    accounts = {a['id']: a for a in cfg['accounts']}
    enabled = [j for j in cfg['jobs'] if j.get('enabled', True) and accounts[j['account']].get('enabled', False)]
    emit('ALERT_SETTINGS', {k: cfg['service'].get(k) for k in ('recovery_samples', 'notify_recovery', 'repeat_seconds', 'retention_days')})
    emit('JOB_SETTINGS', [{'id': j['id'], 'kind': j['kind'], 'account': j['account'],
        'interval_seconds': j['interval_seconds'], 'collection_error_samples': j.get('collection_error_samples', 1),
        'request_timeout_seconds': accounts[j['account']].get('request_timeout_seconds', accounts[j['account']].get('timeout_seconds', 20))}
        for j in enabled])
    for name in ('service.py', 'diagnostics.py', 'providers/aliyun.py', 'providers/rds_cloudmonitor.py', 'providers/rds_performance.py', 'providers/huawei.py'):
        raw = pathlib.Path('/app/cloud_monitor', name).read_bytes().replace(b'\r\n', b'\n')
        emit('SOURCE_HASH', {'file': name, 'sha256_lf': hashlib.sha256(raw).hexdigest()})
    db = cfg['service']['database']
    if isinstance(db, dict):
        options = dict(host=db.get('host', 'mysql'), port=int(db.get('port', 3306)), user=db.get('user'),
            password=db.get('password'), database=db.get('name', 'cloud_monitor'))
    else:
        url = make_url(db)
        options = dict(host=url.host, port=url.port or 3306, user=url.username, password=url.password, database=url.database)
    connection = pymysql.connect(**options, charset='utf8mb4', connect_timeout=10, read_timeout=30, write_timeout=30,
        cursorclass=pymysql.cursors.DictCursor, autocommit=False)
    with connection.cursor() as cursor:
        cursor.execute('SET TRANSACTION READ ONLY')
        cursor.execute('START TRANSACTION WITH CONSISTENT SNAPSHOT')
        cursor.execute('SELECT id, status, updated_at, sample_count, detail FROM jobs ORDER BY id')
        jobs = []
        for row in cursor.fetchall():
            detail = row.pop('detail')
            row['at'] = stamp(row.pop('updated_at'))
            row['error_fields'] = {m[1]: m[2] for m in re.finditer(r'\b(error_type|action|api_code|http_status|metric)=([A-Za-z0-9_.-]{1,96})(?:\s|;|$)', detail)}
            row['detail_type'] = detail.split(':', 1)[0] if re.match(r'^[A-Za-z][A-Za-z0-9]*:', detail) else ''
            jobs.append(row)
        emit('CURRENT_JOBS', jobs)
        cursor.execute('SELECT status, channel, COUNT(*) AS n FROM outbox GROUP BY status, channel')
        emit('OUTBOX_TOTALS', cursor.fetchall())
        cursor.execute("SELECT status, channel, attempts, created_at, payload FROM outbox WHERE created_at >= %s AND JSON_UNQUOTE(JSON_EXTRACT(payload, '$.rule_id')) = 'collection-error' ORDER BY created_at", (time.time() - 7*86400,))
        events = []
        counts = collections.Counter()
        for row in cursor.fetchall():
            payload = json.loads(row['payload'])
            event = {'at': stamp(row['created_at']), 'status': row['status'], 'channel': row['channel'],
                'attempts': row['attempts'], 'job': payload.get('resource_id'), 'phase': payload.get('phase')}
            events.append(event)
            counts[(event['at'][:10], event['job'], event['phase'], event['channel'], event['status'])] += 1
        emit('COLLECTION_MAIL_COUNTS', [{'day': day, 'job': job, 'phase': phase, 'channel': channel, 'status': status, 'count': n}
            for (day, job, phase, channel, status), n in sorted(counts.items())])
        emit('RECENT_COLLECTION_NOTIFICATIONS', events[-100:])
        cursor.execute("SELECT payload FROM states WHERE JSON_UNQUOTE(JSON_EXTRACT(payload, '$.rule_id')) = 'collection-error'")
        states = []
        for row in cursor.fetchall():
            state = json.loads(row['payload'])
            states.append({k: state.get(k) for k in ('job_id', 'active', 'healthy_samples')} | {
                'last_observed': stamp(state['last_observed']), 'last_notified': stamp(state['last_notified'])})
        emit('COLLECTION_ALERT_STATES', states)
    connection.rollback()
    connection.close()
except Exception as exc:
    emit('INSPECTION_ERROR', {'error_type': type(exc).__name__})
'''
result = subprocess.run(['docker', 'exec', '-i', NAME, 'python', '-'], input=script,
    capture_output=True, text=True, timeout=120)
print(result.stdout, end='', flush=True)
if result.returncode:
    output('CONTAINER_INSPECTION_EXIT', {'code': result.returncode})
PY
