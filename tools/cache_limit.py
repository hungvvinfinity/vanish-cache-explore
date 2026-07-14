#!/usr/bin/env python3
"""Fill the demo Varnish cache and report storage pressure."""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_URL = "http://localhost:8080"
DEFAULT_OBJECT_BYTES = 1024 * 1024


def read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def parse_size(value: str) -> int:
    text = value.strip().lower()
    units = {
        "b": 1,
        "k": 1024,
        "kb": 1024,
        "m": 1024**2,
        "mb": 1024**2,
        "g": 1024**3,
        "gb": 1024**3,
    }

    for suffix, multiplier in sorted(units.items(), key=lambda item: len(item[0]), reverse=True):
        if text.endswith(suffix):
            number = text[: -len(suffix)].strip()
            return int(float(number) * multiplier)

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
    stats: dict[str, int] = {}
    for key, payload in counters.items():
        if isinstance(payload, dict) and "value" in payload:
            stats[key] = int(payload["value"])
    return stats


def stat_value(stats: dict[str, int], key: str) -> int:
    return int(stats.get(key, 0))


def storage_total(stats: dict[str, int], suffix: str) -> int | None:
    values = [value for key, value in stats.items() if key.startswith("SMA.") and key.endswith(suffix)]
    if not values:
        return None
    return sum(values)


def fetch(url: str, timeout: float) -> tuple[int, str, int]:
    request = urllib.request.Request(url, headers={"User-Agent": "varnish-cache-limit-tool/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
        return response.status, response.headers.get("X-Cache", ""), len(body)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fill the local Varnish demo cache with unique 1 MiB objects and report cache pressure.",
    )
    parser.add_argument("--base-url", default=os.environ.get("BASE_URL", DEFAULT_URL))
    parser.add_argument("--path", default="/payload/1m.bin")
    parser.add_argument("--varnish-size", default=None, help="Override VARNISH_SIZE, for example 64M or 1G.")
    parser.add_argument("--object-bytes", type=int, default=DEFAULT_OBJECT_BYTES)
    parser.add_argument("--fill-ratio", type=float, default=1.5)
    parser.add_argument("--max-requests", type=int, default=None)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--progress-every", type=int, default=10)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    env_file = read_dotenv(ROOT / ".env")
    example_env = read_dotenv(ROOT / ".env.example")

    size_text = (
        args.varnish_size
        or os.environ.get("VARNISH_SIZE")
        or env_file.get("VARNISH_SIZE")
        or example_env.get("VARNISH_SIZE")
        or "128M"
    )
    cache_bytes = parse_size(size_text)
    max_requests = args.max_requests or max(1, math.ceil((cache_bytes * args.fill_ratio) / args.object_bytes))
    run_id = uuid.uuid4().hex
    base_url = args.base_url.rstrip("/")
    path = args.path if args.path.startswith("/") else f"/{args.path}"

    print(f"Base URL: {base_url}")
    print(f"Configured cache size: {size_text} ({format_bytes(cache_bytes)})")
    print(f"Planned requests: {max_requests} unique objects")

    try:
        before = varnishstat()
    except (subprocess.CalledProcessError, FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"ERROR: cannot read varnishstat through docker compose: {exc}", file=sys.stderr)
        print("Start the stack first with: docker compose up -d", file=sys.stderr)
        return 2

    started = time.time()
    misses = 0
    hits = 0
    bytes_read = 0

    for index in range(1, max_requests + 1):
        separator = "&" if "?" in path else "?"
        url = f"{base_url}{path}{separator}cache_limit_run={run_id}&n={index}"
        try:
            status, cache_header, length = fetch(url, args.timeout)
        except urllib.error.URLError as exc:
            print(f"ERROR: request failed for {url}: {exc}", file=sys.stderr)
            return 3

        if status >= 400:
            print(f"ERROR: origin returned HTTP {status} for {url}", file=sys.stderr)
            return 3

        bytes_read += length
        if cache_header.upper() == "HIT":
            hits += 1
        else:
            misses += 1

        if args.progress_every > 0 and (index % args.progress_every == 0 or index == max_requests):
            print(f"{index:>4}/{max_requests} fetched, last={cache_header or 'no X-Cache'}, bytes={format_bytes(bytes_read)}")

    try:
        after = varnishstat()
    except (subprocess.CalledProcessError, FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"ERROR: cannot read final varnishstat: {exc}", file=sys.stderr)
        return 2

    elapsed = time.time() - started
    lru_delta = stat_value(after, "MAIN.n_lru_nuked") - stat_value(before, "MAIN.n_lru_nuked")
    hit_delta = stat_value(after, "MAIN.cache_hit") - stat_value(before, "MAIN.cache_hit")
    miss_delta = stat_value(after, "MAIN.cache_miss") - stat_value(before, "MAIN.cache_miss")
    object_count = stat_value(after, "MAIN.n_object")
    used = storage_total(after, ".g_bytes")
    free = storage_total(after, ".g_space")

    print()
    print("Varnish counters")
    print(f"  cache_hit delta:  {hit_delta}")
    print(f"  cache_miss delta: {miss_delta}")
    print(f"  LRU nuked delta:  {lru_delta}")
    print(f"  objects:          {object_count}")
    print(f"  storage used:     {format_bytes(used)}")
    print(f"  storage free:     {format_bytes(free)}")
    print(f"  elapsed:          {elapsed:.1f}s")

    if lru_delta > 0:
        print("PASS: Varnish evicted old objects after the cache limit was exceeded.")
        return 0

    if free is not None and free <= args.object_bytes:
        print("PASS: Varnish storage is effectively full, but no LRU eviction was reported yet.")
        return 0

    print("WARN: The run did not hit the cache limit. Increase --fill-ratio or lower VARNISH_SIZE.")
    print(f"Local request headers observed by this tool: {hits} HIT, {misses} MISS.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
