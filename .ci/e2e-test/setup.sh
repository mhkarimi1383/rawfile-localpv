#!/usr/bin/env bash

SCRIPT_DIR="$(dirname "$0")"

set -ex
source $SCRIPT_DIR/../common

if command -v kind &>/dev/null; then
  kind load docker-image $CI_IMAGE_URI || :
fi

helm upgrade --wait \
  -n openebs --create-namespace -i rawfile-csi \
  --set metrics.serviceMonitor.enabled=false \
  --set controller.image.repository=$CI_IMAGE_REPO --set controller.image.tag=$CI_TAG \
  --set node.image.repository=$CI_IMAGE_REPO --set node.image.tag=$CI_TAG \
  $SCRIPT_DIR/../../deploy/charts/rawfile-csi/
