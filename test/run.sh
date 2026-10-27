#!/usr/bin/env bash
# Run the GenApp off-mainframe tests in a container (Podman, or Docker).
#
#   test/run.sh                 build + run all tests
#   test/run.sh -k motor        extra arguments are passed to pytest
#   test/run.sh --shell         open a shell in the test container
#   test/run.sh --setup-only    build image + programs, do not run tests
#
# Set CONTAINER_ENGINE=docker to force Docker.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENGINE="${CONTAINER_ENGINE:-}"
if [[ -z "$ENGINE" ]]; then
    if command -v podman >/dev/null 2>&1; then ENGINE=podman
    elif command -v docker >/dev/null 2>&1; then ENGINE=docker
    else echo "Neither podman nor docker found. Install one of them." >&2; exit 2
    fi
fi

# The image tag follows the Containerfile content, so it is rebuilt only
# when the Containerfile changes.
HASH="$(cat "$ROOT/test/Containerfile" | (sha256sum 2>/dev/null || shasum -a 256) | cut -c1-12)"
IMAGE="localhost/genapp-test:$HASH"
if ! "$ENGINE" image exists "$IMAGE" 2>/dev/null && ! "$ENGINE" image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "Building test image $IMAGE with $ENGINE ..."
    "$ENGINE" build -t "$IMAGE" -f "$ROOT/test/Containerfile" "$ROOT/test"
fi

TTY=()
[[ -t 0 && -t 1 ]] && TTY=(-it)

if [[ "${1:-}" == "--shell" ]]; then
    exec "$ENGINE" run --rm ${TTY[@]+"${TTY[@]}"} -v "$ROOT":/genapp:Z -w /genapp "$IMAGE" \
        bash -c 'test/in-container.sh --setup-only && exec bash'
fi
exec "$ENGINE" run --rm ${TTY[@]+"${TTY[@]}"} -v "$ROOT":/genapp:Z -w /genapp "$IMAGE" test/in-container.sh "$@"
