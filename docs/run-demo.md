# Demo Runbook

Use this runbook to start the local Varnish + Nginx stack and verify the demo behavior.

## Prerequisites

You need:

- Docker with Compose support.
- Python 3 for the cache-limit helper.
- `curl` for HTTP checks.

## 1. Start the Stack

Create a local environment file:

```bash
cp .env.example .env
```

Start both containers:

```bash
docker compose up -d
```

Check service state:

```bash
docker compose ps
```

Expected result:

- `origin` is running and healthy.
- `varnish` is running and healthy.

## 2. Open the Demo Page

Request the index page through Varnish:

```bash
curl -i http://localhost:8080/
```

This goes through:

```text
curl -> Varnish localhost:8080 -> Nginx origin
```

## 3. Verify Cache Hit and Miss

Request the cacheable endpoint twice:

```bash
curl -I http://localhost:8080/cacheable
curl -I http://localhost:8080/cacheable
```

Expected result:

```text
First request:  X-Cache: MISS
Second request: X-Cache: HIT
```

The first request is a miss because Varnish has to fetch the object from Nginx. The second request is a hit because Varnish can serve the stored object.

## 4. Verify Private Responses Are Not Cached

Request the private endpoint twice:

```bash
curl -I http://localhost:8080/private
curl -I http://localhost:8080/private
```

Expected result:

```text
X-Cache: MISS
Cache-Control: private, no-store
Set-Cookie: session=demo; Path=/; HttpOnly; SameSite=Lax
```

Both requests should be misses because the VCL passes private paths, and the origin also returns private cache headers plus a cookie.

## 5. Compare Varnish With Direct Origin Access

Request the same endpoint through Varnish and directly from Nginx:

```bash
curl -I http://localhost:8080/cacheable
curl -I http://localhost:8081/cacheable
```

Port meanings:

- `8080`: Varnish.
- `8081`: Nginx origin exposed for local debugging.

In production, the origin should usually stay private so users cannot bypass Varnish.

## 6. Inspect Varnish Counters

Run:

```bash
docker compose exec varnish varnishstat -1
```

Useful counters:

- `MAIN.cache_hit`: number of cache hits.
- `MAIN.cache_miss`: number of cache misses.
- `MAIN.n_object`: current cached object count.
- `MAIN.n_lru_nuked`: objects evicted because storage was full.
- `SMA.*.g_bytes`: object storage used.
- `SMA.*.g_space`: object storage free.

## 7. Run the Cache-Size Demo

Run:

```bash
python3 tools/cache_limit.py
```

The script requests unique 1 MiB objects until it fills more than the configured `VARNISH_SIZE`. It then reports storage counters and whether Varnish evicted old objects.

Useful variants:

```bash
python3 tools/cache_limit.py --varnish-size 64M
python3 tools/cache_limit.py --fill-ratio 2.0
python3 tools/cache_limit.py --max-requests 200
```

`MAIN.n_lru_nuked` increasing means Varnish reached the cache storage limit and removed least-recently-used objects.

## 8. Stop the Demo

Stop and remove the containers and anonymous volumes:

```bash
docker compose down -v
```

## Troubleshooting

If `curl http://localhost:8080/...` fails, check:

```bash
docker compose ps
docker compose logs varnish
docker compose logs origin
```

If the first `/cacheable` request is already a hit, Varnish may already have the object from an earlier run. Restart Varnish to clear this demo cache:

```bash
docker compose restart varnish
```

If `tools/cache_limit.py` cannot read counters, start the stack first and confirm the `varnish` service is healthy.
