#!/usr/bin/env bash
# Build and publish the native linux/arm64 image, then refresh the multi-arch
# manifests, from an Apple Silicon (arm64) host.
#
# Why this exists:
#   The GitLab CI builds arm64 under QEMU on an amd64 runner (.gitlab-ci.yml,
#   build:arm64, 90m timeout). That path is slow/flaky, so :<ver>-cpu has been
#   shipping amd64-only. On Apple Silicon, the amd64 image runs emulated and
#   lacks AVX2, so Polars segfaults at price time (fetch_holdings.py exit_code
#   -11; "Missing required CPU features: avx, avx2, fma"). A NATIVE arm64 image
#   has no AVX2 requirement and runs cleanly.
#
#   Build arm64 natively here (no QEMU) so the Dockerfile's build-time
#   `import polars` check passes and the arm64 layer actually lands. Do NOT
#   build amd64 on an arm64 Mac: that import check would run under emulation
#   and hit the same AVX2 crash mid-build. Reuse CI's amd64 image instead.
#
# Prereqs:
#   - arm64 (Apple Silicon) host with `docker buildx`
#   - docker login ghcr.io   (user/token with write:packages)
#   - CI already pushed the amd64 image for this version:
#       ghcr.io/argonautsystems/ic-engine:<ver>-cpu-amd64
#
# All dependencies (incl. clio) are vendored in-tree — no GITLAB_TOKEN needed.
#
# Usage:
#   tools/publish-arm64.sh 4.6.1
set -euo pipefail

VERSION="${1:?usage: publish-arm64.sh <version>   e.g. 4.6.0}"
REGISTRY="${REGISTRY:-ghcr.io}"
IMAGE_NAME="${IMAGE_NAME:-argonautsystems/ic-engine}"
IMG="${REGISTRY}/${IMAGE_NAME}"

arch="$(uname -m)"
if [ "${arch}" != "arm64" ] && [ "${arch}" != "aarch64" ]; then
  echo "ERROR: run this on an arm64 host so the build is native (no QEMU). Got: ${arch}" >&2
  exit 1
fi

echo ">> Building + pushing native arm64: ${IMG}:${VERSION}-cpu-arm64"
docker buildx build \
  --platform linux/arm64 \
  --tag "${IMG}:${VERSION}-cpu-arm64" \
  --provenance=false \
  --push \
  .

# Fuse multi-arch manifests, combining CI's amd64 image with the arm64 image
# just pushed. Uses `buildx imagetools create` rather than `docker manifest
# create`: the latter refuses children that are themselves manifest lists
# (e.g. a per-arch tag that was re-tagged via imagetools), while imagetools
# flattens nested lists correctly.
for TAG in "${VERSION}-cpu" "latest"; do
  echo ">> Fusing multi-arch manifest: ${IMG}:${TAG}"
  docker buildx imagetools create --tag "${IMG}:${TAG}" \
    "${IMG}:${VERSION}-cpu-amd64" \
    "${IMG}:${VERSION}-cpu-arm64"
done

echo ">> Done. Verify both arches are present:"
echo "   docker manifest inspect ${IMG}:${VERSION}-cpu | grep architecture"
