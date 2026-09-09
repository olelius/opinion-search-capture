"""使用现有真实截图验证汇总排版，连续帧另用确定性合成数据验证。"""

import hashlib
import json
from pathlib import Path
import random
import sys
from datetime import datetime
from time import perf_counter

from PIL import Image
import PIL

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from opinion_capture.imaging import compose_grid, stitch_vertical


def main() -> None:
    """生成独立版本目录、PNG 和含输入哈希/耗时/合成标记的清单。"""
    out = ROOT / ".local/imaging" / datetime.now().strftime("probe-%Y%m%d-%H%M%S-%f")
    out.mkdir(parents=True)
    started = perf_counter()
    groups = {
        "bilibili-results": ("20点B站TOP20", 3, 3),
        "xiaohongshu-results": ("21点小红书TOP20", 3, 3),
        "bilibili-suggestions": ("20点B站下拉", 4, 2),
        "xiaohongshu-suggestions": ("21点小红书下拉词", 4, 2),
    }
    sources = []
    artifacts = []
    for name, (folder, count, columns) in groups.items():
        # 文件名排序仅为探针的固定展示清单；业务方应传入冻结关键词顺序。
        paths = sorted((ROOT / "docs" / folder).rglob("*.jpg"))[:count]
        if len(paths) != count:
            raise RuntimeError(f"缺少样例图片：{folder}")
        for p in paths:
            sources.append({"path": str(p), "sha256_before": hashlib.sha256(p.read_bytes()).hexdigest()})
        tick = perf_counter()
        result = compose_grid(paths, out / f"{name}.png", columns=columns, cell_width=360, gap=12)
        artifacts.append(dict(result, evidence="existing-real-screenshot-layout", seconds=perf_counter() - tick))
    rng = random.Random(20260908)
    base = Image.frombytes("RGB", (160, 500), rng.randbytes(160 * 500 * 3))
    frames = []
    for i, top in enumerate((0, 130, 260)):
        path = out / f"synthetic-frame-{i}.png"
        base.crop((0, top, 160, top + 240)).save(path)
        frames.append(path)
    tick = perf_counter()
    stitched = stitch_vertical(frames, out / "synthetic-stitched.png")
    with Image.open(stitched["path"]) as im:
        exact = im.size == base.size and im.tobytes() == base.tobytes()
    if not exact:
        raise AssertionError("合成连续帧未逐像素还原")
    artifacts.append(dict(stitched, evidence="synthetic-not-app-capture", pixel_exact=exact,
                          seconds=perf_counter() - tick))
    for item in sources:
        item["sha256_after"] = hashlib.sha256(Path(item["path"]).read_bytes()).hexdigest()
        if item["sha256_before"] != item["sha256_after"]:
            raise AssertionError("原图哈希发生变化")
    manifest = {"python": sys.executable, "pillow": PIL.__version__, "pillow_path": PIL.__file__,
                "sources": sources, "artifacts": artifacts, "seconds": perf_counter() - started,
                "boundary": "真实截图仅验证排版；滚动连续性为合成测试，无 App 实测、无排名计数。"}
    for artifact in artifacts:
        p = Path(artifact["path"])
        artifact["sha256"] = hashlib.sha256(p.read_bytes()).hexdigest()
        with Image.open(p) as im:
            im.verify()
    path = out / "manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"manifest": str(path), "seconds": manifest["seconds"],
                      "artifacts": [{k: a[k] for k in ("path", "width", "height", "seconds")} for a in artifacts]},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
