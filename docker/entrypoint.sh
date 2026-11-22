#!/bin/sh
# Run Fetchbox as PUID:PGID (default 1000:1000), owning /config; /data is left as it is
# (your media keeps its owner), but must be writable by that user.
set -eu
if [ "$(id -u)" = "0" ]; then
    groupadd -o -g "$PGID" fetchbox 2>/dev/null || true
    useradd -o -u "$PUID" -g "$PGID" -M -d /config -s /usr/sbin/nologin fetchbox 2>/dev/null || true
    mkdir -p /config
    chown -R "$PUID:$PGID" /config
    if ! setpriv --reuid="$PUID" --regid="$PGID" --init-groups test -w /data 2>/dev/null; then
        echo "warning: /data isn't writable by $PUID:$PGID; set PUID/PGID to its owner" >&2
    fi
    exec setpriv --reuid="$PUID" --regid="$PGID" --init-groups "$@"
fi
exec "$@"
