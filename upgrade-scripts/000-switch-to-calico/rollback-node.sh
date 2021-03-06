#!/bin/bash
set -e

echo "Rolling back calico upgrade on a node"

source $SNAP/actions/common/utils.sh
CA_CERT=/snap/core/current/etc/ssl/certs/ca-certificates.crt

BACKUP_DIR="$SNAP_DATA/var/tmp/upgrades/000-switch-to-calico"

if [ -e "$BACKUP_DIR/args/cni-network/flannel.conflist" ]; then
  rm -rf "$SNAP_DATA"/args/cni-network/*
  cp "$BACKUP_DIR"/args/cni-network/* "$SNAP_DATA/args/cni-network/"
fi

echo "Restarting kubelet"
if [ -e "$BACKUP_DIR/args/kubelet" ]; then
  cp "$BACKUP_DIR"/args/kubelet "$SNAP_DATA/args/"
  systemctl restart snap.${SNAP_NAME}.daemon-kubelet
fi

echo "Restarting kube-proxy"
if [ -e "$BACKUP_DIR/args/kube-proxy" ]; then
  cp "$BACKUP_DIR"/args/kube-proxy "$SNAP_DATA/args/"
  systemctl restart snap.${SNAP_NAME}.daemon-proxy
fi

echo "Restarting kube-apiserver"
if [ -e "$BACKUP_DIR/args/kube-apiserver" ]; then
  cp "$BACKUP_DIR"/args/kube-apiserver "$SNAP_DATA/args/"
  systemctl restart snap.${SNAP_NAME}.daemon-apiserver
fi

${SNAP}/microk8s-status.wrapper --wait-ready --timeout 30

echo "Restarting flannel"
set_service_expected_to_start flanneld
remove_vxlan_interfaces
run_with_sudo systemctl start snap.${SNAP_NAME}.daemon-flanneld

echo "Restarting kubelet"
if grep -qE "bin_dir.*SNAP_DATA}\/" $SNAP_DATA/args/containerd-template.toml; then
  echo "Restarting containerd"
  run_with_sudo "${SNAP}/bin/sed" -i 's;bin_dir = "${SNAP_DATA}/opt;bin_dir = "${SNAP}/opt;g' "$SNAP_DATA/args/containerd-template.toml"
  run_with_sudo systemctl restart snap.${SNAP_NAME}.daemon-containerd
fi

echo "Calico rolledback"
