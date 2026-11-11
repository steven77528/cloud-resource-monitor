set -eu
docker exec -i cloud-resource-monitor-cloud-monitor-1 python - <<'PY'
import collections
import datetime
import json
import logging
import statistics
from cloud_monitor.config import load_config
from cloud_monitor.diagnostics import safe_collection_error
from cloud_monitor.providers import make_collector
from cloud_monitor.providers.huawei_network_scope import HuaweiIntranetCollector

logging.disable(logging.CRITICAL)
tz = datetime.timezone(datetime.timedelta(hours=8))
rid, region = '821b48c2-5199-41d6-823b-cfc2e262e159', 'cn-east-3'
def emit(kind, value):
    print(kind + ' ' + json.dumps(value, ensure_ascii=False), flush=True)
def stamp(value):
    return datetime.datetime.fromtimestamp(value/1000,tz).isoformat(timespec='seconds')
cfg = load_config('/app/config/config.yaml')
job = next(j for j in cfg['jobs'] if j['id']=='huawei-ecs-network')
account = next(a for a in cfg['accounts'] if a['id']==job['account'])
if not job.get('enabled',True) or not account.get('enabled',False):
    raise SystemExit('Diagnostic target disabled')
probe = HuaweiIntranetCollector(make_collector(account))
metadata,mappings = probe._inventory('ecs',region,job['options'],datetime.datetime.now(datetime.timezone.utc))
eips = mappings[rid]
start = int(datetime.datetime(2026,10,30,2,25,tzinfo=tz).timestamp()*1000)
end = int(datetime.datetime(2026,10,30,14,25,tzinfo=tz).timestamp()*1000)-1
sources = {}
for direction in ('in','out'):
    specs = [('ecs',rid,'SYS.ECS','network_vm_bandwidth_'+direction,'instance_id',1,8)]
    specs.extend(('eip',e['id'],'SYS.VPC','downstream_bandwidth' if direction=='in' else 'upstream_bandwidth',
                  'publicip_id',60,1) for e in eips)
    for kind,resource,namespace,name,dimension,period,multiplier in specs:
        body = probe._request('ces',region,{'namespace':namespace,'metric_name':name,'dim.0':dimension+','+resource,
            'period':period,'filter':'average','from':start,'to':end})
        points = body.get('datapoints',[])
        sources[(kind,resource,direction)] = {int(p['timestamp']):p['average']*multiplier for p in points}
        emit('INPUT_SERIES',{'kind':kind,'direction':direction,'period':period,'samples':len(points)})
groups = {}
for direction in ('in','out'):
    buckets = collections.defaultdict(list)
    for t,v in sources[('ecs',rid,direction)].items():
        buckets[t//300000*300000].append((t,v))
    groups[direction] = buckets
results = []
for bucket in sorted(set(groups['in'])&set(groups['out'])):
    selected = {d:max(groups[d][bucket]) for d in ('in','out')}
    minutes = {d:selected[d][0]//60000*60000 for d in ('in','out')}
    if minutes['in'] != minutes['out']:
        results.append({'bucket':stamp(bucket),'status':'missing_match'})
        continue
    try:
        aligned = {d:selected[d][1]-sum(sources[('eip',e['id'],d)][minutes[d]] for e in eips) for d in ('in','out')}
        old = {d:statistics.mean(v for _,v in groups[d][bucket])-sum(statistics.mean(
            value for t,value in sources[('eip',e['id'],d)].items() if bucket<=t<bucket+300000) for e in eips) for d in ('in','out')}
        results.append({'bucket':stamp(bucket),'status':'matched','minute':stamp(minutes['in']),
            'aligned':aligned,'old':old,'ecs_times':{d:stamp(selected[d][0]) for d in ('in','out')}})
    except KeyError:
        results.append({'bucket':stamp(bucket),'status':'missing_match'})
emit('COMPARISON_SUMMARY',{'from':stamp(start),'to':stamp(end),'buckets':len(results),
    'missing_match':sum(r['status']!='matched' for r in results),
    'old_negative_buckets':sum(any(v<0 for v in r['old'].values()) for r in results if r['status']=='matched'),
    'aligned_negative_buckets':sum(any(v<0 for v in r['aligned'].values()) for r in results if r['status']=='matched')})
emit('FOCUS_RESULT',[r for r in results if r['bucket']=='2026-10-30T14:10:00+08:00'])
emit('REMAINING_INVALID',[r for r in results if r['status']!='matched' or any(v<0 for v in r['aligned'].values())])
PY
