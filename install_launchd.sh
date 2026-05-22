#!/bin/bash
cd "$(dirname "$0")"
cp com.openclaw.bridge2.plist ~/Library/LaunchAgents/
cp com.openclaw.smtp2.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.openclaw.bridge2.plist
launchctl load ~/Library/LaunchAgents/com.openclaw.smtp2.plist
echo "✅ Auto-start installed — both services will start on login"
