#!/bin/bash
# Initial VPS setup for BLS Monitor
# Run once on a fresh VPS: bash setup-vps.sh
set -e

echo "=== BLS Monitor VPS Setup ==="

# Install Docker if not present
if ! command -v docker &> /dev/null; then
    echo "Installing Docker..."
    curl -fsSL https://get.docker.com | sh
    systemctl enable docker
    systemctl start docker
fi

# Install Docker Compose plugin if not present
if ! docker compose version &> /dev/null; then
    echo "Installing Docker Compose plugin..."
    apt-get update && apt-get install -y docker-compose-plugin
fi

# Clone repository
if [ ! -d /opt/bls-monitor ]; then
    echo "Cloning repository..."
    git clone https://github.com/bunnycharl/bls-monitor.git /opt/bls-monitor
else
    echo "Repository already exists, pulling latest..."
    cd /opt/bls-monitor && git pull origin master
fi

cd /opt/bls-monitor

# Create .env file if not exists
if [ ! -f .env ]; then
    echo "Creating .env file from template..."
    cp .env.example .env
    echo ""
    echo "!!! IMPORTANT: Edit /opt/bls-monitor/.env with your credentials !!!"
    echo "    nano /opt/bls-monitor/.env"
    echo ""
fi

# Create config if not exists
if [ ! -f config/settings.yaml ]; then
    echo "Creating settings.yaml from template..."
    cp config/settings.example.yaml config/settings.yaml
fi

# Create runtime directories
mkdir -p logs screenshots session

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit credentials:  nano /opt/bls-monitor/.env"
echo "  2. Edit config:       nano /opt/bls-monitor/config/settings.yaml"
echo "  3. Start monitor:     cd /opt/bls-monitor && docker compose up -d --build"
echo "  4. View logs:         docker compose logs -f"
echo ""
