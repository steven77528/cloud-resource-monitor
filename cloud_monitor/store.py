from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
import threading

from sqlalchemy import (BigInteger, Column, Double, Index, Integer, MetaData, String,
                        Table, Text, UniqueConstraint, case, create_engine, delete, func,
                        insert, select, update)
from sqlalchemy.dialects.mysql import LONGTEXT, insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import StaticPool

from .alerting import resource_alerts_enabled
from .resource_selection import network_estimate_key


METADATA = MetaData()
PAYLOAD = Text().with_variant(LONGTEXT(), "mysql")
TABLE_OPTIONS = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_bin", "mysql_engine": "InnoDB"}
STATES = Table("states", METADATA,
               Column("key", String(64), primary_key=True),
               Column("payload", PAYLOAD, nullable=False), **TABLE_OPTIONS)
SAMPLES = Table("samples", METADATA,
                Column("id", BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True),
                Column("job_id", String(191), nullable=False),
                Column("metric_key", String(64), nullable=False),
                Column("observed_at", Double, nullable=False),
                Column("payload", PAYLOAD, nullable=False),
                UniqueConstraint("job_id", "metric_key", "observed_at", name="sample_identity"),
                Index("sample_time", "observed_at"), **TABLE_OPTIONS)
LATEST_SAMPLES = Table("latest_samples", METADATA,
                       Column("job_id", String(191), primary_key=True),
                       Column("metric_key", String(64), primary_key=True),
                       Column("observed_at", Double, nullable=False),
                       Column("payload", PAYLOAD, nullable=False),
                       Index("latest_sample_time", "observed_at"), **TABLE_OPTIONS)
JOBS = Table("jobs", METADATA,
             Column("id", String(191), primary_key=True),
             Column("status", String(32), nullable=False),
             Column("updated_at", Double, nullable=False),
             Column("sample_count", Integer, nullable=False),
             Column("detail", Text, nullable=False), **TABLE_OPTIONS)
OUTBOX = Table("outbox", METADATA,
               Column("id", String(36), primary_key=True),
               Column("incident_key", String(64), nullable=False),
               Column("channel", String(16), nullable=False),
               Column("payload", PAYLOAD, nullable=False),
               Column("status", String(16), nullable=False, server_default="pending"),
               Column("attempts", Integer, nullable=False, server_default="0"),
               Column("next_attempt", Double, nullable=False),
               Column("created_at", Double, nullable=False),
               Column("last_error", Text),
               Index("outbox_due", "status", "next_attempt"),
               Index("outbox_incident", "incident_key", "status"), **TABLE_OPTIONS)
RECEIPTS = Table("receipts", METADATA,
                 Column("id", String(64), primary_key=True),
                 Column("account", String(191), nullable=False),
                 Column("report_time", Double, nullable=False),
                 Column("success", Integer, nullable=False),
                 Index("receipt_window", "account", "report_time"), **TABLE_OPTIONS)


class Store:
    """MySQL persistence; filesystem paths are retained for isolated offline tests."""

    def __init__(self, database):
        self.delivery_lock = threading.RLock()
        self.receipts_lock = threading.Lock()
        self._lock_connection = None
        self._file_lock = None
        self._memory_lock = None
        self._connection_lock = None
        self.path = None
        if isinstance(database, dict):
            url = URL.create("mysql+pymysql", username=database.get("user"),
                             password=database.get("password"), host=database.get("host", "mysql"),
                             port=int(database.get("port", 3306)), database=database.get("name", "cloud_monitor"),
                             query={"charset": "utf8mb4"})
            timeout = int(database.get("connect_timeout", 10))
        elif isinstance(database, str) and "://" in database:
            url = make_url(database)
            if url.drivername not in {"mysql", "mysql+pymysql"}:
                raise ValueError("Database URL must use mysql+pymysql")
            url = url.set(drivername="mysql+pymysql").update_query_dict({"charset": "utf8mb4"})
            timeout = 10
        else:
            self.path = str(database)
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            url = URL.create("sqlite+pysqlite", database=self.path)
            timeout = 30
        self.dialect = "mysql" if url.get_backend_name() == "mysql" else "sqlite"
        options = {"pool_pre_ping": True, "hide_parameters": True}
        if self.dialect == "mysql":
            options.update(pool_recycle=1800, pool_size=5, max_overflow=10,
                           connect_args={"connect_timeout": timeout, "read_timeout": 30, "write_timeout": 30})
        else:
            options["connect_args"] = {"timeout": timeout, "check_same_thread": False}
            if self.path == ":memory:":
                options["poolclass"] = StaticPool
                self._memory_lock = threading.Lock()
                self._connection_lock = threading.RLock()
        self.engine = create_engine(url, **options)
        self._lock_name = "cloud-resource-monitor:" + hashlib.sha256(
            (url.database or "").encode("utf-8")
        ).hexdigest()[:32]
        try:
            METADATA.create_all(self.engine)
        except SQLAlchemyError:
            self.engine.dispose()
            raise RuntimeError("Database initialization failed; check connectivity and database permissions") from None

    @contextmanager
    def connect(self):
        with self._connection_lock or nullcontext():
            with self.engine.begin() as connection:
                yield connection

    def _upsert(self, connection, table, values, update_columns):
        if self.dialect == "mysql":
            statement = mysql_insert(table).values(values)
            statement = statement.on_duplicate_key_update(
                **{name: statement.inserted[name] for name in update_columns}
            )
        else:
            statement = sqlite_insert(table).values(values)
            statement = statement.on_conflict_do_update(
                index_elements=list(table.primary_key.columns),
                set_={name: statement.excluded[name] for name in update_columns},
            )
        return connection.execute(statement)

    def _insert_unique(self, connection, table, values, identity_columns):
        if self.dialect == "mysql":
            statement = mysql_insert(table).values(values)
            first = identity_columns[0]
            statement = statement.on_duplicate_key_update(**{first: statement.inserted[first]})
        else:
            statement = sqlite_insert(table).values(values).on_conflict_do_nothing(index_elements=identity_columns)
        return connection.execute(statement)

    @contextmanager
    def transaction_state(self, key):
        with self.delivery_lock, self.connect() as connection:
            self._insert_unique(connection, STATES, {"key": key, "payload": "null"}, ["key"])
            payload = connection.execute(
                select(STATES.c.payload).where(STATES.c.key == key).with_for_update()
            ).scalar_one()
            yield connection, json.loads(payload)

    def save_state(self, connection, key, state):
        self._upsert(connection, STATES, {"key": key, "payload": json.dumps(state, ensure_ascii=False)}, ["payload"])

    def mark_collection_failure(self, job_id, now):
        key = hashlib.sha256(f"collection-failure:{job_id}".encode()).hexdigest()
        with self.transaction_state(key) as (connection, state):
            last_failure = max((state or {}).get("last_failure", 0), now.timestamp())
            self.save_state(connection, key, {"last_failure": last_failure})

    def last_collection_failure(self, job_id):
        key = hashlib.sha256(f"collection-failure:{job_id}".encode()).hexdigest()
        with self.connect() as connection:
            payload = connection.execute(select(STATES.c.payload).where(STATES.c.key == key)).scalar_one_or_none()
            return (json.loads(payload) or {}).get("last_failure", 0) if payload else 0

    def update_network_estimate_quality(self, job_id, unavailable, metrics, now, active_resources=None):
        key = hashlib.sha256(f"network-estimate-quality:{job_id}".encode()).hexdigest()
        pairs = {}
        for metric in metrics:
            if (metric.dimensions.get("scope") != "intranet_estimated"
                    or metric.metric not in {"network_in_bps", "network_out_bps"}
                    or not math.isfinite(metric.value) or metric.value < 0):
                continue
            region = metric.dimensions.get("region", "")
            identity = network_estimate_key(metric.resource_id, region)
            pairs.setdefault((identity, metric.observed_at.timestamp()), set()).add(metric.metric)
        with self.transaction_state(key) as (connection, state):
            state = state or {"kind": "network_estimate_quality", "job_id": job_id, "resources": {}}
            resources = state["resources"]
            advanced = {}
            for item in unavailable:
                identity = network_estimate_key(item["resource_id"], item["region"])
                entry = resources.setdefault(identity, {
                    "resource_id": item["resource_id"], "region": item["region"],
                    "invalid_observed_at": 0, "valid_observed_at": 0, "reason": "negative_difference"})
                observed = item["observed_at"].timestamp()
                if observed > entry["invalid_observed_at"]:
                    entry["invalid_observed_at"] = observed
                    advanced[identity] = observed
            for (identity, observed), directions in pairs.items():
                if directions != {"network_in_bps", "network_out_bps"}:
                    continue
                region, resource_id = json.loads(identity)
                entry = resources.setdefault(identity, {
                    "resource_id": resource_id, "region": region, "invalid_observed_at": 0,
                    "valid_observed_at": 0, "reason": "negative_difference"})
                entry["valid_observed_at"] = max(entry["valid_observed_at"], observed)
            if active_resources is not None:
                selected = {network_estimate_key(item["resource_id"], item["region"]) for item in active_resources}
                for identity, entry in resources.items():
                    entry["in_scope"] = identity in selected
            if advanced:
                self._cancel_invalid_estimate_recoveries(connection, job_id, advanced)
            # Retain invalid source timestamps after recovery to break business-rule continuity.
            state["updated_at"] = now.timestamp()
            self.save_state(connection, key, state)
            return resources

    @staticmethod
    def _cancel_invalid_estimate_recoveries(connection, job_id, invalid):
        rows = connection.execute(select(
            OUTBOX.c.id, OUTBOX.c.payload, STATES.c.payload.label("state_payload")
        ).join(STATES, OUTBOX.c.incident_key == STATES.c.key).where(
            OUTBOX.c.status.in_(("pending", "failed"))
        )).mappings()
        cancelled = []
        for row in rows:
            state, event = json.loads(row["state_payload"]), json.loads(row["payload"])
            if (not state or state.get("job_id") != job_id or event.get("phase") != "recovery"
                    or event.get("metric") not in {"network_in_bps", "network_out_bps"}
                    or event.get("dimensions", {}).get("scope") != "intranet_estimated"):
                continue
            identity = network_estimate_key(event["resource_id"], event["dimensions"].get("region"))
            if datetime.fromisoformat(event["observed_at"]).timestamp() <= invalid.get(identity, 0):
                cancelled.append(row["id"])
        if cancelled:
            connection.execute(update(OUTBOX).where(OUTBOX.c.id.in_(cancelled)).values(status="cancelled"))

    def cancel_messages(self, connection, key, statuses=("pending", "failed")):
        connection.execute(update(OUTBOX).where(
            OUTBOX.c.incident_key == key, OUTBOX.c.status.in_(statuses)
        ).values(status="cancelled"))

    @staticmethod
    def _suppress_alerts(connection, keys):
        if not keys:
            return
        connection.execute(update(OUTBOX).where(
            OUTBOX.c.incident_key.in_(keys), OUTBOX.c.status.in_(("pending", "failed"))
        ).values(status="cancelled"))
        connection.execute(delete(STATES).where(STATES.c.key.in_(keys)))

    def suppress_alerts(self, keys):
        if keys:
            with self.delivery_lock, self.connect() as connection:
                self._suppress_alerts(connection, keys)

    def reconcile_rule_alerts(self, jobs, rules, *, system_rule_ids=()):
        rule_ids = {rule["id"] for rule in rules}
        selected = {job["id"]: rule_ids if job.get("rules") is None else rule_ids.intersection(job["rules"])
                    for job in jobs}
        if not selected:
            return
        with self.delivery_lock, self.connect() as connection:
            excluded = []
            for row in connection.execute(select(STATES).with_for_update()).mappings():
                state = json.loads(row["payload"])
                if not state or "metric" not in state:
                    continue
                job_id, rule_id = state.get("job_id"), state.get("rule_id")
                # Built-in health rules are evaluated independently of the job's business rules.
                if (job_id in selected and rule_id and rule_id not in system_rule_ids
                        and rule_id not in selected[job_id]):
                    excluded.append(row["key"])
            self._suppress_alerts(connection, excluded)

    def reconcile_resource_alerts(self, jobs):
        configured = {job["id"]: job for job in jobs if "alerting" in job or (
            job.get("kind") == "network_traffic" and (
                job.get("options", {}).get("scope") == "intranet_estimated" or (
                    job.get("options", {}).get("product") == "ecs"
                    and "instances" in job.get("options", {}))))}
        if not configured:
            return
        domain_filtered_certificates = {job_id for job_id, job in configured.items()
                                        if job.get("kind") == "certificates" and any(
                                            "domain" in entry for entry in job["alerting"].get("resources", []))}
        with self.delivery_lock, self.connect() as connection:
            excluded = []
            for row in connection.execute(select(STATES).with_for_update()).mappings():
                state = json.loads(row["payload"])
                if not state or "metric" not in state:
                    continue
                job = configured.get(state.get("job_id"))
                metric = state["metric"]
                # Legacy certificate state lacks SAN names; wait for complete metadata before re-alerting.
                incomplete_domains = (state.get("job_id") in domain_filtered_certificates
                                      and metric.get("metric") == "expiry_days"
                                      and "domains" not in metric.get("dimensions", {}))
                if job and (incomplete_domains or not resource_alerts_enabled(job, metric)):
                    excluded.append(row["key"])
            self._suppress_alerts(connection, excluded)

    def has_pending(self, connection, key):
        return connection.execute(select(OUTBOX.c.id).where(
            OUTBOX.c.incident_key == key, OUTBOX.c.status == "pending"
        ).limit(1)).first() is not None

    def enqueue(self, connection, key, channel, event, now, status="pending"):
        connection.execute(insert(OUTBOX).values(
            id=event["id"], incident_key=key, channel=channel,
            payload=json.dumps(event, ensure_ascii=False),
            next_attempt=now.timestamp(), created_at=now.timestamp(),
            status=status, last_error="channel_disabled" if status == "skipped" else None,
        ))

    def skip_channel(self, channel):
        with self.delivery_lock, self.connect() as connection:
            return connection.execute(update(OUTBOX).where(
                OUTBOX.c.channel == channel, OUTBOX.c.status.in_(("pending", "failed"))
            ).values(status="skipped", last_error="channel_disabled")).rowcount

    def skipped(self, event_id):
        with self.delivery_lock, self.connect() as connection:
            connection.execute(update(OUTBOX).where(
                OUTBOX.c.id == event_id, OUTBOX.c.status == "pending"
            ).values(status="skipped", last_error="channel_disabled"))

    def save_samples(self, job_id, metrics):
        if not metrics:
            return
        rows = [{"job_id": job_id, "metric_key": metric.key(), "observed_at": metric.observed_at.timestamp(),
                 "payload": json.dumps(metric.to_dict(), ensure_ascii=False)} for metric in metrics]
        newest = {}
        for row in rows:
            previous = newest.get(row["metric_key"])
            if previous is None or row["observed_at"] > previous["observed_at"]:
                newest[row["metric_key"]] = row
        latest = [newest[key] for key in sorted(newest)]
        with self.connect() as connection:
            for offset in range(0, len(rows), 500):
                self._insert_unique(connection, SAMPLES, rows[offset:offset + 500],
                                    ["job_id", "metric_key", "observed_at"])
            for offset in range(0, len(latest), 500):
                self._upsert_latest_samples(connection, latest[offset:offset + 500])

    def _upsert_latest_samples(self, connection, rows):
        if self.dialect == "mysql":
            statement = mysql_insert(LATEST_SAMPLES).values(rows)
            newer = statement.inserted.observed_at > LATEST_SAMPLES.c.observed_at
            # MySQL evaluates updates left to right; compare before replacing the timestamp.
            statement = statement.on_duplicate_key_update([
                ("payload", case((newer, statement.inserted.payload), else_=LATEST_SAMPLES.c.payload)),
                ("observed_at", func.greatest(LATEST_SAMPLES.c.observed_at, statement.inserted.observed_at)),
            ])
        else:
            statement = sqlite_insert(LATEST_SAMPLES).values(rows)
            statement = statement.on_conflict_do_update(
                index_elements=list(LATEST_SAMPLES.primary_key.columns),
                set_={"observed_at": statement.excluded.observed_at, "payload": statement.excluded.payload},
                where=statement.excluded.observed_at > LATEST_SAMPLES.c.observed_at,
            )
        connection.execute(statement)

    def latest_samples(self, job_ids, since=None):
        if not job_ids:
            return []
        statement = select(LATEST_SAMPLES).where(LATEST_SAMPLES.c.job_id.in_(job_ids))
        if since is not None:
            cutoff = since.timestamp() if isinstance(since, datetime) else since
            statement = statement.where(LATEST_SAMPLES.c.observed_at >= cutoff)
        statement = statement.order_by(LATEST_SAMPLES.c.job_id, LATEST_SAMPLES.c.metric_key)
        with self.connect() as connection:
            return [{**dict(row), "payload": json.loads(row["payload"])}
                    for row in connection.execute(statement).mappings()]

    def job_status(self, job_id, status, now, count=0, detail=""):
        with self.connect() as connection:
            self._upsert(connection, JOBS, {"id": job_id, "status": status, "updated_at": now.timestamp(),
                         "sample_count": count, "detail": detail}, ["status", "updated_at", "sample_count", "detail"])

    def due_messages(self, now, limit=50):
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(select(OUTBOX).where(
                OUTBOX.c.status == "pending", OUTBOX.c.next_attempt <= now.timestamp()
            ).order_by(OUTBOX.c.created_at, OUTBOX.c.id).limit(limit)).mappings()]

    def list_messages(self):
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(
                select(OUTBOX).order_by(OUTBOX.c.created_at, OUTBOX.c.id)
            ).mappings()]

    def delivered(self, event_id):
        with self.delivery_lock, self.connect() as connection:
            connection.execute(update(OUTBOX).where(
                OUTBOX.c.id == event_id, OUTBOX.c.status == "pending"
            ).values(status="sent", last_error=None))

    def still_pending(self, event_id):
        with self.connect() as connection:
            return connection.execute(select(OUTBOX.c.id).where(
                OUTBOX.c.id == event_id, OUTBOX.c.status == "pending"
            )).first() is not None

    def delivery_failed(self, event_id, now, max_attempts, error):
        with self.delivery_lock, self.connect() as connection:
            row = connection.execute(select(OUTBOX.c.attempts).where(
                OUTBOX.c.id == event_id, OUTBOX.c.status == "pending"
            ).with_for_update()).first()
            if row is None:
                return
            attempts = row.attempts + 1
            connection.execute(update(OUTBOX).where(OUTBOX.c.id == event_id).values(
                attempts=attempts, status="failed" if attempts >= max_attempts else "pending",
                next_attempt=now.timestamp() + min(3600, 30 * 2 ** min(attempts - 1, 10)), last_error=error,
            ))

    def retry_failed(self, now):
        with self.delivery_lock, self.connect() as connection:
            return connection.execute(update(OUTBOX).where(OUTBOX.c.status == "failed").values(
                status="pending", attempts=0, next_attempt=now.timestamp()
            )).rowcount

    def add_receipts(self, rows):
        unique = {row[0]: {"id": row[0], "account": row[1], "report_time": row[2], "success": row[3]} for row in rows}
        if not unique:
            return 0
        with self.receipts_lock, self.connect() as connection:
            existing = set(connection.execute(select(RECEIPTS.c.id).where(RECEIPTS.c.id.in_(unique))).scalars())
            pending = [row for key, row in unique.items() if key not in existing]
            if pending:
                self._insert_unique(connection, RECEIPTS, pending, ["id"])
            return len(pending)

    def receipt_summary(self, account, since, until):
        with self.connect() as connection:
            return dict(connection.execute(select(
                func.count().label("total"), func.sum(RECEIPTS.c.success).label("success"),
                func.max(RECEIPTS.c.report_time).label("latest"),
            ).where(RECEIPTS.c.account == account, RECEIPTS.c.report_time >= since.timestamp(),
                    RECEIPTS.c.report_time <= until.timestamp())).mappings().one())

    def status(self):
        with self.connect() as connection:
            states = [json.loads(payload) for payload in connection.execute(select(STATES.c.payload)).scalars()]
            return {
                "jobs": [dict(row) for row in connection.execute(select(JOBS).order_by(JOBS.c.id)).mappings()],
                "outbox": {row.status: row.n for row in connection.execute(
                    select(OUTBOX.c.status, func.count().label("n")).group_by(OUTBOX.c.status))},
                "active_alerts": [state for state in states if state and state.get("active")],
                "estimate_quality": {state["job_id"]: state["resources"] for state in states
                                     if state and state.get("kind") == "network_estimate_quality"},
            }

    def prune(self, now: datetime, retention_days: int):
        cutoff = (now - timedelta(days=retention_days)).timestamp()
        with self.connect() as connection:
            connection.execute(delete(SAMPLES).where(SAMPLES.c.observed_at < cutoff))
            connection.execute(delete(LATEST_SAMPLES).where(LATEST_SAMPLES.c.observed_at < cutoff))
            connection.execute(delete(RECEIPTS).where(RECEIPTS.c.report_time < cutoff))
            connection.execute(delete(OUTBOX).where(
                OUTBOX.c.created_at < cutoff, OUTBOX.c.status.in_(("sent", "cancelled", "skipped"))))

    @contextmanager
    def acquire_lock(self):
        if self._lock_connection is not None or self._file_lock is not None:
            raise RuntimeError("This monitor already holds its scheduler lock")
        if self._memory_lock is not None:
            if not self._memory_lock.acquire(blocking=False):
                raise RuntimeError("This monitor already holds its scheduler lock")
            try:
                yield self
            finally:
                self._memory_lock.release()
            return
        if self.dialect == "sqlite":
            from filelock import FileLock, Timeout
            lock = FileLock(self.path + ".lock")
            try:
                lock.acquire(timeout=0)
            except Timeout:
                raise RuntimeError("Another collector is using this database") from None
            self._file_lock = lock
            try:
                yield self
            finally:
                self._file_lock = None
                lock.release()
            return
        connection = self.engine.connect()
        try:
            acquired = connection.execute(select(func.get_lock(self._lock_name, 0))).scalar_one()
            connection.commit()
            if acquired != 1:
                raise RuntimeError("Another collector is using this MySQL database")
            self._lock_connection = connection
            yield self
        finally:
            if self._lock_connection is connection:
                self._lock_connection = None
                try:
                    connection.execute(select(func.release_lock(self._lock_name)))
                    connection.commit()
                except SQLAlchemyError:
                    pass
            connection.close()

    def check_lock(self):
        if self._lock_connection is None:
            return self._file_lock.is_locked if self._file_lock is not None else True
        try:
            owned = self._lock_connection.execute(select(
                func.is_used_lock(self._lock_name) == func.connection_id()
            )).scalar_one()
            self._lock_connection.commit()
            return bool(owned)
        except SQLAlchemyError:
            return False

    def dispose(self):
        self.engine.dispose()
