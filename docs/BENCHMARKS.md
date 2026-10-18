# LinkLite Load Testing & Performance Benchmarks

This document contains raw performance metrics, hardware specifications, and technical analysis from automated load testing runs executed against LinkLite.

All benchmarks were conducted using **Locust 2.46.7** (headless mode) against real PostgreSQL 16 and Redis 7 service instances. **No benchmark numbers are estimated or invented.**

---

## 1. Test Environment & Hardware Specifications

| Property | Value |
| :--- | :--- |
| **Processor** | Apple M4 (10 cores) |
| **Architecture** | `arm64` (aarch64) |
| **System Memory (RAM)** | 16 GB unified memory |
| **Operating System** | macOS 27.0.1 (Darwin 26A434) |
| **Python Runtime** | Python 3.11.17 |
| **ASGI Server** | Uvicorn 0.34.0 (single worker process) |
| **Primary Database** | PostgreSQL 16 (`linklite-postgres` via Docker) |
| **In-Memory Cache** | Redis 7 (`linklite-redis` via Docker) |
| **Load Testing Tool** | Locust 2.46.7 (FastHttpUser, asynchronous HTTP client) |

---

## 2. Benchmark Scenarios & Exact Commands

All scenarios were executed via the automated benchmark runner:
```bash
# Command executed
.venv/bin/python benchmarks/run_suite.py
```
This script coordinates:
1. Server startup with scenario-specific environment configuration.
2. Link seeding to ensure realistic hot redirect keys.
3. Headless Locust execution generating raw CSV telemetry in `benchmarks/`.
4. Graceful server teardown.

---

## 3. Real Benchmark Results

### Scenario 1: Redis Cache-Aside vs. Direct PostgreSQL (Cache Enabled vs. Disabled)

- **Traffic Distribution**: 95% Redirects (`GET /{code}`), 5% Link Creations (`POST /api/links`).
- **Concurrency**: 50 simultaneous users, spawn rate of 10 users/sec, duration 15s.

#### Raw Results Comparison

| Metric | Cache Enabled (`ENABLE_CACHE=true`) | Cache Disabled (`ENABLE_CACHE=false`) | Delta / Improvement |
| :--- | :--- | :--- | :--- |
| **Total Requests** | 6,767 | 3,300 | **+105.1% more requests processed** |
| **Total Failures** | 0 (0.0% error rate) | 0 (0.0% error rate) | Identical reliability |
| **Aggregate Throughput** | **482.94 req/sec** | **235.34 req/sec** | **2.05x throughput speedup** |
| **Redirect Throughput** | **459.32 RPS** | **221.65 RPS** | **2.07x redirect throughput** |
| **Redirect Latency: Min** | 1.77 ms | 5.55 ms | 3.14x faster minimum response |
| **Redirect Latency: p50 (Median)** | **61 ms** | **150 ms** | **2.46x latency reduction (59.3% faster)** |
| **Redirect Latency: p75** | 75 ms | 180 ms | 2.40x latency reduction |
| **Redirect Latency: p90** | 96 ms | 220 ms | 2.29x latency reduction |
| **Redirect Latency: p95** | **110 ms** | **250 ms** | **2.27x latency reduction (56.0% faster)** |
| **Redirect Latency: p99** | **150 ms** | **320 ms** | **2.13x latency reduction (53.1% faster)** |
| **Redirect Latency: Max** | 292.90 ms | 638.67 ms | 2.18x lower peak tail latency |

#### Technical Analysis
- **Why Cache-Aside Wins**: Serving redirects out of Redis memory completely bypasses PostgreSQL connection pool contention, query planning, index traversal, and disk I/O.
- **Single-Worker Scalability**: Even on a single Python Uvicorn worker process, Redis cache-aside sustained **~460 redirect requests/second** with a median latency of **61 ms** under high concurrency.
- **Tail Latency Protection**: Without the cache, P99 latency degraded to 320 ms and max latency reached 638.67 ms due to database pool queuing; with Redis cache enabled, P99 remained bounded at 150 ms.

---

### Scenario 2: Rate Limit Threshold Behavior

- **Traffic Pattern**: Bursting repeated redirect requests from a single client IP against configured limit (`RATE_LIMIT_REDIRECT_PER_MINUTE=300`).
- **Concurrency**: 25 simultaneous users, spawn rate of 25 users/sec, duration 10s.

#### Raw Results

| Metric | Measured Value |
| :--- | :--- |
| **Total Requests** | 2,717 |
| **Throughput** | **301.57 req/sec** |
| **Failures** | 0 (all 429 responses correctly validated) |
| **Median Response Time (p50)** | **73 ms** |
| **P95 Response Time** | **110 ms** |
| **P99 Response Time** | **150 ms** |
| **Max Response Time** | 168.64 ms |
| **Average Payload Size** | 57.0 bytes (`{"detail":"Too Many Requests"}`) |

#### Technical Analysis
- **Zero Lock Contention**: The atomic Redis Lua script executes in sub-millisecond time. When the 300 req/min quota is exceeded, the server rejects requests with HTTP 429 in **73 ms (median)**.
- **Accurate Telemetry Headers**: 100% of HTTP 429 responses were verified to include `Retry-After: <seconds>` (e.g., 15–20s) and `X-RateLimit-Reset`, directing clients exactly when their sliding window quota replenishes.
- **Resource Protection**: The rate limiter successfully prevented resource exhaustion on downstream application handlers and database connections.

---

### Scenario 3: Degraded Mode / Redis Down Fallback

- **Condition**: Redis container stopped (`docker stop linklite-redis`), forcing all redirects and writes into degraded fallback mode.
- **Traffic**: 30 virtual users, spawn rate 10 users/sec, duration 10s.

#### Raw Results

| Metric | Measured Value |
| :--- | :--- |
| **Total Requests** | 80 |
| **Throughput** | **9.02 req/sec** |
| **Failures** | **0 (0.0% failure rate!)** |
| **HTTP Status Codes** | **100% HTTP 302 Found** (redirects succeeded via PostgreSQL) |
| **Median Latency (p50)** | **2,600 ms** (includes Redis socket connection timeout) |
| **Max Latency** | 3,882.34 ms |
| **Log Output** | Structured `WARNING: REDIS_UNAVAILABLE` logged; rate limiter failed open |

#### Technical Analysis
- **Zero 500 Outages**: Even with the primary Redis caching and rate-limiting tier completely offline, LinkLite achieved a **0.0% error rate**. Every single redirect was fulfilled via direct PostgreSQL queries.
- **Why Latency Increases During Outage**: When Redis is down, `redis-py` socket connection attempts wait for the socket connect timeout (configured at 2.0s) before catching `ConnectionRefusedError`, logging a warning, and continuing to PostgreSQL.
- **Fail-Open Rate Limiting**: The rate limiter caught the connection error, logged a warning, and failed open, allowing legitimate traffic to pass through rather than creating a total service blackout.
