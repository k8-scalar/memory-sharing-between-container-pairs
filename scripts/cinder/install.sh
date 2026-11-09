#!/bin/bash

# OpenStack Cinder CSI Driver Installation Script
# Run with: bash install.sh

set -e  # Exit on any error

echo "================================================"
echo "OpenStack Cinder CSI Driver Installation"
echo "================================================"

# Check prerequisites
echo "[1/6] Checking prerequisites..."
if ! command -v kubectl &> /dev/null; then
    echo "Error: kubectl not found"
    exit 1
fi

if ! command -v helm &> /dev/null; then
    echo "Error: helm not found"
    exit 1
fi

if ! kubectl cluster-info &> /dev/null; then
    echo "Error: Cannot connect to Kubernetes cluster"
    exit 1
fi
echo "Prerequisites check passed"

# Check cloud.conf.local
echo "[2/6] Checking cloud configuration..."
if [ ! -f "cloud.conf.local" ]; then
    echo "Error: cloud.conf.local not found"
    echo ""
    echo "Create it with your OpenStack Application Credentials:"
    echo "  cp cloud.conf.template cloud.conf.local"
    echo "  # Edit cloud.conf.local with your credentials"
    echo ""
    echo "See README.md for how to get Application Credentials."
    exit 1
fi
echo "cloud.conf.local found"

# Check if already installed
echo "[3/6] Checking existing installation..."
if kubectl get pod -n kube-system 2>/dev/null | grep -q "openstack-cinder-csi-controllerplugin"; then
    CONTROLLER_STATUS=$(kubectl get pod -n kube-system 2>/dev/null | grep "openstack-cinder-csi-controllerplugin" | awk '{print $3}')
    echo "Cinder CSI already installed (Status: $CONTROLLER_STATUS)"

    if [ "$CONTROLLER_STATUS" == "Running" ]; then
        echo "Already running. Run uninstall.sh first to reinstall."
        exit 0
    fi
else
    echo "Not installed, proceeding..."
fi

# Create secret
echo "[4/6] Creating cloud-config secret..."
if kubectl get secret -n kube-system cloud-config &> /dev/null; then
    echo "Secret already exists, skipping..."
else
    kubectl create secret -n kube-system generic cloud-config --from-file=cloud.conf=cloud.conf.local
    echo "Secret created"
fi

# Add Helm repo
echo "[5/6] Adding Helm repository..."
if helm repo list 2>/dev/null | grep -q "^cpo"; then
    helm repo update
else
    helm repo add cpo https://kubernetes.github.io/cloud-provider-openstack
    helm repo update
fi
echo "Helm repo ready"

# Install via Helm
echo "[6/6] Installing Cinder CSI..."
if helm list -n kube-system 2>/dev/null | grep -q "cinder-csi"; then
    echo "Helm release already exists, skipping..."
else
    helm install cinder-csi cpo/openstack-cinder-csi \
      --namespace kube-system \
      --set secret.enabled=true \
      --set secret.name=cloud-config \
      --set csi.plugin.nodePlugin.tolerations[0].key=node-role.kubernetes.io/master \
      --set csi.plugin.nodePlugin.tolerations[0].effect=NoSchedule \
      --set csi.plugin.nodePlugin.tolerations[1].key=node-role.kubernetes.io/control-plane \
      --set csi.plugin.nodePlugin.tolerations[1].effect=NoSchedule
    echo "Cinder CSI installed"
fi

# Apply StorageClass
if [ -f "storageclass.yaml" ]; then
    kubectl apply -f storageclass.yaml
    echo "StorageClass applied"
fi

echo ""
echo "================================================"
echo "Installation Complete!"
echo "================================================"
echo ""
echo "Verify: kubectl get pod -n kube-system | grep cinder"
echo "Test:   kubectl apply -f test-example.yaml"
echo "================================================"
