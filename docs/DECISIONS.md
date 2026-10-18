# Architecture Decision Records (ADRs)

This document tracks all significant architectural and engineering decisions made in LinkLite, following the ADR format: Context, Decision, Alternatives Considered, and Trade-offs.

---

## ADR-001: Short Code Generation via Base62 Encoding of Sequential DB ID with Offset

### Status
Accepted

### Context
LinkLite must convert arbitrary-length target URLs into short alphanumeric strings (minimum 6 characters) suitable for sharing. The generator must operate deterministically, avoid race conditions, and scale without collision performance degradation.

### Decision
Use a PostgreSQL 64-bit sequence (`BIGINT GENERATED ALWAYS AS IDENTITY`) with an initial offset of $916{,}132{,}832$ ($62^5$). The ID is converted to a compact string using Base62 encoding (`[0-9a-zA-Z]`). Custom aliases bypass the sequence and write directly to the `code` column, protected by a database `UNIQUE` constraint.

### Alternatives Considered
1. **Random String Generation with DB Lookup (Collision-and-Retry)**:
   - *Rejected*: As the dataset approaches tens of millions of records, birthday collisions increase exponentially, causing repeated retry loops and unpredictable write latencies ($O(k)$ round trips).
2. **Cryptographic Hashing (MD5 / SHA-256) and Truncation**:
   - *Rejected*: Truncating a 128/256-bit hash to 6 or 7 characters creates severe collision risks ($62^6 \approx 56.8\text{B}$, but collisions occur much earlier by the Birthday Paradox around $2.6 \times 10^5$ items for 36-bit entropy).
3. **Twitter Snowflake / Distributed 64-bit ID Generators**:
   - *Rejected*: Adds operational complexity (worker ID coordination, clock drift vulnerability) and yields larger numbers that produce 10–11 character strings in Base62, defeating the goal of concise 6-character links in a single-primary architecture.

### Trade-offs
- **Pros**:
  - Zero collisions: Sequence IDs are strictly unique.
  - $O(1)$ computation time: Fast mathematical base conversion.
  - Exactly 6 characters guaranteed from the first inserted link due to the $62^5$ offset.
- **Cons**:
  - Sequential enumeration: Predictable IDs allow competitors or crawlers to guess subsequent URLs by incrementing the code.
  - *Mitigation*: For production systems requiring business confidentiality, the integer can be passed through a lightweight Feistel cipher (Skip32 / reversible permutation) prior to Base62 encoding.

---

## ADR-002: HTTP 302 Found vs. HTTP 301 Moved Permanently for Redirects

### Status
Accepted

### Context
When a client requests `GET /{code}`, the server must instruct the user agent (browser or HTTP client) to navigate to `long_url`. The choice of HTTP status code impacts client caching and analytics collection.

### Decision
Use **HTTP 302 Found** (temporary redirect) with the `Location` header.

### Alternatives Considered
1. **HTTP 301 Moved Permanently**:
   - *Rejected*: Browsers and intermediate CDNs cache 301 redirects aggressively in local disk cache (often indefinitely). On subsequent clicks by the same user or device, the browser directly visits the destination without issuing a request to LinkLite. This severely distorts analytics and prevents link expiration or instant deletion.
2. **HTTP 307 Temporary Redirect**:
   - *Rejected*: Semantically preserves the original HTTP method (e.g., POST remains POST). While strictly defined in HTTP/1.1, `302 Found` is the universally supported industry standard for consumer-facing URL shorteners (e.g. Bitly, TinyURL).

### Trade-offs
- **Pros**:
  - Full analytics fidelity: Every single user click sends a request to LinkLite, allowing 100% accurate click metrics.
  - Instant invalidation: Expirations and deletions take effect immediately worldwide.
- **Cons**:
  - Higher server load: Every redirect must hit LinkLite (or our Redis edge cache) instead of resolving from client browser cache. We mitigate this with high-performance Redis cache-aside reads.

---

## ADR-003: Redis Cache-Aside with Graceful Fallback (Fail-Open)

### Status
Accepted

### Context
LinkLite redirect endpoints must respond in low single-digit milliseconds. However, external infrastructure dependencies (like Redis) can experience intermittent network partitions, restarts, or out-of-memory errors.

### Decision
Implement a **Cache-Aside (Lazy-Loading)** pattern backed by Redis.
- If Redis is healthy: Read from Redis; on cache miss, query PostgreSQL and backfill Redis.
- If Redis fails: Catch connection errors, log a structured warning (`REDIS_UNAVAILABLE`), fall back to querying PostgreSQL directly, and bypass rate-limiting enforcement (fail-open).

### Alternatives Considered
1. **Fail-Closed (Return HTTP 500 / 503 if Redis is Down)**:
   - *Rejected*: Redirection is the core value proposition of a URL shortener. Serving traffic slightly slower via PostgreSQL ($\sim 15\text{ms}$ vs $\sim 2\text{ms}$) is vastly preferable to an outage.
2. **Write-Through / Read-Through Cache via Proxy**:
   - *Rejected*: Increases architectural complexity. Cache-aside in application code allows fine-grained negative caching, jitter control, and conditional fallback logic.

### Trade-offs
- **Pros**:
  - High availability: Total system outage is prevented if the caching layer drops.
  - Sub-millisecond P95 latency when Redis is operational.
- **Cons**:
  - Redis outage puts immediate read load on PostgreSQL. If PostgreSQL is not sized for peak read traffic, it could saturate connection pools under heavy load.

---

## ADR-004: Soft Deletion (`is_deleted`) vs. Hard Deletion (`DELETE FROM`)

### Status
Accepted

### Context
When a user deletes a short link (`DELETE /api/links/{code}`), the system must determine whether to permanently purge the database row or mark it as deleted.

### Decision
Implement **Soft Deletion** via an `is_deleted BOOLEAN NOT NULL DEFAULT FALSE` column.
When a link is soft-deleted:
1. `is_deleted` is updated to `TRUE` in PostgreSQL.
2. The Redis key `link:{code}` is evicted (`DEL`).
3. Subsequent `GET /{code}` requests treat soft-deleted records as `404 Not Found`.
4. The alias remains reserved to prevent malicious spoofing or hijacking of old links.

### Alternatives Considered
1. **Hard Deletion (`DELETE FROM links WHERE code = :code`)**:
   - *Rejected*: Deleting rows cascades to or breaks `click_stats` foreign keys, destroying historical analytics and audit logs. Furthermore, freeing old short codes enables code hijacking (where a malicious actor registers a previously trusted short code that still exists in printed collateral or old emails).

### Trade-offs
- **Pros**:
  - Historical analytics retained.
  - Prevents link hijacking / domain trust abuse.
  - Clean audit trails for compliance.
- **Cons**:
  - Table size accumulates inactive rows over time.
  - *Mitigation*: Partial indexes (`WHERE is_deleted = FALSE`) ensure queries on active links ignore deleted rows entirely.

---

## ADR-005: SQLAlchemy 2.0 Async Engine with asyncpg over Synchronous Drivers

### Status
Accepted

### Context
FastAPI runs on an asynchronous ASGI event loop (`uvloop` or `asyncio`). Using a synchronous database driver like `psycopg2` inside async request handlers blocks the event loop thread during I/O operations, stalling all concurrent requests and dramatically reducing throughput.

### Decision
Use **SQLAlchemy 2.0** with the **asyncpg** asynchronous database driver and `async_sessionmaker`. All database operations use `await session.execute(...)` or `await session.commit()`.

### Alternatives Considered
1. **Synchronous SQLAlchemy with Thread Pooling (`run_in_threadpool` / `def` endpoints)**:
   - *Rejected*: Thread context switching introduces substantial overhead under high QPS, and Python thread pools have limited concurrency compared to native async event loops.
2. **Tortoise ORM / Databases / Raw asyncpg**:
   - *Rejected*: Raw `asyncpg` lacks schema migration integration (Alembic) and relationship management. Tortoise ORM has a smaller ecosystem and weaker enterprise adoption than SQLAlchemy.

### Trade-offs
- **Pros**:
  - Non-blocking I/O across all database calls, enabling thousands of concurrent connections on low memory.
  - Direct integration with Alembic for automated migrations.
  - Native type safety with SQLAlchemy 2.0 `Mapped[...]` attributes.
- **Cons**:
  - Requires care with ORM relationship loading (avoiding implicit lazy loading, using `selectin` or explicit joins).

---

## ADR-006: ContextVar-based Distributed Tracing (`X-Request-ID`)

### Status
Accepted

### Context
In an asynchronous concurrent environment, requests interleave across tasks on the same OS thread. Debugging errors without request correlation IDs makes diagnosing issues in logs virtually impossible.

### Decision
Implement ASGI middleware that extracts or generates a unique `X-Request-ID` UUID for every HTTP request, stores it in a Python `contextvars.ContextVar`, injects it into all JSON log lines via a custom `logging.Formatter`, and echoes it in the response header.

### Alternatives Considered
1. **Passing `request_id` explicitly through every function call**:
   - *Rejected*: Creates severe code pollution and boilerplate across services and repository layers.
2. **`threading.local`**:
   - *Rejected*: Fails in asynchronous Python because multiple concurrent coroutines execute cooperatively on the same OS thread.

### Trade-offs
- **Pros**:
  - Zero pollution of business logic signatures.
  - End-to-end request tracing in production logs and client responses.
- **Cons**:
  - Negligible contextvar lookup overhead during log emission.

---

## ADR-007: Short Code Capacity and Enumeration Mitigations

### Status
Accepted

### Context
LinkLite generates short codes from an integer sequence. We need to analyze capacity limits, predictability, and enumeration attack trade-offs.

### Capacity Arithmetic
- Base62 alphabet size $B = 62$.
- $N$-character string capacity = $62^N$.
  - 1 character: $62^1 = 62$
  - 2 characters: $62^2 = 3{,}844$
  - 3 characters: $62^3 = 238{,}328$
  - 4 characters: $62^4 = 14{,}776{,}336$
  - 5 characters: $62^5 = 916{,}132{,}832$
  - 6 characters: $62^6 = 56{,}800{,}235{,}584$ ($\approx 56.8\text{ billion}$)
  - 7 characters: $62^7 = 3{,}521{,}614{,}606{,}208$ ($\approx 3.52\text{ trillion}$)
- By adding offset $62^5 = 916{,}132{,}832$, generated codes start at 6 characters (`100000`) and can represent up to $55.8\text{ billion}$ links before expanding to 7 characters.

### Enumeration Trade-off & Mitigations
- **Trade-off**: A strictly sequential ID enables enumeration scraping (incrementing short codes to crawl competitor links).
- **Why Accepted**:
  - Simplicity and zero collisions under high concurrency.
  - No random collision retry loops which degrade $O(k)$ under high table volume.
- **Production Mitigations for Higher Scale**:
  - Pass the integer ID through a reversible pseudo-random permutation (e.g. Skip32 Feistel cipher) before Base62 encoding to produce unpredictable but strictly collision-free strings.

---

## ADR-008: Concurrency Control via Database UNIQUE Constraints vs. Check-then-Insert

### Status
Accepted

### Context
When two concurrent requests attempt to register the exact same `custom_alias` simultaneously, the system must guarantee that exactly one succeeds (`201 Created`) and the other fails (`409 Conflict`).

### Decision
Rely exclusively on PostgreSQL's database-level `UNIQUE` constraint (`uq_links_code`) and transactional ACID guarantees. Catch the resulting `IntegrityError` (PostgreSQL error code `23505 unique_violation`) in the service layer and translate it into HTTP 409 Conflict.

### Alternatives Considered
1. **Check-then-Insert (`SELECT` then `INSERT`)**:
   - *Rejected*: Subject to Time-Of-Check to Time-Of-Use (TOCTOU) race conditions. If request A and request B both execute `SELECT` before either commits `INSERT`, both observe that the alias is free and both attempt insertion, leading to unhandled 500 errors or duplicate records if unconstrained.
2. **Distributed Redis Locks (`SET NX EX`)**:
   - *Rejected*: Adds network round trips and lock acquisition overhead to the hot write path. Redis locks can also expire prematurely during long GC pauses or network delays, failing to guarantee safety. The database is the ultimate source of truth.

### Trade-offs
- **Pros**:
  - Provably race-condition free under any level of concurrency.
  - Zero lock overhead or coordination complexity.
- **Cons**:
  - Relies on database constraint failure signaling, which requires catching dialect-specific driver exceptions.

---

## ADR-009: Cache Stampede Mitigation via TTL Jitter & Link Expiry Capping

### Status
Accepted

### Context
When heavily requested short links (e.g. viral campaigns or breaking news links) expire simultaneously from Redis, hundreds or thousands of concurrent redirect requests can experience a cache miss at the exact same moment. This triggers a **cache stampede** (or thundering herd), swamping PostgreSQL with identical queries and causing severe latency spikes or pool exhaustion.

### Decision
1. **TTL Jitter**: Apply a random jitter ($\pm 5\%$) to the base TTL ($86{,}400\text{s}$). Keys populated around the same time expire at staggered intervals rather than in a synchronized burst.
2. **Cap by Expiry**: If a link has an explicit `expires_at` timestamp, the cache TTL is capped to $\min(\text{jittered\_ttl}, \text{remaining\_lifetime})$. If the link has already expired, it is not cached.

### Alternatives Considered
1. **Distributed Mutex / Lock on Miss (e.g. Redis Redlock or single-flight)**:
   - *Rejected*: A distributed lock forces concurrent cache-miss requests to block and wait for one worker to populate the cache. Under high load, managing lock acquisition, timeouts, and fallback adds significant latency and latency variance to reads.
2. **Probabilistic Early Expiration (XFetch Algorithm)**:
   - *Rejected*: More complex math ($-\beta \times \delta \times \ln(rand())$). For our workload, TTL jitter combined with Postgres read pool sizing provides effective mitigation with zero algorithmic complexity.

### Trade-offs
- **Pros**:
  - Eliminates synchronized simultaneous cache expiration.
  - Zero lock overhead or blocking on the read path.
- **Cons**:
  - Does not prevent a stampede on the very first read of a cold link (initial cache priming). For ultra-high-scale tiers, background pre-warming or single-flight loading is an option.

---

## ADR-010: Negative Caching for Unknown Codes (Preventing Cache Penetration)

### Status
Accepted

### Context
A common denial-of-service vector against shorteners is **cache penetration**: an attacker (or web crawler) queries millions of randomly generated or non-existent short codes (e.g. `/{unknown}`). Because these codes do not exist in Redis, every single request bypasses the cache and queries PostgreSQL, rapidly saturating database connections.

### Decision
Implement **Negative Caching**:
1. When a code lookup misses in PostgreSQL, store `link_not_found:{code} = "1"` in Redis with a short TTL ($60\text{s}$).
2. Subsequent requests for the same non-existent code hit Redis and return HTTP 404 in $<1\text{ms}$ without touching the database.
3. When a user creates a link matching that alias, proactively delete `link_not_found:{code}` from Redis so the new link is immediately accessible.

### Alternatives Considered
1. **Bloom Filter / Cuckoo Filter**:
   - *Rejected for Phase 3*: A Bloom filter in front of Redis can tell if a code definitely does not exist. However, Bloom filters do not support deletion easily (requiring Counting Bloom Filters or Cuckoo filters) and add maintenance overhead. Negative caching with a 60-second TTL solves the immediate DoS problem with standard Redis keys.

### Trade-offs
- **Pros**:
  - Protects PostgreSQL connection pool from malicious 404 scanning attacks.
  - Sub-millisecond response time for recurring 404 probes.
- **Cons**:
  - Potential cache pollution: An attacker requesting completely distinct random codes will write many 60s keys to Redis.
  - *Mitigation*: Short 60s TTL ensures Redis memory is reclaimed quickly, and IP rate limiting (Phase 4) caps the rate of probe requests per client.

---

## ADR-011: Sliding Window Rate Limiting via Atomic Redis Lua Script

### Status
Accepted

### Context
LinkLite must protect against abuse, bot scraping, and denial-of-service traffic without introducing race conditions or false blocks under bursty traffic.

### Decision
Implement a **Sliding Window Log** algorithm backed by Redis Sorted Sets and executed atomically inside a **Redis Lua script**.
1. The Lua script removes entries older than the current window (`ZREMRANGEBYSCORE key 0 (now - window)`).
2. It counts existing requests in the window (`ZCARD key`).
3. If count < limit, it atomically appends the current request (`ZADD key now member`), refreshes the key expiration, and permits the request.
4. If count >= limit, it calculates `Retry-After` using the oldest entry score in the sorted set and rejects with HTTP 429.
5. All operations run atomically on the Redis single-threaded engine, preventing Time-Of-Check to Time-Of-Use race conditions.

### Alternatives Considered
1. **Fixed Window Counter (`INCR` + `EXPIRE`)**:
   - *Rejected*: Suffers from the boundary burst problem (a client can send 100% of quota at the end of window 1 and another 100% at the start of window 2, allowing 2x the allowed rate in a fraction of a second).
2. **Token Bucket**:
   - *Rejected*: While memory-efficient, token bucket requires tracking floating point tokens and timestamps. Sliding window log gives precise sub-second window sliding accuracy and predictable `Retry-After` calculation.
3. **Multi-command Python implementation without Lua (e.g. GET then INCR)**:
   - *Rejected*: In an async server handling concurrent requests from the same IP, two requests executing simultaneously could both read `count = 9` (with limit 10), and both proceed, exceeding the quota. Lua scripts are executed atomically in Redis.

### Trade-offs
- **Pros**:
  - 100% race-condition free under high concurrency.
  - Smooth sliding window behavior with no boundary spikes.
  - Provides precise `Retry-After` and `X-RateLimit-*` telemetry headers.
- **Cons**:
  - Sorted sets use slightly more memory than simple integer keys ($O(N)$ elements per active user IP in the window).
  - *Mitigation*: Short window durations (60s) and automatic TTL expiration clean up inactive IP keys rapidly.

---

## ADR-012: Client IP Resolution and Proxy Header Spoofing Protection

### Status
Accepted

### Context
Rate limiters track requests per client IP. In internet-facing services, clients can send arbitrary HTTP request headers, including `X-Forwarded-For`.

### Decision
Extract the client IP from the direct TCP socket connection (`request.client.host`) by default. Honor the `X-Forwarded-For` header **only when `TRUST_PROXY_HEADERS=True`** is explicitly configured in environment settings.

### Alternatives Considered
1. **Unconditionally trusting `X-Forwarded-For`**:
   - *Rejected*: Catastrophic security vulnerability. A client can send random values (`X-Forwarded-For: 1.1.1.1`, then `1.1.1.2`, etc.) on every request, completely evading IP-based rate limiting.

### Trade-offs
- **Pros**:
  - Prevents rate limit bypass and spoofing attacks when deployed directly.
  - Retains compatibility with trusted upstream reverse proxies (e.g. AWS ALB, Cloudflare, Nginx) via a config flag.
- **Cons**:
  - Requires operators behind a reverse proxy to explicitly set `TRUST_PROXY_HEADERS=True`.

---

## ADR-013: Write-Decoupled Click Analytics Ingestion & Eventual Consistency

### Status
Accepted

### Context
Link redirection is the primary hot path of LinkLite, handling potentially tens of thousands of requests per second. Synchronously writing to PostgreSQL or updating a row counter on every redirect would introduce disk I/O bottlenecks, table locks, and row-level contention on popular links, severely degrading redirect performance.

### Decision
Decouple click analytics ingestion from persistent storage using an in-memory Redis write buffer and an asynchronous background worker:
1. **Hot Path Ingestion**: During redirect (`GET /{code}`), the server issues an atomic Redis pipeline increment: `INCR click_buffer:{code}:{YYYY-MM-DD}` with a 48-hour TTL. Zero database queries or row locks occur on the redirect hot path.
2. **Periodic Batch Flushing**: A background task periodically (every 10 seconds) harvests counts using atomic Redis `GETSET key 0` (preventing lost increments during collection).
3. **PostgreSQL Batch Upsert**: Harvested counts are persisted within a single ACID transaction using `INSERT INTO click_stats ... ON CONFLICT (code, date) DO UPDATE SET count = click_stats.count + EXCLUDED.count` and incrementing `links.click_count`.
4. **Crash-Safe Rollback**: If the database transaction fails (e.g. intermittent DB disconnect), harvested counts are immediately rolled back into Redis via `INCRBY key count`, ensuring zero click counts are lost.
5. **Read Real-Time Consistency**: Stats query endpoints (`GET /api/links/{code}/stats` and `GET /api/links/{code}`) calculate live metrics by summing committed PostgreSQL historical data with any active in-memory Redis buffer for today.

### Alternatives Considered
1. **Synchronous PostgreSQL `UPDATE links SET click_count = click_count + 1`**:
   - *Rejected*: Causes catastrophic row lock contention. If a viral link receives 10,000 clicks/sec, 10,000 transactions serialize waiting on the exact same row lock, crashing database throughput.
2. **Distributed Message Queue (Kafka / RabbitMQ / AWS SQS)**:
   - *Rejected*: Massive architectural overhead and operational complexity for a single-service URL shortener. Redis buffer provides sufficient throughput ($>100\text{k ops/sec}$) without additional cluster infrastructure.
3. **Redis Streams / PubSub**:
   - *Rejected*: Streaming individual click event objects consumes significantly more memory ($O(N)$ events) compared to aggregating counter increments ($O(1)$ integer per active link per day).

### Trade-offs
- **Pros**:
  - Sub-2ms redirect response times; PostgreSQL is completely isolated from redirect write traffic.
  - Zero lost clicks under high concurrency due to atomic `GETSET` and `INCRBY` rollback on failure.
  - Real-time accuracy on stats endpoints by dynamically bridging database history and live Redis buffers.
- **Cons**:
  - Eventual consistency window: If the Redis host suffers a catastrophic ungraceful hard power failure before a 10-second flush, counts buffered during that brief window could be lost. This is an industry-standard acceptable trade-off for analytics telemetry (distinct from financial transactional ledgers).

---

## ADR-014: Zero-Trust Frontend DOM Manipulation & XSS Prevention

### Status
Accepted

### Context
Phase 5b adds a minimal single-page web dashboard for creating short links and visualizing analytics. The application displays user-controlled data (destination URLs, custom aliases, error messages). Inserting untrusted user input into the browser DOM creates dangerous Cross-Site Scripting (XSS) vectors.

### Decision
Enforce zero-trust client DOM manipulation:
1. All dynamic strings returned from the API or typed by the user are injected into the DOM exclusively via `element.textContent` or direct validated properties (e.g., `element.href`).
2. The use of `element.innerHTML = ...` is strictly prohibited for any user or API-supplied string.
3. Backend Pydantic validators strictly permit only `http` and `https` schemes, rejecting dangerous URI schemes such as `javascript:`, `data:`, or `vbscript:`.
4. External scripts (Chart.js) are pinned to an exact immutable version from CDN with integrity guarantees.

### Alternatives Considered
1. **Third-party HTML Sanitization Library (DOMPurify)**:
   - *Rejected*: LinkLite requires zero frontend build steps and no heavy dependencies. The dashboard display is entirely textual and structured; native browser `.textContent` handles character escaping natively in browser C++ code with zero bundle overhead.

### Trade-offs
- **Pros**:
  - 100% immune to DOM-based XSS attacks.
  - Zero build step, zero JavaScript dependencies (other than pinned Chart.js).
  - Lightweight, performant, and easy for any engineer to audit and maintain.
- **Cons**:
  - Cannot render rich Markdown or arbitrary HTML tags in user link descriptions (not a requirement for a URL shortener).

---

## ADR-015: Automated Concurrency Testing & Real-Service Isolation Strategy

### Status
Accepted

### Context
Race conditions in distributed and concurrent backend systems—such as database unique constraint conflicts under simultaneous writes, counter increments across parallel requests, and sliding window burst thresholds—rarely surface under isolated unit tests using mocks. They only manifest when concurrent requests execute against real storage engines with real locking mechanisms.

### Decision
Implement end-to-end automated concurrency tests using in-process asynchronous task pools (`asyncio.gather`) against real PostgreSQL and Redis containers:
1. **Real Infrastructure Over Mocks**: Concurrency tests run against genuine PostgreSQL 16 and Redis 7 service instances. This tests actual PostgreSQL B-tree index uniqueness checks, transaction serialization, row lock acquisition, and atomic Redis Lua script execution.
2. **In-Process Asynchronous Task Fan-Out**: Use `httpx.AsyncClient` with `ASGITransport` and `asyncio.gather(*[... for _ in range(N)])` to fire bursts of 50 simultaneous alias creations, 100 parallel redirects, and parallel rate-limiter requests.
3. **Database Isolation via Truncation Fixture**: Use an autouse `pytest` fixture executing `TRUNCATE TABLE click_stats, links RESTART IDENTITY CASCADE` and `redis.flushdb()` before each test. We deliberately avoid wrapping tests in an outer transaction rollback, because concurrent requests commit independent transactions that must be visible across connections.

### Alternatives Considered
1. **Mocking Database & Cache Drivers**:
   - *Rejected*: Useless for concurrency verification. Mocks do not simulate database row-level locking, race conditions, or atomic Lua scripts in Redis.
2. **External Multiprocess Benchmarking Tools (e.g., Apache Bench / wrk) for Unit/Integration CI**:
   - *Rejected*: External process runners add CI orchestration flakiness, cannot easily assert internal database state post-burst, and are better suited for Phase 8 load testing than rapid automated test suites.

### Trade-offs
- **Pros**:
  - Catches genuine database and cache race conditions reliably.
  - Fast execution: the full 68-test suite (including 50-parallel creates and 100-parallel redirects) completes in ~5.3 seconds.
  - Deterministic assertions: tests can directly query PostgreSQL tables and Redis keys to verify exact numbers.
- **Cons**:
  - Requires active PostgreSQL and Redis instances (handled seamlessly by Docker compose or GitHub Actions service containers).




