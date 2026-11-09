#!/bin/bash

# Script to install Docker and Docker Compose
# For Ubuntu/Debian-based systems

set -e  # Exit on error

echo "=========================================="
echo "Docker and Docker Compose Installation"
echo "=========================================="
echo ""

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    echo "This script requires sudo privileges."
    SUDO="sudo"
else
    SUDO=""
fi

# Update package index
echo "Updating package index..."
$SUDO apt-get update

# Install prerequisites
echo "Installing prerequisites..."
$SUDO apt-get install -y \
    ca-certificates \
    curl \
    gnupg \
    lsb-release

# Add Docker's official GPG key
echo "Adding Docker's GPG key..."
$SUDO install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | $SUDO gpg --dearmor -o /etc/apt/keyrings/docker.gpg
$SUDO chmod a+r /etc/apt/keyrings/docker.gpg

# Set up the Docker repository
echo "Setting up Docker repository..."
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(lsb_release -cs) stable" | $SUDO tee /etc/apt/sources.list.d/docker.list > /dev/null

# Update package index again
echo "Updating package index with Docker repository..."
$SUDO apt-get update

# Install Docker Engine, CLI, containerd, and Docker Compose plugin
echo "Installing Docker and Docker Compose..."
$SUDO apt-get install -y \
    docker-ce \
    docker-ce-cli \
    containerd.io \
    docker-buildx-plugin \
    docker-compose-plugin

# Add current user to docker group (if not root)
if [ "$EUID" -ne 0 ]; then
    echo "Adding current user to docker group..."
    $SUDO usermod -aG docker $USER
    echo ""
    echo "⚠️  IMPORTANT: You need to log out and log back in for group changes to take effect!"
    echo "   Or run: newgrp docker"
fi

# Verify installation
echo ""
echo "=========================================="
echo "Verifying installation..."
echo "=========================================="

# Check Docker version
if command -v docker &> /dev/null; then
    echo "✓ Docker installed successfully"
    docker --version
else
    echo "✗ Docker installation failed"
    exit 1
fi

# Check Docker Compose version
if docker compose version &> /dev/null; then
    echo "✓ Docker Compose installed successfully"
    docker compose version
else
    echo "✗ Docker Compose installation failed"
    exit 1
fi

# Test Docker (if user has permissions)
echo ""
echo "Testing Docker..."
if docker run --rm hello-world &> /dev/null; then
    echo "✓ Docker is working correctly"
else
    echo "⚠️  Docker test failed. You may need to:"
    echo "   1. Log out and log back in"
    echo "   2. Run: newgrp docker"
    echo "   3. Or restart your system"
fi

echo ""
echo "=========================================="
echo "Installation complete!"
echo "=========================================="
echo ""
echo "Quick start commands:"
echo "  docker --version              # Check Docker version"
echo "  docker compose version        # Check Compose version"
echo "  docker run hello-world        # Test Docker"
echo "  docker ps                     # List running containers"
echo "  docker compose up -d          # Start services in background"
echo ""
