# Day 10 Reliability Final Report

## 1. Architecture summary

The gateway checks cache first, then routes through a provider fallback chain protected by per-provider circuit breakers. Open circuits fail fast, recovered providers probe in half-open state, and all-provider failure returns a static degraded response.

```
User Request
  -> Gateway
  -> Cache check -> cache hit response
  -> Circuit breaker: primary -> Provider primary
  -> Circuit breaker: backup -> Provider backup
  -> Static fallback message
```

## 2. Configuration

| Setting | Value | Reason |
|---|---:|---|
| failure_threshold | 3 | Detects repeated provider failure quickly without opening on a single transient error. |
| reset_timeout_seconds | 2 | Lets the fake provider recover quickly enough for visible OPEN -> HALF_OPEN -> CLOSED evidence. |
| success_threshold | 1 | One successful probe is sufficient for this lab workload. |
| cache TTL | 300 | Five minutes balances FAQ-style reuse with freshness. |
| similarity_threshold | 0.92 | Keeps semantic reuse strict and avoids date-sensitive false hits. |
| load_test requests | 200 | Enough traffic to trigger circuit and cache behavior reproducibly. |
| load_test concurrency | 10 | Exercises shared gateway state under concurrent load. |

## 3. Metrics Summary

| Metric | Value |
|---|---:|
| total_requests | 800 |
| availability | 0.9925 |
| error_rate | 0.0075 |
| latency_p50_ms | 0.34 |
| latency_p95_ms | 310.77 |
| latency_p99_ms | 498.11 |
| fallback_success_rate | 0.9504 |
| cache_hit_rate | 0.7438 |
| circuit_open_count | 3 |
| recovery_time_ms | 3398.1763124465942 |
| estimated_cost | 0.087298 |
| estimated_cost_saved | 0.595 |

## 4. Cache comparison

| Metric | Without cache | With cache | Delta |
|---|---:|---:|---:|
| latency_p50_ms | 214.57 | 0.27 | -214.3 |
| latency_p95_ms | 238.41 | 221.91 | -16.5 |
| estimated_cost | 0.11688 | 0.03066 | -0.08622 |
| cache_hit_rate | 0.0 | 0.735 | 0.735 |

## 5. Redis shared cache

In-memory cache is process-local, so horizontally scaled gateways would miss entries created by sibling instances. `SharedRedisCache` stores query/response hashes with TTL in Redis, applies the same privacy and false-hit guardrails, and lets multiple gateway instances share cached answers.

Evidence: `tests/test_redis_cache.py::test_shared_state_across_instances` creates two cache instances with the same prefix; the second reads the value written by the first. Redis keys are visible with `docker compose exec redis redis-cli KEYS "rl:cache:*"` after running with `cache.backend: redis`.

Local Redis evidence captured during verification:

```
SharedRedisCache get: ('redis shared evidence response', 1.0)
redis-cli KEYS 'rl:cache:*': rl:cache:e6bb724160ee
```

## 6. Chaos Scenarios

| Scenario | Status | Observed evidence |
|---|---|---|
| primary_timeout_100 | pass | `{'availability': 0.99, 'error_rate': 0.01, 'fallback_success_rate': 0.9583, 'cache_hit_rate': 0.76, 'circuit_open_count': 1, 'recovery_time_ms': 3776.049852371216}` |
| primary_flaky_50 | pass | `{'availability': 0.98, 'error_rate': 0.02, 'fallback_success_rate': 0.9245, 'cache_hit_rate': 0.71, 'circuit_open_count': 1, 'recovery_time_ms': 4008.8374614715576}` |
| all_healthy | pass | `{'availability': 1.0, 'error_rate': 0.0, 'fallback_success_rate': 0.0, 'cache_hit_rate': 0.745, 'circuit_open_count': 0, 'recovery_time_ms': None}` |
| cache_stale_candidate | pass | `{'availability': 1.0, 'error_rate': 0.0, 'fallback_success_rate': 1.0, 'cache_hit_rate': 0.76, 'circuit_open_count': 1, 'recovery_time_ms': 2903.9089679718018}` |

## 7. SLO definitions

| SLI | SLO target | Actual value | Met? |
|---|---|---:|---|
| Availability | >= 99% | 0.9925 | yes |
| Latency P95 | < 2500 ms | 310.77 | yes |
| Cache hit rate | >= 10% | 0.7438 | yes |
| Recovery time | < 5000 ms | 3398.1763124465942 | yes |

## 8. Failure analysis

The remaining production weakness is that circuit breaker state is still process-local. In a real multi-instance deployment, one gateway could learn that a provider is down while another continues sending traffic until its own breaker opens. The next production change would move breaker counters and state transitions to Redis or another shared control plane, with per-provider rate limits.

## 9. Next steps

1. Add Redis-backed circuit breaker state so provider health is shared across instances.
2. Add per-user and per-provider rate limits to reduce retry pressure during incidents.
3. Export Prometheus counters for request totals, latency, cache hits, fallback usage, and circuit state.