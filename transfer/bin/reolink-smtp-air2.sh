#!/bin/bash
# Keep Mac awake while smtp listener is running (prevents missed motion alerts during sleep)
caffeinate -i -w $$ &
cd /Users/dc/Desktop/APPS/reolink-telegram-bridge-air2
exec /usr/bin/python3 -u smtp_listener.py
