# Monitoring Stack

Helm chart for Telegraf and InfluxDB monitoring stack.

## What This Installs

- **Telegraf DaemonSet**: Runs on every node, collecting host system metrics (CPU, memory, disk, network) and pod metrics via cAdvisor scraping
- **InfluxDB**: Time-series database for storing metrics and events
- **RBAC**: Required permissions for Telegraf to scrape cluster metrics

All resources are deployed in the namespace where you install the chart. It is recommended to install this chart in a dedicated `monitoring` namespace.

## Prerequisites

Before installation, ensure:

- **Node Labels**: InfluxDB pods will only be deployed to nodes with `dedicated=monitoring` label. You can use this command to label a node.
  ```bash
  kubectl label nodes <node-name> dedicated=monitoring
  ```

- **Namespace**: Create the `monitoring` namespace if it doesn't exist:
  ```bash
  kubectl create namespace monitoring
  ```

## File Layout

```
monitoring/
├── Chart.yaml                # Helm chart metadata
├── values.yaml               # Configurable parameters
├── dashboards/               # InfluxDB dashboard templates
└── templates/
    ├── telegraf.yaml         # Telegraf DaemonSet with cAdvisor inputs (node metrics)
    └── influxdb.yaml         # InfluxDB StatefulSet and ClusterIP service
```

## Installation

```bash
helm install monitoring ./ -n monitoring
helm upgrade monitoring ./ -n monitoring
```

## Configuration

Key configuration details from [values.yaml](values.yaml):

- **InfluxDB Credentials**: Default `admin/admin123` (change in production)
- **Persistent Storage**: InfluxDB (128Gi)
- **Service Exposure**: ClusterIP service on port 8086 (InfluxDB)

## Verification

**InfluxDB:**
```bash
kubectl port-forward service/influxdb 8086:8086 --namespace=monitoring
```
Visit: http://localhost:8086 (default credentials: admin/admin123)

**Telegraf:**
```bash
kubectl logs -n monitoring daemonset/telegraf-node
```

## Uninstall

```bash
helm uninstall monitoring -n monitoring
```

## Dashboards

Dashboard templates are in `dashboards/` directory. Import to InfluxDB:
```bash
influx apply -f dashboards/polypod-petclinic-dashboard.json --org monitoring
```

### Available Metrics

**Container Metrics** (via cAdvisor, bucket: `metrics-nodes`):
- `container_cpu_usage_seconds_total`
- `container_memory_working_set_bytes`

**Host System Metrics** (via Telegraf inputs, bucket: `metrics-nodes`):
- `cpu`, `mem`, `disk`, `diskio`, `net`, `system`, `processes`, `swap`, `kernel`
