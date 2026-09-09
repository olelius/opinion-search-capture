"""显式运行一次或两类 B站 WEB 实测；普通 unittest 不调用本脚本。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from opinion_capture.web_bilibili import capture


def main() -> int:
    """原图和专属 profile 始终留在 .local/web_bilibili，最新汇总可重复生成。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keyword", default="零跑")
    parser.add_argument("--capture-kind", choices=("results", "suggestions", "both"), default="both")
    parser.add_argument("--top-n", type=int, default=3)
    parser.add_argument("--timeout-seconds", type=float, default=30)
    args = parser.parse_args()
    local = ROOT / ".local" / "web_bilibili"
    local.mkdir(parents=True, exist_ok=True)
    results = []
    kinds = ("results", "suggestions") if args.capture_kind == "both" else (args.capture_kind,)
    for kind in kinds:
        result = capture(dict(keyword=args.keyword, capture_kind=kind,
                              top_n=args.top_n, timeout_seconds=args.timeout_seconds,
                              output_dir=str(local / "captures")))
        results.append(result)
        document = {"platform": "bilibili", "client": "WEB", "results": results}
        temporary = local / "probe-result.pending.json"
        temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(local / "probe-result.json")
        print(json.dumps(result, ensure_ascii=False), flush=True)
    return 1 if any(r["status"] in ("failed", "blocked") for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
