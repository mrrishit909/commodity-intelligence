# Performance

Docker stack (one uvicorn process, one worker, Postgres 16) after the demo: 592 weekly prices, 5,400 cargoes, two nowcast runs.
Reproduce with `make demo`, then `make load-test`.

## Reads: the balance view, the flows view and the anomaly list, in rotation, closed loop, 10 s per level

| Concurrency | Requests | Throughput | p50 | p95 | p99 | Errors |
|---|---|---|---|---|---|---|
| 1 | 1026 | 102.6 req/s | 5.2 ms | 21.3 ms | 25.3 ms | 0.00% |
| 8 | 1059 | 105.0 req/s | 69.5 ms | 124.5 ms | 160.4 ms | 0.00% |
| 32 | 1017 | 99.8 req/s | 255.1 ms | 313.6 ms | 347.2 ms | 0.00% |

Target from the blueprint: p95 under 500 ms for reads: met. The signals, fitted equations and nowcast are computed once per state of
knowledge (seed, shocks, current week) and kept in memory, so the balance view is a lookup plus serialisation; the anomaly list runs
six CUSUMs over 300 weeks on every call (about 20 ms), which is what caps throughput here. The flows view is three SQL aggregates
over the cargo table.

## Compute (in-process, one core)

| Work | Time |
|---|---|
| Generate six years of a market, 5,600 cargoes | 0.15 s |
| Fit the observation equations | under 10 ms |
| Kalman filter over 296 weeks and the nowcast | 10 ms |
| Inventory forecast, 400 paths × 12 weeks | 0.15 s |
| Scenario, baseline and alternative, 400 paths each | 0.35 s |
| 32-period back-test at load | 0.4 s |
| Evaluation: three markets, 96 nowcasts and forecasts | about 10 s |

Not measured: tick data, more commodities, or a feature store; the weekly series here are small enough that none is needed.
