#!/bin/bash
# Compatibility entrypoint for the canonical local verification gate.
#
# This wrapper intentionally performs no fetch, pull, stash, remote scan, or
# deployment action. Use run_checks.sh directly for new automation; retain this
# path for existing developer and CI integrations.

set -euo pipefail

usage() {
    echo "Usage: $0 [project_dir] [--fast|--docs-only] [--fix|--autofix]" >&2
}

PROJECT_DIR=""
ARGS=()
for arg in "$@"; do
    case "$arg" in
        --fast|--docs|--docs-only|--fix|--autofix)
            ARGS+=("$arg")
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        -*)
            usage
            exit 2
            ;;
        *)
            if [[ -n "$PROJECT_DIR" ]]; then
                usage
                exit 2
            fi
            PROJECT_DIR="$arg"
            ;;
    esac
done

if [[ -n "$PROJECT_DIR" ]]; then
    if [[ ! -d "$PROJECT_DIR" ]]; then
        echo "ERROR: project directory does not exist: $PROJECT_DIR" >&2
        exit 2
    fi
    cd "$PROJECT_DIR"
fi

if [[ ! -f "run_checks.sh" ]]; then
    echo "ERROR: run_checks.sh was not found in $(pwd)" >&2
    exit 2
fi

exec bash run_checks.sh ${ARGS[@]+"${ARGS[@]}"}
