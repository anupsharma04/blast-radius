#!/bin/bash
# Run the safety harness (tools/validate.py) inside the builder image.
# Run from the repo root:   bash tools/validate.sh [extra args for validate.py]
set -e
docker build -q --platform linux/amd64 -f tools/Dockerfile.build -t blast-build .
MSYS_NO_PATHCONV=1 docker run --rm --platform linux/amd64 \
  -v "$(pwd -W 2>/dev/null || pwd):/work" blast-build \
  python /work/tools/validate.py "$@"
