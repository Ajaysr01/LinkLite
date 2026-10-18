import random
import uuid

from locust import FastHttpUser, between, task

# Global pool of active short codes shared across virtual users
KNOWN_CODES: list[str] = []


class LinkLiteUser(FastHttpUser):
    """Simulates realistic traffic: 95% redirects, 5% link creations."""

    # Pacing: 0 to 50ms between requests to simulate concurrent active users
    wait_time = between(0.001, 0.05)

    def on_start(self):
        """Seed a short link on user startup if the pool is small."""
        if len(KNOWN_CODES) < 50:
            unique_id = uuid.uuid4().hex[:8]
            payload = {
                "long_url": f"https://example.com/seeded-article-{unique_id}",
            }
            with self.client.post(
                "/api/links",
                json=payload,
                catch_response=True,
                name="/api/links [seed]",
            ) as res:
                if res.status_code == 201:
                    code = res.json().get("code")
                    if code and code not in KNOWN_CODES:
                        KNOWN_CODES.append(code)
                    res.success()
                elif res.status_code == 429:
                    res.success()  # Rate limit during seeding is expected

    @task(95)
    def redirect_hot_path(self):
        """Hot path: 95% of traffic is redirects."""
        if not KNOWN_CODES:
            code = "default_seed"
        else:
            code = random.choice(KNOWN_CODES)

        with self.client.get(
            f"/{code}",
            allow_redirects=False,
            catch_response=True,
            name="GET /{code} [redirect]",
        ) as res:
            if res.status_code == 302:
                res.success()
            elif res.status_code == 429:
                # Rate limited at threshold
                res.success()
            elif res.status_code == 404:
                res.success()  # Missing or not yet created
            else:
                res.failure(f"Unexpected status: {res.status_code}")

    @task(5)
    def create_link(self):
        """Write path: 5% of traffic creates new short links."""
        unique_id = uuid.uuid4().hex
        payload = {
            "long_url": f"https://techcrunch.com/article/{unique_id}",
        }
        with self.client.post(
            "/api/links",
            json=payload,
            catch_response=True,
            name="POST /api/links [create]",
        ) as res:
            if res.status_code == 201:
                code = res.json().get("code")
                if code and len(KNOWN_CODES) < 500:
                    KNOWN_CODES.append(code)
                res.success()
            elif res.status_code == 429:
                # Expected when create rate limit (10/min) is hit
                res.success()
            else:
                res.failure(f"Create failed with status: {res.status_code}")


class RateLimitThresholdUser(FastHttpUser):
    """Stress tests the rate limiter to verify threshold behavior and 429s."""

    wait_time = between(0.001, 0.01)

    @task
    def burst_redirects(self):
        with self.client.get(
            "/test_threshold_code",
            allow_redirects=False,
            catch_response=True,
            name="GET /{code} [threshold_test]",
        ) as res:
            if res.status_code in (302, 404):
                res.success()
            elif res.status_code == 429:
                # Verify telemetry headers
                if "Retry-After" in res.headers and "X-RateLimit-Reset" in res.headers:
                    res.success()
                else:
                    res.failure("Missing rate limit headers on 429 response")
            else:
                res.failure(f"Unexpected status code: {res.status_code}")
