"""导入外部拉取的份额 JSON(配合 export_shares.py, 解决服务器出口被封锁问题)。

用法:
  python scripts/import_shares.py shares_export.json
  python scripts/import_shares.py --missing-dates missing_dates.json  # 仅导出库中缺失日期
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from base.store.database import init_db
from base.store.daily_repo import get_missing_share_dates
from base.scheduler.shares_jobs import job_import_shares


def _print_progress(current: int, total: int, message: str) -> None:
    print(f"  [{current}/{total}] {message}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="import externally fetched ETF shares")
    parser.add_argument("json_file", nargs="?", help="export_shares.py 生成的 JSON 文件")
    parser.add_argument("--missing-dates", help="把库中份额缺失日期写入指定 JSON 文件后退出")
    args = parser.parse_args()
    init_db()

    if args.missing_dates:
        dates = get_missing_share_dates()
        Path(args.missing_dates).write_text(json.dumps(dates))
        print(f"[MISSING] {len(dates)} 个缺失交易日 -> {args.missing_dates}")
        sys.exit(0)

    if not args.json_file:
        parser.error("需要 JSON 文件参数, 或使用 --missing-dates 导出缺失日期")
    payload = json.loads(Path(args.json_file).read_text())
    result = job_import_shares(_print_progress, payload)
    print(f"[IMPORT] done: {result}")
    left = get_missing_share_dates()
    print(f"[IMPORT] 剩余缺失交易日: {len(left)}")
