#!/bin/bash
# Keep Mac awake while Telegram bridge is running
caffeinate -i -w $$ &
cd /Users/dc/Desktop/APPS/reolink-telegram-bridge-air2
exec /usr/bin/python3 -u telegram_bridge.py
