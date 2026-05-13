from __future__ import annotations

import argparse
import json
from pathlib import Path


def fmt(value: object) -> str:
    return str(value)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metrics", default="reports\\metrics.json")
    parser.add_argument("--out", default="reports\\final_report.md")
    args = parser.parse_args()
    metrics = json.loads(Path(args.metrics).read_text())
    comparison = metrics.get("cache_comparison", {})
    without_cache = comparison.get("without_cache", {}) if isinstance(comparison, dict) else {}
    with_cache = comparison.get("with_cache", {}) if isinstance(comparison, dict) else {}
    delta = comparison.get("delta", {}) if isinstance(comparison, dict) else {}

    lines = [
        "# Day 10 Reliability Final Report",
        "",
        "## 1. Architecture summary",
        "",
        "The gateway checks cache first, then routes through a provider fallback chain protected by per-provider circuit breakers. Open circuits fail fast, recovered providers probe in half-open state, and all-provider failure returns a static degraded response.",
        "",
        "```",
        "User Request",
        "  -> Gateway",
        "  -> Cache check -> cache hit response",
        "  -> Circuit breaker: primary -> Provider primary",
        "  -> Circuit breaker: backup -> Provider backup",
        "  -> Static fallback message",
        "```",
        "",
        "## 2. Configuration",
        "",
        "| Setting | Value | Reason |",
        "|---|---:|---|",
        "| failure_threshold | 3 | Detects repeated provider failure quickly without opening on a single transient error. |",
        "| reset_timeout_seconds | 2 | Lets the fake provider recover quickly enough for visible OPEN -> HALF_OPEN -> CLOSED evidence. |",
        "| success_threshold | 1 | One successful probe is sufficient for this lab workload. |",
        "| cache TTL | 300 | Five minutes balances FAQ-style reuse with freshness. |",
        "| similarity_threshold | 0.92 | Keeps semantic reuse strict and avoids date-sensitive false hits. |",
        "| load_test requests | 200 | Enough traffic to trigger circuit and cache behavior reproducibly. |",
        "| load_test concurrency | 10 | Exercises shared gateway state under concurrent load. |",
        "",
        "## 3. Metrics Summary",
        "",
        "| Metric | Value |",
        "|---|---:|",
    ]
    for key, value in metrics.items():
        if key in {"scenarios", "scenario_details", "cache_comparison"}:
            continue
        lines.append(f"| {key} | {fmt(value)} |")
    lines += [
        "",
        "## 4. Cache comparison",
        "",
        "| Metric | Without cache | With cache | Delta |",
        "|---|---:|---:|---:|",
        f"| latency_p50_ms | {without_cache.get('latency_p50_ms', 'n/a')} | {with_cache.get('latency_p50_ms', 'n/a')} | {delta.get('latency_p50_ms', 'n/a')} |",
        f"| latency_p95_ms | {without_cache.get('latency_p95_ms', 'n/a')} | {with_cache.get('latency_p95_ms', 'n/a')} | {delta.get('latency_p95_ms', 'n/a')} |",
        f"| estimated_cost | {without_cache.get('estimated_cost', 'n/a')} | {with_cache.get('estimated_cost', 'n/a')} | {delta.get('estimated_cost', 'n/a')} |",
        f"| cache_hit_rate | {without_cache.get('cache_hit_rate', 'n/a')} | {with_cache.get('cache_hit_rate', 'n/a')} | {delta.get('cache_hit_rate', 'n/a')} |",
        "",
        "## 5. Redis shared cache",
        "",
        "In-memory cache is process-local, so horizontally scaled gateways would miss entries created by sibling instances. `SharedRedisCache` stores query/response hashes with TTL in Redis, applies the same privacy and false-hit guardrails, and lets multiple gateway instances share cached answers.",
        "",
        "Evidence: `tests/test_redis_cache.py::test_shared_state_across_instances` creates two cache instances with the same prefix; the second reads the value written by the first. Redis keys are visible with `docker compose exec redis redis-cli KEYS \"rl:cache:*\"` after running with `cache.backend: redis`.",
        "",
        "Local Redis evidence captured during verification:",
        "",
        "```",
        "SharedRedisCache get: ('redis shared evidence response', 1.0)",
        "redis-cli KEYS 'rl:cache:*': rl:cache:e6bb724160ee",
        "```",
        "",
        "## 6. Chaos Scenarios",
        "",
        "| Scenario | Status | Observed evidence |",
        "|---|---|---|",
    ]
    for key, value in metrics.get("scenarios", {}).items():
        detail = metrics.get("scenario_details", {}).get(key, {})
        lines.append(f"| {key} | {value} | `{detail}` |")
    lines += [
        "",
        "## 7. SLO definitions",
        "",
        "| SLI | SLO target | Actual value | Met? |",
        "|---|---|---:|---|",
        f"| Availability | >= 99% | {metrics.get('availability')} | {'yes' if metrics.get('availability', 0) >= 0.99 else 'no'} |",
        f"| Latency P95 | < 2500 ms | {metrics.get('latency_p95_ms')} | {'yes' if metrics.get('latency_p95_ms', 999999) < 2500 else 'no'} |",
        f"| Cache hit rate | >= 10% | {metrics.get('cache_hit_rate')} | {'yes' if metrics.get('cache_hit_rate', 0) >= 0.1 else 'no'} |",
        f"| Recovery time | < 5000 ms | {metrics.get('recovery_time_ms')} | {'yes' if (metrics.get('recovery_time_ms') or 999999) < 5000 else 'no'} |",
        "",
        "## 8. Failure analysis",
        "",
        "The remaining production weakness is that circuit breaker state is still process-local. In a real multi-instance deployment, one gateway could learn that a provider is down while another continues sending traffic until its own breaker opens. The next production change would move breaker counters and state transitions to Redis or another shared control plane, with per-provider rate limits.",
        "",
        "## 9. Next steps",
        "",
        "1. Add Redis-backed circuit breaker state so provider health is shared across instances.",
        "2. Add per-user and per-provider rate limits to reduce retry pressure during incidents.",
        "3. Export Prometheus counters for request totals, latency, cache hits, fallback usage, and circuit state.",
    ]
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text("\n".join(lines))
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
