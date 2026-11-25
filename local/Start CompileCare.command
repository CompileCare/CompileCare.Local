#!/bin/bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
exec "$ROOT/scripts/start-macos.sh"
