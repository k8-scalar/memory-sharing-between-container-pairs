cd /var/lib/kubelet/

sudo grep -q "housekeepingInterval" config.yaml && \
sudo sed -i 's/housekeepingInterval:.*/housekeepingInterval: 3s/' config.yaml || \
sudo sed -i '/apiVersion:/a housekeepingInterval: 3s' config.yaml
grep "housekeepingInterval" config.yaml

sudo systemctl restart kubelet