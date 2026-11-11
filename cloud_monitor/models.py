from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Metric:
    provider: str
    account: str
    resource_id: str
    resource_name: str
    metric: str
    value: float
    unit: str
    observed_at: datetime
    dimensions: dict = field(default_factory=dict)

    def __post_init__(self):
        if isinstance(self.value, bool) or not math.isfinite(float(self.value)):
            raise ValueError("Metric value must be a finite number")
        if self.observed_at.tzinfo is None:
            raise ValueError("Metric observed_at must include a timezone")
        if not all([self.provider, self.account, self.resource_id, self.metric]):
            raise ValueError("Metric identity must not be empty")

    def key(self) -> str:
        labels = self.dimensions.get("identity", {
            key: self.dimensions[key] for key in
            ("region", "namespace", "metric_name", "quota_id", "dimension_id", "scope", "engine")
            if key in self.dimensions
        })
        identity = [self.provider, self.account, self.resource_id, self.metric, labels]
        return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()

    def to_dict(self) -> dict:
        result = asdict(self)
        result["observed_at"] = self.observed_at.isoformat()
        return result
