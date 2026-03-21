#!/bin/sh
# Entrypoint wrapper that forwards the proxy port on localhost
# so bots that connect to localhost:GamePort can reach the proxy.
#
# PROXY_FWD_PORT (default 8080): port to forward to proxy_controller.

PORT="${PROXY_FWD_PORT:-8080}"
socat TCP-LISTEN:"${PORT}",fork,reuseaddr TCP:proxy_controller:"${PORT}" &
exec ./bot_controller "$@"
