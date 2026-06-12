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

# NO borrar los *.dist-info: OpenTelemetry (dependencia de strands-agents) resuelve
# su runtime context en import-time vía importlib.metadata.entry_points(
# group="opentelemetry_context"), que lee entry_points.txt desde el .dist-info.
# Sin esa metadata, entry_points() devuelve vacío -> next() lanza StopIteration
# sin atrapar -> el import de `strands` (y por tanto la Lambda) revienta.
# Solo se podan los archivos pesados/innecesarios de cada dist-info, conservando
# entry_points.txt y METADATA (lo que OTel y otros plugins necesitan).
find "${PYTHON_TARGET}" -type d -name "*.dist-info" -prune -exec sh -c '
  for d; do
    rm -rf "${d}/RECORD" "${d}/REQUESTED" "${d}/direct_url.json" \
           "${d}/INSTALLER" "${d}/licenses" "${d}/LICENSE"* 2>/dev/null || true
  done
' sh {} +

echo "==> Zipping layer -> ${ARTIFACT}"
( cd "${LAYER_DIR}" && zip -qr "${ARTIFACT}" python )

echo "==> Done: ${ARTIFACT}"
