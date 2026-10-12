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
