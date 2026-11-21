#!/bin/bash
# Build the line-coverage map (maps/linemap_index.json + maps/cov/) inside the
# builder image. Run from the repo root:   bash tools/build_linemap.sh
set -e
docker build -q --platform linux/amd64 -f tools/Dockerfile.build -t blast-build .
MSYS_NO_PATHCONV=1 docker run --rm --platform linux/amd64 \
  -v "$(pwd -W 2>/dev/null || pwd):/work" blast-build \
  python /work/tools/build_linemap.py --tests sample/tests.json --out /work/maps
