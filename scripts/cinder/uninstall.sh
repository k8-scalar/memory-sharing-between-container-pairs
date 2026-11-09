#!/bin/bash

# OpenStack Cinder CSI Driver Uninstallation Script
# Run with: bash uninstall.sh

set -e  # Exit on any error

echo "================================================"
echo "OpenStack Cinder CSI Driver Uninstallation"
echo "================================================"

# Check prerequisites
echo "[1/4] Checking prerequisites..."
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

# Check if installed
echo "[2/4] Checking installation..."
if ! kubectl get pod -n kube-system 2>/dev/null | grep -q "openstack-cinder-csi-controllerplugin"; then
    echo "Cinder CSI not found. Nothing to uninstall."
    exit 0
fi
echo "Cinder CSI found"

# Check for existing PVCs
echo "[3/4] Checking for PVCs..."
if kubectl get pvc --all-namespaces 2>/dev/null | grep -q "cinder-csi"; then
    echo "Error: Found PVCs using cinder-csi:"
    kubectl get pvc --all-namespaces | grep "cinder-csi"
    echo ""
    echo "Please delete all PVCs using cinder-csi before uninstalling."
    echo "Uninstallation aborted."
    exit 1
fi
echo "No PVCs using cinder-csi"

# Uninstall
echo "[4/4] Uninstalling..."
if helm list -n kube-system 2>/dev/null | grep -q "cinder-csi"; then
    helm uninstall cinder-csi -n kube-system
    echo "Helm release uninstalled"
fi

if kubectl get secret -n kube-system cloud-config &> /dev/null; then
    kubectl delete secret -n kube-system cloud-config
    echo "Secret deleted"
fi

if kubectl get storageclass cinder-csi &> /dev/null; then
    kubectl delete storageclass cinder-csi
    echo "StorageClass deleted"
fi

echo ""
echo "================================================"
echo "Uninstallation Complete!"
echo "================================================"
echo ""
echo "Verify: kubectl get pod -n kube-system | grep cinder"
echo "================================================"
