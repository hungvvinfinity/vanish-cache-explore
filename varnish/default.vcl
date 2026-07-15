vcl 4.1;

import std;

backend default {
    .host = "seaweed";
    .port = "8333";
    .connect_timeout = 2s;
    .first_byte_timeout = 30s;
    .between_bytes_timeout = 10s;
    .probe = {
        .url = "/healthz";
        .timeout = 2s;
        .interval = 5s;
        .window = 5;
        .threshold = 3;
    }
}

acl purge {
    "localhost";
    "127.0.0.1";
    "::1";
    "10.0.0.0"/8;
    "172.16.0.0"/12;
    "192.168.0.0"/16;
}

sub vcl_recv {
    if (req.method == "PURGE") {
        if (!client.ip ~ purge) {
            return (synth(405, "PURGE is not allowed from this client"));
        }
        if (req.url !~ "^/cache-demo/objects/[^?]+$") {
            return (synth(400, "PURGE requires an exact /cache-demo/objects/<key> URL"));
        }
        set req.url = std.querysort(req.url);
        return (purge);
    }

    # Mutations never enter the cache. Nginx rejects them publicly; this is a
    # second guard for requests made directly on the private listener.
    if (req.method != "GET" && req.method != "HEAD") {
        return (pass);
    }

    if (req.http.Authorization) {
        return (pass);
    }

    if (req.http.Cookie) {
        return (pass);
    }

    # Only path-style reads of seeded S3 objects are cache candidates.
    if (req.url !~ "^/cache-demo/objects/[^?]+") {
        return (pass);
    }

    # Equivalent query strings share a hash, without removing parameters that
    # may affect a real S3 response.
    set req.url = std.querysort(req.url);
    return (hash);
}

sub vcl_backend_response {
    # Never store writes, errors, bucket responses, or non-object responses.
    if ((bereq.method != "GET" && bereq.method != "HEAD") ||
        beresp.status != 200 ||
        bereq.url !~ "^/cache-demo/objects/[^?]+") {
        set beresp.uncacheable = true;
        set beresp.ttl = 0s;
        return (deliver);
    }

    if (beresp.http.Set-Cookie || beresp.http.Cache-Control ~ "(?i)(private|no-cache|no-store)") {
        set beresp.uncacheable = true;
        set beresp.ttl = 0s;
        return (deliver);
    }

    set beresp.ttl = 1h;
    set beresp.grace = 5m;
    set beresp.keep = 10m;
    # The Docker entrypoint also provides a default malloc store. Store actual
    # cache objects in the explicitly configured second `file` store instead.
    set beresp.storage = storage.s1;
    return (deliver);
}

sub vcl_purge {
    return (synth(200, "Purged cached URL and its variants"));
}

sub vcl_synth {
    if (resp.status == 200 && resp.reason == "Purged cached URL and its variants") {
    set resp.http.Content-Type = "text/plain; charset=utf-8";
    synthetic("Purged cached URL and its variants. SeaweedFS source data was not changed.");
    return (deliver);
    }
}

sub vcl_deliver {
    if (obj.hits > 0) {
        set resp.http.X-Cache = "HIT";
    } else {
        set resp.http.X-Cache = "MISS";
    }
    set resp.http.X-Cache-Hits = obj.hits;
    set resp.http.X-Cache-Host = server.hostname;
}
