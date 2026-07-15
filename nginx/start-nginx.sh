#!/bin/sh
set -eu

tls_dir=/etc/nginx/tls
cert="$tls_dir/tls.crt"
key="$tls_dir/tls.key"

mkdir -p "$tls_dir"
if [ ! -s "$cert" ] || [ ! -s "$key" ]; then
    echo "Generating a self-signed localhost TLS certificate in the nginx_tls volume"
    openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 365 \
        -subj '/CN=localhost' \
        -addext 'subjectAltName=DNS:localhost,IP:127.0.0.1' \
        -keyout "$key" -out "$cert"
    chmod 600 "$key"
fi

exec nginx -g 'daemon off;'
