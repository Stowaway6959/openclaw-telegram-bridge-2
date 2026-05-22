#!/bin/bash
cd "$(dirname "$0")"

if [ -f bridge.pid ] && kill -0 $(cat bridge.pid) 2>/dev/null; then
    echo "Already running (PID $(cat bridge.pid))"
    exit 0
fi

nohup python3 telegram_bridge.py > bridge2.log 2>&1 &
echo $! > bridge.pid
echo "Bridge 2 AIR started (PID $!)"
