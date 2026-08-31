#!/usr/bin/env bash
# SSE 份额代理网关: 本机出口 IP 被上交所 WAF 封锁, 经 SS 节点换出口。
# 多节点模式(推荐): ~/.etf-monitor/clash_sub.yaml → gen_ss_config.py 生成
#   sslocal.json, balancer=best_ping 自动测速选优/故障切换; 进程崩溃自动重启。
# 单节点兜底: 无订阅文件时用 ~/.etf-monitor/sse_node.conf。
# 成功后导出 SSE_PROXY_URL; 由 start.sh / start-prod.sh 自动调用, 也可单独 source。
#
# 用法: source scripts/start_sse_proxy.sh

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOME_CONF="$HOME/.etf-monitor"
SUB="$HOME_CONF/clash_sub.yaml"
SSJSON="$HOME_CONF/sslocal.json"
NODE_CONF="$HOME_CONF/sse_node.conf"
SSLOCAL="$ROOT/tools/sslocal"
VENV_PY="$ROOT/.venv/bin/python"

if [ ! -x "$SSLOCAL" ]; then
  echo "[sse-proxy] 缺 tools/sslocal, 跳过代理" >&2
  return 0 2>/dev/null || true
fi

# 已在监听则直接复用
if [ -n "${SSE_PROXY_ADDR:-}" ] && curl -sS --connect-timeout 2 -x "http://$SSE_PROXY_ADDR" -o /dev/null https://www.baidu.com 2>/dev/null; then
  export SSE_PROXY_URL="http://$SSE_PROXY_ADDR"
  echo "[sse-proxy] 复用已运行的代理 $SSE_PROXY_URL" >&2
  return 0 2>/dev/null || true
fi
if curl -sS --connect-timeout 2 -x http://127.0.0.1:1089 -o /dev/null https://www.baidu.com 2>/dev/null; then
  export SSE_PROXY_URL="http://127.0.0.1:1089"
  echo "[sse-proxy] 复用已运行的代理 $SSE_PROXY_URL" >&2
  return 0 2>/dev/null || true
fi

PROXY_ADDR="127.0.0.1:1089"
if [ -f "$SUB" ] && [ -x "$VENV_PY" ]; then
  # 订阅更新后自动重新生成配置
  if [ ! -f "$SSJSON" ] || [ "$SUB" -nt "$SSJSON" ]; then
    "$VENV_PY" "$ROOT/scripts/gen_ss_config.py" --local "$PROXY_ADDR" >&2 || true
  fi
fi

if [ -f "$SSJSON" ]; then
  # 多节点: 带守护循环(进程崩溃 5s 后自动拉起); protocol 由 CLI 指定
  nohup bash -c "while :; do '$SSLOCAL' -c '$SSJSON' -b '$PROXY_ADDR' --protocol http >> '$HOME_CONF/sslocal.log' 2>&1; echo \"[sslocal] exited \$?, restart in 5s\" >> '$HOME_CONF/sslocal.log'; sleep 5; done" \
    > /dev/null 2>&1 &
else
  . "$NODE_CONF" 2>/dev/null || { echo "[sse-proxy] 无订阅/节点配置, 跳过" >&2; return 0 2>/dev/null || true; }
  nohup bash -c "while :; do '$SSLOCAL' -s '$SS_SERVER' -m '$SS_METHOD' -k '$SS_PASSWORD' -b '$PROXY_ADDR' --protocol http >> '$HOME_CONF/sslocal.log' 2>&1; sleep 5; done" \
    > /dev/null 2>&1 &
fi
sleep 3

# balancer 首轮需测完全部节点才 bind(约 10-30s), 轮询等待端口就绪
for _ in $(seq 1 20); do
  if curl -sS --connect-timeout 2 -x "http://$PROXY_ADDR" -o /dev/null https://www.baidu.com 2>/dev/null; then
    break
  fi
  sleep 2
done

if curl -sS --connect-timeout 8 -x "http://$PROXY_ADDR" -o /dev/null https://www.baidu.com 2>/dev/null; then
  export SSE_PROXY_URL="http://$PROXY_ADDR"
  echo "[sse-proxy] 代理已启动 $SSE_PROXY_URL (多节点自动测速切换)" >&2
else
  echo "[sse-proxy] 启动失败, 详见 ~/.etf-monitor/sslocal.log (不影响主流程启动)" >&2
fi
return 0 2>/dev/null || true
