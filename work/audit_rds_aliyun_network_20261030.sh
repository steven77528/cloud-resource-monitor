set -eu
docker exec -i cloud-resource-monitor-cloud-monitor-1 python - <<'PY'
import collections
import datetime
import json
import logging
import math
import statistics
import time

import pymysql
from cloud_monitor.config import load_config
from cloud_monitor.diagnostics import safe_collection_error
from cloud_monitor.models import utcnow
from cloud_monitor.providers import collect_job, make_collector
from cloud_monitor.providers.aliyun import _time
from cloud_monitor.providers.huawei_network_scope import HuaweiIntranetCollector, _RDS_UNITS, _BIT_UNITS
from cloud_monitor.providers.network_traffic import NetworkTrafficCollector

logging.disable(logging.CRITICAL)
TZ = datetime.timezone(datetime.timedelta(hours=8))
NOW = utcnow()
END = int(NOW.timestamp() // 300) * 300000 - 1
START = END + 1 - 24 * 3600000


def emit(kind, data):
    print(kind + ' ' + json.dumps(data, ensure_ascii=False), flush=True)


def local(value):
    return datetime.datetime.fromtimestamp(value / 1000, TZ).isoformat(timespec='seconds')


cfg = load_config('/app/config/config.yaml')
accounts = {a['id']: a for a in cfg['accounts']}
jobs = [j for j in cfg['jobs'] if j['kind'] == 'network_traffic' and j.get('enabled', True)
        and accounts[j['account']].get('enabled', False)]
job_ids = {j['id'] for j in jobs}
emit('WINDOW', {'at': NOW.astimezone(TZ).isoformat(), 'from': local(START), 'to': local(END)})
emit('NETWORK_JOBS', [{'job': j['id'], 'provider': accounts[j['account']]['provider'],
    'account': j['account'], 'product': j['options']['product'], 'scope': j['options'].get('scope', 'provider_native'),
    'period': j['options'].get('period'), 'interval': j['interval_seconds'],
    'selected_instances': len(j['options']['instances']) if 'instances' in j['options'] else 'automatic'} for j in jobs])

# Inspect only the monitor database, in a read-only consistent snapshot.
db = cfg['service']['database']
connection = pymysql.connect(host=db.get('host', 'mysql'), port=int(db.get('port', 3306)),
    user=db['user'], password=db['password'], database=db.get('name', 'cloud_monitor'), charset='utf8mb4',
    connect_timeout=10, read_timeout=45, cursorclass=pymysql.cursors.DictCursor, autocommit=False)
with connection.cursor() as cursor:
    cursor.execute('SET TRANSACTION READ ONLY')
    cursor.execute('START TRANSACTION WITH CONSISTENT SNAPSHOT')
    cursor.execute('SELECT id,status,updated_at,sample_count FROM jobs')
    emit('PERSISTED_JOBS', [{**r, 'at': local(r['updated_at'] * 1000)} for r in cursor.fetchall() if r['id'] in job_ids])
    cursor.execute("SELECT status,channel,created_at,payload FROM outbox WHERE created_at >= %s "
                   "AND JSON_UNQUOTE(JSON_EXTRACT(payload,'$.rule_id'))='collection-error'", (time.time()-7*86400,))
    notifications, other = collections.Counter(), collections.Counter()
    for row in cursor.fetchall():
        event = json.loads(row['payload'])
        job_id = event.get('resource_id', '')
        key = (job_id, event.get('phase'), row['channel'], row['status'])
        if job_id in job_ids:
            notifications[key] += 1
        elif job_id.startswith(('aliyun-', 'huawei-rds-')):
            other[key] += 1
    emit('NETWORK_COLLECTION_NOTIFICATIONS_7D', [{'job': j, 'phase': p, 'channel': c, 'status': s, 'count': n}
         for (j, p, c, s), n in sorted(notifications.items())])
    emit('OTHER_RELEVANT_COLLECTION_NOTIFICATIONS_7D', [{'job': j, 'phase': p, 'channel': c, 'status': s, 'count': n}
         for (j, p, c, s), n in sorted(other.items())])
    cursor.execute("SELECT payload FROM states WHERE JSON_UNQUOTE(JSON_EXTRACT(payload,'$.kind'))='network_estimate_quality'")
    quality = []
    for row in cursor.fetchall():
        state = json.loads(row['payload'])
        if state['job_id'] not in job_ids:
            continue
        entries = list(state['resources'].values())
        quality.append({'job': state['job_id'], 'resources': len(entries),
                        'ever_invalid': sum(e['invalid_observed_at'] > 0 for e in entries),
                        'currently_invalid': sum(e['valid_observed_at'] <= e['invalid_observed_at']
                                                 and e.get('in_scope', True) for e in entries)})
    emit('PERSISTED_ESTIMATE_QUALITY', quality)
    placeholders = ','.join(['%s'] * len(job_ids))
    cursor.execute('SELECT job_id,observed_at,payload FROM samples WHERE job_id IN (' + placeholders
                   + ') AND observed_at >= %s ORDER BY job_id,observed_at', (*sorted(job_ids), START/1000))
    history = {}
    for row in cursor.fetchall():
        metric = json.loads(row['payload'])
        if metric['metric'] not in {'network_in_bps', 'network_out_bps'}:
            continue
        item = history.setdefault(row['job_id'], {'samples': 0, 'resources': set(), 'negative_samples': 0,
                                                  'minimum_bps': math.inf, 'latest': 0})
        item['samples'] += 1
        item['resources'].add(metric['resource_id'])
        item['negative_samples'] += metric['value'] < 0
        item['minimum_bps'] = min(item['minimum_bps'], metric['value'])
        item['latest'] = max(item['latest'], row['observed_at'])
    emit('PERSISTED_NETWORK_SAMPLES_24H', [{**v, 'job': k, 'resources': len(v['resources']),
        'latest': local(v['latest']*1000)} for k, v in sorted(history.items())])
connection.rollback()
connection.close()


def ces_series(probe, region, namespace, name, dimension, rid, period, units):
    body = probe._request('ces', region, {'namespace': namespace, 'metric_name': name,
        'dim.0': dimension + ',' + rid, 'period': period, 'filter': 'average', 'from': START, 'to': END})
    points = body.get('datapoints', [])
    result, used_units = {}, collections.Counter()
    for point in points:
        value, stamp, unit = point['average'], point['timestamp'], point['unit']
        if (type(value) not in (float, int) or not math.isfinite(value) or value < 0
                or unit not in units or stamp in result or not START <= stamp <= END):
            raise ValueError('Invalid source metric in diagnostic')
        result[int(stamp)] = value * units[unit]
        used_units[unit] += 1
    return result, dict(used_units)


def ces_metadata(kind, direction, period, data, units):
    stamps = sorted(data)
    return {'kind': kind, 'direction': direction, 'period': period, 'samples': len(data), 'units': units,
        'first': local(stamps[0]) if stamps else None, 'last': local(stamps[-1]) if stamps else None,
        'offsets_300s': dict(collections.Counter(t % 300000 for t in stamps)),
        'steps_seconds': dict(collections.Counter((b-a)/1000 for a,b in zip(stamps,stamps[1:])))}


for job in jobs:
    if accounts[job['account']]['provider'] != 'huawei' or job['options']['product'] != 'rds':
        continue
    owner = make_collector(accounts[job['account']])
    network = NetworkTrafficCollector(owner, 'huawei')
    regions = job['options'].get('regions', accounts[job['account']].get('regions', []))
    if job['options'].get('region'):
        regions = [job['options']['region']]
    instances = job['options'].get('instances')
    if instances is None:
        instances = network._discover(job['options'], regions)
    probe = HuaweiIntranetCollector(owner)
    inventories = {}
    for instance in instances:
        rid, region = instance['resource_id'], instance['region']
        base = {'job': job['id'], 'resource_id': rid, 'name': instance.get('resource_name'), 'region': region}
        try:
            if region not in inventories:
                inventories[region] = probe._inventory('rds', region, job['options'], NOW)
            metadata, mappings = inventories[region]
            resource, eips = metadata[rid], mappings[rid]
            if not eips:
                raise ValueError('No complete RDS EIP mapping')
            emit('HUAWEI_RDS_MAPPING', {**base, 'engine': resource['engine'], 'topology': resource['topology'],
                'public_addresses': len(resource['addresses']), 'associated_eips': len(eips),
                'unique_address_coverage': resource['addresses'] == {e['address'] for e in eips}})
            dimension = 'postgresql_cluster_id' if resource['engine'] == 'PostgreSQL' else 'rds_cluster_id'
            data = {}
            for direction, rds_metric, eip_metric in (
                    ('in', 'rds004_bytes_in', 'downstream_bandwidth'),
                    ('out', 'rds005_bytes_out', 'upstream_bandwidth')):
                specs = [('rds', 'SYS.RDS', rds_metric, dimension, rid, _RDS_UNITS)]
                specs += [('eip', 'SYS.VPC', eip_metric, 'publicip_id', e['id'], _BIT_UNITS) for e in eips]
                for kind, namespace, name, dim, source_id, units in specs:
                    for period in (1, 60, 300):
                        series, used = ces_series(probe, region, namespace, name, dim, source_id, period, units)
                        data[(direction, kind, source_id, period)] = series
                        emit('HUAWEI_RDS_SOURCE', {**base, **ces_metadata(kind, direction, period, series, used)})
                    raw = data[(direction, kind, source_id, 1)]
                    minute = data[(direction, kind, source_id, 60)]
                    aggregate = data[(direction, kind, source_id, 300)]
                    groups = collections.defaultdict(list)
                    for timestamp, value in raw.items():
                        groups[timestamp // 300000 * 300000].append(value)
                    compared = [abs(statistics.mean(v)-aggregate[t]) for t,v in groups.items() if t in aggregate]
                    emit('HUAWEI_RDS_AGGREGATION', {**base, 'kind': kind, 'direction': direction,
                        'raw_equals_period60': raw == minute, 'native_points_per_bucket': dict(collections.Counter(map(len, groups.values()))),
                        'compared_buckets': len(compared), 'maximum_mean_difference_bps': max(compared, default=None)})
                total = data[(direction, 'rds', rid, 300)]
                publics = [data[(direction, 'eip', e['id'], 300)] for e in eips]
                common = sorted(set(total).intersection(*(set(p) for p in publics)))
                negatives = [(t,total[t],sum(p[t] for p in publics)) for t in common
                             if total[t] < sum(p[t] for p in publics)]
                differences = [total[t]-sum(p[t] for p in publics) for t in common]
                emit('HUAWEI_RDS_DIFFERENCE_24H', {**base, 'direction': direction, 'common_buckets': len(common),
                    'rds_buckets': len(total), 'missing_public_buckets': len(total)-len(common),
                    'negative_buckets': len(negatives), 'minimum_difference_bps': min(differences, default=None),
                    'worst_negative': [{'at':local(t),'total_bps':total_value,'public_bps':public_value,
                        'difference_bps':total_value-public_value}
                        for t,total_value,public_value in sorted(negatives,key=lambda x:x[1]-x[2])[:5]]})
        except Exception as exc:
            emit('HUAWEI_RDS_PROBE_ERROR', {**base, 'detail': safe_collection_error(exc)})


for job in jobs:
    if accounts[job['account']]['provider'] != 'aliyun':
        continue
    owner = make_collector(accounts[job['account']])
    try:
        metrics = collect_job(owner, job, NOW)
        emit('ALIYUN_LIVE_RESULT', {'job': job['id'], 'samples': len(metrics),
            'resources': len({m.resource_id for m in metrics}),
            'partial_failures': getattr(owner, 'network_partial_failures', 0),
            'scopes': sorted({m.dimensions['scope'] for m in metrics}),
            'source_units': sorted({m.dimensions.get('source_unit', '') for m in metrics})})
        by_resource = collections.defaultdict(dict)
        for metric in metrics:
            by_resource[metric.resource_id][metric.metric] = metric.observed_at.timestamp()
            dims = metric.dimensions
            params = {'Namespace': dims['namespace'], 'MetricName': dims['metric_name'],
                      'Dimensions': [{'instanceId': metric.resource_id}], 'Period': dims['period'],
                      'Length': 1440, 'StartTime': START, 'EndTime': END}
            points, tokens = [], set()
            while True:
                response = owner._rpc('cms', 'DescribeMetricList', '2019-01-01', params, dims['region'])
                raw = response.get('Datapoints', [])
                points.extend(json.loads(raw) if isinstance(raw, str) else raw)
                token = response.get('NextToken')
                if not token:
                    break
                if token in tokens or len(tokens) >= 10:
                    raise ValueError('Repeated historical pagination token')
                tokens.add(token)
                params['NextToken'] = token
            values, invalid, negative, conflicts = {}, 0, 0, 0
            for point in points:
                try:
                    stamp, value = int(_time(point['timestamp']).timestamp()*1000), float(point['Average'])
                    if not math.isfinite(value) or not START <= stamp <= END:
                        raise ValueError()
                    if stamp in values and values[stamp] != value:
                        conflicts += 1
                    values[stamp] = value
                    negative += value < 0
                except (ValueError, TypeError, KeyError, OverflowError):
                    invalid += 1
            stamps = sorted(values)
            emit('ALIYUN_SOURCE_24H', {'job': job['id'], 'resource_id': metric.resource_id,
                'name': metric.resource_name, 'metric': metric.metric, 'source_metric': dims['metric_name'],
                'source_unit': dims.get('source_unit'), 'multiplier': dims['multiplier'],
                'scope': dims['scope'], 'period': dims['period'], 'samples': len(values),
                'negative_points': negative, 'invalid_points': invalid, 'conflicting_points': conflicts,
                'minimum_source_value': min(values.values(), default=None),
                'first': local(stamps[0]) if stamps else None, 'last': local(stamps[-1]) if stamps else None,
                'maximum_gap_seconds': max(((b-a)/1000 for a,b in zip(stamps,stamps[1:])),default=None),
                'live_source_age_seconds': round((NOW-metric.observed_at).total_seconds(),3)})
        emit('ALIYUN_LATEST_DIRECTION_TIMES', {'job': job['id'], 'resources': [
            {'resource_id': rid, 'direction_time_difference_seconds': abs(
                stamps.get('network_in_bps',0)-stamps.get('network_out_bps',0))}
            for rid,stamps in by_resource.items()]})
    except Exception as exc:
        emit('ALIYUN_PROBE_ERROR', {'job': job['id'], 'detail': safe_collection_error(exc)})
emit('AUDIT_COMPLETE', {'database_transaction': 'read_only_rolled_back', 'notifications_sent': False,
                        'scheduler_started': False, 'configuration_changed': False})
PY
