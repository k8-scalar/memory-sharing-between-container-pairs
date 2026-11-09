sudo kubeadm reset -f
sudo ip link delete cni0
sudo ip link delete flannel.1
sudo rm -rf /etc/cni/net.d
sudo rm -rf /var/lib/cni/
sudo rm -rf /var/lib/kubelet/*
sudo systemctl restart kubelet
