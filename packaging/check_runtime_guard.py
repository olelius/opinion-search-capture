"""本次base_library.zip误拦截缺陷的定点静态检查；不执行spec构建或应用代码。"""

import argparse
import ast
import json
from pathlib import Path

from build_support import ROOT, forbidden_member


def main() -> int:
    """仅提取spec中的纯来源判定函数，对路径样例和已生成TOC执行静态检查。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workpath", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    work = args.workpath.resolve()
    tree = ast.parse((ROOT / "packaging/opinion_capture.spec").read_text(encoding="utf-8"))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "accepted")
    namespace = {"Path": Path, "root": ROOT, "CONF": {"workpath": str(work)}, "forbidden_member": forbidden_member}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "<spec-guard-only>", "exec"), namespace)
    accepted = namespace["accepted"]
    cases = [
        ("current_generated_runtime", "base_library.zip", work / "base_library.zip", "DATA", True),
        ("wrong_destination", "renamed.zip", work / "base_library.zip", "DATA", False),
        ("wrong_kind", "base_library.zip", work / "base_library.zip", "BINARY", False),
        ("unlisted_work_file", "Analysis-00.toc", work / "Analysis-00.toc", "DATA", False),
        ("other_build", "base_library.zip", work.parent.parent.parent / "other-build/base_library.zip", "DATA", False),
        ("non_build_local", "base_library.zip", ROOT / ".local/base_library.zip", "DATA", False),
        ("raw_docs", "asset.jpg", ROOT / "docs/original.jpg", "DATA", False),
        ("renamed_profile", "asset.txt", ROOT / "profiles/Cookies", "DATA", False),
        ("renamed_session", "asset.txt", ROOT / "sessions/state.json", "DATA", False),
        ("private_browser_root", "asset.txt", ROOT / "browser-profiles/state.json", "DATA", False),
        ("user_data_root", "asset.txt", ROOT / "User Data/Default/data", "DATA", False),
        ("session_destination", "sessions/state.json", ROOT / "packaging/resources/portable-defaults.json", "DATA", False),
        ("allowlisted_public_resource", "portable_resources/portable-defaults.json", ROOT / "packaging/resources/portable-defaults.json", "DATA", True),
    ]
    results = []
    for name, destination, source, kind, expected in cases:
        try:
            actual = bool(accepted((destination, str(source), kind)))
        except ValueError:
            actual = False
        results.append({"name": name, "expected": expected, "actual": actual, "ok": expected == actual})
    # 只解析上次Analysis缓存，定位其数据条目；不import PyInstaller或运行应用。
    toc = ast.literal_eval((work / "Analysis-00.toc").read_text(encoding="utf-8"))
    rejected = []
    runtime_entries = []
    def visit(value):
        if isinstance(value, (tuple, list)):
            if len(value) == 3 and all(isinstance(x, str) for x in value) and value[2] in ("DATA", "BINARY", "EXTENSION"):
                try:
                    accepted(value)
                except ValueError as exc:
                    rejected.append({"destination": value[0], "error": str(exc)})
                if value[0] == "base_library.zip":
                    runtime_entries.append(value)
            else:
                for child in value:
                    visit(child)
    visit(toc)
    report = {"kind": "targeted-static-guard-check", "ok": all(x["ok"] for x in results) and not rejected and bool(runtime_entries),
              "cases": results, "runtime_entries": runtime_entries, "rejected_cached_entries": rejected,
              "application_executed": False, "spec_build_executed": False}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": report["ok"], "case_count": len(results), "rejected_cached_entries": rejected,
                      "report": str(args.output.resolve())}, ensure_ascii=False))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
