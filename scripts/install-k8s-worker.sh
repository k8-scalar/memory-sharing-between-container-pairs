#!/bin/bash

# Kubernetes Worker Node Installation Script for Ubuntu 22.04
# Run with: sudo bash install-k8s-worker.sh
# This installs K8s components but does NOT join the cluster yet

set -e  # Exit on any error

echo "================================================"
echo "Kubernetes Worker Node Installation"
echo "================================================"

# Update system
echo "[1/8] Updating system packages..."
apt-get update && apt-get upgrade -y

# Disable swap (required for Kubernetes)
echo "[2/8] Disabling swap..."
if [ "$(swapon --show)" ]; then
    swapoff -a
    sed -i '/ swap / s/^/#/' /etc/fstab
    echo "Swap disabled"
else
    echo "Swap already disabled, skipping..."
fi

# Load required kernel modules
echo "[3/8] Loading kernel modules..."
cat <<EOF | tee /etc/modules-load.d/k8s.conf
overlay
br_netfilter
EOF

modprobe overlay
modprobe br_netfilter

# Set up required sysctl params
echo "[4/8] Configuring sysctl parameters..."
cat <<EOF | tee /etc/sysctl.d/k8s.conf
net.bridge.bridge-nf-call-iptables  = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.ip_forward                 = 1
EOF

sysctl --system

# Install containerd
echo "[5/8] Installing containerd..."
if systemctl is-active --quiet containerd; then
    echo "Containerd already installed and running, skipping..."
else
    apt-get install -y ca-certificates curl gnupg lsb-release

    # Add Docker's official GPG key (containerd is part of Docker packages)
    install -m 0755 -d /etc/apt/keyrings
    if [ ! -f /etc/apt/keyrings/docker.gpg ]; then
        curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
        chmod a+r /etc/apt/keyrings/docker.gpg
    fi

    # Set up Docker repository
    echo \
      "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
      $(lsb_release -cs) stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null

    apt-get update
    apt-get install -y containerd.io

    # Configure containerd
    mkdir -p /etc/containerd
    containerd config default | tee /etc/containerd/config.toml
    sed -i 's/SystemdCgroup = false/SystemdCgroup = true/' /etc/containerd/config.toml

    systemctl restart containerd
    systemctl enable containerd
    echo "Containerd installed successfully"
fi

# Install Kubernetes components
echo "[6/8] Installing Kubernetes packages..."
if command -v kubeadm &> /dev/null; then
    echo "Kubernetes components already installed, skipping..."
else
    apt-get install -y apt-transport-https ca-certificates curl gpg

    # Add Kubernetes GPG key and repository
    if [ ! -f /etc/apt/keyrings/kubernetes-apt-keyring.gpg ]; then
        curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.34/deb/Release.key | gpg --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg
    fi

    echo 'deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v1.34/deb/ /' | tee /etc/apt/sources.list.d/kubernetes.list

    apt-get update
    apt-get install -y kubelet kubeadm kubectl
    apt-mark hold kubelet kubeadm kubectl
    echo "Kubernetes components installed successfully"
fi

# Enable kubelet service (it will fail to start until joined to cluster - this is normal)
echo "[7/8] Enabling kubelet service..."
systemctl enable kubelet

# Install Helm
echo "[8/8] Installing Helm..."
if command -v helm &> /dev/null; then
    echo "Helm already installed ($(helm version --short)), skipping..."
else
    curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
    echo "Helm installed successfully ($(helm version --short))"
fi

echo "================================================"
echo "Installation Complete!"
echo "================================================"
echo ""
echo "Your worker node is ready to join the cluster!"
echo ""
echo "To check Helm version, run:"
echo "  helm version"
echo ""
echo "To join this worker to the master node:"
echo "1. Get the join command from your master node:"
echo "   sudo kubeadm token create --print-join-command"
echo ""
echo "2. Run the join command on this worker node with sudo:"
echo "   sudo kubeadm join <master-ip>:6443 --token <token> --discovery-token-ca-cert-hash sha256:<hash>"
echo ""
echo "Note: kubelet service is enabled but won't start successfully until"
echo "      this node joins the cluster. This is expected behavior."
echo "================================================"