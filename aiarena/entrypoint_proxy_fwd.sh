#!/bin/sh
# Entrypoint wrapper that forwards the proxy port on localhost via socat.
#
# In the aiarena multi-container setup, fast-starting bots (C++, .NET)
# can connect to the proxy before SC2 instances finish starting.  The
# proxy's WebSocket connection to SC2 then fails silently and the bot
# hangs waiting for a Ping response.
#
# To avoid this, ACBOT_PROXY_HOST is set to 127.0.0.1 in the compose
# override so bot_controller passes --LadderServer 127.0.0.1.  The bot
# is forced through this socat relay, which starts after a delay to give
# SC2 time to become ready.  The bot's client library retries the
# localhost connection until socat is up.
#
# PROXY_FWD_PORT  (default 8080): port to forward to proxy_controller.
# PROXY_FWD_DELAY (default 5):    seconds to wait before starting socat.

PORT="${PROXY_FWD_PORT:-8080}"
DELAY="${PROXY_FWD_DELAY:-5}"

if [ "$DELAY" -gt 0 ] 2>/dev/null; then
    ( sleep "$DELAY" && exec socat TCP-LISTEN:"${PORT}",fork,reuseaddr TCP:proxy_controller:"${PORT}" ) &
else
    socat TCP-LISTEN:"${PORT}",fork,reuseaddr TCP:proxy_controller:"${PORT}" &
fi
exec ./bot_controller "$@"
