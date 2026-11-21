#!/bin/bash
set -e
docker build --platform linux/amd64 --tag blast-radius-entry .
MSYS_NO_PATHCONV=1 docker run --rm --platform linux/amd64 --network none \
  --cpus 2 --memory 4096m --memory-swap 4096m --pids-limit 256 --read-only \
  --tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL \
  --security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp \
  -v "$(pwd -W)/sample/commits:/data/commits:ro" \
  -v "$(pwd -W)/sample/tests.json:/data/tests.json:ro" \
  blast-radius-entry > output.jsonl
python score.py sample sample/answers.json output.jsonl
