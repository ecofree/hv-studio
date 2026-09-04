#!/usr/bin/env bash
# hv CLI 启动脚本 (Linux/macOS)
set -e
cd "$(dirname "$0")"
exec python -m app.cli "$@"
