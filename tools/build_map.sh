#!/bin/bash
# Build maps/deps.json inside the scorer's exact Python image (same digest as
# the Dockerfile), with network access so it can install the pinned
# requirements and download sqlglot. Run from the repo root:
#     bash tools/build_map.sh
set -e
IMAGE="python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90"
MSYS_NO_PATHCONV=1 docker run --rm --platform linux/amd64 \
  -v "$(pwd -W 2>/dev/null || pwd):/work" -w /work "$IMAGE" \
  bash -c "pip install -q --root-user-action=ignore -r requirements.lock && python tools/build_map.py --tests sample/tests.json --out maps/deps.json"
