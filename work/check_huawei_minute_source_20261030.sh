set -eu
docker exec -i cloud-resource-monitor-cloud-monitor-1 python - <<'PY'
import datetime,json,logging,statistics
from cloud_monitor.config import load_config
from cloud_monitor.providers import make_collector
from cloud_monitor.providers.huawei_network_scope import HuaweiIntranetCollector
from cloud_monitor.diagnostics import safe_collection_error
logging.disable(logging.CRITICAL)
tz=datetime.timezone(datetime.timedelta(hours=8))
rid,region='821b48c2-5199-41d6-823b-cfc2e262e159','cn-east-3'
def emit(kind,value):
    print(kind+' '+json.dumps(value,ensure_ascii=False),flush=True)
cfg=load_config('/app/config/config.yaml')
job=next(j for j in cfg['jobs'] if j['id']=='huawei-ecs-network')
account=next(a for a in cfg['accounts'] if a['id']==job['account'])
if not job.get('enabled',True) or not account.get('enabled',False):
    raise SystemExit('Diagnostic target disabled')
probe=HuaweiIntranetCollector(make_collector(account))
start=int(datetime.datetime(2026,10,30,2,25,tzinfo=tz).timestamp()*1000)
end=int(datetime.datetime(2026,10,30,14,25,tzinfo=tz).timestamp()*1000)-1
for metric in ('net_bitRecv','net_bitSent'):
    try:
        body=probe._request('ces',region,{'namespace':'AGT.ECS','metric_name':metric,'dim.0':'instance_id,'+rid,
            'period':60,'filter':'average','from':start,'to':end})
        points=body.get('datapoints',[])
        emit('AGENT_MINUTE_SOURCE',{'metric':metric,'points':len(points),'units':list(set(p.get('unit') for p in points)),
            'response_keys':sorted(k for k in body if k in ('datapoints','message','code'))})
    except Exception as exc:
        emit('AGENT_QUERY_ERROR',{'metric':metric,'detail':safe_collection_error(exc)})
metadata,mappings=probe._inventory('ecs',region,job['options'],datetime.datetime.now(datetime.timezone.utc))
eips=mappings[rid]
for direction in ('in','out'):
    body=probe._request('ces',region,{'namespace':'SYS.ECS','metric_name':'network_vm_bandwidth_'+direction,
        'dim.0':'instance_id,'+rid,'period':1,'filter':'average','from':start,'to':end})
    total={int(p['timestamp']):p['average']*8 for p in body.get('datapoints',[])}
    publics=[]
    for eip in eips:
        body=probe._request('ces',region,{'namespace':'SYS.VPC','metric_name':'downstream_bandwidth' if direction=='in' else 'upstream_bandwidth',
            'dim.0':'publicip_id,'+eip['id'],'period':60,'filter':'average','from':start,'to':end+60000})
        publics.append({int(p['timestamp']):p['average'] for p in body.get('datapoints',[])})
    for mode in ('same_minute','nearest_minute','next_minute','previous_minute'):
        diffs=[]
        missing=0
        for t,value in total.items():
            base=t//60000*60000
            target={'same_minute':base,'nearest_minute':(t+30000)//60000*60000,
                'next_minute':base+60000,'previous_minute':base-60000}[mode]
            if not all(target in p for p in publics):
                missing+=1
                continue
            diffs.append(value-sum(p[target] for p in publics))
        emit('RAW_MATCH_COMPARISON',{'direction':direction,'mode':mode,'pairs':len(diffs),'missing':missing,
            'negative':sum(d<0 for d in diffs),'minimum_bps':min(diffs) if diffs else None})
PY
