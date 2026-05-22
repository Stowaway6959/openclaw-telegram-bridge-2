#!/bin/bash
cd "$(dirname "$0")"
echo "Setting up OpenClaw Telegram Bridge 2 AIR..."

# Install dependencies
pip3 install anthropic python-dotenv --quiet

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

chmod +x start_services.sh stop_services.sh
echo "Done. Edit .env then run ./start_services.sh"
