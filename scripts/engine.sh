#!/usr/bin/env bash
# The engine under a keep-alive loop.
#
#   scripts/engine.sh start     start the loop (and the engine) if not running
#   scripts/engine.sh stop      stop the engine gracefully, then the loop
#   scripts/engine.sh restart   stop, then start
#   scripts/engine.sh status    pids, health, checkout
#
# The loop runs uvicorn again whenever it exits — a crash, an out-of-memory
# kill — until `stop` has asked it not to. The pod's /post_start.sh calls
# `start` at boot, so a pod restart brings the engine back without anyone
# noticing it was gone. A case in flight when the engine stops is audited
# again when it starts (app/services/cartograph/resume.py), so a stop does
# not wait for it: uvicorn is given IDP_STOP_GRACE seconds to finish what it
# is doing and is then killed.
#
# Settings: IDP_PORT (8001), IDP_STOP_GRACE (30). Logs append to engine.log.
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN="$ROOT/run"
LOG="$ROOT/engine.log"
PORT="${IDP_PORT:-8001}"
GRACE="${IDP_STOP_GRACE:-30}"
SUP_PID="$RUN/supervisor.pid"
APP_PID="$RUN/engine.pid"
STOP_FLAG="$RUN/stop"
mkdir -p "$RUN"

alive() { [[ -n "${1:-}" ]] && kill -0 "$1" 2>/dev/null; }
pid_in() { [[ -f "$1" ]] && cat "$1" 2>/dev/null || true; }
stamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }
note() { echo "$(stamp) | supervisor | $*" >> "$LOG"; }
health() { curl -s -m 3 "http://127.0.0.1:$PORT/health" 2>/dev/null; }

supervise() {
    echo $$ > "$SUP_PID"
    trap 'rm -f "$SUP_PID"' EXIT
    while [[ ! -f "$STOP_FLAG" ]]; do
        cd "$ROOT" || exit 1
        note "starting engine on port $PORT ($(git -C "$ROOT" log --oneline -1 2>/dev/null || echo 'no git'))"
        "$ROOT/.venv/bin/uvicorn" app.main:app --host 0.0.0.0 --port "$PORT" \
            --timeout-graceful-shutdown "$GRACE" >> "$LOG" 2>&1 &
        echo $! > "$APP_PID"
        wait $! || true
        rm -f "$APP_PID"
        [[ -f "$STOP_FLAG" ]] && break
        note "engine exited — starting it again in 5s"
        sleep 5
    done
    note "supervisor stopped"
}

start() {
    local sup; sup="$(pid_in "$SUP_PID")"
    if alive "$sup"; then
        echo "already running (supervisor $sup, engine $(pid_in "$APP_PID"))"
        return 0
    fi
    if [[ -n "$(health)" ]]; then
        echo "something already answers on port $PORT and it is not under this supervisor — stop it first"
        return 1
    fi
    rm -f "$STOP_FLAG" "$SUP_PID" "$APP_PID"
    nohup setsid bash "${BASH_SOURCE[0]}" _supervise >/dev/null 2>&1 &
    for _ in $(seq 1 40); do
        sleep 1
        if [[ -n "$(health)" ]]; then
            echo "engine up (supervisor $(pid_in "$SUP_PID"), engine $(pid_in "$APP_PID")): $(health)"
            return 0
        fi
    done
    echo "engine did not answer on port $PORT within 40s — see $LOG"
    return 1
}

stop() {
    local sup app; sup="$(pid_in "$SUP_PID")"; app="$(pid_in "$APP_PID")"
    touch "$STOP_FLAG"
    if alive "$app"; then
        kill -TERM "$app" 2>/dev/null || true
        for _ in $(seq 1 $((GRACE + 10))); do
            alive "$app" || break
            sleep 1
        done
        if alive "$app"; then
            note "engine did not stop within $((GRACE + 10))s — killed"
            kill -KILL "$app" 2>/dev/null || true
        fi
    fi
    if alive "$sup"; then
        kill -TERM "$sup" 2>/dev/null || true
        sleep 1
    fi
    rm -f "$SUP_PID" "$APP_PID"
    echo "stopped"
}

status() {
    local sup app; sup="$(pid_in "$SUP_PID")"; app="$(pid_in "$APP_PID")"
    if alive "$sup"; then echo "supervisor: running (pid $sup)"; else echo "supervisor: not running"; fi
    if alive "$app"; then
        echo "engine: running (pid $app, started $(ps -o lstart= -p "$app"))"
    else
        echo "engine: not running"
    fi
    echo "health: $(health || echo 'no answer')"
    echo "checkout: $(git -C "$ROOT" log --oneline -1 2>/dev/null)"
}

case "${1:-}" in
    start) start ;;
    stop) stop ;;
    restart) stop; start ;;
    status) status ;;
    _supervise) supervise ;;
    *) echo "usage: $0 {start|stop|restart|status}"; exit 2 ;;
esac
