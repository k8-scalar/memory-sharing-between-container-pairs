#!/bin/bash

# Kubernetes Master Node Installation Script for Ubuntu 22.04
# Run with: sudo bash install-k8s-master.sh

set -e  # Exit on any error

echo "================================================"
echo "Kubernetes Master Node Installation"
echo "================================================"

# Update system
echo "[1/10] Updating system packages..."
apt-get update && apt-get upgrade -y

# Disable swap (required for Kubernetes)
echo "[2/10] Disabling swap..."
if [ "$(swapon --show)" ]; then
    swapoff -a
    sed -i '/ swap / s/^/#/' /etc/fstab
    echo "Swap disabled"
else
    echo "Swap already disabled, skipping..."
fi

# Load required kernel modules
echo "[3/10] Loading kernel modules..."
cat <<EOF | tee /etc/modules-load.d/k8s.conf
overlay
br_netfilter
EOF

modprobe overlay
modprobe br_netfilter

# Set up required sysctl params
echo "[4/10] Configuring sysctl parameters..."
cat <<EOF | tee /etc/sysctl.d/k8s.conf
net.bridge.bridge-nf-call-iptables  = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.ip_forward                 = 1
EOF

sysctl --system

# Install containerd
echo "[5/10] Installing containerd..."
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
echo "[6/10] Installing Kubernetes packages..."
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

# Initialize Kubernetes master
echo "[7/10] Initializing Kubernetes master node..."
if [ -f /etc/kubernetes/admin.conf ]; then
    echo "Kubernetes cluster already initialized, skipping..."
else
    POD_CIDR="10.244.0.0/16"  # Default for Flannel, change if needed
    kubeadm init --pod-network-cidr=$POD_CIDR --apiserver-advertise-address=$(hostname -I | awk '{print $1}')
    echo "Kubernetes master initialized successfully"
fi

# Set up kubectl for ubuntu user
echo "[8/10] Configuring kubectl for ubuntu user..."
mkdir -p /home/ubuntu/.kube
if [ ! -f /home/ubuntu/.kube/config ]; then
    cp -i /etc/kubernetes/admin.conf /home/ubuntu/.kube/config
    chown -R ubuntu:ubuntu /home/ubuntu/.kube
    echo "kubectl configured for ubuntu user"
else
    echo "kubectl already configured for ubuntu user, skipping..."
fi

# Also set up for root
mkdir -p /root/.kube
if [ ! -f /root/.kube/config ]; then
    cp -i /etc/kubernetes/admin.conf /root/.kube/config
    echo "kubectl configured for root user"
fi

# Install CNI plugin (Flannel)
echo "[9/10] Installing Flannel CNI..."
if sudo -u ubuntu kubectl get ns kube-flannel &> /dev/null || sudo -u ubuntu kubectl get daemonset -n kube-system kube-flannel-ds &> /dev/null; then
    echo "Flannel already installed, skipping..."
else
    sudo -u ubuntu kubectl apply -f https://github.com/flannel-io/flannel/releases/latest/download/kube-flannel.yml
    echo "Flannel CNI installed successfully"
fi

# Install Helm
echo "[10/10] Installing Helm..."
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
echo "Your Kubernetes master node is ready!"
echo ""
echo "To check cluster status, run:"
echo "  kubectl get nodes"
echo "  kubectl get pods -A"
echo ""
echo "To check Helm version, run:"
echo "  helm version"
echo ""
echo "To join worker nodes, use the command shown above (starting with 'kubeadm join')"
echo "Or retrieve it later with:"
echo "  kubeadm token create --print-join-command"
echo ""
echo "IMPORTANT: Save the 'kubeadm join' command that was displayed above!"
echo "================================================"