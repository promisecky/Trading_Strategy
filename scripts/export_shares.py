"""在本机(可直连交易所的网络)拉取 ETF 份额并导出 JSON, 供服务器端 import_shares.py 导入。

适用场景: 服务器出口 IP 被上交所 WAF 封锁, 本机正常。本机无需数据库,
仅依赖 requests(沪市)与 akshare(深市, 可选)。

用法:
  python scripts/export_shares.py --start 2025-08-15 --end 2026-08-14 -o shares_export.json
  python scripts/export_shares.py --dates-file missing_dates.json -o shares_export.json
"""
import argparse
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from base.fetch.shares import fetch_shares_for_date


def _weekdays(start: str, end: str) -> list[str]:
    d = datetime.strptime(start, "%Y-%m-%d")
    stop = datetime.strptime(end, "%Y-%m-%d")
    out: list[str] = []
    while d <= stop:
        if d.weekday() < 5:
            out.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return out


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="export ETF shares for offline import")
    parser.add_argument("--start", help="起始日 YYYY-MM-DD")
    parser.add_argument("--end", help="结束日 YYYY-MM-DD(默认今天)")
    parser.add_argument("--dates-file", help="JSON 日期列表文件(与 --start/--end 二选一)")
    parser.add_argument("-o", "--output", default="shares_export.json", help="输出文件")
    parser.add_argument("--sleep", type=float, default=0.3, help="相邻日期拉取间隔(秒)")
    args = parser.parse_args()

    if args.dates_file:
        dates = sorted(json.loads(Path(args.dates_file).read_text()))
    elif args.start:
        dates = _weekdays(args.start, args.end or datetime.now().strftime("%Y-%m-%d"))
    else:
        parser.error("需要 --start/--end 或 --dates-file")

    payload: dict[str, dict[str, float]] = {}
    for i, date in enumerate(dates, 1):
        shares = fetch_shares_for_date(date)
        if shares:
            payload[date] = shares
        print(f"  [{i}/{len(dates)}] {date}: {len(shares)} 只")
        time.sleep(args.sleep)

    Path(args.output).write_text(json.dumps(payload, ensure_ascii=False, indent=1))
    print(f"[EXPORT] {len(payload)} 天 / {sum(len(v) for v in payload.values())} 行 -> {args.output}")
