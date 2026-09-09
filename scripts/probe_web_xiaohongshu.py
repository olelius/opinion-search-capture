"""独立实页最小探针：每种采集一次；原始证据和 profile 仅写入 .local。"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from opinion_capture.web_xiaohongshu import capture


def main() -> int:
    """支持反复手动运行；每次截图使用唯一目录，汇总 JSON 记录最近一轮。"""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keyword", default="零跑")
    parser.add_argument("--top-n", type=int, default=3)
    parser.add_argument("--timeout-seconds", type=float, default=30)
    parser.add_argument("--kind", choices=("both", "results", "suggestions"), default="both")
    args = parser.parse_args()
    local = ROOT / ".local" / "web_xiaohongshu"
    temporary = local / "tmp"
    temporary.mkdir(parents=True, exist_ok=True)
    os.environ["TEMP"] = os.environ["TMP"] = os.environ["TMPDIR"] = str(temporary)
    kinds = ("results", "suggestions") if args.kind == "both" else (args.kind,)
    results = [capture(dict(keyword=args.keyword, capture_kind=kind, top_n=args.top_n,
                            timeout_seconds=args.timeout_seconds, output_dir=str(local / "captures")))
               for kind in kinds]
    target = local / "probe-result.json"
    pending = local / "probe-result.pending.json"
    pending.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    pending.replace(target)
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if all(item["status"] == "success" for item in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
