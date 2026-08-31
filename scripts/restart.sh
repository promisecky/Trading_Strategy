#!/usr/bin/env bash
# 安全重启脚本: 按端口精确杀旧进程(避免 pkill 模式串匹配到自己的 shell),
# 等待端口释放后交给 start.sh 拉起。交互终端下 start.sh 会自动 nohup 后台化。
#
# 用法:
#   ./scripts/restart.sh               # 交互终端: 后台重启
#   ./scripts/restart.sh --foreground  # 前台运行(调试)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

for port in 8001 5174; do
  pids="$(lsof -ti ":$port" 2>/dev/null || true)"
  if [ -n "$pids" ]; then
    echo "[restart] 杀掉占用端口 $port 的进程: $(echo $pids | tr '\n' ' ')"
    kill -9 $pids 2>/dev/null || true
  fi
done

# 等待端口释放(子进程退出可能滞后)
for port in 8001 5174; do
  for _ in $(seq 1 10); do
    lsof -ti ":$port" >/dev/null 2>&1 || break
    sleep 1
  done
done

echo "[restart] 端口已释放, 启动服务 ..."
exec "$ROOT/start.sh" "$@"
