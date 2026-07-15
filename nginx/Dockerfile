ARG NGINX_IMAGE=nginx:1.30.3-alpine
FROM ${NGINX_IMAGE}

# The upstream Alpine image has TLS libraries but not the openssl command-line
# utility used to create this demo's self-signed certificate at startup.
RUN apk add --no-cache openssl
