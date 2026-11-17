# Starts and stops Feed's local server. The launcher (omarchy-feed), setup
# and update source this after setting DIR to the checkout.
# shellcheck shell=bash

PORT=47220
STATE="${XDG_STATE_HOME:-$HOME/.local/state}/omarchy-feed"
LOG="$STATE/server.log"

# Whether Feed's server answers on its port.
running() {
  curl -fs --max-time 1 "http://127.0.0.1:$PORT/api/ping" 2>/dev/null | grep -q '"omarchy-feed"'
}

# Whether nothing listens on Feed's port any more: curl can't connect.
gone() {
  local code=0
  curl -fs --max-time 1 -o /dev/null "http://127.0.0.1:$PORT/api/ping" 2>/dev/null || code=$?
  ((code == 7))
}

# Starts the checkout's server and waits until it answers. One that does not
# answer is stopped, so it does not hold the port.
start_server() {
  local pid _
  mkdir -p "$STATE"
  chmod 700 "$STATE"
  setsid python3 "$DIR/server.py" --port "$PORT" >>"$LOG" 2>&1 </dev/null &
  pid=$!
  for _ in $(seq 50); do
    running && return 0
    sleep 0.1
  done
  kill "$pid" 2>/dev/null || true
  return 1
}

# Stops a running server now, whatever windows are open, and waits until its
# port is free, so the next start runs the current code. An open Feed window
# then says the server stopped.
stop_server() {
  local _
  running || return 0
  curl -fs --max-time 2 -X POST -H "Origin: http://127.0.0.1:$PORT" \
    "http://127.0.0.1:$PORT/api/stop" >/dev/null 2>&1 || true
  for _ in $(seq 50); do
    gone && return 0
    sleep 0.1
  done
  return 1
}
