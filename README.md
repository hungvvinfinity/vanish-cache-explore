# Varnish + Nginx cache lab

This repo runs a small Varnish Cache layer in front of an Nginx origin. It also includes a Python tool that fills the cache with unique 1 MiB objects and reports whether the configured cache limit is being reached.

## Quick start

```bash
cp .env.example .env
docker compose up -d
curl -I http://localhost:8080/cacheable
curl -I http://localhost:8080/cacheable
python3 tools/cache_limit.py
```

The first `curl` to `/cacheable` should show `X-Cache: MISS`. The second request should show `X-Cache: HIT`.

Use these endpoints:

- `http://localhost:8080/` serves a small demo index through Varnish.
- `http://localhost:8080/cacheable` is explicitly cacheable.
- `http://localhost:8080/private` is explicitly private and should not be cached.
- `http://localhost:8080/payload/1m.bin` is a generated 1 MiB payload used by the cache-fill tool.
- `http://localhost:8081/` reaches the Nginx origin directly.

Stop and remove the stack with:

```bash
docker compose down -v
```

## Configuration

Copy `.env.example` to `.env` and adjust the values:

```dotenv
VARNISH_IMAGE=varnish:9.0
NGINX_IMAGE=nginx:1.30.3-alpine
VARNISH_PORT=8080
ORIGIN_PORT=8081
VARNISH_SIZE=128M
```

`VARNISH_SIZE` controls the Varnish object storage. The official Varnish image reads this environment variable and starts Varnish with malloc storage of that size.

The Compose file also sets `workspace_backend=1M`. That workspace is separate from object storage and is used while Varnish receives backend responses.

With `malloc`, cached objects live in process memory. Do not set `VARNISH_SIZE` equal to all available container or host memory. Varnish also needs memory for worker threads, metadata, headers, workspaces, logs, and allocator overhead. A practical starting point is to reserve only the hot working set and leave comfortable headroom outside the cache storage size.

For example:

- Small local test: `VARNISH_SIZE=64M` or `128M`.
- Small production service: start from the measured hot object set, then add margin.
- Memory-constrained host: set a Docker memory limit higher than `VARNISH_SIZE`, not equal to it.

The current Compose file exposes the origin on `ORIGIN_PORT` for easy inspection. In production, keep the origin private and expose only Varnish.

## Cache-limit tool

Run:

```bash
python3 tools/cache_limit.py
```

The tool:

1. Reads `VARNISH_SIZE` from `.env`, the environment, or `.env.example`.
2. Requests unique URLs under `/payload/1m.bin`.
3. Reads Varnish counters through `docker compose exec -T varnish varnishstat -1 -j`.
4. Reports cache misses, hits, object count, storage used/free, and `MAIN.n_lru_nuked`.

Useful options:

```bash
python3 tools/cache_limit.py --fill-ratio 2.0
python3 tools/cache_limit.py --varnish-size 64M
python3 tools/cache_limit.py --base-url http://localhost:8080 --max-requests 200
```

`MAIN.n_lru_nuked` increasing means Varnish has reached storage pressure and evicted least-recently-used objects to make room for new ones.

## Varnish cache best practices

Prefer origin cache headers first. The best long-term setup is for the application or origin server to emit accurate `Cache-Control` headers. VCL should enforce safety and handle exceptions, not become the only place where application freshness rules exist.

Cache only safe methods. The VCL passes anything that is not `GET` or `HEAD`, which avoids accidentally caching mutation responses such as `POST`, `PUT`, or `DELETE`.

Do not cache personalized responses by default. The VCL passes requests with `Authorization`, session-like cookies, private paths, `Set-Cookie`, or `Cache-Control: private/no-store/no-cache`. If you later cache authenticated content, design the cache key and invalidation rules explicitly.

Normalize only what is safe. This demo sorts query parameters with `std.querysort()` so equivalent query strings hash consistently. Do not strip query parameters or cookies globally unless you know they do not affect the response.

Use TTL, grace, and keep deliberately:

- `ttl` is how long an object is fresh.
- `grace` lets Varnish serve stale content while refreshing or while the backend is unhealthy.
- `keep` retains expired objects for conditional backend validation.

The demo gives `/payload/*` and `/cacheable` a `1h` TTL, `5m` grace, and `10m` keep. Real values should reflect how stale each response is allowed to be.

Monitor the right counters:

- `MAIN.cache_hit` and `MAIN.cache_miss` show cache effectiveness.
- `MAIN.n_lru_nuked` shows eviction caused by storage pressure.
- `MAIN.n_object` shows the current object count.
- `SMA.*.g_bytes` and `SMA.*.g_space` show malloc storage used and free.

Size the cache from the hot working set. Total site size is usually the wrong number. Estimate the objects repeatedly requested within the freshness window, then validate with real counters. If `n_lru_nuked` rises during normal traffic and hit rate drops, the cache is too small or the cache key is too fragmented.

Avoid cache-key fragmentation. Excess cookies, tracking query parameters, per-user headers, and high-cardinality URL parameters can create many variants of the same content. Normalize or ignore them only when the origin response is truly identical.

Plan invalidation. This demo supports `PURGE` from loopback and private Docker networks. For production, restrict purge access tightly and prefer application-driven invalidation for content that changes before its TTL expires.

Pin images and update intentionally. `.env.example` pins image tags so rebuilds are repeatable. Review Varnish and Nginx release notes before changing major or minor versions.
