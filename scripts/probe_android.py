"""串行探测 APP，输出统一 JSON；不自动安装 App 或连接猜测端口。"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from opinion_capture.android import capture


def main() -> int:
    """从命令行接收单次采集参数，状态为 blocked/failed 时非零退出。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", required=True, choices=("xiaohongshu", "bilibili"))
    parser.add_argument("--keyword", default="零跑")
    parser.add_argument("--capture-kind", choices=("results", "suggestions"), default="results")
    parser.add_argument("--top-n", type=int, default=20)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--serial")
    parser.add_argument("--timeout-seconds", type=float, default=30)
    result = capture(vars(parser.parse_args()))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] in ("success", "partial") else 2


if __name__ == "__main__":
    raise SystemExit(main())
