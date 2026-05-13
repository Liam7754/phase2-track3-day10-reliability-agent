from __future__ import annotations

import copy
import json
import random
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker
from reliability_lab.config import LabConfig, ScenarioConfig
from reliability_lab.gateway import GatewayResponse, ReliabilityGateway
from reliability_lab.metrics import RunMetrics
from reliability_lab.providers import FakeLLMProvider


def load_queries(path: str | Path = "data/sample_queries.jsonl") -> list[str]:
    queries: list[str] = []
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        queries.append(json.loads(line)["query"])
    return queries


def build_gateway(config: LabConfig, provider_overrides: dict[str, float] | None = None) -> ReliabilityGateway:
    providers = []
    for p in config.providers:
        fail_rate = provider_overrides.get(p.name, p.fail_rate) if provider_overrides else p.fail_rate
        providers.append(FakeLLMProvider(p.name, fail_rate, p.base_latency_ms, p.cost_per_1k_tokens))
    breakers = {
        p.name: CircuitBreaker(
            name=p.name,
            failure_threshold=config.circuit_breaker.failure_threshold,
            reset_timeout_seconds=config.circuit_breaker.reset_timeout_seconds,
            success_threshold=config.circuit_breaker.success_threshold,
        )
        for p in config.providers
    }
    cache: ResponseCache | SharedRedisCache | None = None
    if config.cache.enabled:
        if config.cache.backend == "redis":
            cache = SharedRedisCache(
                config.cache.redis_url,
                config.cache.ttl_seconds,
                config.cache.similarity_threshold,
            )
        else:
            cache = ResponseCache(config.cache.ttl_seconds, config.cache.similarity_threshold)
    return ReliabilityGateway(providers, breakers, cache)


def calculate_recovery_time_ms(gateway: ReliabilityGateway) -> float | None:
    """Derive recovery time from circuit breaker transition logs.

    Recovery time = time between circuit opening and next successful close.
    Returns the average recovery time across all breakers, or None if no recovery occurred.
    """
    recovery_times: list[float] = []
    for breaker in gateway.breakers.values():
        open_ts: float | None = None
        for entry in breaker.transition_log:
            if entry["to"] == "open" and open_ts is None:
                open_ts = entry["ts"]
            elif entry["to"] == "closed" and open_ts is not None:
                recovery_times.append((float(entry["ts"]) - open_ts) * 1000)
                open_ts = None
    if not recovery_times:
        return None
    return sum(recovery_times) / len(recovery_times)


def run_scenario(config: LabConfig, queries: list[str], scenario: ScenarioConfig) -> RunMetrics:
    """Run a single named chaos scenario."""
    gateway = build_gateway(config, scenario.provider_overrides or None)
    metrics = RunMetrics()
    request_count = config.load_test.requests

    def complete_random_prompt(_: int) -> GatewayResponse:
        prompt = random.choice(queries)
        return gateway.complete(prompt)

    if config.load_test.concurrency > 1:
        with ThreadPoolExecutor(max_workers=config.load_test.concurrency) as executor:
            results = list(executor.map(complete_random_prompt, range(request_count)))
    else:
        results = [complete_random_prompt(i) for i in range(request_count)]

    for result in results:
        metrics.total_requests += 1
        metrics.estimated_cost += result.estimated_cost
        if result.cache_hit:
            metrics.cache_hits += 1
            metrics.estimated_cost_saved += 0.001
        if result.route.startswith("fallback:"):
            metrics.fallback_successes += 1
            metrics.successful_requests += 1
        elif result.route == "static_fallback":
            metrics.static_fallbacks += 1
            metrics.failed_requests += 1
        else:
            metrics.successful_requests += 1
        if result.latency_ms:
            metrics.latencies_ms.append(result.latency_ms)

    for provider in gateway.providers:
        breaker = gateway.breakers[provider.name]
        if breaker.state.value == "open":
            provider.fail_rate = 0.0
            time.sleep(config.circuit_breaker.reset_timeout_seconds + 0.05)
            gateway.complete(f"recovery probe for {scenario.name} {provider.name} {time.time()}")

    metrics.circuit_open_count = sum(
        1 for breaker in gateway.breakers.values() for t in breaker.transition_log if t["to"] == "open"
    )
    metrics.recovery_time_ms = calculate_recovery_time_ms(gateway)
    return metrics


def scenario_passed(name: str, metrics: RunMetrics) -> bool:
    if name == "primary_timeout_100":
        return metrics.availability >= 0.95 and metrics.circuit_open_count > 0
    if name == "primary_flaky_50":
        return metrics.availability >= 0.9 and metrics.circuit_open_count > 0
    if name == "cache_stale_candidate":
        cache = ResponseCache(ttl_seconds=60, similarity_threshold=0.3)
        cache.set("Summarize refund policy for 2024 deadline", "Old refund policy")
        cached, _ = cache.get("Summarize refund policy for 2026 deadline")
        return cached is None and bool(cache.false_hit_log)
    if name == "all_healthy":
        return metrics.error_rate == 0.0
    return metrics.successful_requests > 0


def summarize_scenario(metrics: RunMetrics) -> dict[str, object]:
    return {
        "availability": round(metrics.availability, 4),
        "error_rate": round(metrics.error_rate, 4),
        "fallback_success_rate": round(metrics.fallback_success_rate, 4),
        "cache_hit_rate": round(metrics.cache_hit_rate, 4),
        "circuit_open_count": metrics.circuit_open_count,
        "recovery_time_ms": metrics.recovery_time_ms,
    }


def build_cache_comparison(config: LabConfig, queries: list[str]) -> dict[str, dict[str, object]]:
    with_cache = copy.deepcopy(config)
    with_cache.cache.enabled = True
    without_cache = copy.deepcopy(config)
    without_cache.cache.enabled = False
    healthy_overrides = {provider.name: 0.0 for provider in config.providers}
    baseline = run_scenario(
        without_cache, queries, ScenarioConfig(name="cache_off", provider_overrides=healthy_overrides)
    )
    cached = run_scenario(
        with_cache, queries, ScenarioConfig(name="cache_on", provider_overrides=healthy_overrides)
    )
    return {
        "without_cache": baseline.to_report_dict(),
        "with_cache": cached.to_report_dict(),
        "delta": {
            "latency_p50_ms": round(cached.percentile(50) - baseline.percentile(50), 2),
            "latency_p95_ms": round(cached.percentile(95) - baseline.percentile(95), 2),
            "estimated_cost": round(cached.estimated_cost - baseline.estimated_cost, 6),
            "cache_hit_rate": round(cached.cache_hit_rate - baseline.cache_hit_rate, 4),
        },
    }


def run_simulation(config: LabConfig, queries: list[str]) -> RunMetrics:
    """Run all named scenarios from config, or a default run if none defined.

    TODO(student): Add a cache vs no-cache comparison scenario.
    Extend with your own custom scenarios (e.g., cost cap near limit).
    """
    if not config.scenarios:
        default_scenario = ScenarioConfig(name="default", description="baseline run")
        metrics = run_scenario(config, queries, default_scenario)
        metrics.scenarios = {"default": "pass" if metrics.successful_requests > 0 else "fail"}
        return metrics

    combined = RunMetrics()
    for scenario in config.scenarios:
        result = run_scenario(config, queries, scenario)

        # TODO(student): Define pass/fail criteria per scenario.
        # Example: primary_timeout_100 passes if fallback_success_rate > 0.9
        passed = scenario_passed(scenario.name, result)
        combined.scenarios[scenario.name] = "pass" if passed else "fail"
        combined.scenario_details[scenario.name] = summarize_scenario(result)

        combined.total_requests += result.total_requests
        combined.successful_requests += result.successful_requests
        combined.failed_requests += result.failed_requests
        combined.fallback_successes += result.fallback_successes
        combined.static_fallbacks += result.static_fallbacks
        combined.cache_hits += result.cache_hits
        combined.circuit_open_count += result.circuit_open_count
        combined.estimated_cost += result.estimated_cost
        combined.estimated_cost_saved += result.estimated_cost_saved
        combined.latencies_ms.extend(result.latencies_ms)
        if result.recovery_time_ms is not None:
            if combined.recovery_time_ms is None:
                combined.recovery_time_ms = result.recovery_time_ms
            else:
                combined.recovery_time_ms = (combined.recovery_time_ms + result.recovery_time_ms) / 2

    combined.cache_comparison = build_cache_comparison(config, queries)
    return combined
