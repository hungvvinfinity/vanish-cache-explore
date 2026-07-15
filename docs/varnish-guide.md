# Developer Guide

## Responsibilities

```text
Client -> Nginx HTTPS -> Varnish -> SeaweedFS S3
```

- **SeaweedFS** is the persistent, single-node S3 origin. `weed mini` creates `cache-demo`, and the one-shot `seed` service writes deterministic 1 MiB objects below `objects/`.
- **Varnish** caches only successful `GET` and `HEAD` responses for path-style URLs below `/cache-demo/objects/`. Its `file` storage uses a separate fixed-size cache file; storage pressure uses normal Varnish LRU eviction.
- **Nginx** is the only public entry point. It terminates local self-signed TLS, preserves the request URI and `Host`, and proxies only `GET` and `HEAD` to Varnish.

No SeaweedFS port is published to the host. Public mutations and `PURGE` receive `405` from Nginx. The Varnish port is published only as `127.0.0.1:${VARNISH_PURGE_PORT}` for local purge and inspection.

## Varnish cache policy

`varnish/default.vcl` sorts query parameters before hashing but does not remove them. It passes requests containing authorization or cookies and all paths outside the seeded object prefix. A response is stored only when it is a `200` response to a qualifying `GET` or `HEAD`; errors, bucket responses, writes, and responses marked private/no-store are uncacheable.

Cached objects receive a one-hour TTL, five-minute grace, and ten-minute keep. `X-Cache` and `X-Cache-Hits` are deliberately exposed for the lab.

`varnish-init` writes and fsyncs the full `VARNISH_DISK_SIZE` file before Varnish is allowed to start. This verifies volume capacity early. The Varnish file backend is a cache, not durable object storage: recreating it or evicting an object never modifies SeaweedFS.

Monitor these counters:

- `MAIN.cache_hit` and `MAIN.cache_miss`: cache effectiveness.
- `MAIN.n_lru_nuked`: entries evicted due to storage pressure.
- `MAIN.n_object`: current cached objects.
- `SMF.*.g_bytes` and `SMF.*.g_space`: file storage use and free space.

## Cache-only purge

The VCL accepts `PURGE` only from loopback/private-network source IPs and only for `/cache-demo/objects/<key>`. It normalizes query ordering before the purge hash, so the selected URL's Vary variants are invalidated. The cache key includes `Host`; therefore a direct local purge needs the public host header, for example:

```bash
curl -X PURGE -H 'Host: localhost:8443' \
  http://127.0.0.1:6081/cache-demo/objects/object-01.bin
```

This affects Varnish only. SeaweedFS retains the object, and the next HTTPS request is a cache miss that refetches it.

## Local-demo boundary

SeaweedFS's unauthenticated `weed mini` mode and the self-signed gateway certificate are intentional local-demo choices. This Compose file is not an HA design, an S3 authorization design, or a production TLS configuration. For the exact commands and behavioral checks, use the [demo runbook](run-demo.md).
