set -eu
docker exec -i cloud-resource-monitor-cloud-monitor-1 python - <<'PY'
import collections
import datetime
import json
import logging
import time

import pymysql
from cloud_monitor.config import load_config
from cloud_monitor.models import utcnow
from cloud_monitor.providers import make_collector
from cloud_monitor.providers.huawei_network_scope import HuaweiIntranetCollector, _RDS_UNITS, _BIT_UNITS

logging.disable(logging.CRITICAL)
TZ = datetime.timezone(datetime.timedelta(hours=8))
cfg = load_config('/app/config/config.yaml')
accounts = {a['id']: a for a in cfg['accounts']}
jobs = [j for j in cfg['jobs'] if j['kind'] == 'network_traffic']
target = {j['id'] for j in jobs if accounts[j['account']]['provider'] == 'aliyun'}


def emit(kind, data):
    print(kind + ' ' + json.dumps(data, ensure_ascii=False), flush=True)


def local(stamp):
    return datetime.datetime.fromtimestamp(stamp, TZ).isoformat(timespec='seconds')


db = cfg['service']['database']
connection = pymysql.connect(host=db.get('host','mysql'),port=int(db.get('port',3306)),user=db['user'],
    password=db['password'],database=db.get('name','cloud_monitor'),charset='utf8mb4',connect_timeout=10,
    read_timeout=30,cursorclass=pymysql.cursors.DictCursor,autocommit=False)
with connection.cursor() as cursor:
    cursor.execute('SET TRANSACTION READ ONLY')
    cursor.execute('START TRANSACTION WITH CONSISTENT SNAPSHOT')
    cursor.execute("SELECT created_at,status,payload FROM outbox WHERE created_at >= %s "
        "AND JSON_UNQUOTE(JSON_EXTRACT(payload,'$.rule_id'))='collection-error' ORDER BY created_at",(time.time()-7*86400,))
    events = []
    for row in cursor.fetchall():
        event = json.loads(row['payload'])
        if event.get('resource_id') in target:
            events.append({'at':local(row['created_at']), 'job':event['resource_id'],
                'phase':event['phase'], 'status':row['status']})
    emit('ALIYUN_NETWORK_MAIL_TIMELINE_7D', events)
    emit('ALIYUN_NETWORK_MAIL_BY_DAY', [{'day':day,'job':job,'phase':phase,'count':count}
        for (day,job,phase),count in sorted(collections.Counter((e['at'][:10],e['job'],e['phase']) for e in events).items())])
connection.rollback()
connection.close()

# Recheck the one directional record omitted by the first tool output's display limit.
job = next(j for j in jobs if j['id']=='huawei-rds-network')
probe = HuaweiIntranetCollector(make_collector(accounts[job['account']]))
region, rid = 'cn-east-3', '3425739cca19444894ff68389deea829in01'
metadata,mappings = probe._inventory('rds',region,job['options'],utcnow())
start = int(datetime.datetime(2026,10,29,15,20,tzinfo=TZ).timestamp()*1000)
end = int(datetime.datetime(2026,10,30,15,20,tzinfo=TZ).timestamp()*1000)-1


def series(namespace,metric,dimension,source,units):
    body = probe._request('ces',region,{'namespace':namespace,'metric_name':metric,'dim.0':dimension+','+source,
        'period':300,'filter':'average','from':start,'to':end})
    return {int(p['timestamp']):p['average']*units[p['unit']] for p in body['datapoints']}


total = series('SYS.RDS','rds005_bytes_out','rds_cluster_id',rid,_RDS_UNITS)
publics = [series('SYS.VPC','upstream_bandwidth','publicip_id',e['id'],_BIT_UNITS) for e in mappings[rid]]
common = set(total).intersection(*(set(p) for p in publics))
differences = [total[t]-sum(p[t] for p in publics) for t in common]
emit('HUAWEI_RDS_PMS_OUT_RECHECK',{'name':metadata[rid]['name'],'common_buckets':len(common),
    'negative_buckets':sum(v<0 for v in differences),'minimum_difference_bps':min(differences)})
emit('FOLLOWUP_COMPLETE',{'read_only':True,'configuration_changed':False})
PY
