from datetime import datetime, timedelta, timezone
import hashlib
import json

from .models import Metric


def parse_receipts(payload, account, now):
    if not isinstance(payload, list) or not payload or len(payload) > 1000:
        raise ValueError("Expected 1 to 1000 SMS delivery receipts")
    rows = []
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("Each SMS delivery receipt must be an object")
        if any(not item.get(field) for field in ("phone_number", "send_time", "report_time")):
            raise ValueError("Missing receipt identity or report_time")
        success = item.get("success")
        if success is True or success == "true":
            success = 1
        elif success is False or success == "false":
            success = 0
        else:
            raise ValueError("Receipt success must be boolean or true/false string")
        reported = datetime.fromisoformat(str(item["report_time"]).replace("Z", "+00:00"))
        if reported.tzinfo is None:
            reported = reported.replace(tzinfo=timezone(timedelta(hours=8)))
        if reported > now + timedelta(minutes=5) or reported < now - timedelta(days=30):
            raise ValueError("Receipt report_time is outside accepted range")
        # Store an irreversible identity hash, never the phone number or SMS body.
        identity = [account, item["phone_number"], item["send_time"], item.get("biz_id", ""),
                    item.get("out_id", ""), item["report_time"], success]
        key = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
        rows.append((key, account, reported.timestamp(), success))
    return rows


def collect_receipts(store, account, options, now):
    window = int(options.get("window_seconds", 300))
    minimum = int(options.get("minimum_samples", 20))
    if window < 60 or minimum < 1:
        raise ValueError("SMS receipt window must be >=60s and minimum_samples >=1")
    result = store.receipt_summary(account["id"], now - timedelta(seconds=window), now)
    if result["total"] < minimum:
        return []
    observed = datetime.fromtimestamp(result["latest"], timezone.utc)
    return [Metric(provider="aliyun", account=account["id"], resource_id="sms-delivery",
                   resource_name="SMS delivery receipts", metric="sms_success_percent",
                   value=result["success"] / result["total"] * 100, unit="%", observed_at=observed,
                   dimensions={"window_seconds": window, "samples": result["total"]})]

