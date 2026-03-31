#!/bin/sh
# Extended bot_controller entrypoint.
#
# This image installs extra system packages (e.g. socat and its deps)
# that some compiled bots need for networking to work correctly inside
# the container.
#
# If PROXY_FWD_ENABLE=1 is set, socat forwards localhost:<port> to
# proxy_controller:<port> so bots that ignore --LadderServer and
# connect to localhost can still reach the proxy.  This is OFF by
# default — the image is mainly used for the extra libraries it
# provides.

if [ "${PROXY_FWD_ENABLE:-0}" = "1" ]; then
    PORT="${PROXY_FWD_PORT:-8080}"
    DELAY="${PROXY_FWD_DELAY:-5}"
    if [ "$DELAY" -gt 0 ] 2>/dev/null; then
        ( sleep "$DELAY" && exec socat TCP-LISTEN:"${PORT}",fork,reuseaddr TCP:proxy_controller:"${PORT}" ) &
    else
        socat TCP-LISTEN:"${PORT}",fork,reuseaddr TCP:proxy_controller:"${PORT}" &
    fi
fi
exec ./bot_controller "$@"
