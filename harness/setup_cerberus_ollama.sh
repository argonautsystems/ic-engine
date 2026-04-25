#!/bin/bash
# Setup Ollama on CERBERUS for Phase 3b testing
# Run on CERBERUS (192.168.207.96) with sudo access

set -e

echo "========================================"
echo "CERBERUS Ollama Setup Script"
echo "========================================"
echo ""

# Check if Ollama is already running
if pgrep -x "ollama" > /dev/null; then
    echo "✓ Ollama is already running"
    echo ""
    echo "Checking model availability..."
    curl -s http://localhost:11434/api/tags | jq '.models[] | .name' || echo "models:"
    echo ""
else
    echo "Installing Ollama..."

    # Check if Ollama is installed
    if ! command -v ollama &> /dev/null; then
        echo "Downloading Ollama..."
        curl -fsSL https://ollama.ai/install.sh | sh || {
            echo "Fallback: Installing from package manager"
            apt-get update
            apt-get install -y ollama
        }
    fi

    echo "Starting Ollama service..."
    systemctl start ollama || ollama serve &
    sleep 3

    # Verify it's running
    if curl -s http://localhost:11434/api/tags > /dev/null 2>&1; then
        echo "✓ Ollama started successfully"
    else
        echo "✗ Ollama failed to start"
        exit 1
    fi
fi

echo ""
echo "Pulling gemma4-consult model (if not present)..."
ollama pull gemma4-consult || echo "Model already available"

echo ""
echo "========================================"
echo "Setup Complete"
echo "========================================"
echo ""
echo "Ollama is running on: http://192.168.207.96:11434"
echo "Available models:"
curl -s http://localhost:11434/api/tags | jq '.models[] | .name'
echo ""
