# Reliability Engineering Final Report

## 1. Architecture

```text
Client -> ReliabilityGateway -> Semantic cache (memory or Redis)
                              | cache miss
                              v
                    Circuit breaker: primary -> Provider primary
                              | failure/open
                              v
                    Circuit breaker: backup  -> Provider backup
                              | failure/open
                              v
                         Static fallback
```

Each provider has an independent, thread-safe CLOSED/OPEN/HALF_OPEN circuit breaker. Only one HALF_OPEN probe is admitted. Sensitive queries bypass both cache backends, and mismatched four-digit years or IDs are rejected as semantic false hits.

## 2. Configuration and rationale

| Setting | Value | Rationale |
|---|---:|---|
| failure_threshold | 3 | Opens quickly after a short failure burst without reacting to one transient error. |
| reset_timeout_seconds | 2.0 | Short enough for the lab recovery SLO while still suppressing retry storms. |
| success_threshold | 1 | One successful probe restores service promptly. |
| cache TTL | 300 s | Limits stale responses while retaining repeated FAQ-style requests. |
| similarity_threshold | 0.92 | Conservative matching plus explicit year/ID false-hit protection. |
| load_test requests | 100 per scenario | Provides repeated traffic for cache and breaker behavior. |
| deterministic seed | 42 | Makes routes, failures, costs, and pass/fail results reproducible. |

## 3. SLO evaluation

SLOs use the controlled healthy benchmark for normal availability/latency, the forced-primary-failure scenario for fallback, and the full chaos run for recovery.

| SLI | Target | Actual | Met? |
|---|---:|---:|---|
| Healthy availability | >= 99% | 100.00% | YES |
| Healthy P95 latency | < 2500 ms | 234.48 ms | YES |
| Forced-failure fallback success | >= 95% | 100.00% | YES |
| Cache hit rate | >= 10% | 59.00% | YES |
| Recovery time | < 5000 ms | 2284.83 ms | YES |

## 4. Aggregate chaos metrics

| Metric | Value |
|---|---:|
| total_requests | 400 |
| availability | 0.75 |
| error_rate | 0.25 |
| latency_p50_ms | 0.01 |
| latency_p95_ms | 456.33 |
| latency_p99_ms | 529.1 |
| fallback_success_rate | 0.4444 |
| cache_hit_rate | 0.4575 |
| circuit_open_count | 13 |
| recovery_time_ms | 2284.8336696624756 |
| estimated_cost | 0.05173 |
| estimated_cost_saved | 0.10801 |

The aggregate includes deliberately destructive chaos traffic, so its availability is not interpreted as the normal-operation SLO.

## 5. Cache comparison

Both runs use 100 healthy requests and the same seed/workload.

| Metric | Without cache | Memory cache | Delta |
|---|---:|---:|---:|
| P50 latency (ms) | 207.85 | 0.0 | -207.85 |
| P95 latency (ms) | 238.49 | 234.48 | -4.01 |
| Estimated cost | 0.05914 | 0.02396 | -0.03518 |
| Cache hit rate | 0.00% | 59.00% | +59.00% |

Cost saved is estimated from the avoided primary-provider token cost for each cache hit rather than a fixed per-hit constant.

## 6. Redis shared cache evidence

An in-memory cache is process-local, so replicas cannot reuse one another's responses. Redis stores hashes with server-side TTLs, allowing independent gateway instances to share state.

- Instance B read value written by instance A: `visible from second instance`
- Exact-match score: `1.0`
- Redis benchmark availability: `100.00%`
- Redis benchmark cache hit rate: `59.00%`

```text
rl:cache:9e413fd814eb
rl:cache:d354658dc020
rl:cache:98332d0d1c9c
rl:cache:fff10da1c72c
rl:cache:095946136fea
rl:cache:844ef0143a5c
rl:cache:4fc3c69b9376
rl:cache:f840a1cb7d2c
rl:cache:8baa2cfa11fa
rl:cache:0bc3b1acf73d
rl:cache:734852f3cf4a
rl:cache:3936614ac4c2
rl:cache:3dab98c0e49e
rl:cache:dacb2b833659
```

## 7. Chaos scenarios

| Scenario | Expected | Observed | Result |
|---|---|---|---|
| primary_timeout_100 | Primary opens; backup serves requests | availability=100.00%, fallback=100.00%, opens=6 | PASS |
| primary_flaky_50 | Primary oscillates; fallback preserves availability | availability=100.00%, fallback=100.00%, opens=5 | PASS |
| all_healthy | Primary serves all misses without circuit opening | availability=100.00%, fallback=0.00%, opens=0 | PASS |
| all_providers_down | Both circuits open; bounded static fallback serves every request | availability=0.00%, fallback=0.00%, opens=2 | PASS |

## 8. Concurrent load

A 10-worker ThreadPoolExecutor completed 100 requests in 1105.61 ms.

| Metric | Value |
|---|---:|
| Availability | 100.00% |
| P95 latency | 232.65 ms |
| Cache hit rate | 50.00% |
| Circuit opens | 0 |

Circuit state and the in-memory cache are protected by locks; HALF_OPEN permits one probe, preventing a concurrent retry storm.

## 9. Failure analysis

The remaining production weakness is the Redis semantic lookup: it uses SCAN and computes similarity locally, making each miss O(n) as the cache grows. Before production, responses should be indexed in a vector database or Redis vector index, partitioned by tenant, and monitored with a cache-latency SLO. Circuit state is also process-local; a multi-replica deployment should coordinate breaker state or use an upstream service mesh.

## 10. Reproduction

```powershell
docker compose up -d
pip install -e ".[dev]"
python -m pytest -v
ruff check src tests scripts
mypy src
python scripts/run_chaos.py --config configs/default.yaml --out reports/metrics.json
python scripts/run_benchmarks.py
python scripts/generate_report.py
```

Validated result: 35 tests passed and all 7 pedagogical xfail requirements XPASS with Redis running.
