"""从 Clash 订阅生成 shadowsocks-rust 多节点配置(自动测速/切换)。

读取 ~/.etf-monitor/clash_sub.yaml 中全部 SS 节点, 输出
~/.etf-monitor/sslocal.json: balancer=best_ping 周期主动测速,
自动选最优节点, 节点故障自动切换。订阅更新后重跑本脚本即可。

用法: python scripts/gen_ss_config.py [--local 127.0.0.1:1089]
"""
import argparse
import json
import re
from pathlib import Path

SUB_PATH = Path.home() / ".etf-monitor" / "clash_sub.yaml"
OUT_PATH = Path.home() / ".etf-monitor" / "sslocal.json"

# 单行 flow 映射: - {name: ..., server: ..., port: 123, type: ss, cipher: ..., password: ...}
NODE_RE = re.compile(
    r"server:\s*(?P<server>\S+),\s*port:\s*(?P<port>\d+),\s*type:\s*(?P<type>\w+),\s*"
    r"cipher:\s*(?P<cipher>[\w-]+),\s*password:\s*(?P<password>[^,}]+)"
)


def parse_nodes(text: str) -> list[dict]:
    """仅解析 proxies: 段内的 ss 节点(跳过 Proxy: 段的占位 socks5)。"""
    nodes: list[dict] = []
    in_proxies = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "proxies:":
            in_proxies = True
            continue
        if in_proxies and stripped and not stripped.startswith("-"):
            in_proxies = False  # 段结束
        if not in_proxies or not stripped.startswith("- {"):
            continue
        m = NODE_RE.search(stripped)
        if m and m.group("type") == "ss":
            nodes.append(
                {
                    "server": m.group("server"),
                    "server_port": int(m.group("port")),
                    "method": m.group("cipher"),
                    "password": m.group("password").strip(),
                    "timeout": 30,
                }
            )
    return nodes


def main() -> None:
    parser = argparse.ArgumentParser(description="generate shadowsocks-rust multi-node config")
    parser.add_argument("--local", default="127.0.0.1:1089", help="本地监听地址")
    args = parser.parse_args()

    if not SUB_PATH.exists():
        raise SystemExit(f"[gen-ss] 缺少订阅文件 {SUB_PATH}")
    nodes = parse_nodes(SUB_PATH.read_text())
    if not nodes:
        raise SystemExit("[gen-ss] 未解析到任何 SS 节点")

    host, _, port = args.local.rpartition(":")
    # 注意: shadowsocks-rust 1.24 的 local 不能同时写在 config 和 CLI:
    # config 里写 local_address/local_port 会与 CLI -b 双重 bind 同端口冲突;
    # locals 段的 local_addr 会报 missing `addr`。故 local 完全由启动命令
    # `-b <addr> --protocol http` 提供, config 只含 servers + balancer。
    config = {
        "servers": nodes,
        # best_ping: 周期主动测速选最优节点; 故障节点自动剔除切换
        "balancer": {
            "strategy": "best_ping",
            "check_interval": 60,
            "check_best_interval": 30,
            "max_server_rtt": 8000,
        },
    }
    OUT_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=1))
    OUT_PATH.chmod(0o600)
    print(f"[gen-ss] {len(nodes)} 个 SS 节点 -> {OUT_PATH} (local {args.local})")


if __name__ == "__main__":
    main()
