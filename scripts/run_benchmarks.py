from __future__ import annotations

import json
import random
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from reliability_lab.cache import SharedRedisCache
from reliability_lab.chaos import build_gateway, calculate_recovery_time_ms, load_queries, run_scenario
from reliability_lab.config import LabConfig, ScenarioConfig, load_config
from reliability_lab.gateway import GatewayResponse, ReliabilityGateway
from reliability_lab.metrics import RunMetrics


def _healthy_scenario() -> ScenarioConfig:
    return ScenarioConfig(
        name="benchmark_healthy",
        description="Both providers healthy for controlled comparisons",
        provider_overrides={"primary": 0.0, "backup": 0.0},
    )


def _summarize(
    config: LabConfig,
    prompts: list[str],
    results: list[GatewayResponse],
    gateway: ReliabilityGateway,
) -> RunMetrics:
    metrics = RunMetrics(total_requests=len(results))
    primary_cost = config.providers[0].cost_per_1k_tokens
    for prompt, result in zip(prompts, results, strict=True):
        metrics.estimated_cost += result.estimated_cost
        metrics.latencies_ms.append(result.latency_ms)
        if result.cache_hit:
            metrics.cache_hits += 1
            avoided_tokens = max(1, len(prompt.split())) + 50
            metrics.estimated_cost_saved += avoided_tokens / 1000.0 * primary_cost
        if result.route == "static_fallback":
            metrics.failed_requests += 1
            metrics.static_fallbacks += 1
        else:
            metrics.successful_requests += 1
            if result.route == "fallback":
                metrics.fallback_successes += 1
    metrics.circuit_open_count = sum(
        transition["to"] == "open"
        for breaker in gateway.breakers.values()
        for transition in breaker.transition_log
    )
    metrics.recovery_time_ms = calculate_recovery_time_ms(gateway)
    return metrics


def _run_concurrent(
    config: LabConfig, queries: list[str], workers: int = 10
) -> tuple[RunMetrics, float]:
    scenario = _healthy_scenario()
    gateway = build_gateway(config, scenario.provider_overrides)
    if isinstance(gateway.cache, SharedRedisCache):
        gateway.cache.flush()
    query_rng = random.Random(42)
    prompts = [query_rng.choice(queries) for _ in range(config.load_test.requests)]

    import time

    started_at = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(gateway.complete, prompts))
    wall_time_ms = (time.perf_counter() - started_at) * 1000
    metrics = _summarize(config, prompts, results, gateway)
    if isinstance(gateway.cache, SharedRedisCache):
        gateway.cache.close()
    return metrics, wall_time_ms


def _delta(without_cache: dict[str, object], with_cache: dict[str, object]) -> dict[str, float]:
    def as_float(value: object) -> float:
        if not isinstance(value, (int, float)):
            raise TypeError(f"Expected numeric benchmark value, got {type(value).__name__}")
        return float(value)

    keys = ["latency_p50_ms", "latency_p95_ms", "estimated_cost", "cache_hit_rate"]
    return {
        key: round(as_float(with_cache[key]) - as_float(without_cache[key]), 6)
        for key in keys
    }


def main() -> None:
    config = load_config("configs/default.yaml")
    queries = load_queries()
    scenario = _healthy_scenario()

    no_cache_config = config.model_copy(
        update={"cache": config.cache.model_copy(update={"enabled": False})}
    )
    memory_config = config.model_copy(
        update={"cache": config.cache.model_copy(update={"enabled": True, "backend": "memory"})}
    )
    redis_config = config.model_copy(
        update={"cache": config.cache.model_copy(update={"enabled": True, "backend": "redis"})}
    )

    random.seed(42)
    without_cache = run_scenario(no_cache_config, queries, scenario).to_report_dict()
    random.seed(42)
    with_memory = run_scenario(memory_config, queries, scenario).to_report_dict()
    random.seed(42)
    with_redis = run_scenario(redis_config, queries, scenario).to_report_dict()
    random.seed(42)
    concurrent_metrics, concurrent_wall_ms = _run_concurrent(memory_config, queries)

    redis_a = SharedRedisCache(config.cache.redis_url, config.cache.ttl_seconds, 0.92)
    redis_b = SharedRedisCache(config.cache.redis_url, config.cache.ttl_seconds, 0.92)
    redis_a.set("shared-state benchmark proof", "visible from second instance")
    shared_value, shared_score = redis_b.get("shared-state benchmark proof")
    redis_keys = list(redis_a._redis.scan_iter(f"{redis_a.prefix}*"))
    redis_a.close()
    redis_b.close()

    report = {
        "cache_comparison": {
            "without_cache": without_cache,
            "with_memory_cache": with_memory,
            "delta": _delta(without_cache, with_memory),
        },
        "redis": {
            "metrics": with_redis,
            "shared_state_value": shared_value,
            "shared_state_score": shared_score,
            "keys": redis_keys,
        },
        "concurrent_load": {
            "workers": 10,
            "wall_time_ms": round(concurrent_wall_ms, 2),
            "metrics": concurrent_metrics.to_report_dict(),
        },
    }
    output = Path("reports/benchmarks.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"wrote {output}")


if __name__ == "__main__":
    main()
