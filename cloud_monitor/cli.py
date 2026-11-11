import argparse
from datetime import timedelta
import json
import logging
import signal
import sys
import threading

from filelock import Timeout
from sqlalchemy.exc import SQLAlchemyError

from .api import start_server
from .config import load_config, validate_database
from .diagnostics import diagnose_jobs
from .engine import Engine
from .models import Metric, utcnow
from .service import Service
from .store import Store


def main(argv=None):
    parser = argparse.ArgumentParser(description="Cloud resource monitoring and tiered alerts")
    parser.add_argument("--config", default="config/config.yaml")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="Validate configuration without API calls or messages")
    check.add_argument("--live", action="store_true", help="Also validate filled credentials and enabled channels")
    diagnose = commands.add_parser("diagnose", help="Read-only cloud checks; no database, alerts or messages")
    diagnose.add_argument("--job", action="append", help="Enabled job ID to check; repeat for multiple jobs")
    commands.add_parser("serve", help="Run scheduler, notification worker and health endpoint")
    once = commands.add_parser("once", help="Collect once and persist alerts; no sends unless --send")
    once.add_argument("--send", action="store_true")
    commands.add_parser("status", help="Inspect stored job and alert state")
    commands.add_parser("retry-failed", help="Requeue failed notifications for the running worker")
    demo = commands.add_parser("demo", help="Run offline expiry escalation and recovery; never send")
    demo.add_argument("--database", default=":memory:")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    store = None
    try:
        cfg = load_config(args.config, live=args.command in ("serve", "once") or
                          (args.command == "check" and args.live))
        if args.command == "check":
            print(json.dumps({"valid": True, "accounts": len(cfg["accounts"]),
                              "jobs": len(cfg["jobs"]), "rules": len(cfg["rules"])}, ensure_ascii=False))
            return 0
        if args.command == "diagnose":
            failed = False
            for report in diagnose_jobs(cfg, args.job):
                print(json.dumps(report, ensure_ascii=False), flush=True)
                failed = failed or report["status"] == "error"
            return 1 if failed else 0
        database = args.database if args.command == "demo" else cfg["service"]["database"]
        if args.command != "demo":
            validate_database(database)
        store = Store(database)
        if args.command == "status":
            print(json.dumps(store.status(), ensure_ascii=False, indent=2))
            return 0
        if args.command == "retry-failed":
            print(json.dumps({"requeued": store.retry_failed(utcnow())}))
            return 0
        with store.acquire_lock():
            if args.command == "demo":
                _demo(cfg, store)
                return 0
            service = Service(cfg, store)
            if args.command == "once":
                return 0 if service.run_once(send=args.send) else 1
            stop = threading.Event()
            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, lambda *_: stop.set())
            service.start()
            server = None
            try:
                server = start_server(service)
                logging.info("monitor started accounts=%s jobs=%s", len(service.accounts), len(service.jobs))
                while not stop.wait(5):
                    if not store.check_lock():
                        raise RuntimeError("MySQL scheduler lock was lost; stopping collection")
            finally:
                if server:
                    server.shutdown()
                    server.server_close()
                service.stop()
            return 0
    except Timeout:
        print("Another collector is using this database", file=sys.stderr)
        return 2
    except SQLAlchemyError:
        print("MySQL connection or operation failed; check database service, credentials and permissions", file=sys.stderr)
        return 2
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"Configuration/startup error: {exc}", file=sys.stderr)
        return 2
    finally:
        if store is not None:
            store.dispose()


def _demo(config, store):
    now = utcnow()
    engine = Engine(store, config)
    rule = {"id": "demo-expiry", "metric": "expiry_days", "summary": "资源即将到期",
            "levels": [{"severity": "P0", "op": "le", "threshold": 7},
                       {"severity": "P1", "op": "le", "threshold": 30}]}
    job = {"id": "demo", "interval_seconds": 86400}
    for index, days in enumerate([25, 25, 5, 60, 60]):
        at = now + timedelta(minutes=index)
        metric = Metric("aliyun", "demo-account", "demo-instance", "Demo ECS", "expiry_days", days, "days", at)
        engine.evaluate(job, metric, at, rules=[rule])
    rows = [{key: row[key] for key in ("channel", "status", "payload")} for row in store.list_messages()]
    print(json.dumps({"mode": "offline-no-messages-sent", "notifications": rows}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
