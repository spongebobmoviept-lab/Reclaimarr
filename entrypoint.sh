#!/bin/sh
# Starts as root only long enough to make the data folder writable, then
# drops to PUID:PGID (default 1000:1000). The app itself never runs as root.
# If the container is already started as a non-root user (compose `user:`),
# this does nothing and just runs the app.
set -e

DATA_DIR="${DATA_DIR:-/data}"

if [ "$(id -u)" = "0" ]; then
    PUID="${PUID:-1000}"
    PGID="${PGID:-1000}"
    case "$PUID:$PGID" in
        *[!0-9:]*|:*|*:) echo "PUID and PGID must be numbers (got '$PUID:$PGID')" >&2; exit 1 ;;
    esac
    if [ "$PUID" = "0" ]; then
        echo "Refusing to run the app as root (PUID=0). Set PUID/PGID to a normal user." >&2
        exit 1
    fi
    mkdir -p "$DATA_DIR"
    # A bind-mounted folder Docker created on first start is owned by root;
    # hand it to the app user. Only touches the data folder, nothing else.
    if [ "$(stat -c %u "$DATA_DIR")" != "$PUID" ] || [ "$(stat -c %g "$DATA_DIR")" != "$PGID" ]; then
        chown -R "$PUID:$PGID" "$DATA_DIR"
    fi
    exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups -- "$@"
fi

exec "$@"
