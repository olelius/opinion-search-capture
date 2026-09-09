"""构建前依赖清单、源码指纹和分发文件审计；输出只落到调用者指定目录。"""

from __future__ import annotations

import argparse
import ast
import hashlib
from importlib import metadata, util
import json
import os
from pathlib import Path
import platform
import sys
import tomllib

from portable_runtime import ADAPTERS, CLI_EXE, GUI_EXE

ROOT = Path(__file__).resolve().parents[1]
BUILD_REQUIREMENTS = ("PyInstaller>=6.2,<7", "pyinstaller-hooks-contrib>=2024.6")


def source_fingerprint(root: Path = ROOT) -> dict:
    """只对源码及允许的构建输入计算SHA-256，不读取截图/账号/.local。"""
    paths = [root / name for name in ("pyproject.toml", "requirements-tested.txt", "scripts/build-portable.ps1")]
    paths += list((root / "src").rglob("*.py"))
    paths += [p for p in (root / "packaging").rglob("*")
              if p.is_file() and p.suffix in (".py", ".spec", ".json", ".md")]
    items = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(paths) if p.is_file()}
    digest = hashlib.sha256(json.dumps(items, sort_keys=True).encode("utf-8")).hexdigest()
    return {"sha256": digest, "files": items}


def preflight() -> dict:
    """验证当前venv及相关依赖闭包，记录实际来源，不安装或升级包。"""
    from packaging.requirements import Requirement
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    failures, warnings, versions = [], [], {}
    expected_prefix = (ROOT / ".venv").resolve()
    if Path(sys.prefix).resolve() != expected_prefix:
        failures.append("构建解释器应属于项目.venv")
    if sys.platform != "win32" or platform.architecture()[0] != "64bit":
        failures.append("此规范要求Windows x64构建机")
    from packaging.specifiers import SpecifierSet
    if platform.python_version() not in SpecifierSet(pyproject["project"]["requires-python"]):
        failures.append("Python版本不满足pyproject.toml")
    pending = list(pyproject["project"]["dependencies"]) + list(BUILD_REQUIREMENTS)
    seen = set()
    while pending:
        raw = pending.pop(0)
        req = Requirement(raw)
        if req.marker and not req.marker.evaluate():
            continue
        key = req.name.lower().replace("_", "-")
        try:
            dist = metadata.distribution(req.name)
            if req.specifier and dist.version not in req.specifier:
                failures.append(f"{raw}与已装{dist.version}不兼容")
            if key in seen:
                continue
            seen.add(key)
            versions[req.name] = {"version": dist.version, "location": str(dist.locate_file("")),
                                  "project_local": Path(dist.locate_file("")).resolve().is_relative_to(expected_prefix)}
            pending.extend(dist.requires or [])
        except metadata.PackageNotFoundError:
            failures.append(f"缺少依赖：{raw}")
    tested = []
    for line in (ROOT / "requirements-tested.txt").read_text(encoding="utf-8-sig").splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            req = Requirement(line)
            try:
                actual = metadata.version(req.name)
                match = actual in req.specifier
            except metadata.PackageNotFoundError:
                actual, match = None, False
            tested.append({"requirement": line, "actual": actual, "matches": match})
            if not match:
                failures.append(f"已验证依赖组合有漂移：{line}，实际{actual}")
    config = (expected_prefix / "pyvenv.cfg").read_text(encoding="utf-8-sig")
    if "include-system-site-packages = true" in config:
        warnings.append("venv复用宿主只读包：兼容性取决于本次闭包，不等于独立可重现环境")
    for module in (*("opinion_capture." + n for n in ADAPTERS), "opinion_capture.web_results_bilibili",
                   "opinion_capture.web_results_xiaohongshu", "opinion_capture.android_results",
                   "opinion_capture.paths"):
        if util.find_spec(module) is None:
            failures.append(f"缺少待收集模块：{module}")
    # 仅解析文本语法，不import核心半写模块，也不执行spec。
    for name in source_fingerprint()["files"]:
        if Path(name).suffix in (".py", ".spec"):
            try:
                ast.parse((ROOT / name).read_text(encoding="utf-8-sig"), filename=name)
            except SyntaxError as exc:
                failures.append(f"源码语法未就绪：{name}:{exc.lineno}")
    return {"ok": not failures, "python": sys.version, "executable": sys.executable,
            "base_executable": getattr(sys, "_base_executable", None), "prefix": sys.prefix,
            "platform": platform.platform(), "dependencies": versions, "tested": tested,
            "failures": failures, "warnings": warnings, "fingerprint": source_fingerprint(),
            "formal_build_allowed": False, "reason": "仍需主明确核心稳定并通过冻结验收"}


def forbidden_member(relative: str) -> bool:
    """审计分发目录名；用户配置/浏览器会话/源码资料均不属于发布载荷。"""
    p = Path(relative.replace("\\", "/"))
    parts = {part.lower() for part in p.parts}
    if parts & {".local", ".venv", ".git", ".capture-state", "docs", "user data", "profiles",
                "browser-profiles", "session-log", "sessions",
                ".local-browsers", "ms-playwright", "__pycache__"}:
        return True
    if p.name.lower() in {"cookies", "cookies-journal", "login data", "web data", "plan.json",
                           "portable-dependencies.json", ".env", "adb.exe", "adbwinapi.dll", "adbwinusbapi.dll"}:
        return True
    if p.name.lower().startswith(".env."):
        return True
    return p.suffix.lower() in {".sqlite", ".sqlite3", ".db", ".log", ".pyc"}


def ensure_dist_child(target: Path, root: Path = ROOT) -> Path:
    """resolve后要求目标严格位于仓库dist内；不接受dist自身、链接逃逸或外部路径。"""
    dist = (root / "dist").resolve()
    expected_dist = root.resolve() / "dist"
    if dist != expected_dist:
        raise ValueError("dist不可指向仓库外或其他位置")
    resolved = target.resolve()
    if resolved == dist or not resolved.is_relative_to(dist):
        raise ValueError("目标必须是经过resolve验证的dist子目录")
    return resolved


def audit_distribution(target: Path, *, root: Path = ROOT) -> dict:
    """在自检产物尚未写入前审计整个onedir并生成SHA-256清单。"""
    target = ensure_dist_child(target, root)
    failures, files = [], {}
    if not target.is_dir():
        failures.append("分发目录不存在")
    for name in (GUI_EXE, CLI_EXE, "_internal/portable_resources/portable-defaults.json"):
        if not (target / name).is_file():
            failures.append(f"缺少必需载荷：{name}")
    for path in sorted(target.rglob("*")):
        if not path.is_file():
            continue
        if not path.resolve().is_relative_to(target):
            failures.append(f"分发包含外部链接：{path.name}")
            continue
        name = path.relative_to(target).as_posix()
        if forbidden_member(name):
            failures.append(f"禁止分发成员：{name}")
        files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"ok": not failures, "failures": failures, "file_count": len(files), "sha256": files}


def main(argv=None) -> int:
    """支持预检、构建前后指纹和分发审计；绝不删除文件或自动安装。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preflight", "fingerprint", "audit", "finish"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--target", type=Path)
    parser.add_argument("--before", type=Path)
    args = parser.parse_args(argv)
    if args.action == "preflight":
        data = preflight()
    elif args.action == "fingerprint":
        data = source_fingerprint()
    elif args.action == "audit":
        if args.target is None:
            parser.error("audit需要--target")
        data = audit_distribution(args.target)
    else:
        if args.before is None:
            parser.error("finish需要--before")
        before = json.loads(args.before.read_text(encoding="utf-8"))
        now = source_fingerprint()
        changes = sorted(k for k in before["files"].keys() | now["files"].keys()
                         if before["files"].get(k) != now["files"].get(k))
        data = {"ok": not changes, "source_stable": not changes,
                "changed_files": changes, "before": before["sha256"], "after": now["sha256"],
                "final_delivery": False}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": data.get("ok", True), "report": str(args.output.resolve()),
                      "failures": data.get("failures", [])}, ensure_ascii=False))
    return 0 if data.get("ok", True) else 2


if __name__ == "__main__":
    # 预检由项目venv启动；不从全局命令名寻找解释器。
    sys.path.insert(0, str(ROOT / "src"))
    raise SystemExit(main())
