#!/bin/bash
cd "$(dirname "$0")"

for service in bridge smtp; do
    pidfile="${service}.pid"
    if [ -f "$pidfile" ]; then
        PID=$(cat "$pidfile")
        if kill -0 $PID 2>/dev/null; then
            kill $PID && echo "Stopped $service (PID $PID)"
        else
            echo "$service not running"
        fi
        rm -f "$pidfile"
    fi
done
