import csv
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
PYTHON_EXE = sys.executable


def wait_for_server(url="http://127.0.0.1:8000/health", timeout=15):
    """Wait until uvicorn server is up and responsive."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                if resp.status in (200, 207, 503):
                    return True
        except urllib.error.HTTPError as e:
            if e.code in (200, 207, 503):
                return True
            time.sleep(0.5)
        except Exception:
            time.sleep(0.5)
    return False


def seed_test_link():
    """Create initial short link for load testing."""
    req_data = json.dumps(
        {
            "long_url": "https://example.com/target-benchmark-page",
            "custom_alias": "bench-target",
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        "http://127.0.0.1:8000/api/links",
        data=req_data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            print(f"Seeded benchmark link: {resp.status}")
    except Exception as e:
        print(f"Seed request info: {e}")


def read_csv_stats(csv_prefix):
    stats_file = f"{csv_prefix}_stats.csv"
    if not os.path.exists(stats_file):
        return None
    rows = []
    with open(stats_file, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append(r)
    return rows


def run_scenario(
    name,
    env_overrides,
    locust_users=50,
    locust_rate=10,
    duration="15s",
    user_class="LinkLiteUser",
):
    print("\n=======================================================")
    print(f" RUNNING BENCHMARK SCENARIO: {name}")
    print("=======================================================")

    env = os.environ.copy()
    env.update(env_overrides)

    # Start uvicorn server
    server_cmd = [
        PYTHON_EXE,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        "8000",
        "--log-level",
        "warning",
    ]
    server_proc = subprocess.Popen(server_cmd, cwd=BASE_DIR, env=env)

    try:
        if not wait_for_server():
            raise RuntimeError("Server failed to start within timeout.")
        print("Server is healthy and ready.")

        # Seed link
        seed_test_link()

        csv_prefix = str(BASE_DIR / "benchmarks" / f"results_{name.lower().replace(' ', '_')}")
        locust_cmd = [
            PYTHON_EXE,
            "-m",
            "locust",
            "-f",
            "benchmarks/locustfile.py",
            "--headless",
            "-u",
            str(locust_users),
            "-r",
            str(locust_rate),
            "--run-time",
            duration,
            "-H",
            "http://127.0.0.1:8000",
            "--csv",
            csv_prefix,
            "--only-summary",
            user_class,
        ]

        print(f"Executing: {' '.join(locust_cmd)}")
        locust_res = subprocess.run(locust_cmd, cwd=BASE_DIR, capture_output=True, text=True)
        print("Locust STDOUT:")
        print(locust_res.stdout)
        if locust_res.stderr:
            print("Locust STDERR:")
            print(locust_res.stderr)

        return read_csv_stats(csv_prefix)

    finally:
        server_proc.terminate()
        try:
            server_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server_proc.kill()
        time.sleep(1)


def main():
    results = {}

    # Scenario 1a: Cache Enabled
    results["cache_enabled"] = run_scenario(
        name="Cache_Enabled",
        env_overrides={
            "ENABLE_CACHE": "true",
            "RATE_LIMIT_REDIRECT_PER_MINUTE": "1000000",
            "RATE_LIMIT_CREATE_PER_MINUTE": "1000000",
        },
        locust_users=50,
        locust_rate=10,
        duration="15s",
    )

    # Scenario 1b: Cache Disabled
    results["cache_disabled"] = run_scenario(
        name="Cache_Disabled",
        env_overrides={
            "ENABLE_CACHE": "false",
            "RATE_LIMIT_REDIRECT_PER_MINUTE": "1000000",
            "RATE_LIMIT_CREATE_PER_MINUTE": "1000000",
        },
        locust_users=50,
        locust_rate=10,
        duration="15s",
    )

    # Scenario 2: Rate Limit Threshold Behavior
    results["rate_limit_threshold"] = run_scenario(
        name="Rate_Limit_Threshold",
        env_overrides={
            "ENABLE_CACHE": "true",
            "RATE_LIMIT_REDIRECT_PER_MINUTE": "300",
            "RATE_LIMIT_CREATE_PER_MINUTE": "10",
        },
        locust_users=25,
        locust_rate=25,
        duration="10s",
        user_class="RateLimitThresholdUser",
    )

    # Scenario 3: Redis Stopped (Degraded Fallback)
    print("\n[Scenario 3] Stopping Redis container...")
    subprocess.run(["docker", "stop", "linklite-redis"], check=True)
    try:
        results["redis_stopped"] = run_scenario(
            name="Redis_Stopped",
            env_overrides={
                "ENABLE_CACHE": "true",
                "RATE_LIMIT_REDIRECT_PER_MINUTE": "1000000",
                "RATE_LIMIT_CREATE_PER_MINUTE": "1000000",
            },
            locust_users=30,
            locust_rate=10,
            duration="10s",
        )
    finally:
        print("[Scenario 3] Restarting Redis container...")
        subprocess.run(["docker", "start", "linklite-redis"], check=True)
        time.sleep(2)

    print("\n\nAll scenarios complete! Output saved in benchmarks/results_*.csv")


if __name__ == "__main__":
    main()
