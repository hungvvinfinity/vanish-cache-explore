# SeaweedFS S3 gateway with a bounded Varnish disk cache

This is a single-node local demo of a read-only S3 gateway:

```text
S3 client -> HTTPS Nginx :443 -> Varnish file cache -> SeaweedFS S3
```

SeaweedFS persistently owns the source objects. Varnish has a separate, fixed-size local file cache and removes least-recently-used cached objects when that capacity is exhausted. Cache pressure never deletes SeaweedFS objects.

## Quick start

```bash
cp .env.example .env
docker compose up -d --build
docker compose ps

curl -k -I https://localhost:8443/cache-demo/objects/object-01.bin
curl -k -I https://localhost:8443/cache-demo/objects/object-01.bin
python3 tools/cache_limit.py
```

The two `curl` calls show `X-Cache: MISS`, then `X-Cache: HIT`. The helper fetches the seeded distinct 1 MiB objects, overflows the disk cache, reports `MAIN.n_lru_nuked`, and checks that the earliest object is then a miss.

The TLS certificate is self-signed and generated in the untracked `nginx_tls` Docker volume. `-k` is appropriate only for this local demo. To trust it instead, copy the certificate from the volume/container and pass it to your client as a CA file.

## Endpoint and data

- Public, read-only, path-style S3 endpoint: `https://localhost:${S3_HTTPS_PORT:-8443}`
- Demo bucket: `cache-demo` (configurable with `S3_BUCKET`)
- Seeded keys: `cache-demo/objects/object-01.bin` through `object-64.bin`, each 1 MiB by default
- SeaweedFS administration and S3 ports are private to the Compose network; only Nginx publishes a public port.

For example, an AWS CLI read uses path-style addressing and the demo certificate bypass:

```bash
aws --endpoint-url https://localhost:8443 --no-verify-ssl \
  --no-sign-request s3 cp s3://cache-demo/objects/object-01.bin /tmp/object-01.bin
```

The public Nginx endpoint permits only `GET` and `HEAD`. `PUT`, `POST`, `DELETE`, bucket management methods, and `PURGE` return `405`; they are never sent to SeaweedFS or Varnish.

## Configuration

Copy `.env.example` to `.env`. The primary settings are:

| Setting | Default | Purpose |
| --- | --- | --- |
| `S3_HTTPS_PORT` | `8443` | Host port published by the HTTPS gateway. |
| `VARNISH_DISK_SIZE` | `32M` | Fixed size of Varnish's `file` storage backend. |
| `VARNISH_PURGE_PORT` | `6081` | Varnish management listener, bound only to `127.0.0.1`. |
| `S3_BUCKET` | `cache-demo` | Bucket created by `weed mini` and populated by the seed job. |
| `SEED_OBJECT_COUNT` | `64` | Number of fixed, 1 MiB source objects written once by the seed job. |

Before Varnish starts, `varnish-init` writes and fsyncs the full cache backing file in `varnish_cache`. A capacity failure therefore fails the init service clearly instead of letting Varnish begin serving with an undersized cache. The backing file is deliberately distinct from the persistent `seaweed_data` volume.

`MAIN.n_lru_nuked` is Varnish's storage-pressure counter. A growing value means it evicted least-recently-used cache entries to make room; it is expected with a deliberately small cache in this demo. See the [Varnish storage backend documentation](https://www.varnish.org/docs/users-guide/storage-backends/) and its [cache sizing guide](https://varnish-cache.org/docs/5.2/users-guide/sizing-your-cache.html).

## Cache-only purge

Varnish's only host-published port is loopback-bound. A purge must use the same `Host` that Nginx used for the cached request; this preserves the Varnish hash. It removes the selected cached URL and Vary variants only, leaving SeaweedFS source data unchanged.

```bash
curl -i -X PURGE \
  -H 'Host: localhost:8443' \
  http://127.0.0.1:6081/cache-demo/objects/object-01.bin
```

The VCL also restricts purge clients to loopback and private networks. See [Varnish's purge documentation](https://www.varnish.org/docs/users-guide/purging/).

## Documentation

- [Demo runbook](docs/run-demo.md): full verification sequence, including public read-only enforcement and cache-only purge.
- [Developer guide](docs/varnish-guide.md): component responsibilities and cache policy.

Stop and remove persistent demo data, cache, and the generated certificate with:

```bash
docker compose down -v
```

SeaweedFS is pinned and started with its documented `weed mini` single-node S3 setup. This is a local demo, not an HA or production SeaweedFS deployment; see the [SeaweedFS Docker quick start](https://github.com/seaweedfs/seaweedfs).
