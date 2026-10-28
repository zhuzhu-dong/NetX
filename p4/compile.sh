#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
P4C_IMAGE="${P4C_IMAGE:-p4lang/p4c:stable}"

mkdir -p "$SCRIPT_DIR/build"
if command -v p4c-bm2-ss >/dev/null 2>&1; then
    p4c-bm2-ss --p4v 16 \
        --p4runtime-files "$SCRIPT_DIR/build/idn_basic.p4info.txt" \
        -o "$SCRIPT_DIR/build/idn_basic.json" \
        "$SCRIPT_DIR/idn_basic.p4"
elif command -v docker >/dev/null 2>&1; then
    docker run --rm \
        -v "$PROJECT_ROOT:/workspace" \
        -w /workspace/p4 \
        "$P4C_IMAGE" \
        p4c-bm2-ss --p4v 16 \
        --p4runtime-files build/idn_basic.p4info.txt \
        -o build/idn_basic.json \
        idn_basic.p4
else
    echo "ERROR: neither p4c-bm2-ss nor Docker is available" >&2
    exit 1
fi

python3 "$SCRIPT_DIR/generate_java_constants.py" \
    --p4info "$SCRIPT_DIR/build/idn_basic.p4info.txt" \
    --output "$PROJECT_ROOT/onos-pipeconf/src/main/java/org/onosproject/pipelines/idn/P4InfoConstants.java" \
    --shell-output "$SCRIPT_DIR/build/p4info.env"
cp "$SCRIPT_DIR/build/p4info.env" "$SCRIPT_DIR/p4info.env"

echo "P4 pipeline, P4Info, Java constants, and shell IDs are synchronized."
