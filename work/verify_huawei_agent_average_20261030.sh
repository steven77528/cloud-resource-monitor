set -eu
docker exec -i cloud-resource-monitor-cloud-monitor-1 python - <<'PY'
import collections,datetime,json,logging,math,statistics
from cloud_monitor.config import load_config
from cloud_monitor.providers import make_collector
from cloud_monitor.providers.huawei_network_scope import HuaweiIntranetCollector
from cloud_monitor.diagnostics import safe_collection_error
from huaweicloudsdkces.v1 import ListMetricsRequest
logging.disable(logging.CRITICAL)
tz=datetime.timezone(datetime.timedelta(hours=8))
rid,region='821b48c2-5199-41d6-823b-cfc2e262e159','cn-east-3'
def emit(kind,value):
    print(kind+' '+json.dumps(value,ensure_ascii=False),flush=True)
def local(t):
    return datetime.datetime.fromtimestamp(t/1000,tz).isoformat(timespec='seconds')
def get(row,key,default=None):
    return row.get(key,default) if isinstance(row,dict) else getattr(row,key,default)
cfg=load_config('/app/config/config.yaml')
job=next(j for j in cfg['jobs'] if j['id']=='huawei-ecs-network')
account=next(a for a in cfg['accounts'] if a['id']==job['account'])
if not job.get('enabled',True) or not account.get('enabled',False):
    raise SystemExit('Diagnostic target disabled')
collector=make_collector(account)
probe=HuaweiIntranetCollector(collector)
metadata,mappings=probe._inventory('ecs',region,job['options'],datetime.datetime.now(datetime.timezone.utc))
for name in ('net_bitRecv','net_bitSent'):
    found=set()
    markers=set()
    marker=None
    shapes=collections.Counter()
    for _ in range(20):
        response=collector._call('ces',region,'list_metrics',ListMetricsRequest(namespace='AGT.ECS',metric_name=name,limit=1000,start=marker))
        for row in get(response,'metrics',[]) or []:
            dims=get(row,'dimensions',[]) or []
            shapes[tuple(get(d,'name') for d in dims)]+=1
            found.update(get(d,'value') for d in dims if get(d,'name')=='instance_id')
        marker=get(get(response,'meta_data'),'marker')
        if not marker or marker in markers:
            break
        markers.add(marker)
    emit('AGENT_INVENTORY',{'metric':name,'ecs_total':len(metadata),'covered':len(set(metadata)&found),
        'missing':[{'id':i,'name':metadata[i]['name']} for i in sorted(set(metadata)-found)],
        'dimension_shapes':[{'names':k,'count':v} for k,v in shapes.items()]})
start=int(datetime.datetime(2026,10,30,2,25,tzinfo=tz).timestamp()*1000)
end=int(datetime.datetime(2026,10,30,14,25,tzinfo=tz).timestamp()*1000)-1
series={}
specs=[('agent_recv','AGT.ECS','net_bitRecv','instance_id',rid,60,1),
       ('agent_sent','AGT.ECS','net_bitSent','instance_id',rid,60,1),
       ('sys_in','SYS.ECS','network_vm_bandwidth_in','instance_id',rid,1,8),
       ('sys_out','SYS.ECS','network_vm_bandwidth_out','instance_id',rid,1,8)]
for eip in mappings[rid]:
    specs.extend([('public_in','SYS.VPC','downstream_bandwidth','publicip_id',eip['id'],60,1),
                  ('public_out','SYS.VPC','upstream_bandwidth','publicip_id',eip['id'],60,1)])
for key,namespace,name,dimension,resource,period,factor in specs:
    body=probe._request('ces',region,{'namespace':namespace,'metric_name':name,'dim.0':dimension+','+resource,
        'period':period,'filter':'average','from':start,'to':end})
    points=body.get('datapoints',[])
    values={int(p['timestamp']):p['average']*factor for p in points}
    if key.startswith('public_') and key in series:
        values={t:values[t]+series[key][t] for t in set(values)&set(series[key])}
    series[key]=values
    emit('SOURCE_SUMMARY',{'key':key,'samples':len(values),'units':list(set(p.get('unit') for p in points)),
        'mean_bps':statistics.mean(values.values()) if values else None})
def buckets(values):
    result=collections.defaultdict(list)
    for t,v in values.items():
        result[t//300000*300000].append(v)
    return {t:statistics.mean(v) for t,v in result.items() if len(v)==5}
aggregated={key:buckets(value) for key,value in series.items() if not key.startswith('sys_')}
def corr(xs,ys):
    pairs=list(zip(xs,ys))
    if len(pairs)<3:
        return None
    mx,my=statistics.mean(xs),statistics.mean(ys)
    den=math.sqrt(sum((x-mx)**2 for x in xs)*sum((y-my)**2 for y in ys))
    return sum((x-mx)*(y-my) for x,y in pairs)/den if den else None
for name in ('agent_recv','agent_sent'):
    for direction in ('in','out'):
        public=aggregated['public_'+direction]
        agent=aggregated[name]
        common=sorted(set(public)&set(agent))
        diff=[agent[t]-public[t] for t in common]
        emit('AGENT_PUBLIC_COMPARISON',{'agent':name,'public_direction':direction,'buckets':len(common),
            'negative':sum(v<0 for v in diff),'min_difference_bps':min(diff) if diff else None,
            'correlation':corr([agent[t] for t in common],[public[t] for t in common])})
    for direction in ('in','out'):
        total=series['sys_'+direction]
        pairs=[(v,aggregated[name][t//300000*300000]) for t,v in total.items() if t//300000*300000 in aggregated[name]]
        emit('AGENT_SYSTEM_DIRECTION',{'agent':name,'sys_direction':direction,'correlation':corr([v[0] for v in pairs],[v[1] for v in pairs])})
old_negative=set()
new_negative=set()
for t,value in series['sys_out'].items():
    bucket=t//300000*300000
    minute=t//60000*60000
    if bucket in aggregated['public_out'] and value-aggregated['public_out'][bucket]<0:
        old_negative.add(bucket)
    if minute in series['public_out'] and value-series['public_out'][minute]<0:
        new_negative.add(bucket)
emit('NEGATIVE_SET_TRANSITION',{'old_negative':len(old_negative),'same_minute_negative':len(new_negative),
    'retained':len(old_negative&new_negative),'resolved':len(old_negative-new_negative),'introduced':len(new_negative-old_negative),
    'introduced_times':[local(t) for t in sorted(new_negative-old_negative)]})
for direction,agent_key in (('out','agent_recv'),('in','agent_sent')):
    agent=series[agent_key]
    public=series['public_'+direction]
    comparisons=[]
    for shift in range(-300,301,60):
        common=sorted(t for t in agent if t+shift*1000 in public)
        pairs=[(agent[t],public[t+shift*1000]) for t in common]
        diffs=[a-b for a,b in pairs]
        grouped=collections.defaultdict(list)
        for t,diff in zip(common,diffs):
            grouped[t//300000*300000].append(diff)
        five=[statistics.mean(v) for v in grouped.values() if len(v)==5]
        comparisons.append({'public_shift_seconds':shift,'minute_pairs':len(common),
            'correlation':corr([p[0] for p in pairs],[p[1] for p in pairs]),
            'negative_minutes':sum(d<0 for d in diffs),'full_5min_buckets':len(five),
            'negative_5min_buckets':sum(d<0 for d in five),'min_5min_difference_bps':min(five) if five else None})
    emit('AGENT_MINUTE_LAG_COMPARISON',{'direction':direction,'relation':'agent[t] vs public[t+shift]', 'results':comparisons})
focus_start=int(datetime.datetime(2026,10,30,13,55,tzinfo=tz).timestamp()*1000)
focus_end=int(datetime.datetime(2026,10,30,14,15,tzinfo=tz).timestamp()*1000)
emit('BURST_TIMELINE',[{'time':local(t),'agent_out_bps':series['agent_recv'].get(t),'public_out_bps':series['public_out'].get(t)}
    for t in sorted(set(series['agent_recv'])&set(series['public_out'])) if focus_start<=t<=focus_end])
for hour,minute in ((8,45),(9,5),(14,10)):
    t=int(datetime.datetime(2026,10,30,hour,minute,tzinfo=tz).timestamp()*1000)
    emit('WINDOW_DETAIL',{'bucket':local(t),'values':{key:[{'time':local(stamp),'bps':value} for stamp,value in sorted(values.items()) if t<=stamp<t+300000] for key,values in series.items()}})
PY
