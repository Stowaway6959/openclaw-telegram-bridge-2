#!/bin/bash
cd "$(dirname "$0")"

if [ -f bridge.pid ]; then
    PID=$(cat bridge.pid)
    if kill -0 $PID 2>/dev/null; then
        kill $PID
        echo "Bridge 2 AIR stopped (PID $PID)"
    else
        echo "Process not running"
    fi
    rm -f bridge.pid
else
    pkill -f telegram_bridge.py && echo "Bridge 2 AIR stopped" || echo "Not running"
fi
