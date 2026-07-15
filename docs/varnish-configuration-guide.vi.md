# Hướng dẫn cấu hình VCL cho Varnish–SeaweedFS

Tài liệu này chỉ tập trung vào VCL. Trong triển khai native của repo, file
nguồn là `ansible/roles/seaweed_varnish_gateway/templates/default.vcl.j2` và
được Ansible render thành `/etc/varnish/default.vcl`. Với Docker Compose, file
tương ứng là `varnish/default.vcl`.

```text
Nginx -> Varnish (VCL) -> SeaweedFS S3
```

SeaweedFS là origin lưu object; VCL quyết định request nào được cache, cache
bao lâu và request nào phải đi thẳng tới SeaweedFS.

## Cấu trúc file VCL

```vcl
vcl 4.1;

import std;

backend default { ... }
acl purge { ... }

sub vcl_recv { ... }             # Xử lý request trước cache lookup
sub vcl_backend_response { ... } # Quyết định có lưu response origin hay không
sub vcl_purge { ... }            # Phản hồi khi PURGE thành công
sub vcl_synth { ... }            # Nội dung response do Varnish tự tạo
sub vcl_deliver { ... }          # Header trả về client
```

Luồng chính là: `vcl_recv` trả `hash` để tra cache hoặc `pass` để gọi origin
không lưu; cache miss sẽ lấy response từ SeaweedFS, rồi
`vcl_backend_response` quyết định lưu hay không; cuối cùng `vcl_deliver` trả
response cho client.

## 1. Khai báo backend SeaweedFS

Varnish phải gọi đúng cổng S3 của SeaweedFS, không phải master/volume server.
Native deployment dùng loopback; Compose dùng hostname service `seaweed`.

```vcl
backend default {
    .host = "127.0.0.1";
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
```

- `.connect_timeout`: thời gian tối đa để mở kết nối tới SeaweedFS.
- `.first_byte_timeout`: thời gian chờ byte đầu của response.
- `.between_bytes_timeout`: khoảng im lặng tối đa khi SeaweedFS đang stream.
- Probe coi backend healthy khi ít nhất `threshold` trong `window` lần kiểm tra
  gần nhất thành công.

Trong template Ansible, dùng biến thay vì hard-code cổng:

```vcl
.host = "127.0.0.1";
.port = "{{ gateway_seaweedfs_s3_port }}";
```

### `backend default` hỗ trợ loại backend nào?

`backend default` là một **origin HTTP đơn lẻ**, không phải adapter cho mọi giao thức. Varnish gửi HTTP request tới backend và nhận HTTP response, nên nó phù hợp trực tiếp với SeaweedFS S3, API HTTP hoặc web server HTTP.

| Cách kết nối | Cấu hình VCL | Khi dùng |
| --- | --- | --- |
| TCP HTTP | `.host` và `.port` | Mặc định; SeaweedFS S3 của repo dùng cách này. Host có thể là IPv4, IPv6 hoặc hostname được phân giải khi nạp VCL. |
| Unix-domain socket HTTP | `.path = "/run/origin.sock";` | Origin chạy cùng máy và web server mở HTTP qua socket Unix. |
| Nhiều origin | Khai báo nhiều `backend`, rồi chọn bằng `req.backend_hint` hoặc director | Failover/load balancing; `backend default` vẫn chỉ là một origin cụ thể. |
| Backend động | VMOD phù hợp, rồi gán `req.backend_hint` | Service discovery hoặc topology thay đổi; chỉ dùng khi có yêu cầu vận hành rõ ràng. |

Ví dụ Unix socket:

```vcl
backend default {
    .path = "/run/seaweed-s3.sock";
    .connect_timeout = 2s;
    .first_byte_timeout = 30s;
}
```

Ví dụ lựa chọn origin theo path (không áp dụng cho stack hiện tại):

```vcl
backend seaweed_primary { .host = "10.0.0.11"; .port = "8333"; }
backend image_api       { .host = "10.0.0.12"; .port = "8080"; }

sub vcl_recv {
    if (req.url ~ "^/images/") {
        set req.backend_hint = image_api;
    } else {
        set req.backend_hint = seaweed_primary;
    }
    # Sau đây là chính sách cache của ứng dụng.
}
```

Varnish trong mô hình này nói HTTP cleartext với origin. Không chỉ đổi `.port` thành `443` để dùng HTTPS: cổng 443 thường yêu cầu TLS. Nếu origin chỉ có HTTPS, đặt một TLS-terminating proxy cục bộ (Nginx, HAProxy hoặc stunnel) giữa Varnish và origin, hoặc dùng giải pháp/VMOD TLS đã được đội vận hành hỗ trợ.

## 2. Xác định chính xác cái gì nên cache

Không bắt đầu bằng regex URL. Trước hết developer cần lập "hợp đồng cache" cho từng endpoint: cùng một cache key có luôn trả cùng một representation cho mọi client được phép truy cập không, và object cũ có chấp nhận được trong bao lâu?

Chỉ cache khi **tất cả** điều sau đúng:

1. Request an toàn để lặp lại: thường là `GET` hoặc `HEAD`, không phải ghi hay thao tác có side effect.
2. Response được chia sẻ an toàn: không phụ thuộc user, cookie, bearer token, quyền S3, địa lý, A/B test hoặc header mà cache key không bao gồm.
3. URL/query/header dùng làm cache key mô tả đầy đủ representation. Hai request có cùng key phải được phép nhận chính xác cùng body và header cacheable.
4. Có chính sách freshness/invalidation: TTL phù hợp, versioned key immutable, hoặc PURGE sau khi dữ liệu gốc thay đổi.
5. Response thành công và có kích thước/chi phí đủ đáng để cache.

| Loại request/response | Chính sách khuyến nghị | Lý do |
| --- | --- | --- |
| `GET /bucket/objects/app.abc123.js` trả `200`, public | Cache | Key có version/hash là immutable; dùng TTL dài. |
| `GET` object public, key có thể bị ghi đè | Cache có TTL ngắn hoặc PURGE sau ghi | Tránh trả object cũ quá lâu. |
| `HEAD` của object public | Cache cùng chính sách GET | Metadata ổn định cùng object; vẫn xác nhận response thực tế. |
| `GET` có `Authorization`, `Cookie` hoặc signed/presigned URL | `pass` mặc định | Response có thể phụ thuộc danh tính hoặc quyền truy cập. |
| `PUT`, `POST`, `DELETE`, multipart upload | `pass`/chặn ở proxy | Không được lưu response ghi trong shared cache. |
| `206 Partial Content`, range download | Không cache ở chính sách đơn giản | Cần thiết kế riêng cho Range; repo chỉ cache `200`. |
| `404`, `403`, `5xx`, redirect, bucket listing | Không cache mặc định | Tránh cache lỗi/quyền tạm thời hoặc response không phải object. |

### Developer kiểm tra response backend thế nào?

`vcl_recv` chạy **trước** khi Varnish gọi SeaweedFS, nên chỉ biết method, URL, request header và client. Nó chỉ nên chọn *ứng viên cache*. Quyết định cuối cùng dựa trên status/header/body metadata của backend phải đặt trong `vcl_backend_response`.

Trước khi thêm một endpoint vào policy, lấy mẫu response đại diện từ origin và liệt kê các biến thể thực tế:

```bash
# Header của object thành công
curl -sS -D - -o /dev/null http://127.0.0.1:8333/cache-demo/objects/object-01.bin

# Các biến thể cần kiểm tra riêng nếu ứng dụng hỗ trợ
curl -sS -D - -o /dev/null -H 'Range: bytes=0-99' http://127.0.0.1:8333/cache-demo/objects/object-01.bin
curl -sS -D - -o /dev/null -I http://127.0.0.1:8333/cache-demo/objects/object-01.bin
curl -sS -D - -o /dev/null 'http://127.0.0.1:8333/cache-demo/objects/object-01.bin?versionId=example'
```

Ghi nhận tối thiểu `status`, `Content-Type`, `Content-Length`, `ETag`, `Last-Modified`, `Cache-Control`, `Set-Cookie`, `Vary` và `Content-Range`. Sau đó kiểm tra cùng URL với các query parameter, Authorization/Cookie, Range và các mã lỗi mà client thật có thể gửi. `varnishlog` trên môi trường test giúp so sánh request được Varnish gửi tới backend với response thực tế.

Ví dụ: endpoint chỉ được đưa vào regex cache sau khi xác nhận mọi response `200` public của nó không đặt cookie, không thay đổi theo user và query string không thay đổi representation ngoài các parameter đã được đưa vào cache key. Nếu chưa chứng minh được điều đó, chọn `pass` là chính sách đúng.

## 3. `vcl_recv`: chọn request được cache

Đây là phần quan trọng nhất của VCL. Mẫu dưới đây chỉ cache object S3
path-style trong bucket mong muốn, chỉ với `GET`/`HEAD` và không có thông tin
riêng theo người dùng.

```vcl
sub vcl_recv {
    if (req.method == "PURGE") {
        if (!client.ip ~ purge) {
            return (synth(405, "PURGE is not allowed from this client"));
        }
        if (req.url !~ "^/cache-demo/objects/[^?]+$") {
            return (synth(400, "PURGE requires an exact object URL"));
        }
        set req.url = std.querysort(req.url);
        return (purge);
    }

    if (req.method != "GET" && req.method != "HEAD") {
        return (pass);
    }

    if (req.http.Authorization || req.http.Cookie) {
        return (pass);
    }

    # Presigned S3 URL mang chữ ký/quyền trong query string, không phải cache
    # shared object chỉ vì nó không có Authorization header.
    if (req.url ~ "(?i)([?&]X-Amz-(Algorithm|Credential|Signature|Security-Token)=)") {
        return (pass);
    }

    if (req.url !~ "^/cache-demo/objects/[^?]+") {
        return (pass);
    }

    set req.url = std.querysort(req.url);
    return (hash);
}
```

### Ý nghĩa các `return`

| Lệnh | Ý nghĩa |
| --- | --- |
| `return (hash)` | Dùng cache key để tìm object trong cache; miss mới gọi SeaweedFS. |
| `return (pass)` | Bỏ qua lookup và không lưu response; dùng cho request riêng tư, ghi dữ liệu hoặc URL ngoài phạm vi cache. |
| `return (purge)` | Xóa object khớp cache key và các biến thể Vary khỏi Varnish. |
| `return (synth(...))` | Tự trả response từ Varnish, không gọi backend. |

### Cache key và query string

Varnish hash theo URL và `Host` mặc định. Do đó URL và `Host` của PURGE phải
giống request đã cache. `std.querysort()` sắp xếp query parameter, khiến:

```text
/cache-demo/objects/a.bin?partNumber=1&x=2
/cache-demo/objects/a.bin?x=2&partNumber=1
```

dùng cùng cache key. Hàm này **không xóa** parameter; đây là điểm quan trọng
với S3, vì query có thể làm thay đổi response hoặc là chữ ký request.

Không cache request có `Authorization` hoặc presigned S3 parameter: response có thể phụ thuộc quyền truy cập. Nếu muốn cache endpoint authenticated, cần thiết kế cache key và chính sách phân quyền riêng; không chỉ bỏ điều kiện này.

## 4. `vcl_backend_response`: quy tắc lưu response

Phần này chỉ chạy sau khi Varnish đã nhận response từ SeaweedFS. Luôn đặt
điều kiện không-cache trước, rồi mới gán TTL cho response an toàn.

```vcl
sub vcl_backend_response {
    if ((bereq.method != "GET" && bereq.method != "HEAD") ||
        beresp.status != 200 ||
        bereq.url !~ "^/cache-demo/objects/[^?]+") {
        set beresp.uncacheable = true;
        set beresp.ttl = 0s;
        return (deliver);
    }

    if (beresp.http.Set-Cookie ||
        beresp.http.Cache-Control ~ "(?i)(private|no-cache|no-store)") {
        set beresp.uncacheable = true;
        set beresp.ttl = 0s;
        return (deliver);
    }

    set beresp.ttl = 1h;
    set beresp.grace = 5m;
    set beresp.keep = 10m;
    set beresp.storage = storage.s0;
    return (deliver);
}
```

- `bereq` là request gửi tới SeaweedFS; `beresp` là response từ SeaweedFS.
- `beresp.status != 200` ngăn cache lỗi, redirect, response bucket/listing và
  các response ngoài chính sách.
- `beresp.uncacheable = true` bảo đảm response vẫn đến client nhưng không tạo
  cached object.
- Không xóa `Set-Cookie` để ép cache một response riêng tư. Nếu origin trả
  cookie bất ngờ, hãy tìm nguyên nhân tại origin hoặc giữ response uncacheable.

### TTL, grace và keep

```vcl
set beresp.ttl = 1h;
set beresp.grace = 5m;
set beresp.keep = 10m;
```

| Giá trị | Tác dụng |
| --- | --- |
| `ttl` | Thời gian object được coi là fresh và có thể trả trực tiếp từ cache. |
| `grace` | Sau TTL, cho phép tạm trả object cũ trong khi Varnish refresh từ SeaweedFS. |
| `keep` | Giữ metadata sau TTL/grace để phục vụ request có điều kiện; không đảm bảo body vẫn còn trong storage. |

Với object immutable (ví dụ key có version/hash), TTL dài như `24h` hoặc lâu
hơn thường hợp lý. Với key bị ghi đè, dùng TTL ngắn hoặc PURGE URL sau khi ghi.
Không đặt TTL dài nếu không có cơ chế invalidation.

### Chọn storage từ VCL

`storage.s0` là storage đầu tiên khi varnishd chỉ được truyền một `-s`.
Nếu daemon định nghĩa storage có tên, VCL phải dùng tên đó:

```vcl
# -s ram,malloc,2G -s disk,file,/var/lib/varnish/cache/cache.bin,20G
if (beresp.http.Content-Length &&
    std.integer(beresp.http.Content-Length, 0) <= 8388608) {
    set beresp.storage = storage.ram;
} else {
    set beresp.storage = storage.disk;
}
```

Đặt đoạn này thay cho `set beresp.storage = storage.s0;`. Ví dụ đưa object tối
đa 8 MiB vào RAM; response không có `Content-Length` đi disk để tránh chiếm
RAM bất ngờ. `beresp.storage` chỉ chọn nơi đặt **cache**, không thay đổi nơi
SeaweedFS lưu object gốc.

## 5. PURGE an toàn trong VCL

Khai báo ACL hẹp cho listener Varnish private:

```vcl
acl purge {
    "localhost";
    "127.0.0.1";
    "::1";
}
```

Nhánh `PURGE` trong `vcl_recv` phải kiểm tra cả IP lẫn prefix URL. Sau khi
Purge thành công, Varnish gọi `vcl_purge`; `vcl_synth` có thể tạo response rõ
ràng cho client:

```vcl
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
```

Gửi PURGE với `Host` đúng cache key:

```bash
curl -i -X PURGE \
  -H 'Host: s3-cache.example.com' \
  http://127.0.0.1:6081/cache-demo/objects/object-01.bin
```

PURGE không xóa object trong SeaweedFS. Request `GET` tiếp theo là cache miss
và lấy lại object từ origin.

## 6. Header debug trong `vcl_deliver`

Trong môi trường demo hoặc nội bộ, thêm header để kiểm tra policy:

```vcl
sub vcl_deliver {
    if (obj.hits > 0) {
        set resp.http.X-Cache = "HIT";
    } else {
        set resp.http.X-Cache = "MISS";
    }
    set resp.http.X-Cache-Hits = obj.hits;
    set resp.http.X-Cache-Host = server.hostname;
}
```

Hai lần gọi cùng URL phải lần lượt có `X-Cache: MISS` và `X-Cache: HIT`:

```bash
curl -k -I https://s3-cache.example.com/cache-demo/objects/object-01.bin
curl -k -I https://s3-cache.example.com/cache-demo/objects/object-01.bin
```

Không nên công khai các header debug nếu chúng tiết lộ hostname/topology không
phù hợp với môi trường production.

## 7. Kiểm tra VCL trước khi áp dụng

Sau khi Ansible render file trên host, compile VCL trước khi restart:

```bash
sudo varnishd -C -f /etc/varnish/default.vcl
```

Sau khi deploy, dùng các lệnh sau để chẩn đoán policy:

```bash
sudo varnishadm backend.list
sudo varnishlog -g request -q 'ReqURL ~ "object-01.bin"'
sudo varnishstat -1 | grep -E 'MAIN.(cache_hit|cache_miss|n_lru_nuked|n_object)'
```

Nếu request luôn MISS, kiểm tra theo thứ tự: URL có khớp regex cache không,
có `Authorization`/`Cookie` không, status từ SeaweedFS có phải `200` không,
response có `Set-Cookie` hoặc `Cache-Control` cấm cache không, và storage còn
capacity không.
