# OpenStack Cinder CSI Driver

Scripts for installing OpenStack Cinder CSI driver to enable Kubernetes persistent volumes backed by OpenStack Cinder block storage.

## Prerequisites

- Kubernetes cluster with kubectl configured
- Helm 3 installed
- OpenStack Application Credentials

## Getting Application Credentials

1. Log in to OpenStack Horizon dashboard
2. Go to **Identity** > **Application Credentials**
3. Click **Create Application Credential**
4. Save the **Application Credential ID** and **Secret** (shown only once!)

## Configuration

1. Copy the template and edit with your credentials:
   ```bash
   cp cloud.conf.template cloud.conf.local
   # Edit cloud.conf.local with your OpenStack auth URL, credential ID, and secret
   ```

2. **Important**: `cloud.conf.local` contains sensitive data - never commit it!

## Installation

```bash
bash install.sh
```

Verify installation:
```bash
kubectl get pod -n kube-system | grep cinder
```

Expected output:
```
openstack-cinder-csi-controllerplugin-xxxxx   6/6     Running   0          XXs
```

## Test

```bash
kubectl apply -f test-example.yaml
kubectl get pvc
```

**Cleanup test resources before uninstalling:**
```bash
kubectl delete -f test-example.yaml
```

## Uninstallation

```bash
bash uninstall.sh
```

## Troubleshooting

- Check logs: `kubectl logs -n kube-system <pod-name>`
- Verify credentials in `cloud.conf.local`
- Check StorageClass: `kubectl get storageclass`
