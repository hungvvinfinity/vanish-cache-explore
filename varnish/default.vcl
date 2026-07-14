vcl 4.1;

import std;

backend default {
    .host = "origin";
    .port = "80";
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
        return (purge);
    }

    if (req.method != "GET" && req.method != "HEAD") {
        return (pass);
    }

    if (req.http.Authorization) {
        return (pass);
    }

    if (req.url ~ "^/(admin|account|api/private|login|logout|private)(/|$)") {
        return (pass);
    }

    if (req.http.Cookie) {
        if (req.http.Cookie ~ "(?i)(auth|logged_in|session|token)=") {
            return (pass);
        }

        unset req.http.Cookie;
    }

    set req.url = std.querysort(req.url);
    return (hash);
}

sub vcl_backend_response {
    if (beresp.http.Set-Cookie) {
        set beresp.uncacheable = true;
        set beresp.ttl = 120s;
        return (deliver);
    }

    if (beresp.http.Cache-Control ~ "(?i)(private|no-cache|no-store)") {
        set beresp.uncacheable = true;
        set beresp.ttl = 120s;
        return (deliver);
    }

    if (bereq.url ~ "^/payload/" || bereq.url ~ "^/cacheable") {
        set beresp.ttl = 1h;
        set beresp.grace = 5m;
        set beresp.keep = 10m;
        return (deliver);
    }

    if (beresp.ttl <= 0s) {
        set beresp.uncacheable = true;
        set beresp.ttl = 120s;
        return (deliver);
    }

    set beresp.grace = 1m;
    set beresp.keep = 5m;
    return (deliver);
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
