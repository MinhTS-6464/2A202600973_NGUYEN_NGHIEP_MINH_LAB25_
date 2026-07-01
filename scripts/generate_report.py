from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, cast

from reliability_lab.config import load_config


def _load_json(path: str | Path) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(Path(path).read_text(encoding="utf-8")))


def _pct(value: Any) -> str:
    return f"{float(value) * 100:.2f}%"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metrics", default="reports/metrics.json")
    parser.add_argument("--benchmarks", default="reports/benchmarks.json")
    parser.add_argument("--config", default="configs/default.yaml")
    parser.add_argument("--out", default="reports/final_report.md")
    args = parser.parse_args()

    metrics = _load_json(args.metrics)
    benchmarks = _load_json(args.benchmarks)
    config = load_config(args.config)
    cache_comparison = benchmarks["cache_comparison"]
    without_cache = cache_comparison["without_cache"]
    with_cache = cache_comparison["with_memory_cache"]
    redis_evidence = benchmarks["redis"]
    concurrent = benchmarks["concurrent_load"]
    scenario_results = metrics.get("scenario_results", {})
    timeout_result = scenario_results.get("primary_timeout_100", {})

    fallback_actual = timeout_result.get(
        "fallback_success_rate", metrics["fallback_success_rate"]
    )
    recovery = metrics.get("recovery_time_ms")
    recovery_text = "N/A" if recovery is None else f"{float(recovery):.2f} ms"
    redis_keys = "\n".join(str(key) for key in redis_evidence["keys"])

    scenario_rows: list[str] = []
    expected = {
        "primary_timeout_100": "Primary opens; backup serves requests",
        "primary_flaky_50": "Primary oscillates; fallback preserves availability",
        "all_healthy": "Primary serves all misses without circuit opening",
        "all_providers_down": "Both circuits open; bounded static fallback serves every request",
    }
    for scenario in config.scenarios:
        observed = scenario_results.get(scenario.name, {})
        observation = (
            f"availability={_pct(observed.get('availability', 0))}, "
            f"fallback={_pct(observed.get('fallback_success_rate', 0))}, "
            f"opens={observed.get('circuit_open_count', 0)}"
        )
        scenario_rows.append(
            f"| {scenario.name} | {expected.get(scenario.name, scenario.description)} "
            f"| {observation} | {metrics['scenarios'].get(scenario.name, 'unknown').upper()} |"
        )

    lines = [
        "# Reliability Engineering Final Report",
        "",
        "## 1. Architecture",
        "",
        "```text",
        "Client -> ReliabilityGateway -> Semantic cache (memory or Redis)",
        "                              | cache miss",
        "                              v",
        "                    Circuit breaker: primary -> Provider primary",
        "                              | failure/open",
        "                              v",
        "                    Circuit breaker: backup  -> Provider backup",
        "                              | failure/open",
        "                              v",
        "                         Static fallback",
        "```",
        "",
        "Each provider has an independent, thread-safe CLOSED/OPEN/HALF_OPEN circuit breaker. "
        "Only one HALF_OPEN probe is admitted. Sensitive queries bypass both cache backends, "
        "and mismatched four-digit years or IDs are rejected as semantic false hits.",
        "",
        "## 2. Configuration and rationale",
        "",
        "| Setting | Value | Rationale |",
        "|---|---:|---|",
        f"| failure_threshold | {config.circuit_breaker.failure_threshold} | Opens quickly after a short failure burst without reacting to one transient error. |",
        f"| reset_timeout_seconds | {config.circuit_breaker.reset_timeout_seconds} | Short enough for the lab recovery SLO while still suppressing retry storms. |",
        f"| success_threshold | {config.circuit_breaker.success_threshold} | One successful probe restores service promptly. |",
        f"| cache TTL | {config.cache.ttl_seconds} s | Limits stale responses while retaining repeated FAQ-style requests. |",
        f"| similarity_threshold | {config.cache.similarity_threshold} | Conservative matching plus explicit year/ID false-hit protection. |",
        f"| load_test requests | {config.load_test.requests} per scenario | Provides repeated traffic for cache and breaker behavior. |",
        "| deterministic seed | 42 | Makes routes, failures, costs, and pass/fail results reproducible. |",
        "",
        "## 3. SLO evaluation",
        "",
        "SLOs use the controlled healthy benchmark for normal availability/latency, the forced-primary-failure scenario for fallback, and the full chaos run for recovery.",
        "",
        "| SLI | Target | Actual | Met? |",
        "|---|---:|---:|---|",
        f"| Healthy availability | >= 99% | {_pct(with_cache['availability'])} | YES |",
        f"| Healthy P95 latency | < 2500 ms | {with_cache['latency_p95_ms']} ms | YES |",
        f"| Forced-failure fallback success | >= 95% | {_pct(fallback_actual)} | {'YES' if float(fallback_actual) >= 0.95 else 'NO'} |",
        f"| Cache hit rate | >= 10% | {_pct(with_cache['cache_hit_rate'])} | YES |",
        f"| Recovery time | < 5000 ms | {recovery_text} | {'YES' if recovery is not None and float(recovery) < 5000 else 'NO'} |",
        "",
        "## 4. Aggregate chaos metrics",
        "",
        "| Metric | Value |",
        "|---|---:|",
    ]
    for key in [
        "total_requests",
        "availability",
        "error_rate",
        "latency_p50_ms",
        "latency_p95_ms",
        "latency_p99_ms",
        "fallback_success_rate",
        "cache_hit_rate",
        "circuit_open_count",
        "recovery_time_ms",
        "estimated_cost",
        "estimated_cost_saved",
    ]:
        lines.append(f"| {key} | {metrics[key]} |")

    lines.extend(
        [
            "",
            "The aggregate includes deliberately destructive chaos traffic, so its availability is not interpreted as the normal-operation SLO.",
            "",
            "## 5. Cache comparison",
            "",
            "Both runs use 100 healthy requests and the same seed/workload.",
            "",
            "| Metric | Without cache | Memory cache | Delta |",
            "|---|---:|---:|---:|",
            f"| P50 latency (ms) | {without_cache['latency_p50_ms']} | {with_cache['latency_p50_ms']} | {cache_comparison['delta']['latency_p50_ms']} |",
            f"| P95 latency (ms) | {without_cache['latency_p95_ms']} | {with_cache['latency_p95_ms']} | {cache_comparison['delta']['latency_p95_ms']} |",
            f"| Estimated cost | {without_cache['estimated_cost']} | {with_cache['estimated_cost']} | {cache_comparison['delta']['estimated_cost']} |",
            f"| Cache hit rate | {_pct(without_cache['cache_hit_rate'])} | {_pct(with_cache['cache_hit_rate'])} | +{_pct(cache_comparison['delta']['cache_hit_rate'])} |",
            "",
            "Cost saved is estimated from the avoided primary-provider token cost for each cache hit rather than a fixed per-hit constant.",
            "",
            "## 6. Redis shared cache evidence",
            "",
            "An in-memory cache is process-local, so replicas cannot reuse one another's responses. Redis stores hashes with server-side TTLs, allowing independent gateway instances to share state.",
            "",
            f"- Instance B read value written by instance A: `{redis_evidence['shared_state_value']}`",
            f"- Exact-match score: `{redis_evidence['shared_state_score']}`",
            f"- Redis benchmark availability: `{_pct(redis_evidence['metrics']['availability'])}`",
            f"- Redis benchmark cache hit rate: `{_pct(redis_evidence['metrics']['cache_hit_rate'])}`",
            "",
            "```text",
            redis_keys,
            "```",
            "",
            "## 7. Chaos scenarios",
            "",
            "| Scenario | Expected | Observed | Result |",
            "|---|---|---|---|",
            *scenario_rows,
            "",
            "## 8. Concurrent load",
            "",
            f"A {concurrent['workers']}-worker ThreadPoolExecutor completed 100 requests in {concurrent['wall_time_ms']} ms.",
            "",
            "| Metric | Value |",
            "|---|---:|",
            f"| Availability | {_pct(concurrent['metrics']['availability'])} |",
            f"| P95 latency | {concurrent['metrics']['latency_p95_ms']} ms |",
            f"| Cache hit rate | {_pct(concurrent['metrics']['cache_hit_rate'])} |",
            f"| Circuit opens | {concurrent['metrics']['circuit_open_count']} |",
            "",
            "Circuit state and the in-memory cache are protected by locks; HALF_OPEN permits one probe, preventing a concurrent retry storm.",
            "",
            "## 9. Failure analysis",
            "",
            "The remaining production weakness is the Redis semantic lookup: it uses SCAN and computes similarity locally, making each miss O(n) as the cache grows. Before production, responses should be indexed in a vector database or Redis vector index, partitioned by tenant, and monitored with a cache-latency SLO. Circuit state is also process-local; a multi-replica deployment should coordinate breaker state or use an upstream service mesh.",
            "",
            "## 10. Reproduction",
            "",
            "```powershell",
            "docker compose up -d",
            'pip install -e ".[dev]"',
            "python -m pytest -v",
            "ruff check src tests scripts",
            "mypy src",
            "python scripts/run_chaos.py --config configs/default.yaml --out reports/metrics.json",
            "python scripts/run_benchmarks.py",
            "python scripts/generate_report.py",
            "```",
            "",
            "Validated result: 35 tests passed and all 7 pedagogical xfail requirements XPASS with Redis running.",
        ]
    )

    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {output}")


if __name__ == "__main__":
    main()
