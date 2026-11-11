from datetime import timedelta
import json
import logging
import threading
import time

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.executors.pool import ThreadPoolExecutor

from .diagnostics import safe_collection_error
from .engine import Engine
from .models import Metric, utcnow
from .notifiers import NotificationDisabled, Notifier, sms_enabled
from .receipts import collect_receipts
from .resource_selection import network_estimate_key, network_resource_selected
from .providers import EXTERNAL_PROVIDERS, collect_job, make_collector
from .providers.common import ProviderError
from .providers.aliyun import SmsStatisticsInsufficientData


LOG = logging.getLogger(__name__)
COLLECTION_RULE = {
    "id": "collection-error", "metric": "collection_failed", "summary": "云资源采集失败，请检查权限和接口",
    "levels": [{"severity": "P2", "op": "ge", "threshold": 1}],
}
COLLECTION_NAMES = {
    "ecs_expiry": "ECS 云服务器到期采集",
    "rds_expiry": "RDS 数据库到期采集",
    "rds_performance": "RDS 数据库性能采集",
    "rds_cloudmonitor": "RDS 数据库云监控指标采集",
    "rds_sql": "RDS 数据库只读统计采集",
    "network_traffic": "云资源网络速率采集",
    "slb_expiry": "负载均衡到期采集",
    "domains": "域名到期采集",
    "certificates": "SSL 证书到期采集",
    "packages": "资源包用量与到期采集",
    "balance": "账号余额采集",
    "metrics": "云资源指标采集",
    "sms_statistics": "短信发送量统计采集",
    "sms_receipts": "短信回执统计采集",
    "billing_snapshot": "费用快照采集",
    "resource_packages_month": "资源包当月额度采集",
    "resource_packages": "资源包有效期采集",
    "kodo_usage": "Kodo 标准存储用量采集",
    "cdn_usage": "CDN 流量与带宽采集",
    "cdn_status": "CDN HTTP 状态码采集",
    "settlements": "汇付出金查询",
    "local_certificate": "本地 X.509 证书有效期采集",
    "company_verification": "爱签企业认证状态采集",
    "company_verification_snapshot": "爱签企业认证结果文件采集",
    "package_ledger": "爱签套餐台账采集",
    "api_quota": "微信关键 API 日额度采集",
    "charge_packages": "微信付费能力资源包采集",
    "annual_review": "微信认证年审截止时间采集",
    "js_errors": "小程序 JS 错误采集",
    "performance": "小程序每日性能采集",
}
METRIC_RESOURCES = {
    "acs_oss_dashboard": "OSS 对象存储",
    "acs_slb_dashboard": "CLB 负载均衡",
    "acs_alb": "ALB 负载均衡",
    "acs_alb_dashboard": "ALB 负载均衡",
    "acs_nlb": "NLB 负载均衡",
    "acs_vpc_eip": "EIP 弹性公网 IP",
    "SYS.ELB": "ELB 负载均衡",
    "SYS.VPC": "公网 IP 与带宽",
    "SYS.OBS": "OBS 对象存储",
}


def _collection_name(job):
    kind = job["kind"]
    options = job.get("options", {})
    name = COLLECTION_NAMES.get(kind, "云资源采集")
    if kind == "network_traffic":
        name = {"ecs": "ECS 网络速率采集", "rds": "RDS 网络吞吐采集"}.get(options.get("product"), name)
    resources = []
    if kind == "metrics":
        entries = options.get("metrics", [])
        categories = list(dict.fromkeys(METRIC_RESOURCES.get(entry.get("namespace"), "云资源")
                                        for entry in entries))
        if categories:
            name = "、".join(categories) + "指标采集"
        for entry in entries:
            dimensions = entry.get("dimensions", {})
            resource_id = entry.get("resource_id") or next((dimensions[key] for key in
                ("BucketName", "instanceId", "loadBalancerId", "bandwidth_id", "publicip_id", "lbaas_instance_id")
                if dimensions.get(key)), "")
            resource_name = entry.get("resource_name")
            label = str(resource_name or resource_id)
            if resource_name and resource_id and str(resource_name) != str(resource_id):
                label += f" ({resource_id})"
            if label:
                resources.append(label)
    else:
        for instance in options.get("instances", []):
            resources.append(str(instance.get("resource_name") or instance.get("resource_id") or ""))
        for key in ("resource_ids", "certificate_ids", "domain_names", "package_ids"):
            resources.extend(str(value) for value in options.get(key, []) if value)
    resources = list(dict.fromkeys(resources))
    if resources:
        scope = "、".join(resources[:3])
        if len(resources) > 3:
            scope += f" 等 {len(resources)} 个资源"
        name += f"（配置范围：{scope}）"
    return name


class Service:
    def __init__(self, config, store, collector_factory=None, notifier=None):
        self.config = config
        self.store = store
        self.engine = Engine(store, config)
        if notifier is not None:
            self.notifier = notifier
        elif "gateway" in config["notifications"]:
            from .notification_client import NotificationClient
            self.notifier = NotificationClient(config["notifications"]["gateway"])
        else:
            self.notifier = Notifier(config["notifications"])
        if not sms_enabled(config["notifications"]):
            self.store.skip_channel("sms")
        self.accounts = {a["id"]: a for a in config["accounts"]}
        self.jobs = [j for j in config["jobs"] if j.get("enabled", True)
                     and self.accounts[j["account"]].get("enabled", False)]
        self.store.reconcile_rule_alerts(config["jobs"], config["rules"],
                                         system_rule_ids={COLLECTION_RULE["id"]})
        self.store.reconcile_resource_alerts(config["jobs"])
        self.collector_factory = collector_factory or self._collector
        self.collectors = {}
        self.collectors_lock = threading.Lock()
        self.dispatch_lock = threading.Lock()
        self.last_tick = utcnow()
        self.started = False
        self.scheduler = BackgroundScheduler(
            timezone="UTC", executors={"default": ThreadPoolExecutor(max_workers=8)},
            job_defaults={"max_instances": 1, "coalesce": True, "misfire_grace_time": 60},
        )

    @staticmethod
    def _collector(account):
        return make_collector(account)

    def collect(self, job, now=None):
        now = now or utcnow()
        account = self.accounts[job["account"]]
        external = account["provider"] in EXTERNAL_PROVIDERS
        instrumented = external and job["kind"] not in {
            "package_ledger", "company_verification_snapshot", "local_certificate"}
        query_performed = True
        estimated = (account["provider"] == "huawei" and job["kind"] == "network_traffic"
                     and job.get("options", {}).get("scope") == "intranet_estimated")
        collector = None
        quality = {}
        quality_recorded = False
        started = time.perf_counter()
        try:
            if job["kind"] == "sms_receipts":
                metrics = collect_receipts(self.store, account, job.get("options", {}), now)
            elif external or job["kind"] in {"rds_performance", "rds_cloudmonitor", "rds_sql", "network_traffic"}:
                # One collector per job preserves metadata caches without sharing SDK clients across jobs.
                with self.collectors_lock:
                    if job["id"] not in self.collectors:
                        collector = self.collector_factory(account)
                        if job["kind"] == "rds_sql":
                            collector.state_store = self.store
                        self.collectors[job["id"]] = collector
                    collector = self.collectors[job["id"]]
                metrics = collect_job(collector, job, now)
                query_performed = getattr(collector, "query_performed", True)
            else:
                metrics = self.collector_factory(account).collect(job["kind"], job.get("options", {}), now)
            if estimated:
                quality = self._network_estimate_quality(job, collector, metrics, now)
                quality_recorded = True
                metrics = [metric for metric in metrics if self._estimate_sample_available(metric, quality)]
            unavailable = sum(entry["valid_observed_at"] <= entry["invalid_observed_at"]
                              for entry in quality.values())
            sample_count = len(metrics)
            if external and not metrics:
                raise ProviderError("no_data")
            if instrumented and query_performed:
                metrics = [*metrics, *self._query_metrics(job, account, True, now, time.perf_counter() - started)]
            evaluation_job = ({**job, "_collection_failure_at": self.store.last_collection_failure(job["id"])}
                              if external else job)
            if job["kind"] in {"rds_performance", "rds_cloudmonitor"}:
                self.engine.evaluate_batch(job, metrics, now)
            else:
                for metric in metrics:
                    metric_job = evaluation_job
                    if estimated:
                        entry = quality.get(network_estimate_key(metric.resource_id, metric.dimensions.get("region")), {})
                        metric_job = {**job, "_collection_failure_at": entry.get("invalid_observed_at", 0)}
                    self.engine.evaluate(metric_job, metric, now)
            self.store.save_samples(job["id"], metrics)
            failures = getattr(collector, "network_partial_failures", 0) if job["kind"] == "network_traffic" else 0
            status = "error" if failures else "degraded" if unavailable else "ok" if sample_count else "no_data"
            detail = f"Network collection partially failed; failed_resources={failures}" if failures else ""
            if unavailable:
                detail = (detail + "; " if detail else "") + f"Network estimate unavailable; unavailable_resources={unavailable}"
            self.store.job_status(job["id"], status, now, sample_count, detail=detail)
            self._collection_health(job, account, bool(failures), now)
            if failures:
                LOG.error("collection job=%s status=error samples=%s failed_resources=%s",
                          job["id"], sample_count, failures)
            else:
                LOG.info("collection job=%s status=%s samples=%s", job["id"], status, sample_count)
            return not failures
        except SmsStatisticsInsufficientData as exc:
            detail = safe_collection_error(exc)
            self.store.job_status(job["id"], "insufficient_data", now, detail=detail)
            self._collection_health(job, account, False, now)
            LOG.info("collection job=%s status=insufficient_data samples=0 detail=%s", job["id"], detail)
            return True
        except Exception as exc:
            if estimated and collector is not None and not quality_recorded:
                self._network_estimate_quality(job, collector, [], now)
            error = safe_collection_error(exc)
            if external:
                self.store.mark_collection_failure(job["id"], now)
            if instrumented:
                health = self._query_metrics(job, account, False, now, time.perf_counter() - started,
                                             signature_failed=isinstance(exc, ProviderError)
                                             and exc.reason == "signature_invalid")
                for metric in health:
                    self.engine.evaluate(job, metric, now)
                self.store.save_samples(job["id"], health)
            self.store.job_status(job["id"], "error", now, detail=error)
            self._collection_health(job, account, True, now)
            LOG.error("collection job=%s failed error_type=%s detail=%s", job["id"], type(exc).__name__, error)
            return False

    def _network_estimate_quality(self, job, collector, metrics, now):
        resources = self.store.update_network_estimate_quality(
            job["id"], getattr(collector, "network_estimate_unavailable", []), metrics, now,
            active_resources=getattr(collector, "network_estimate_resources", None))
        return {key: entry for key, entry in resources.items() if entry.get("in_scope", True) and network_resource_selected(job, {
            "account": job["account"], "resource_id": entry["resource_id"], "metric": "network_out_bps",
            "dimensions": {"region": entry["region"], "scope": "intranet_estimated"}})}

    @staticmethod
    def _estimate_sample_available(metric, quality):
        entry = quality.get(network_estimate_key(metric.resource_id, metric.dimensions.get("region")))
        return not entry or (entry["valid_observed_at"] > entry["invalid_observed_at"]
                             and metric.observed_at.timestamp() > entry["invalid_observed_at"])

    @staticmethod
    def _query_metrics(job, account, success, now, elapsed, signature_failed=False):
        values = [("api_query_success", int(success), "bool"),
                  ("api_query_duration_seconds", elapsed, "seconds")]
        if success:
            values.append(("api_last_success_timestamp_seconds", now.timestamp(), "seconds"))
        if account["provider"] == "huifu" and (success or signature_failed):
            values.append(("api_signature_failed", int(signature_failed), "bool"))
        return [Metric(account["provider"], account["id"], job["id"], _collection_name(job), name, value,
                       unit, now, {"source": "local_probe", "product": "api", "scope": job["kind"]})
                for name, value, unit in values]

    def _collection_health(self, job, account, failed, now):
        metric = Metric(account["provider"], account["id"], job["id"], _collection_name(job),
                        "collection_failed", int(failed), "bool", now,
                        {"collection_kind": job["kind"]})
        rule = {**COLLECTION_RULE, "levels": [{**COLLECTION_RULE["levels"][0],
                                               "for_samples": job.get("collection_error_samples", 1)}]}
        self.engine.evaluate(job, metric, now, rules=[rule])

    def dispatch(self, now=None):
        now = now or utcnow()
        self.last_tick = now
        if not self.dispatch_lock.acquire(blocking=False):
            return
        try:
            if not sms_enabled(self.config["notifications"]):
                self.store.skip_channel("sms")
            for message in self.store.due_messages(now):
                with self.store.delivery_lock:
                    if not self.store.still_pending(message["id"]):
                        continue
                # A slow notification must not hold the lock used to persist collection results.
                try:
                    self.notifier.send(message["channel"], json.loads(message["payload"]))
                except NotificationDisabled:
                    self.store.skipped(message["id"])
                    LOG.info("notification id=%s skipped channel_disabled", message["id"])
                except Exception as exc:
                    error = type(exc).__name__
                    self.store.delivery_failed(message["id"], now, self.config["service"]["retry_max_attempts"], error)
                    LOG.error("notification id=%s channel=%s failed error_type=%s", message["id"], message["channel"], error)
                else:
                    self.store.delivered(message["id"])
                    LOG.info("notification id=%s channel=%s accepted", message["id"], message["channel"])
        finally:
            self.dispatch_lock.release()

    def run_once(self, send=False):
        success = True
        for job in self.jobs:
            success = self.collect(job) and success
        if send:
            self.dispatch()
        return success

    def start(self):
        for index, job in enumerate(self.jobs):
            self.scheduler.add_job(self.collect, "interval", args=[job], id=job["id"],
                                   seconds=job["interval_seconds"],
                                   next_run_time=utcnow() + timedelta(seconds=index % 30))
        self.scheduler.add_job(self.dispatch, "interval", seconds=15, id="_dispatch", next_run_time=utcnow())
        self.scheduler.add_job(self._heartbeat, "interval", seconds=15, id="_heartbeat", next_run_time=utcnow())
        self.scheduler.add_job(lambda: self.store.prune(utcnow(), self.config["service"]["retention_days"]),
                               "interval", hours=24, id="_prune", next_run_time=utcnow())
        self.scheduler.start()
        self.started = True

    def stop(self):
        self.started = False
        self.scheduler.shutdown(wait=True)

    def _heartbeat(self):
        self.last_tick = utcnow()

    def status(self):
        status = self.store.status()
        active_ids = {j["id"] for j in self.jobs}
        status["jobs"] = [j for j in status["jobs"] if j["id"] in active_ids]
        now = utcnow().timestamp()
        by_id = {j["id"]: j for j in status["jobs"]}
        for job in self.jobs:
            state = by_id.get(job["id"])
            if state is None:
                status["jobs"].append({"id": job["id"], "status": "waiting", "updated_at": None})
            elif now - state["updated_at"] > job["interval_seconds"] * 2.5 + 60:
                state["status"] = "stale"
        status["scheduler_alive"] = self.started and (utcnow() - self.last_tick).total_seconds() < 120
        if "gateway" in self.config["notifications"]:
            status["notifications"] = {"mode": "gateway"}
        else:
            status["notifications"] = {"sms": {"enabled": sms_enabled(self.config["notifications"]),
                                               "provider": self.config["notifications"].get("sms", {}).get(
                                                   "provider", "aliyun")}}
        return status
