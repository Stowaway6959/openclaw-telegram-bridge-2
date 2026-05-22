#!/bin/bash
cd "$(dirname "$0")"
DIR="$(pwd)"
USER_HOME="$HOME"
echo "Setting up OpenClaw Telegram Bridge 2 AIR..."

# Install dependencies
pip3 install anthropic python-dotenv aiosmtpd --quiet

# Create .env from template if missing
if [ ! -f .env ]; then
    cat > .env << 'EOF'
TELEGRAM_TOKEN=your_token
TELEGRAM_CHAT_ID=your_chat_id
CAMERA_HOST=192.168.1.199
CAMERA_USER=admin
CAMERA_PASSWORD=your_password
WEATHER_API_KEY=your_key
DEFAULT_LOCATION=65802
GOLDAPI_KEY=
NTFY_TOPIC=openclaw-sar-2
ANTHROPIC_API_KEY=your_key
EOF
    echo ".env created — fill in your keys"
else
    echo ".env already exists"
fi

# Fix plist paths for this machine/user
for plist in com.openclaw.bridge2.plist com.openclaw.smtp2.plist; do
    sed -i '' "s|/Users/dc/Desktop/APPS/openclaw-telegram-bridge-2|$DIR|g" "$plist"
done

chmod +x start_services.sh stop_services.sh
echo "Done. Edit .env then run ./start_services.sh"
echo "To enable auto-start on login: ./install_launchd.sh"
