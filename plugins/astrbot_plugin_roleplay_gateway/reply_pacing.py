"""Small, deterministic reply policy; persistence belongs to ReceiptStore."""
from __future__ import annotations

from dataclasses import dataclass, fields
import hashlib
import math
from statistics import NormalDist
from typing import Any, Mapping


@dataclass(frozen=True)
class ReplyPacing:
    enabled: bool = False
    probability: float = .6
    first_delay_min: float = 5.0
    first_delay_max: float = 10.0
    first_delay_mean: float = 7.5
    first_delay_std: float = 1.25
    interval_min: float = 3.0
    interval_max: float = 5.0
    interval_mean: float = 4.0
    interval_std: float = .5
    max_bubbles: int = 4
    max_characters: int = 125
    cooldown_seconds: float = 15.0
    window_seconds: float = 60.0
    max_rounds: int = 3

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("invalid_pacing_enabled")
        integer_limits = {"max_bubbles": 16, "max_characters": 2000, "max_rounds": 100}
        for key, ceiling in integer_limits.items():
            value = getattr(self, key)
            if type(value) is not int or not 1 <= value <= ceiling:
                raise ValueError("invalid_pacing_" + key)
        for field in fields(self):
            if field.name == "enabled" or field.name in integer_limits:
                continue
            value = getattr(self, field.name)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value)):
                raise ValueError("invalid_pacing_" + field.name)
        if not 0 <= self.probability <= 1:
            raise ValueError("invalid_pacing_probability")
        for prefix in ("first_delay", "interval"):
            low, high, mean, std = (getattr(self, prefix + "_" + suffix)
                                    for suffix in ("min", "max", "mean", "std"))
            if not (0 <= low < high <= 60 and low <= mean <= high and 0 < std <= 60):
                raise ValueError("invalid_pacing_" + prefix)
            normal = NormalDist(mean, std)
            if normal.cdf(high) - normal.cdf(low) < 1e-12:
                raise ValueError("invalid_pacing_distribution_precision")
        if not 0 <= self.cooldown_seconds <= self.window_seconds <= 3600 or self.window_seconds <= 0:
            raise ValueError("invalid_pacing_window")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ReplyPacing":
        if not isinstance(raw, Mapping) or set(raw) - {f.name for f in fields(cls)}:
            raise ValueError("invalid_reply_pacing")
        return cls(**raw)

    def validate_deadlines(self, *, first: float, generation: float, total: float,
                           send_timeout: float) -> float:
        if not self.enabled:
            return first
        first = max(first, self.first_delay_max + send_timeout)
        if (first > generation or generation > total
                or self.first_delay_max + (self.max_bubbles - 1) * self.interval_max > total):
            raise ValueError("incompatible_pacing_deadlines")
        return first

    def sample(self, key: str, prefix: str) -> float:
        normal = NormalDist(getattr(self, prefix + "_mean"), getattr(self, prefix + "_std"))
        low, high = getattr(self, prefix + "_min"), getattr(self, prefix + "_max")
        left, right = normal.cdf(low), normal.cdf(high)
        # Inverse CDF samples the truncated distribution itself; no endpoint clamp.
        return normal.inv_cdf(left + stable_uniform(key) * (right - left))


def stable_uniform(key: str) -> float:
    """A versioned, reproducible score strictly between zero and one."""
    digest = hashlib.sha256(("reply-pacing-v1\0" + key).encode("utf-8")).digest()
    integer = int.from_bytes(digest[:8], "big") >> 12
    return (integer + .5) / 2**52


@dataclass(frozen=True)
class PacingDecision:
    selected: bool
    reason: str
    decided_at: float
    score: float
    first_delay: float
    intervals: tuple[float, ...]
