#!/bin/bash
cd "$(dirname "$0")"

# Start Telegram bridge
if [ -f bridge.pid ] && kill -0 $(cat bridge.pid) 2>/dev/null; then
    echo "Bridge already running (PID $(cat bridge.pid))"
else
    nohup python3 telegram_bridge.py > bridge2.log 2>&1 &
    echo $! > bridge.pid
    echo "Bridge 2 AIR started (PID $!)"
fi

# Start SMTP motion listener
if [ -f smtp.pid ] && kill -0 $(cat smtp.pid) 2>/dev/null; then
    echo "SMTP listener already running (PID $(cat smtp.pid))"
else
    nohup python3 smtp_listener.py > smtp.log 2>&1 &
    echo $! > smtp.pid
    echo "SMTP listener started (PID $!)"
fi
