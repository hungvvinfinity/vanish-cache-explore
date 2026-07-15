#!/usr/bin/env python3
"""Overflow the bounded Varnish disk cache with seeded SeaweedFS objects."""

from __future__ import annotations

import argparse
import json
import math
import os
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
import urllib.parse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_URL = "https://localhost:8443"
DEFAULT_OBJECT_BYTES = 1024 * 1024


def read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def parse_size(value: str) -> int:
    text = value.strip().lower()
    units = {"b": 1, "k": 1024, "kb": 1024, "m": 1024**2, "mb": 1024**2, "g": 1024**3, "gb": 1024**3}
    for suffix, multiplier in sorted(units.items(), key=lambda item: len(item[0]), reverse=True):
        if text.endswith(suffix):
            return int(float(text[: -len(suffix)].strip()) * multiplier)
    return int(float(text))


def format_bytes(value: int | float | None) -> str:
    if value is None:
        return "unknown"
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if amount < 1024 or unit == "GiB":
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} GiB"


def varnishstat() -> dict[str, int]:
    result = subprocess.run(
        ["docker", "compose", "exec", "-T", "varnish", "varnishstat", "-1", "-j"],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
    data = json.loads(result.stdout)
    counters = data.get("counters", data)
    return {key: int(value["value"]) for key, value in counters.items() if isinstance(value, dict) and "value" in value}


def stat_value(stats: dict[str, int], key: str) -> int:
    return int(stats.get(key, 0))


def storage_total(stats: dict[str, int], suffix: str) -> int | None:
    # The Varnish Docker entrypoint has a default malloc store (SMA.s0), while
    # this demo explicitly stores objects in the file backend (SMF.s1).
    values = [value for key, value in stats.items() if key.startswith("SMF.") and key.endswith(suffix)]
    return sum(values) if values else None


def fetch(url: str, timeout: float, context: ssl.SSLContext | None) -> tuple[int, str, int]:
    request = urllib.request.Request(url, headers={"User-Agent": "varnish-cache-limit-tool/2.0"})
    with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
        body = response.read()
        return response.status, response.headers.get("X-Cache", ""), len(body)


def object_url(base_url: str, bucket: str, index: int) -> str:
    return f"{base_url.rstrip('/')}/{bucket}/objects/object-{index:02d}.bin"


def purge(url: str, purge_url: str, host: str, timeout: float) -> None:
    path = urllib.parse.urlsplit(url).path
    request = urllib.request.Request(
        f"{purge_url.rstrip('/')}{path}",
        method="PURGE",
        headers={"Host": host},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"PURGE returned HTTP {response.status}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Overflow the Varnish file cache with seeded 1 MiB S3 objects.")
    parser.add_argument("--base-url", default=os.environ.get("BASE_URL", DEFAULT_URL))
    parser.add_argument("--bucket", default=os.environ.get("S3_BUCKET", "cache-demo"))
    parser.add_argument("--varnish-disk-size", default=None, help="Override VARNISH_DISK_SIZE, for example 32M.")
    parser.add_argument("--seed-object-count", type=int, default=None)
    parser.add_argument("--object-bytes", type=int, default=DEFAULT_OBJECT_BYTES)
    parser.add_argument("--fill-ratio", type=float, default=1.5)
    parser.add_argument("--max-requests", type=int, default=None, help="Number of distinct objects to fetch after object-01 is warmed.")
    parser.add_argument("--purge-url", default=os.environ.get("VARNISH_PURGE_URL", "http://127.0.0.1:6081"), help="Loopback Varnish URL used to reset only object-01 before the test.")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--verify-tls", action="store_true", help="Verify the self-signed certificate instead of using the demo default.")
    parser.add_argument("--ca-file", help="CA/certificate file to use with --verify-tls.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    env_file, example_env = read_dotenv(ROOT / ".env"), read_dotenv(ROOT / ".env.example")
    size_text = args.varnish_disk_size or os.environ.get("VARNISH_DISK_SIZE") or env_file.get("VARNISH_DISK_SIZE") or example_env.get("VARNISH_DISK_SIZE") or "32M"
    cache_bytes = parse_size(size_text)
    seed_count = args.seed_object_count or int(os.environ.get("SEED_OBJECT_COUNT") or env_file.get("SEED_OBJECT_COUNT") or example_env.get("SEED_OBJECT_COUNT") or "64")
    requested = args.max_requests or max(1, math.ceil((cache_bytes * args.fill_ratio) / args.object_bytes))
    # Object 01 is warmed first, so the requested objects begin at 02.
    if requested + 1 > seed_count:
        print(f"ERROR: need {requested + 1} seeded objects, but SEED_OBJECT_COUNT is {seed_count}. Increase it or lower --max-requests.", file=sys.stderr)
        return 2
    context: ssl.SSLContext | None = None
    if args.base_url.startswith("https://"):
        context = ssl.create_default_context(cafile=args.ca_file) if args.verify_tls else ssl._create_unverified_context()

    print(f"Base URL: {args.base_url}")
    print(f"Bucket: {args.bucket}")
    print(f"Configured Varnish disk cache: {size_text} ({format_bytes(cache_bytes)})")
    print(f"Distinct overflow objects: {requested}")
    oldest_url = object_url(args.base_url, args.bucket, 1)
    try:
        public_host = urllib.parse.urlsplit(args.base_url).netloc
        if not public_host:
            raise RuntimeError(f"invalid --base-url: {args.base_url}")
        purge(oldest_url, args.purge_url, public_host, args.timeout)
        before = varnishstat()
        _, first_cache, _ = fetch(oldest_url, args.timeout, context)
        _, second_cache, _ = fetch(oldest_url, args.timeout, context)
    except (urllib.error.URLError, RuntimeError, subprocess.CalledProcessError, FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"ERROR: cannot reset and warm {oldest_url}: {exc}", file=sys.stderr)
        return 3
    if first_cache.upper() != "MISS" or second_cache.upper() != "HIT":
        print(f"ERROR: warm-up expected MISS then HIT, observed {first_cache!r} then {second_cache!r}", file=sys.stderr)
        return 3

    started = time.time()
    for index in range(2, requested + 2):
        url = object_url(args.base_url, args.bucket, index)
        try:
            status, cache_header, length = fetch(url, args.timeout, context)
        except urllib.error.URLError as exc:
            print(f"ERROR: request failed for {url}: {exc}", file=sys.stderr)
            return 3
        if status != 200 or length != args.object_bytes:
            print(f"ERROR: expected a {args.object_bytes}-byte HTTP 200 object for {url}; got {status}, {length} bytes", file=sys.stderr)
            return 3
        if index == 2 or index == requested + 1 or index % 10 == 0:
            print(f"{index - 1:>4}/{requested} fetched, last={cache_header or 'no X-Cache'}")

    try:
        _, oldest_cache, _ = fetch(oldest_url, args.timeout, context)
        after = varnishstat()
    except (urllib.error.URLError, subprocess.CalledProcessError, FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"ERROR: final cache check failed: {exc}", file=sys.stderr)
        return 3

    lru_delta = stat_value(after, "MAIN.n_lru_nuked") - stat_value(before, "MAIN.n_lru_nuked")
    print("\nVarnish counters")
    print(f"  cache_hit delta:  {stat_value(after, 'MAIN.cache_hit') - stat_value(before, 'MAIN.cache_hit')}")
    print(f"  cache_miss delta: {stat_value(after, 'MAIN.cache_miss') - stat_value(before, 'MAIN.cache_miss')}")
    print(f"  LRU nuked delta:  {lru_delta}")
    print(f"  objects:          {stat_value(after, 'MAIN.n_object')}")
    print(f"  storage used:     {format_bytes(storage_total(after, '.g_bytes'))}")
    print(f"  storage free:     {format_bytes(storage_total(after, '.g_space'))}")
    print(f"  oldest re-fetch:  {oldest_cache or 'no X-Cache'}")
    print(f"  elapsed:          {time.time() - started:.1f}s")

    if lru_delta > 0 and oldest_cache.upper() == "MISS":
        print("PASS: disk-cache pressure caused LRU eviction and the oldest object was re-fetched from SeaweedFS.")
        return 0
    print("FAIL: expected at least one LRU nuke and a MISS for the oldest untouched object. Increase --fill-ratio or lower VARNISH_DISK_SIZE.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
