#!/usr/bin/env bash

SCRIPT_DIR="$(dirname "$0")"

set -exuo pipefail
source "$SCRIPT_DIR/common"

OUTFILE="$(mktemp -d)/image.tar"

docker buildx build -o type=oci,dest=${OUTFILE} \
  -t $CI_IMAGE_URI \
  --platform=${CI_IMAGE_PLATFORMS} \
  --build-arg "IMAGE_REPOSITORY=${IMAGE}" \
  --build-arg "IMAGE_TAG=${COMMIT}" "$SCRIPT_DIR/.."

docker load -i "${OUTFILE}" && rm -f ${OUTFILE}

if [ -n "${DNAME:-}" ] && [ -n "${DPASS:-}" ]; then
  docker login -u "${DNAME}" -p "${DPASS}";
  TagAndPushImage $CI_IMAGE_REPO $CI_TAG;
fi
