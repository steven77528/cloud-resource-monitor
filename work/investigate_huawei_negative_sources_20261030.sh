set -eu
docker exec -i cloud-resource-monitor-cloud-monitor-1 python - <<'PY'
import collections
import datetime
import json
import logging
import math
import statistics

from cloud_monitor.config import load_config
from cloud_monitor.diagnostics import safe_collection_error
from cloud_monitor.models import utcnow
from cloud_monitor.providers import make_collector
from cloud_monitor.providers.huawei_network_scope import HuaweiIntranetCollector

logging.disable(logging.CRITICAL)
TZ = datetime.timezone(datetime.timedelta(hours=8))
RID = '821b48c2-5199-41d6-823b-cfc2e262e159'
REGION = 'cn-east-3'
def emit(kind, value):
    print(kind + ' ' + json.dumps(value, ensure_ascii=False), flush=True)
def local(stamp):
    return datetime.datetime.fromtimestamp(stamp/1000, TZ).isoformat(timespec='seconds')
cfg = load_config('/app/config/config.yaml')
job = next(j for j in cfg['jobs'] if j['id']=='huawei-ecs-network')
account = next(a for a in cfg['accounts'] if a['id']==job['account'])
if not job.get('enabled', True) or not account.get('enabled', False):
    raise SystemExit('Diagnostic target disabled')
probe = HuaweiIntranetCollector(make_collector(account))
ecs_rows = probe._pages('ecs', REGION, job['options'])
eip_rows = probe._pages('vpc', REGION, job['options'])
metadata = {row['id']: probe._resource(row, 'ecs') for row in ecs_rows}
mappings = probe._map(metadata, eip_rows)
resource = metadata[RID]
mapped = mappings[RID]
if not mapped:
    raise SystemExit('Target has no unambiguous EIP mapping')
identity = []
for eip in mapped:
    row = next(r for r in eip_rows if r['id']==eip['id'])
    candidates = [rid for rid, data in metadata.items() if row.get('public_ip_address') in data['addresses'] or row.get('port_id') in data['ports']]
    identity.append({'eip_id':eip['id'], 'address_matches': row.get('public_ip_address') in resource['addresses'],
        'port_matches': row.get('port_id') in resource['ports'], 'candidate_ecs_count':len(candidates),
        'duplicate_eip_address_count':sum(r.get('public_ip_address')==row.get('public_ip_address') for r in eip_rows)})
emit('BINDING_VERIFICATION', {'resource':resource['name'], 'ecs_id':RID, 'eip_count':len(mapped),
    'resource_port_count':len(resource['ports']), 'matches':identity})
now = utcnow()
end = int(now.timestamp()//300)*300000-1
start = end+1-12*3600000
emit('QUERY_WINDOW', {'started_at':now.astimezone(TZ).isoformat(), 'from':local(start), 'to':local(end)})
data = {}
for direction in ('out','in'):
    specs = [('ecs', 'SYS.ECS', 'network_vm_bandwidth_'+direction, 'instance_id', RID, 1),
             ('ecs', 'SYS.ECS', 'network_vm_bandwidth_'+direction, 'instance_id', RID, 300)]
    for eip in mapped:
        specs.extend([('eip', 'SYS.VPC', 'upstream_bandwidth' if direction=='out' else 'downstream_bandwidth',
            'publicip_id', eip['id'], period) for period in (1,60,300)])
    for kind,namespace,metric,dimension,rid,period in specs:
        key = (kind,direction,period,rid)
        try:
            body = probe._request('ces',REGION, {'namespace':namespace,'metric_name':metric,
                'dim.0':dimension+','+rid, 'period':period,'filter':'average','from':start,'to':end})
            points = body.get('datapoints', [])
            units = collections.Counter(p.get('unit') for p in points)
            values = {}
            multipliers = {'Byte/s':8,'byte/s':8,'Bytes/s':8,'bytes/s':8,'B/s':8} if kind=='ecs' else {'bit/s':1,'bits/s':1,'bps':1}
            for point in points:
                value, stamp, unit = point.get('average'),point.get('timestamp'),point.get('unit')
                if type(value) not in (int,float) or not math.isfinite(value) or unit not in multipliers or type(stamp) not in (int,float):
                    raise ValueError('Invalid diagnostic metric point')
                values[int(stamp)] = value*multipliers[unit]
            data[key] = values
            stamps = sorted(values)
            steps = collections.Counter(round((b-a)/1000,3) for a,b in zip(stamps,stamps[1:]))
            emit('SERIES_METADATA', {'kind':kind,'direction':direction,'period':period,'count':len(points),'units':dict(units),
                'first':local(stamps[0]) if stamps else None,'last':local(stamps[-1]) if stamps else None,
                'timestamp_offsets_300s':dict(collections.Counter(t%300000 for t in stamps)), 'steps_seconds':dict(steps)})
        except Exception as exc:
            emit('QUERY_ERROR', {'kind':kind,'direction':direction,'period':period,'detail':safe_collection_error(exc)})

def combined(kind, direction, period):
    ids = [RID] if kind=='ecs' else [e['id'] for e in mapped]
    sources = [data.get((kind,direction,period,rid),{}) for rid in ids]
    common = set(sources[0]).intersection(*(set(s) for s in sources[1:]))
    return {t:sum(s[t] for s in sources) for t in common}
def correlation(pairs):
    if len(pairs)<3:
        return None
    xs,ys = zip(*pairs)
    mx,my = statistics.mean(xs), statistics.mean(ys)
    sxx = sum((x-mx)**2 for x in xs)
    syy = sum((y-my)**2 for y in ys)
    return round(sum((x-mx)*(y-my) for x,y in pairs)/math.sqrt(sxx*syy),6) if sxx and syy else None
for direction in ('out','in'):
    total = combined('ecs',direction,300)
    public = combined('eip',direction,300)
    lag_results = []
    for shift in range(-900,901,60):
        pairs = [(total[t],public[t+shift*1000]) for t in total if t+shift*1000 in public]
        if not pairs:
            continue
        diffs = [a-b for a,b in pairs]
        lag_results.append({'eip_shift_seconds':shift,'pairs':len(pairs),'correlation':correlation(pairs),
            'negative_pairs':sum(d<0 for d in diffs),'mean_abs_difference_bps':round(statistics.mean(abs(d) for d in diffs),3),
            'min_difference_bps':round(min(diffs),3)})
    emit('LAG_COMPARISON', {'direction':direction,'relation':'ecs[t] compared with eip[t+shift]', 'results':lag_results})
    for kind in ('ecs','eip'):
        raw = combined(kind,direction,1)
        agg = combined(kind,direction,300)
        groups = collections.defaultdict(list)
        for t,value in raw.items():
            groups[t//300000*300000].append(value)
        comparisons = [(t,statistics.mean(values),agg[t],len(values)) for t,values in groups.items() if t in agg]
        emit('AGGREGATION_CHECK', {'kind':kind,'direction':direction,'compared_buckets':len(comparisons),
            'raw_samples_per_bucket':dict(collections.Counter(x[3] for x in comparisons)),
            'max_abs_api_vs_raw_mean_bps':round(max((abs(x[1]-x[2]) for x in comparisons),default=0),6)})
        focus_from = int(datetime.datetime(2026,10,30,13,45,tzinfo=TZ).timestamp()*1000)
        focus_to = int(datetime.datetime(2026,10,30,14,25,tzinfo=TZ).timestamp()*1000)
        emit('FOCUS_POINTS', {'kind':kind,'direction':direction,'period':1,
            'points':[{'time':local(t),'bps':raw[t]} for t in sorted(raw) if focus_from<=t<=focus_to]})
    aligned = sorted(set(total)&set(public))
    worst = sorted(aligned, key=lambda t:total[t]-public[t])[:5]
    emit('WORST_WINDOWS', {'direction':direction,'points':[{'time':local(t),'total_bps':total[t],
        'public_bps':public[t],'difference_bps':total[t]-public[t]} for t in worst]})
PY
