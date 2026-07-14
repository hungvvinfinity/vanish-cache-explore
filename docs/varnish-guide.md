# Varnish and Nginx Developer Guide

This project runs Varnish Cache in front of an Nginx origin server. It is a small local lab for learning how a reverse proxy cache works, how cache decisions are configured, and how to verify that responses are being served from cache.

The intended reader is a developer who has not used Varnish before.

## System Overview

The request path is:

```text
Browser or curl
  -> Varnish on localhost:8080
  -> Nginx origin container on port 80 inside Docker
```

For local debugging, the Nginx origin is also exposed directly on `localhost:8081`.

Varnish is the public entry point for cached traffic. Nginx is the origin, which means it generates or serves the real response when Varnish does not already have a valid cached object.

The important files are:

- `.env.example`: default image tags, ports, and Varnish cache size.
- `compose.yaml`: starts the `origin` and `varnish` containers.
- `nginx/default.conf`: defines demo origin endpoints and their HTTP cache headers.
- `varnish/default.vcl`: defines Varnish caching rules.
- `tools/cache_limit.py`: fills the cache and reports Varnish storage pressure.

## Quick Start

Create a local environment file:

```bash
cp .env.example .env
```

Start the stack:

```bash
docker compose up -d
```

Request the cacheable endpoint through Varnish twice:

```bash
curl -I http://localhost:8080/cacheable
curl -I http://localhost:8080/cacheable
```

The first response should contain:

```text
X-Cache: MISS
```

The second response should contain:

```text
X-Cache: HIT
```

A `MISS` means Varnish did not have a usable object and fetched the response from Nginx. A `HIT` means Varnish served the response from its cache.

Stop the stack when finished:

```bash
docker compose down -v
```

## Configuration

Copy `.env.example` to `.env` and adjust values there:

```dotenv
VARNISH_IMAGE=varnish:9.0
NGINX_IMAGE=nginx:1.30.3-alpine

VARNISH_PORT=8080
ORIGIN_PORT=8081

VARNISH_SIZE=128M
```

### Image Tags

`VARNISH_IMAGE` and `NGINX_IMAGE` choose the container images used by Docker Compose.

Keep these pinned to explicit versions so local runs are repeatable. When changing versions, review the Varnish and Nginx release notes because defaults and supported parameters can change between versions.

### Ports

`VARNISH_PORT` exposes Varnish to the host machine. With the default value, developers use:

```text
http://localhost:8080
```

`ORIGIN_PORT` exposes Nginx directly to the host machine. With the default value, developers use:

```text
http://localhost:8081
```

Direct origin access is useful for local inspection, but production deployments should normally expose only Varnish. The origin should stay private so users cannot bypass cache, purge, and routing rules.

### Cache Size

`VARNISH_SIZE` controls the amount of memory reserved for Varnish object storage. The official Varnish image reads this value and starts Varnish with malloc storage of that size.

For example:

```dotenv
VARNISH_SIZE=128M
```

This does not mean the Varnish container only needs 128 MiB of memory. Varnish also needs memory for worker threads, request and response workspaces, object metadata, logs, headers, and allocator overhead.

Use a cache size smaller than the container or host memory limit. A good starting point is the size of the hot working set: the objects repeatedly requested while they are still fresh.

### Varnish Runtime Parameters

The `varnish` service in `compose.yaml` starts Varnish with:

```yaml
- -F
- -f
- /etc/varnish/default.vcl
- -p
- feature=+http2
- -p
- workspace_backend=1M
- -p
- http_resp_hdr_len=65536
- -p
- http_resp_size=98304
```

These settings mean:

- `-F`: run Varnish in the foreground so Docker can manage the process.
- `-f /etc/varnish/default.vcl`: load this project's VCL configuration.
- `feature=+http2`: enable HTTP/2 support in Varnish.
- `workspace_backend=1M`: give backend response processing more workspace memory.
- `http_resp_hdr_len=65536`: allow larger individual response headers than the default.
- `http_resp_size=98304`: allow a larger total response header area.

Object storage is controlled separately by `VARNISH_SIZE`.

## How Requests Are Handled

Varnish behavior is defined in `varnish/default.vcl`. VCL is Varnish's configuration language.

### Backend

The backend tells Varnish where to fetch content when it has a cache miss:

```vcl
backend default {
    .host = "origin";
    .port = "80";
}
```

`origin` is the Docker Compose service name for Nginx. Docker networking lets Varnish connect to that name directly.

The backend also has timeouts:

- `connect_timeout = 2s`: how long Varnish waits to connect to Nginx.
- `first_byte_timeout = 30s`: how long it waits for the first byte of the response.
- `between_bytes_timeout = 10s`: how long it waits between response chunks.

The backend probe checks `/healthz` every 5 seconds and marks the backend healthy when enough checks pass.

### Incoming Request Rules

`sub vcl_recv` runs when Varnish receives a client request.

The current rules are:

- `PURGE` is allowed only from trusted IP ranges in the `purge` ACL.
- Methods other than `GET` and `HEAD` are passed to Nginx and not cached.
- Requests with an `Authorization` header are passed.
- Private paths such as `/admin`, `/account`, `/api/private`, `/login`, `/logout`, and `/private` are passed.
- Requests with session-like cookies such as `auth=`, `logged_in=`, `session=`, or `token=` are passed.
- Other cookies are removed so harmless tracking or analytics cookies do not create separate cache entries.
- Query parameters are sorted with `std.querysort(req.url)` so URLs with the same parameters in a different order share the same cache key.

These rules are intentionally conservative. They protect personalized or sensitive responses from being cached by accident.

### Backend Response Rules

`sub vcl_backend_response` runs after Nginx returns a response.

The current rules are:

- Responses with `Set-Cookie` are treated as uncacheable.
- Responses with `Cache-Control: private`, `no-cache`, or `no-store` are treated as uncacheable.
- `/payload/*` and `/cacheable` get a Varnish TTL of `1h`, grace of `5m`, and keep of `10m`.
- Responses with no positive TTL are treated as uncacheable.
- Other cacheable responses get grace of `1m` and keep of `5m`.

Important terms:

- `ttl`: how long a cached object is fresh.
- `grace`: how long Varnish may serve a stale object while refreshing it or while the backend is unhealthy.
- `keep`: how long Varnish keeps an expired object for backend revalidation.

### Delivery Headers

`sub vcl_deliver` adds debug headers to every response:

```text
X-Cache: HIT or MISS
X-Cache-Hits: number of cache hits for the object
X-Cache-Host: Varnish server hostname
```

These headers are useful in this lab because they show whether Varnish served a cached object. In production, decide whether these headers should remain visible to users.

## Nginx Origin Endpoints

`nginx/default.conf` defines these demo endpoints:

- `/healthz`: health check endpoint with `Cache-Control: no-store`.
- `/`: demo HTML index with `Cache-Control: public, max-age=60`.
- `/cacheable`: text response intended to be cached.
- `/private`: private response with `Cache-Control: private, no-store` and `Set-Cookie`.
- `/payload/1m.bin`: generated 1 MiB file used for cache-fill testing.

The origin emits cache headers. Varnish then applies additional safety rules in VCL.

Prefer this pattern in real applications: the origin should describe freshness with HTTP headers, and Varnish should enforce shared-cache safety and handle exceptions.

## Common Local Checks

Check a cacheable response:

```bash
curl -I http://localhost:8080/cacheable
curl -I http://localhost:8080/cacheable
```

Expected behavior:

- First request: `X-Cache: MISS`.
- Second request: `X-Cache: HIT`.

Check that private content is not cached:

```bash
curl -I http://localhost:8080/private
curl -I http://localhost:8080/private
```

Expected behavior:

- Both responses should be `MISS`.
- The response should include private cache headers and a cookie from Nginx.

Compare Varnish and direct origin access:

```bash
curl -I http://localhost:8080/cacheable
curl -I http://localhost:8081/cacheable
```

The `8080` request goes through Varnish. The `8081` request goes directly to Nginx.

Inspect Varnish counters:

```bash
docker compose exec varnish varnishstat -1
```

Useful counters include:

- `MAIN.cache_hit`: successful cache hits.
- `MAIN.cache_miss`: cache misses that fetched from the backend.
- `MAIN.n_object`: current number of cached objects.
- `MAIN.n_lru_nuked`: objects evicted because storage was full.
- `SMA.*.g_bytes`: malloc storage used.
- `SMA.*.g_space`: malloc storage free.

## Cache Size Testing

The helper script fills the cache with unique URLs for the generated 1 MiB payload:

```bash
python3 tools/cache_limit.py
```

The script:

1. Reads `VARNISH_SIZE` from `.env`, the environment, or `.env.example`.
2. Requests unique `/payload/1m.bin` URLs through Varnish.
3. Reads Varnish counters with `docker compose exec -T varnish varnishstat -1 -j`.
4. Reports hits, misses, object count, storage use, free space, and LRU eviction.

Useful options:

```bash
python3 tools/cache_limit.py --fill-ratio 2.0
python3 tools/cache_limit.py --varnish-size 64M
python3 tools/cache_limit.py --base-url http://localhost:8080 --max-requests 200
```

If `MAIN.n_lru_nuked` increases, Varnish has reached storage pressure and evicted least-recently-used objects to make room for new ones.

## Changing Cache Behavior

Most cache behavior changes happen in one of two places:

- Change origin headers in `nginx/default.conf` or the real application.
- Change shared-cache policy in `varnish/default.vcl`.

Examples:

- To cache a new public endpoint, make the origin return a public `Cache-Control` header and confirm the VCL does not pass that path.
- To prevent caching for a sensitive path, add it to the private path check in `vcl_recv` or return `Cache-Control: private, no-store` from the origin.
- To change freshness for `/cacheable`, update the TTL/grace/keep rule in `vcl_backend_response`.
- To allow purge from another trusted network, update the `purge` ACL carefully.

After changing VCL, restart Varnish:

```bash
docker compose restart varnish
```

Then verify with repeated `curl -I` requests and `varnishstat`.

## Beginner Mistakes To Avoid

Do not cache authenticated responses unless the cache key and invalidation design are explicit. A shared cache can leak personalized data if it stores responses that depend on a user session.

Do not expose the origin in production unless there is a specific reason. Users should normally reach Varnish, not Nginx directly.

Do not set `VARNISH_SIZE` equal to total available memory. Leave room for Varnish overhead and the rest of the system.

Do not blindly strip all cookies or query parameters. Some cookies and parameters change the response. Removing them can cause incorrect content to be shared.

Do not allow purge requests from untrusted networks. Purge can remove cached content and create extra backend load.

Do not rely only on local `HIT` and `MISS` tests for production sizing. Use real traffic patterns, object sizes, hit rate, eviction counters, and backend load.

## Production Notes

This repository is a local learning lab. Before using the same pattern in production:

- Keep the origin private.
- Restrict purge access.
- Size cache storage from real traffic.
- Monitor hit rate, miss rate, eviction, backend health, and memory use.
- Review whether debug headers should be exposed.
- Use application-driven invalidation for content that changes before its TTL expires.
- Pin and update Varnish and Nginx images intentionally.
