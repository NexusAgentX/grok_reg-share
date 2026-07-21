#!/bin/sh
set -eu

umask 077
mkdir -p "$GROK_REG_DATA_DIR" "$HOME"

if [ "$#" -eq 0 ]; then
    set -- python web_app.py
fi

_display_number=${DISPLAY#:}
_display_number=${_display_number%%.*}
rm -f "/tmp/.X${_display_number}-lock"
Xvfb "$DISPLAY" -screen 0 "${XVFB_SCREEN:-1920x1080x24}" -nolisten tcp -noreset &
xvfb_pid=$!

cleanup() {
    if [ -n "${app_pid:-}" ]; then
        kill "$app_pid" 2>/dev/null || true
    fi
    kill "$xvfb_pid" 2>/dev/null || true
    wait "$xvfb_pid" 2>/dev/null || true
}
trap 'cleanup; exit 143' INT TERM HUP

socket="/tmp/.X11-unix/X${_display_number}"
i=0
while [ ! -S "$socket" ]; do
    if ! kill -0 "$xvfb_pid" 2>/dev/null; then
        echo "Xvfb failed to start" >&2
        exit 1
    fi
    i=$((i + 1))
    if [ "$i" -ge 100 ]; then
        echo "timed out waiting for Xvfb" >&2
        exit 1
    fi
    sleep 0.05
done

"$@" &
app_pid=$!
set +e
wait "$app_pid"
status=$?
set -e
cleanup
exit "$status"
