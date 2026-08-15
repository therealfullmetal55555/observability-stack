#!/usr/bin/env bash
#
# Build dist/observability-stack-<version>.tar.gz
#
# The tarball is meant to be dropped onto a server and run. Nothing in it
# requires a network round-trip to a package registry except the Docker images,
# which are pinned by digest-able tag.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VERSION="$(grep -m1 '^version' pyproject.toml | cut -d'"' -f2)"
NAME="observability-stack-${VERSION}"
OUT="$ROOT/dist"
STAGE="$(mktemp -d)"
TARGET="$STAGE/$NAME"

cleanup() { rm -rf "$STAGE"; }
trap cleanup EXIT

echo "packaging $NAME"

mkdir -p "$TARGET"
mkdir -p "$OUT"

# Files that belong in a deployable artifact. Everything else — caches, virtual
# envs, the dist folder itself — stays out.
INCLUDE=(
  README.md
  SPEC.md
  CHANGELOG.md
  LICENSE
  Makefile
  pyproject.toml
  requirements.txt
  docker-compose.yml
  .env.example
  alertmanager
  blackbox
  docker
  grafana
  loki
  otel-collector
  prometheus
  promtail
  tempo
  scripts
  instrumentation
  tests
  docs
  helm
)

for entry in "${INCLUDE[@]}"; do
  if [ -e "$entry" ]; then
    cp -R "$entry" "$TARGET/"
  else
    echo "  skip (not present): $entry"
  fi
done

# Strip the things that have no business in a tarball.
find "$TARGET" -type d \( -name __pycache__ -o -name .pytest_cache -o -name .ruff_cache \
  -o -name node_modules -o -name dist -o -name .mypy_cache \) -prune -exec rm -rf {} +
find "$TARGET" -type f \( -name '*.pyc' -o -name '.env' -o -name '*.log' \) -delete

# Go module files are only useful with the toolchain present; keep them, they're tiny.
cat > "$TARGET/INSTALL.txt" <<'EOF'
observability-stack
===================

Quick start
-----------
    cp .env.example .env      # then edit it
    make up                   # everything comes up on localhost
    open http://localhost:3000

    # make the dashboards show something
    make seed

Where things are
----------------
    docker-compose.yml           the whole stack
    otel-collector/config.yaml   pipeline: sampling, cost, citations
    grafana/dashboards/*.json    five dashboards, provisioned on boot
    prometheus/alerts.yml        fifteen alert rules — the single source of truth
    instrumentation/python/      pip-installable package for your agents
    instrumentation/n8n/         n8n community nodes
    docs/                        one integration guide per project

Verify before you trust it
--------------------------
    make validate     config sanity: dashboards, alerts, pipeline wires
    make test         unit + config tests, no network needed
    make up && pytest tests -q    integration tests against the live stack

Requirements: docker compose v2, python 3.10+ for the tooling and tests.
EOF

tar -C "$STAGE" -czf "$OUT/$NAME.tar.gz" "$NAME"

# Also make it easy to diff what changed between two builds.
( cd "$TARGET" && find . -type f | LC_ALL=C sort | sed 's|^\./||' ) > "$OUT/$NAME.filelist.txt"

SIZE="$(du -h "$OUT/$NAME.tar.gz" | cut -f1)"
FILES="$(wc -l < "$OUT/$NAME.filelist.txt" | tr -d ' ')"
echo "wrote $OUT/$NAME.tar.gz  ($SIZE, $FILES files)"
