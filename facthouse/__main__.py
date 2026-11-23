"""命令行入口：可持续刷新事实仓输出。

用法：
    python3 -m facthouse --facts data/facts.json --out build/facthouse.json
    python3 -m facthouse --facts data/facts.json --ledger build/facthouse.json --trace ADJ-xxxx
    python3 -m facthouse --facts data/facts.json --trace CERT-81
"""
from __future__ import annotations

import argparse
import json
import sys

from . import FactHouse, FactValidationError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="修缮工程验工事实仓")
    parser.add_argument("--facts", required=True, help="原始资料 JSON")
    parser.add_argument("--ledger", help="上一版事实仓输出（用于幂等复跑）")
    parser.add_argument("--out", help="本次输出路径")
    parser.add_argument("--trace", help="按调整键或签认号追查证据链")
    args = parser.parse_args(argv)

    try:
        house = FactHouse.from_paths(args.facts, args.ledger)
    except (FactValidationError, KeyError, ValueError) as exc:
        print(f"事实仓构建失败: {exc}", file=sys.stderr)
        return 2

    if args.trace:
        print(json.dumps(house.trace(args.trace), ensure_ascii=False, indent=2))
        return 0

    payload = house.export()
    if args.out:
        house.save(args.out)
        print(f"已写入 {args.out}")
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
