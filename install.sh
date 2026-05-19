#!/usr/bin/env bash
# Install hermes-node-client on Linux/macOS
# Usage: ./install.sh --gateway ws://host:port/ws --token secret [--node-id name] [--install-dir /path]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Defaults
GATEWAY_URL=""
TOKEN=""
NODE_ID="$(hostname)"
INSTALL_DIR="/opt/hermes-node-client"
USER_MODE=false

# Parse args
while [[ $# -gt 0 ]]; do
    case $1 in
        --gateway)
            GATEWAY_URL="$2"
            shift 2
            ;;
        --token)
            TOKEN="$2"
            shift 2
            ;;
        --node-id)
            NODE_ID="$2"
            shift 2
            ;;
        --install-dir)
            INSTALL_DIR="$2"
            shift 2
            ;;
        --user)
            USER_MODE=true
            shift
            ;;
        --help)
            echo "Usage: $0 --gateway ws://host:port/ws --token secret [options]"
            echo ""
            echo "Options:"
            echo "  --gateway URL     WebSocket URL of Hermes Gateway"
            echo "  --token TOKEN     Authentication token"
            echo "  --node-id NAME    Node identifier (default: hostname)"
            echo "  --install-dir DIR Installation directory (default: /opt/hermes-node-client)"
            echo "  --user            Install for current user only (systemd --user)"
            echo "  --help            Show this help"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

if [[ -z "$GATEWAY_URL" || -z "$TOKEN" ]]; then
    echo "Error: --gateway and --token are required"
    exit 1
fi

echo "=== Hermes Node Client Installer ==="
echo ""

# Check prerequisites
echo "Checking prerequisites..."
if ! command -v python3 &> /dev/null; then
    echo "Error: python3 not found"
    exit 1
fi
if ! command -v pip3 &> /dev/null; then
    echo "Error: pip3 not found"
    exit 1
fi
if ! command -v git &> /dev/null; then
    echo "Error: git not found"
    exit 1
fi
echo "  OK"

# Create directory
echo "Creating installation directory..."
if [[ "$USER_MODE" == true ]]; then
    INSTALL_DIR="$HOME/.local/share/hermes-node-client"
    mkdir -p "$INSTALL_DIR"
else
    sudo mkdir -p "$INSTALL_DIR"
    sudo chown "$(id -u):$(id -g)" "$INSTALL_DIR"
fi

REPO_DIR="$INSTALL_DIR/hermes-node-client"

# Clone or update
echo "Downloading hermes-node-client..."
if [[ -d "$REPO_DIR/.git" ]]; then
    echo "  Updating existing installation..."
    cd "$REPO_DIR"
    git pull
else
    [[ -d "$REPO_DIR" ]] && rm -rf "$REPO_DIR"
    git clone https://github.com/goodbaikin/hermes-node-client.git "$REPO_DIR"
fi

# Install dependencies
echo "Installing Python dependencies..."
pip3 install --user -r "$REPO_DIR/requirements.txt"

# Create .env
echo "Creating configuration..."
cat > "$REPO_DIR/.env" << EOF
HERMES_NODE_ID=$NODE_ID
HERMES_GATEWAY_URL=$GATEWAY_URL
HERMES_NODE_TOKEN=$TOKEN
EOF

# Install systemd service or user service
echo "Installing service..."
if [[ "$USER_MODE" == true ]]; then
    # User systemd service
    SYSTEMD_DIR="$HOME/.config/systemd/user"
    mkdir -p "$SYSTEMD_DIR"
    
    cat > "$SYSTEMD_DIR/hermes-node-client.service" << EOF
[Unit]
Description=Hermes Node Client
After=network.target

[Service]
Type=simple
WorkingDirectory=$REPO_DIR
ExecStart=$(command -v python3) $REPO_DIR/hermes_node_client.py
Restart=always
RestartSec=5
Environment=HERMES_NODE_ID=$NODE_ID
Environment=HERMES_GATEWAY_URL=$GATEWAY_URL
Environment=HERMES_NODE_TOKEN=$TOKEN

[Install]
WantedBy=default.target
EOF

    systemctl --user daemon-reload
    systemctl --user enable hermes-node-client.service
    systemctl --user start hermes-node-client.service
    
    echo "  User service installed"
    echo "  Status: systemctl --user status hermes-node-client"
else
    # System systemd service
    SYSTEMD_DIR="/etc/systemd/system"
    
    sudo tee "$SYSTEMD_DIR/hermes-node-client.service" > /dev/null << EOF
[Unit]
Description=Hermes Node Client
After=network.target

[Service]
Type=simple
WorkingDirectory=$REPO_DIR
ExecStart=$(command -v python3) $REPO_DIR/hermes_node_client.py
Restart=always
RestartSec=5
Environment=HERMES_NODE_ID=$NODE_ID
Environment=HERMES_GATEWAY_URL=$GATEWAY_URL
Environment=HERMES_NODE_TOKEN=$TOKEN

[Install]
WantedBy=multi-user.target
EOF

    sudo systemctl daemon-reload
    sudo systemctl enable hermes-node-client.service
    sudo systemctl start hermes-node-client.service
    
    echo "  System service installed"
    echo "  Status: sudo systemctl status hermes-node-client"
fi

# Install LSP servers (optional)
echo ""
echo "Installing LSP servers..."

# Python
if command -v pip3 &> /dev/null; then
    echo "  Python (pyright)..."
    pip3 install --user pyright
fi

# Node.js-based LSPs (if npm available)
if command -v npm &> /dev/null; then
    echo "  TypeScript (typescript-language-server)..."
    npm install -g typescript-language-server typescript
fi

echo ""
echo "=== Installation Complete ==="
echo "Node ID: $NODE_ID"
echo "Gateway: $GATEWAY_URL"
echo "Install: $REPO_DIR"
echo ""
echo "Logs:"
echo "  Linux: journalctl -u hermes-node-client -f"
echo "  macOS: log stream --predicate 'process == \"python3\"'"
