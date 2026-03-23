#!/usr/bin/env bash
# SPELAB 服务启动脚本（供 launchd 或手动后台运行）
# 用法：在项目根目录执行 ./scripts/run_server.sh，或由 launchd 调用

set -e
cd "$(dirname "$0")/.."
mkdir -p logs
exec /usr/bin/env python3 -m server.app >> logs/server.log 2>&1
