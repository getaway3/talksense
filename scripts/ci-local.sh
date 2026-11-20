#!/usr/bin/env bash
# Локальный аналог .github/workflows/ci.yml (без сборки/публикации Docker-образов).
#   pip install -r services/requirements-dev.txt && bash scripts/ci-local.sh
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== api-contracts"
npx --yes @redocly/cli@2 lint api/*-service.yaml
npx --yes @asyncapi/cli@6 validate api/asyncapi.yaml

for svc in venue-service device-service recording-service transcription-service analysis-service report-service; do
  echo "== $svc: lint, build, unit tests"
  ruff check services/common "services/$svc"
  python -m compileall -q services/common "services/$svc"
  (cd "services/$svc" && pytest -q -p no:cacheprovider)
done

echo "== integration"
pytest -q -p no:cacheprovider services/common/tests tests

echo "OK: все проверки пайплайна пройдены"
