#!/usr/bin/env bash
#
# Build the shared Lambda Layer for BTG ConnectAI (Python 3.13).
#
# Produces layer/python/ containing:
#   - the src/shared/ package
#   - the runtime dependencies from src/requirements.txt
# and zips it into layer/shared-layer.zip ready for upload to the artifacts
# bucket created by the `infra` repo.
#
# Run on Linux x86_64 (CI or a container) so binary wheels match the Lambda
# runtime.
set -euo pipefail

PYTHON_VERSION="3.13"
LAYER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${LAYER_DIR}/.." && pwd)"
PYTHON_TARGET="${LAYER_DIR}/python"
ARTIFACT="${LAYER_DIR}/shared-layer.zip"

echo "==> Cleaning previous build"
rm -rf "${PYTHON_TARGET}" "${ARTIFACT}"
mkdir -p "${PYTHON_TARGET}"

echo "==> Installing runtime dependencies (python ${PYTHON_VERSION})"
pip install \
  --requirement "${REPO_ROOT}/src/requirements.txt" \
  --target "${PYTHON_TARGET}" \
  --python-version "${PYTHON_VERSION}" \
  --only-binary=:all: \
  --platform manylinux2014_x86_64 \
  --implementation cp \
  --upgrade

echo "==> Copying shared package"
cp -r "${REPO_ROOT}/src/shared" "${PYTHON_TARGET}/shared"

echo "==> Pruning bytecode / caches"
find "${PYTHON_TARGET}" -type d -name "__pycache__" -prune -exec rm -rf {} +
find "${PYTHON_TARGET}" -type d -name "*.dist-info" -prune -exec rm -rf {} + || true

echo "==> Zipping layer -> ${ARTIFACT}"
( cd "${LAYER_DIR}" && zip -qr "${ARTIFACT}" python )

echo "==> Done: ${ARTIFACT}"
