# Demo Runbook

This runbook verifies the local path:

```text
HTTPS S3 client -> Nginx -> Varnish bounded file cache -> SeaweedFS S3
```

It assumes Docker Compose, Python 3, and `curl` are installed.

## 1. Start from empty volumes

```bash
cp .env.example .env
docker compose down -v
docker compose up -d --build
docker compose ps -a
```

Wait until `seaweed`, `varnish`, and `nginx` are healthy and the `seed` and `varnish-init` jobs have exited with code zero. `varnish-init` must finish before Varnish can start; its log reports the exact preallocated cache file size.

```bash
docker compose logs varnish-init seed
docker compose exec varnish varnishadm backend.list
```

SeaweedFS's S3 and administration ports are intentionally not host-published. The only public endpoint is Nginx HTTPS on `S3_HTTPS_PORT` (default `8443`). Nginx creates a self-signed certificate in the `nginx_tls` Docker volume during its first start.

## 2. Read a seeded S3 object through HTTPS

```bash
curl -k -I https://localhost:8443/cache-demo/objects/object-01.bin
curl -k -I https://localhost:8443/cache-demo/objects/object-01.bin
```

Expected result:

```text
First request:  X-Cache: MISS
Second request: X-Cache: HIT
Content-Length: 1048576
```

`-k` accepts the local self-signed certificate. An endpoint-style S3 client should use `https://localhost:8443`, path-style addressing, and either this certificate as its CA or disabled verification for the local demo:

```bash
aws --endpoint-url https://localhost:8443 --no-verify-ssl --no-sign-request \
  s3api head-object --bucket cache-demo --key objects/object-01.bin
```

## 3. Verify the public endpoint is read-only

Each request below must return `405`. It is safe to run them because Nginx rejects them before proxying.

```bash
curl -k -o /dev/null -w '%{http_code}\n' -X PUT --data demo \
  https://localhost:8443/cache-demo/objects/should-not-write.bin
curl -k -o /dev/null -w '%{http_code}\n' -X DELETE \
  https://localhost:8443/cache-demo/objects/object-01.bin
curl -k -o /dev/null -w '%{http_code}\n' -X PURGE \
  https://localhost:8443/cache-demo/objects/object-01.bin
```

## 4. Demonstrate bounded disk-cache LRU eviction

The default has 64 seeded 1 MiB objects and a 32 MiB Varnish cache, so it has ample distinct objects to exceed capacity.

```bash
python3 tools/cache_limit.py
```

The tool warms `object-01.bin` (`MISS`, then `HIT`), requests later distinct objects until capacity is exceeded, and fetches object 01 again. It succeeds only if both of these are true:

- `MAIN.n_lru_nuked` increased.
- The last fetch of object 01 is `X-Cache: MISS`.

Inspect the raw counters at any time:

```bash
docker compose exec varnish varnishstat -1 | grep -E 'MAIN.(cache_hit|cache_miss|n_lru_nuked|n_object)|SMF.*g_(bytes|space)'
```

## 5. Purge only a cached object

First make an object warm, then issue the loopback-only purge. The `Host` must match the public request because it is part of Varnish's cache key.

```bash
curl -k -I https://localhost:8443/cache-demo/objects/object-02.bin
curl -k -I https://localhost:8443/cache-demo/objects/object-02.bin

curl -i -X PURGE \
  -H 'Host: localhost:8443' \
  http://127.0.0.1:6081/cache-demo/objects/object-02.bin

curl -k -I https://localhost:8443/cache-demo/objects/object-02.bin
```

The purge response says that SeaweedFS was not changed. The final HTTPS request must be `X-Cache: MISS` and return `200`, proving the source object remained available while only the cached representation was removed. The Varnish listener is bound to `127.0.0.1`; it is unavailable from other hosts.

## 6. Check preallocation failure behavior

Use an intentionally impossible capacity for the local Docker disk. The stack must stop before Varnish serves traffic, with an error from `varnish-init`.

```bash
docker compose down -v
VARNISH_DISK_SIZE=999999G docker compose up varnish-init
docker compose logs varnish-init
```

The expected log contains `ERROR: could not preallocate ... for Varnish`. Remove the failed volume and start normally again:

```bash
docker compose down -v
docker compose up -d --build
```

## 7. Stop the demo

```bash
docker compose down -v
```

This removes the persistent SeaweedFS source data, the bounded Varnish cache file, and the generated self-signed certificate volume.
