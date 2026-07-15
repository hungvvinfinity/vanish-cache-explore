#!/usr/bin/env python3
"""Seed deterministic 1 MiB objects through SeaweedFS's internal S3 endpoint."""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request


endpoint = os.environ.get("S3_ENDPOINT", "http://seaweed:8333").rstrip("/")
bucket = os.environ.get("S3_BUCKET", "cache-demo")
count = int(os.environ.get("SEED_OBJECT_COUNT", "64"))
object_size = 1024 * 1024


def payload(index: int) -> bytes:
    prefix = f"cache-demo seeded object {index:02d}\n".encode("ascii")
    return (prefix * ((object_size // len(prefix)) + 1))[:object_size]


def main() -> int:
    if count < 2:
        print("SEED_OBJECT_COUNT must be at least 2", file=sys.stderr)
        return 2

    for index in range(1, count + 1):
        key = f"objects/object-{index:02d}.bin"
        request = urllib.request.Request(
            f"{endpoint}/{bucket}/{key}",
            data=payload(index),
            method="PUT",
            headers={"Content-Type": "application/octet-stream"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                if response.status not in (200, 201):
                    raise RuntimeError(f"unexpected HTTP {response.status}")
        except (urllib.error.URLError, RuntimeError) as exc:
            print(f"failed to seed {key}: {exc}", file=sys.stderr)
            return 1
        print(f"seeded s3://{bucket}/{key} ({object_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
