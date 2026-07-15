#!/bin/sh
set -eu

: "${VARNISH_DISK_SIZE:?VARNISH_DISK_SIZE must be set}"
cache_dir=/var/lib/varnish/cache
cache_file="$cache_dir/cache.bin"

mkdir -p "$cache_dir"
rm -f "$cache_file"

case "$VARNISH_DISK_SIZE" in
    *[Kk]) bytes=$((${VARNISH_DISK_SIZE%?} * 1024)) ;;
    *[Mm]) bytes=$((${VARNISH_DISK_SIZE%?} * 1024 * 1024)) ;;
    *[Gg]) bytes=$((${VARNISH_DISK_SIZE%?} * 1024 * 1024 * 1024)) ;;
    *[Kk][Bb]) bytes=$((${VARNISH_DISK_SIZE%??} * 1024)) ;;
    *[Mm][Bb]) bytes=$((${VARNISH_DISK_SIZE%??} * 1024 * 1024)) ;;
    *[Gg][Bb]) bytes=$((${VARNISH_DISK_SIZE%??} * 1024 * 1024 * 1024)) ;;
    *[!0-9]*)
        echo "ERROR: VARNISH_DISK_SIZE must be a whole number of bytes, K, M, or G (got $VARNISH_DISK_SIZE)" >&2
        exit 64
        ;;
    *) bytes=$VARNISH_DISK_SIZE ;;
esac

available_kib=$(df -Pk "$cache_dir" | awk 'NR == 2 { print $4 }')
required_kib=$(((bytes + 1023) / 1024))
if [ -z "$available_kib" ] || [ "$required_kib" -gt "$available_kib" ]; then
    echo "ERROR: could not preallocate $VARNISH_DISK_SIZE for Varnish at $cache_file (need ${required_kib} KiB, have ${available_kib:-unknown} KiB)" >&2
    exit 70
fi

# Write and fsync the whole file rather than using a sparse truncate. This
# fails here, before Varnish starts serving, when the cache volume cannot hold
# the configured capacity.
if ! dd if=/dev/zero of="$cache_file" bs="$VARNISH_DISK_SIZE" count=1 conv=fsync status=none; then
    echo "ERROR: could not preallocate $VARNISH_DISK_SIZE for Varnish at $cache_file" >&2
    rm -f "$cache_file"
    exit 70
fi

# The official Varnish image launches the cache-owning child as uid/gid 1000.
# The init job runs as root on a newly created named volume, so hand ownership
# of the preallocated backing file to that child before it starts.
chown 1000:1000 "$cache_dir" "$cache_file"
actual_size=$(wc -c < "$cache_file" | tr -d ' ')
echo "Preallocated Varnish disk cache: $actual_size bytes at $cache_file"
