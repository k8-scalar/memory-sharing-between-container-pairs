#!/bin/bash

set -e

echo "Updating package list..."
sudo apt update -y

echo "Installing vim and git..."
sudo apt update && sudo apt install -y \
  vim git make jq \
  iputils-ping \
  dnsutils \
  curl \
  net-tools \
  iproute2 \
  python3-openstackclient \
  openjdk-17-jdk \
  maven

echo "Installing Node.js and npm..."
curl -fsSL https://deb.nodesource.com/setup_lts.x | sudo -E bash -
sudo apt install -y nodejs

echo "Installing InfluxDB CLI 2.x..."
ARCH=$(dpkg --print-architecture)
INFLUX_VERSION="2.7.5"
INFLUX_TMP=$(mktemp -d)
curl -fLO "https://dl.influxdata.com/influxdb/releases/influxdb2-client-${INFLUX_VERSION}-linux-${ARCH}.tar.gz"
tar xzf "influxdb2-client-${INFLUX_VERSION}-linux-${ARCH}.tar.gz" -C "$INFLUX_TMP"
sudo mv "$INFLUX_TMP/influx" /usr/local/bin/
rm -rf "$INFLUX_TMP" "influxdb2-client-${INFLUX_VERSION}-linux-${ARCH}.tar.gz"

echo "Installation complete!"
vim --version | head -n 1
git --version
make --version
jq --version
node --version
npm --version
influx version
java -version
mvn -version