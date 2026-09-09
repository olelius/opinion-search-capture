"""Android APP 首版适配器：ADB 原始 PNG、控件证据及保守业务状态。

不安装业务 App、不接受协议、不代办登录；同一设备需由调度器串行使用。
未验证平台条目身份/排序前始终不报告 TOP N 成功。
"""
from __future__ import annotations

import io
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from datetime import datetime
import uuid
import xml.etree.ElementTree as ET

from .progress import phase

PACKAGES = {"xiaohongshu": "com.xingin.xhs", "bilibili": "tv.danmaku.bili"}
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class AndroidCancelled(RuntimeError):
    """主runner取消标记已出现；不继续发出设备命令。"""


def _check(deadline: float, cancel_file: str | None = None) -> None:
    """在每个阻塞边界共享总预算和取消条件。"""
    if cancel_file and Path(cancel_file).exists():
        raise AndroidCancelled("cancel_requested")
    if time.monotonic() >= deadline:
        raise TimeoutError("capture_deadline_exceeded")


def _run_bounded(command, deadline, cancel_file=None, env=None):
    """有取消标记时轮询子进程；结束前回收进程，防止停止后继续点击。"""
    _check(deadline, cancel_file)
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    if not cancel_file:
        return subprocess.run(command, capture_output=True, timeout=deadline - time.monotonic(), check=False, env=env, creationflags=flags)
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, creationflags=flags)
    try:
        while True:
            _check(deadline, cancel_file)
            try:
                stdout, stderr = process.communicate(timeout=min(.1, deadline - time.monotonic()))
                _check(deadline, cancel_file)
                return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                continue
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def discover_devices() -> list[dict]:
    """GUI后台发现：最多8秒，按需连接已启动MuMu；不启动模拟器或访问App。"""
    client = AdbClient(time.monotonic() + 8)
    devices = client.list_devices(refresh=True)
    return [dict(name=d.get("name") or d.get("model") or d["serial"], serial=d["serial"], state=d["state"]) for d in devices]


class AndroidBlocked(RuntimeError):
    """设备、会话或界面条件未满足；保留原因供调度器展示。"""


def parse_devices(output: str) -> list[dict]:
    """解析 adb devices -l，保留离线/未确认设备，避免错误自动选机。"""
    devices = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 2 or line.startswith(("List of devices", "*")):
            continue
        item = {"serial": parts[0], "state": parts[1]}
        for part in parts[2:]:
            if ":" in part:
                key, value = part.split(":", 1)
                item[key] = value
        devices.append(item)
    return devices


def select_device(devices: list[dict], serial: str | None = None) -> dict:
    """单设备自动选；多设备包括 offline 情况均要求明确串号。"""
    if not serial and len(devices) != 1:
        raise AndroidBlocked("no_device" if not devices else "multiple_devices_require_serial")
    found = next((d for d in devices if d["serial"] == serial), None) if serial else devices[0]
    if found is None:
        raise AndroidBlocked("serial_not_found")
    if found["state"] != "device":
        raise AndroidBlocked("device_" + found["state"])
    return dict(found)


class AdbClient:
    """仅面向已连接设备的二进制 ADB 包装；所有命令共用总截止时间。"""

    def __init__(self, deadline: float, executable: str | None = None, cancel_file: str | None = None):
        from .android_discovery import find_adb
        if not executable:
            _check(deadline, cancel_file)
        self.executable = executable or find_adb(deadline)
        if not self.executable:
            raise AndroidBlocked("adb_not_found")
        if not executable:
            _check(deadline, cancel_file)
        # 仅传给本进程及其UI子进程，adbutils与截图命令使用同一工具，不改系统环境。
        if Path(self.executable).is_file():
            os.environ["ADBUTILS_ADB_PATH"] = str(Path(self.executable).resolve())
        self.deadline = deadline
        self.cancel_file = cancel_file
        self.serial = None

    def run(self, *args: str, device: bool = True) -> bytes:
        """以字节返回 stdout；PNG 不经文本编码或 PowerShell 重定向。"""
        _check(self.deadline, self.cancel_file)
        command = [self.executable]
        if device:
            if not self.serial:
                raise AndroidBlocked("serial_not_selected")
            command += ["-s", self.serial]
        result = _run_bounded(command + list(args), self.deadline, self.cancel_file)
        if result.returncode:
            raise AndroidBlocked("adb_error: " + result.stderr.decode("utf-8", "replace").strip()[:400])
        return result.stdout

    def text(self, *args: str, device: bool = True) -> str:
        """只对命令文本输出执行 UTF-8 解码。"""
        return self.run(*args, device=device).decode("utf-8", "replace").strip()

    def list_devices(self, serial: str | None = None, *, refresh: bool = False) -> list[dict]:
        """已连接设备直接复用；刷新或目标缺失时仅连接管理器报告的运行实例。"""
        devices = parse_devices(self.text("devices", "-l", device=False))
        if not refresh and devices and (not serial or any(d["serial"] == serial for d in devices)):
            return devices
        from .android_discovery import running_instances
        instances = running_instances(self.executable, self.deadline,
            lambda command: _run_bounded(command, self.deadline, self.cancel_file))
        known = {d["serial"] for d in devices}
        attempted = False
        for instance in instances:
            endpoint = instance["serial"]
            if endpoint in known or (serial and endpoint != serial):
                continue
            _check(self.deadline, self.cancel_file)
            self.text("connect", endpoint, device=False)
            attempted = True
            known.add(endpoint)
        if attempted:
            devices = parse_devices(self.text("devices", "-l", device=False))
        names = {row["serial"]: row["name"] for row in instances}
        for device in devices:
            if device["serial"] in names:
                device["name"] = names[device["serial"]]
        return devices

    def discover(self, serial: str | None = None) -> dict:
        """发现并确认设备启动完成，返回真实设备属性。"""
        device = select_device(self.list_devices(serial), serial)
        self.serial = device["serial"]
        if self.text("shell", "getprop", "sys.boot_completed") != "1":
            raise AndroidBlocked("android_not_booted")
        device["android_version"] = self.text("shell", "getprop", "ro.build.version.release")
        device["sdk"] = self.text("shell", "getprop", "ro.build.version.sdk")
        device["screen_size"] = self.text("shell", "wm", "size")
        return device

    def installed(self, package: str) -> bool:
        """检查精确包名，不把相似名称或商店条目视为已安装。"""
        return "package:" + package in self.text("shell", "pm", "list", "packages", package).splitlines()

    def screenshot(self, path: Path) -> None:
        """校验 PNG 内容后保存原始字节；不重编码、不拼接、不覆盖。"""
        from PIL import Image
        data = self.run("exec-out", "screencap", "-p")
        if not data.startswith(PNG_SIGNATURE):
            raise ValueError("screencap_not_png")
        with Image.open(io.BytesIO(data)) as img:
            if img.format != "PNG":
                raise ValueError("screencap_not_png")
            img.verify()
        with path.open("xb") as file:
            file.write(data)


def detect_blocker(xml: str) -> str | None:
    """仅识别明确拦截提示；普通页面的登录入口不代表强制登录。"""
    root = ET.fromstring(xml)
    text = " ".join(n.get("text", "") + " " + n.get("content-desc", "") for n in root.iter("node"))
    for words, reason in (
        (("请完成验证", "拖动滑块", "安全验证", "请输入验证码"), "verification_required"),
        (("登录后查看", "请先登录", "登录后使用", "手机号登录", "输入手机号", "登录查看更多精彩内容"), "login_required"),
        (("用户协议和隐私政策", "用户协议与隐私政策", "个人信息保护指引", "个人信息保护提示"), "consent_required"),
        (("网络尚未连接", "网络连接失败", "网络不给力", "网络异常，请"), "network_unavailable"),
    ):
        if any(word in text for word in words):
            return reason
    return None


def _connect_ui(serial: str, deadline: float):
    """延迟加载项目 uiautomator2；总超时由外层子进程约束。"""
    import uiautomator2 as u2
    device = u2.connect(serial)
    device.settings["wait_timeout"] = 0
    return device


def _worker_runtime() -> tuple[str, dict]:
    """Windows绕过venv redirector，保证被等待/终止的PID就是实际UI Python进程。"""
    executable = getattr(sys, "_base_executable", sys.executable) if os.name == "nt" else sys.executable
    env = os.environ.copy()
    paths = [str(Path(__file__).resolve().parents[1])]
    if os.name == "nt":
        paths.append(str(Path(sys.prefix) / "Lib" / "site-packages"))
    if env.get("PYTHONPATH"):
        paths.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(paths)
    return executable, env


def _ui_call(serial: str, deadline: float, action: str, cancel_file: str | None = None, **kwargs) -> str:
    """隔离 UI 调用，超时终止工作进程，避免后台继续输入改变唯一设备。

    uiautomator2 的连接/重试拥有自己的超时，单改等待设置不足以形成
    总时限；Windows使用实际CPython并注入venv site-packages，避免launcher先退出而UI子进程残留。
    """
    _check(deadline, cancel_file)
    code = (
        "import sys,json;sys.path.insert(0,sys.argv[1]);"
        "from opinion_capture.android import _ui_worker;"
        "_ui_worker(json.loads(sys.argv[2]))"
    )
    payload = dict(serial=serial, deadline=deadline, cancel_file=cancel_file, action=action, **kwargs)
    if getattr(sys, "frozen", False):
        executable = Path(sys.executable).with_name("OpinionCapture-CLI.exe")
        if not executable.is_file():
            raise AndroidBlocked("frozen_cli_worker_not_found")
        command, env = [str(executable), "--ui-worker", json.dumps(payload)], os.environ.copy()
    else:
        executable, env = _worker_runtime()
        command = [executable, "-B", "-X", "utf8", "-c", code, str(Path(__file__).resolve().parents[1]), json.dumps(payload)]
    output = _run_bounded(
        command,
        deadline, cancel_file, env=env,
    )
    if output.returncode:
        raise RuntimeError("ui_worker_failed: " + output.stderr.decode("utf-8", "replace")[-500:])
    response = json.loads(output.stdout.decode("utf-8"))
    if response.get("blocked"):
        raise AndroidBlocked(response["blocked"])
    return response["xml"]


def _ui_worker(payload: dict) -> None:
    """内部 UI 工作进程入口，仅返回 JSON 控件证据或明确阻塞原因。"""
    try:
        _check(payload["deadline"], payload.get("cancel_file"))
        device = _connect_ui(payload["serial"], payload["deadline"])
        if payload["action"] == "navigate":
            xml = _navigate(device, payload["package"], payload["keyword"], payload["kind"], payload["deadline"], payload.get("cancel_file"), payload.get("navigation_dir"))
        else:
            xml = device.dump_hierarchy()
        print(json.dumps({"xml": xml}))
    except AndroidBlocked as exc:
        print(json.dumps({"blocked": str(exc)}))


def _is_bilibili_mode_notice(xml: str) -> bool:
    """识别启动时的模式介绍弹层，不把已开启的模式或解锁页当成可关闭提示。"""
    nodes = {node.get("resource-id"): node.get("text", "").strip()
             for node in ET.fromstring(xml).iter("node")
             if node.get("package") == PACKAGES["bilibili"]}
    prefix = PACKAGES["bilibili"] + ":id/"
    return (nodes.get(prefix + "title") in ("未成年人模式", "青少年模式")
            and nodes.get(prefix + "text2") in ("进入未成年人模式 >", "进入青少年模式 >")
            and nodes.get(prefix + "button") == "不再提醒")


def _navigate(device, package: str, keyword: str, kind: str, deadline: float, cancel_file: str | None = None,
              navigation_dir: str | None = None) -> str:
    """有明确控件才输入/搜索；不猜坐标、不处理登录或授权弹窗。"""
    _check(deadline, cancel_file)
    if device.app_current().get("package") != package:
        device.app_start(package, stop=False)
        time.sleep(min(0.8, max(0, deadline - time.monotonic())))
    xml = device.dump_hierarchy()
    if navigation_dir:
        with (Path(navigation_dir) / "navigation-before.xml").open("x", encoding="utf-8") as stream:
            stream.write(xml)
    if package == PACKAGES["bilibili"] and _is_bilibili_mode_notice(xml):
        # 实拍失败时只有介绍弹层在UI树内，首页搜索框被遮挡。只返回关闭一次；
        # 不点击“不再提醒”、不修改模式设置，也不处理密码/模式退出页面。
        _check(deadline, cancel_file)
        device.press("back")
        time.sleep(min(.3, max(0, deadline - time.monotonic())))
        xml = device.dump_hierarchy()
        if _is_bilibili_mode_notice(xml):
            raise AndroidBlocked("startup_notice_not_dismissed")
    blocker = detect_blocker(xml)
    # 非模态登录横幅只允许转入公开搜索输入；不点击“去登录”或处理登录弹窗。
    public_search = package == PACKAGES["xiaohongshu"] and "登录查看更多精彩内容" in xml and kind == "suggestions"
    if blocker and not public_search and blocker != "network_unavailable":
        raise AndroidBlocked(blocker)
    edit = device(className="android.widget.EditText", packageName=package)
    if not edit.exists:
        selectors = [{"description": "搜索"}, {"text": "搜索"}]
        if package == PACKAGES["bilibili"]:
            selectors.insert(0, {"resourceId": "tv.danmaku.bili:id/expand_search"})
            selectors.insert(0, {"resourceId": "tv.danmaku.bili:id/search_fake_text"})
        elif package == PACKAGES["xiaohongshu"]:
            # 混淆资源名不作依据；查询框须和明确的返回/搜索控件同属一行。
            from .android_results import xhs_query_nodes
            queries = xhs_query_nodes(ET.fromstring(xml))
            if queries:
                # 结果页的“搜索”是提交按钮，不是输入入口；已识别查询框时不退到该按钮。
                selectors = [{"text": n.get("text"), "className": n.get("class")} for n in queries]
        for selector in selectors:
            _check(deadline, cancel_file)
            control = device(packageName=package, **selector)
            if control.count == 1:
                control.click()
                break
        opened = edit.wait(timeout=min(5, max(0, deadline - time.monotonic())))
        if navigation_dir:
            with (Path(navigation_dir) / "navigation-after-entry.xml").open("x", encoding="utf-8") as stream:
                stream.write(device.dump_hierarchy())
        if not opened:
            raise AndroidBlocked("search_entry_not_identified")
    if edit.count != 1:
        raise AndroidBlocked("ambiguous_search_input")
    _check(deadline, cancel_file)
    edit.set_text(keyword)
    entered = edit.get_text()
    # 小红书本次实测无障碍文本带“搜索, ”前缀，原屏显示的是纯关键词。
    if package == PACKAGES["xiaohongshu"] and entered.startswith("搜索, "):
        entered = entered[len("搜索, "):]
    if entered != keyword:
        raise AndroidBlocked("keyword_input_not_verified")
    if kind == "results":
        _check(deadline, cancel_file)
        search = device(text="搜索", packageName=package)
        if search.count == 1:
            search.click()
        else:
            raise AndroidBlocked("search_submit_not_identified")
    time.sleep(min(1, max(0, deadline - time.monotonic())))
    xml = device.dump_hierarchy()
    ready_deadline = min(deadline, time.monotonic() + 5)
    while "正在加载数据" in xml and time.monotonic() < ready_deadline:
        _check(deadline, cancel_file)
        time.sleep(min(.5, max(0, ready_deadline - time.monotonic())))
        xml = device.dump_hierarchy()
    if package == PACKAGES["xiaohongshu"] and kind == "results" and not detect_blocker(xml):
        from .android_results import parse_page
        general = device(packageName=package, text="综合")
        if general.count == 1:
            _check(deadline, cancel_file)
            general.click()
            time.sleep(min(.5, max(0, deadline - time.monotonic())))
            xml = device.dump_hierarchy()
        # 同词重搜复用旧滚动位置，scroll.toBeginning实拍未证明回顶。
        # 最后一步在内容区有界下拖，必须看到列表头占位+完整首卡；不以悬浮综合栏判断起点。
        for attempt in range(9):
            _check(deadline, cancel_file)
            blocker = detect_blocker(xml)
            if blocker:
                raise AndroidBlocked(blocker)
            page = parse_page(xml, "xiaohongshu", "results", keyword)
            if page["start_verified"] and page["keyword_verified"] and all(i["bounds"][1] >= page["viewport"][1] for i in page["items"]):
                break
            if attempt == 8 or not page["viewport"][2]:
                raise AndroidBlocked("result_start_not_confirmed")
            x = int(page["viewport"][2] * .75)
            y1 = page["viewport"][1] + 200
            y2 = page["viewport"][3] - 160
            device.swipe(x, y1, x, y2, duration=.6)
            time.sleep(min(.5, max(0, deadline - time.monotonic())))
            xml = device.dump_hierarchy()
    return xml


def _dump(adb) -> str:
    """复用运行问题库方案：只读XML偶发断连最多重试一次，不重放导航。"""
    try:
        return _ui_call(adb.serial, adb.deadline, "dump", cancel_file=adb.cancel_file)
    except RuntimeError as exc:
        if isinstance(exc, (AndroidBlocked, AndroidCancelled)):
            raise
        _check(adb.deadline, adb.cancel_file)
        return _ui_call(adb.serial, adb.deadline, "dump", cancel_file=adb.cancel_file)


def _snapshot(adb, directory: Path, result: dict, index: int) -> dict:
    """保存XML前后快照与原始ADB PNG，并验证内容签名未跨截图改变。"""
    import hashlib
    from .android_results import parse_page, page_signature
    prefix = directory / "source" / f"frame-{index:03d}"
    prefix.parent.mkdir(exist_ok=True)
    paths = {}
    for label in ("before", "png", "after"):
        _check(adb.deadline, adb.cancel_file)
        if label == "png":
            path = prefix.with_suffix(".png")
            adb.screenshot(path)
        else:
            path = prefix.with_name(prefix.name + f"-{label}.xml")
            xml = _dump(adb)
            ET.fromstring(xml)
            with path.open("x", encoding="utf-8") as stream:
                stream.write(xml)
            paths[label + "_xml"] = xml
        paths[label] = str(path.resolve())
        result["source_artifact_paths"].append(paths[label])
        result["artifact_paths"].append(paths[label])
    before = parse_page(paths["before_xml"], result["platform"], result["capture_kind"], result["keyword"])
    after = parse_page(paths["after_xml"], result["platform"], result["capture_kind"], result["keyword"])
    stable = page_signature(before) == page_signature(after)
    transient = any(word in paths["before_xml"] or word in paths["after_xml"] for word in ("下拉支持刷新内容", "正在加载数据"))
    stable = stable and not transient
    frame = dict(png_path=paths["png"], xml_path=paths["before"], after_xml_path=paths["after"],
                 png_sha256=hashlib.sha256(Path(paths["png"]).read_bytes()).hexdigest(),
                 xml_sha256=hashlib.sha256(Path(paths["before"]).read_bytes()).hexdigest(),
                 stable=stable, page=before, source_index=index, accepted=False,
                 captured_at=datetime.now().astimezone().isoformat())
    result["frames"].append(frame)
    result["captured_at"] = frame["captured_at"]
    if not before["viewport"][2] or not after["viewport"][2]:
        raise AndroidBlocked("foreground_app_changed")
    blocker = detect_blocker(paths["after_xml"])
    # 下拉层后面的旧结果树可能仍含非模态登录横幅，只有识别到完整下拉层才忽略横幅。
    if blocker and not (blocker == "login_required" and result["capture_kind"] == "suggestions" and before["recognized"]):
        raise AndroidBlocked(blocker)
    return frame


def _suggestions(frame: dict, directory: Path, result: dict) -> None:
    """导出一张带原生搜索栏的下拉派生图；账号/商品仅保留在图中，单列排除原因。"""
    from PIL import Image
    page = frame["page"]
    result["items"] = [dict(item, rank=index + 1, source_mappings=[dict(
        frame_index=len(result["frames"]) - 1, png_path=frame["png_path"], xml_path=frame["xml_path"],
        bounds=item["bounds"])]) for index, item in enumerate(page["items"])]
    result.update(observed_count=len(page["items"]), excluded=page["excluded"],
                  rank_policy="text_suggestions_exclude_accounts_products")
    valid = frame["stable"] and page["recognized"] and page["keyword_verified"] and page["complete"]
    result["completion_evidence"] = dict(stable_pair=frame["stable"], keyword_verified=page["keyword_verified"],
                                        list_complete=page["complete"], end_marker=page["end_marker"])
    if not valid:
        result.update(status="partial", reason="suggestion_list_incomplete_or_unverified")
        return
    frame["accepted"] = True
    bottom = max(i["bounds"][3] for i in page["items"])
    if page["end_marker"]:
        bottom = max(bottom, page["end_marker"]["bounds"][3])
    output = directory / "derived" / "suggestions.png"
    output.parent.mkdir(exist_ok=True)
    with Image.open(frame["png_path"]) as original:
        image = original.crop((0, 0, original.width, min(original.height, bottom + 24)))
        with output.open("xb") as stream:
            image.save(stream, format="PNG")
    with Image.open(output) as verify:
        verify.verify()
    result["artifact_paths"].append(str(output.resolve()))
    result.update(primary_artifact_path=str(output.resolve()), status="success", reason="suggestions_complete",
                  rank_verified=True, outcome="complete")


def _swipe_forward(adb, page: dict, *, cautious: bool = False, remaining: int | None = None,
                   record: dict | None = None) -> dict:
    """在已确认可见内容区拖动，避开小红书底部提示和双列间隙。

    只生成导航动作，不以手势距离计算内容位移；真实位移仍由下一帧锚点证明。
    """
    _check(adb.deadline, adb.cancel_file)
    left, top, right, bottom = page["viewport"]
    width, height = right - left, bottom - top
    if width < 64 or height < 200:
        raise AndroidBlocked("no_visible_area_for_scroll")
    if page.get("platform") == "xiaohongshu" or cautious or page.get("occlusions"):
        x = left + int(width * .75)
    else:
        x = left + width // 2
    # 起点90%、终点10%，以有效内容区为基准，不让起点截断80%的目标行程。
    normal_delta = round(height * .8)
    delta = normal_delta
    visible_count = sum(bool(item.get("complete")) for item in page.get("items", []))
    boundary_limited = remaining is not None and remaining < max(2, visible_count)
    if boundary_limited:
        # 接近TOP N时依本帧卡片密度缩短；不是全局像素上限。
        delta = max(1, round(normal_delta * max(1, remaining) / max(2, visible_count)))
    y = top + round(height * .9)
    end_y = y - delta
    # 快速释放可让内容移动超过手势距离；复用原ADB通道，按距离延长时长而非缩短行程。
    # 450px/s是保守导航速度，不是实际位移保证；仍由下一帧验证内容位移和覆盖。
    duration_ms = max(600, math.ceil(delta * 1000 / 450))
    action = record if record is not None else {}
    action.update({"start": [x, y], "end": [x, end_y], "cautious": cautious,
            "viewport": list(page["viewport"]), "normal_target_px": normal_delta,
            "target_px": delta, "target_ratio": delta / height,
            "expected_overlap_ratio": 1 - delta / height,
            "remaining": remaining, "boundary_limited": boundary_limited,
            "gesture": "adb_distance_scaled_swipe", "duration_ms": duration_ms,
            "nominal_speed_px_s": delta * 1000 / duration_ms, "dispatch_status": "planned"})
    try:
        adb.run("shell", "input", "swipe", str(x), str(y), str(x), str(end_y), str(duration_ms))
    except Exception:
        action["dispatch_status"] = "interrupted_or_unknown"
        raise
    action["dispatch_status"] = "command_completed"
    return action


def _check_visible_covers(frame: dict, check=lambda: None) -> None:
    """小红书完整封面不足以判断已加载时暂停本帧，不把历史封面补入当前原图。

    XML稳定不代表图片已完成加载；内容统计只用于保守等待，不作为身份或排名证明。
    """
    from .android_image_evidence import assess_cover
    from .android_results import EvidenceError
    page = frame["page"]
    if page.get("platform") != "xiaohongshu":
        return
    details = []
    frame["cover_readiness"] = details
    for item in page["items"]:
        check()
        if not item.get("complete") or not item.get("cover_bounds"):
            continue
        evidence = assess_cover(frame["png_path"], item["cover_bounds"], fully_visible=True)
        details.append(dict(identity=item["identity"], **evidence))
        if evidence["status"] != "content_visible":
            raise EvidenceError("cover_content_unverified")


def _collect(adb, directory: Path, result: dict, request: dict) -> None:
    """推进连续帧采集，排名、像素衔接、边界三项验证共同决定成功。"""
    from .android_results import RankLedger, EvidenceError, render_long
    strict = request.get("capture_mode", "visual_scrollshot") == "strict_evidence"
    ledger = RankLedger(result["requested_top_n"], strict=strict)
    check = lambda: _check(adb.deadline, adb.cancel_file)
    result.update(status="partial", reason="capture_started", observed_count=0)
    stable_retries = 0
    pixel_retries = 0
    stationary_attempts = 0
    cover_waits = 0
    last_scroll = None
    try:
        for _ in range(request.get("max_frames", 80)):
            check()
            phase("screenshot", f"APP原始截图与控件快照：第{len(result['frames']) + 1}帧")
            frame = _snapshot(adb, directory, result, len(result["frames"]))
            if not frame["stable"]:
                stable_retries += 1
                if stable_retries >= 3:
                    raise EvidenceError("page_kept_changing_during_screenshot")
                time.sleep(min(.5, max(0, adb.deadline - time.monotonic())))
                continue
            stable_retries = 0
            phase("validate_capture", f"APP内容与连续性校验：已确认{len(ledger.items)}条")
            if result["capture_kind"] == "suggestions":
                _suggestions(frame, directory, result)
                return
            try:
                # 视觉拼接也须等待完整封面加载；XML稳定本身不足以排除实拍中的白色占位。
                _check_visible_covers(frame, check)
                ledger.add(frame, check)
            except EvidenceError as exc:
                frame["rejection_reason"] = str(exc)
                if str(exc) == "cover_content_unverified":
                    cover_waits += 1
                    if cover_waits > 2:
                        raise EvidenceError("cover_loading_not_confirmed_within_budget") from exc
                    # 原地等两次，不滚动越过缺图，也不放宽重复身份的原始封面hash验证。
                    time.sleep(min(1, max(0, adb.deadline - time.monotonic())))
                    continue
                if str(exc) == "no_scroll_progress":
                    stationary_attempts += 1
                    if stationary_attempts > 2:
                        raise EvidenceError("scroll_stalled_without_verified_end") from exc
                    # 提示消失可能消耗一次手势；不把重复帧加进排名，不以静止判到底。
                    last_scroll = _swipe_forward(adb, frame["page"], cautious=True,
                        remaining=max(0, ledger.top_n - len(ledger.items)), record=frame.setdefault("next_scroll", {}))
                    time.sleep(min(.6, max(0, adb.deadline - time.monotonic())))
                    continue
                # 瞬时提示/封面迟到先保存问题源帧，再原地等待一次；不继续滚动掩盖缝隙。
                if str(exc).startswith("pixel_overlap_mismatch") and pixel_retries < 2:
                    pixel_retries += 1
                    time.sleep(min(1, max(0, adb.deadline - time.monotonic())))
                    continue
                raise
            pixel_retries = 0
            stationary_attempts = 0
            cover_waits = 0
            if len(ledger.frames) > 1:
                previous, link = ledger.frames[-2], ledger.links[-1]
                source_height = previous["page"]["viewport"][3] - previous["page"]["viewport"][1]
                frame["scroll_measurement"] = dict(previous_frame=previous["source_index"],
                    gesture_target_px=(last_scroll or {}).get("target_px"),
                    gesture_viewport=(last_scroll or {}).get("viewport"),
                    gesture_dispatch_status=(last_scroll or {}).get("dispatch_status"),
                    verified_content_shift_px=link["shift"], verified_overlap_px=link["overlap"],
                    source_viewport_height=source_height, content_shift_ratio=link["shift"] / source_height,
                    observed_overlap_ratio=link["overlap"] / source_height)
            result["items"] = ledger.items[:ledger.top_n]
            result.update(observed_count=len(result["items"]), excluded=ledger.excluded, unknown_regions=ledger.unknown)
            if strict and any(not region.get("edge") for region in frame["page"]["unknown"]):
                # 完整未知模块靠继续翻页不会获得语义，不浪费总预算，也不跳过它报成功。
                last = ledger.items[ledger.top_n - 1] if len(ledger.items) >= ledger.top_n else None
                cutoff = last["content_bounds"][1 if frame["page"].get("reading_order") == "visible_top_y_then_x" else 3] if last else float("inf")
                if any(not region.get("edge") and region["bounds"][1] + frame["offset"] < cutoff for region in frame["page"]["unknown"]):
                    raise EvidenceError("unclassified_full_module_before_target")
            marker = frame["page"]["end_marker"]
            count = min(len(ledger.items), ledger.top_n)
            if ledger.target_ready() or (marker and ledger.boundary_clear(count)):
                derived = render_long(ledger, directory / "derived" / "results-long.png", count, check)
                result["artifact_paths"].append(derived["path"])
                result.update(primary_artifact_path=derived["path"], long_image=derived,
                              rank_verified=True, status="success", reason="top_n_verified" if count == ledger.top_n else "explicit_end_of_results",
                              outcome="complete" if count == ledger.top_n else "insufficient_results",
                              completion_evidence=dict(start="fresh_search_submit", end_marker=marker,
                              verified_frame_count=len(ledger.frames), ranked_count=count))
                return
            past_gaps = [region for region in ledger.unresolved_regions(count)
                         if region["bounds"][3] + region["offset"] <= frame["page"]["viewport"][1] + frame["offset"]]
            if past_gaps:
                # 缺证片段已离开当前视口，继续前滚只会扩大缺口，保留现场供离线定位。
                frame["unresolved_past_regions"] = past_gaps
                raise EvidenceError("unresolved_content_gap_before_viewport")
            # 80%是手势目标而非位移证据；接近目标缩短，下一帧仍验证实际重叠与排名。
            last_scroll = _swipe_forward(adb, frame["page"], remaining=max(0, ledger.top_n - len(ledger.items)),
                record=frame.setdefault("next_scroll", {}))
            time.sleep(min(.6, max(0, adb.deadline - time.monotonic())))
        raise EvidenceError("max_frames_reached_without_verified_boundary")
    finally:
        if result["capture_kind"] == "results":
            result["items"] = ledger.items[:ledger.top_n]
            result.update(observed_count=len(result["items"]), rank_links=ledger.links,
                          excluded=ledger.excluded, unknown_regions=ledger.unknown)


def capture(request: dict) -> dict:
    """统一APP采集接口；新增主产物/源证据/条目映射，旧字典字段保持兼容。

    支持正整数top_n、正有限timeout_seconds（如180至1800）、绝对cancel_file。
    同设备互斥由主runner负责；本适配器不connect设备、不登录、不创建模拟器。
    输出目录每次新建，不覆盖历史。只有primary_artifact_path用于导出和汇总。
    """
    result = {
        "platform": request.get("platform"), "client": "APP",
        "capture_kind": request.get("capture_kind"), "keyword": request.get("keyword"),
        "status": "failed", "reason": "", "requested_top_n": request.get("top_n", 20),
        "observed_count": None, "rank_verified": False, "artifact_paths": [],
        "primary_artifact_path": None, "source_artifact_paths": [], "items": [], "frames": [],
        "rank_policy": "independent_content_cards", "rank_order": "visible_top_y_then_x",
        "capture_mode": request.get("capture_mode", "visual_scrollshot"), "outcome": None,
        "captured_at": datetime.now().astimezone().isoformat(), "source_url": None, "device": {},
    }
    directory = None
    adb = None
    try:
        if result["platform"] not in PACKAGES:
            raise ValueError("invalid_platform")
        if result["capture_kind"] not in ("results", "suggestions"):
            raise ValueError("invalid_capture_kind")
        if not isinstance(result["keyword"], str) or not result["keyword"].strip():
            raise ValueError("keyword_required")
        n = result["requested_top_n"]
        if isinstance(n, bool) or not isinstance(n, int) or n <= 0:
            raise ValueError("top_n_must_be_positive_integer")
        capture_mode = request.get("capture_mode", "visual_scrollshot")
        if capture_mode not in ("visual_scrollshot", "strict_evidence"):
            raise ValueError("capture_mode_invalid")
        timeout = request.get("timeout_seconds", 180)
        if isinstance(timeout, bool) or not isinstance(timeout, (float, int)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout_seconds_must_be_positive_finite")
        max_frames = request.get("max_frames", 80)
        if isinstance(max_frames, bool) or not isinstance(max_frames, int) or max_frames <= 0:
            raise ValueError("max_frames_must_be_positive_integer")
        cancel_file = request.get("cancel_file")
        if cancel_file is not None and (not isinstance(cancel_file, (str, Path)) or not Path(cancel_file).is_absolute()):
            raise ValueError("cancel_file_must_be_absolute")
        cancel_file = str(cancel_file) if cancel_file else None
        deadline = time.monotonic() + timeout
        _check(deadline, cancel_file)
        output = Path(request.get("output_dir", ""))
        if not output.is_absolute():
            raise ValueError("output_dir_must_be_absolute")
        directory = output / (datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:10])
        directory.mkdir(parents=True, exist_ok=False)
        adb = AdbClient(deadline, cancel_file=cancel_file)
        result["device"] = adb.discover(request.get("serial"))
        phase("device_ready", "Android设备已就绪")
        package = PACKAGES[result["platform"]]
        result["device"].update(package=package, package_installed=adb.installed(package))
        if not result["device"]["package_installed"]:
            raise AndroidBlocked("package_not_installed: " + package)
        phase("prepare_search", "进入APP搜索并确认关键词及结果起点")
        _ui_call(adb.serial, deadline, "navigate", cancel_file=cancel_file,
                 package=package, keyword=result["keyword"], kind=result["capture_kind"], navigation_dir=str(directory))
        _collect(adb, directory, result, request)
        _check(deadline, cancel_file)
    except AndroidCancelled as exc:
        result.update(status="cancelled", reason=str(exc), rank_verified=False, primary_artifact_path=None)
    except AndroidBlocked as exc:
        result.update(status="partial" if str(exc) == "network_unavailable" else "blocked", reason=str(exc), rank_verified=False)
    except (TimeoutError, subprocess.TimeoutExpired) as exc:
        result.update(status="partial", reason="capture_timeout: " + str(exc)[:200], rank_verified=False)
    except Exception as exc:
        from .android_results import EvidenceError
        result.update(status="partial" if isinstance(exc, EvidenceError) or result["source_artifact_paths"] else "failed", reason=f"{type(exc).__name__}: {exc}", rank_verified=False)
    if directory is not None:
        # 导航阶段失败仍保存诊断PNG/XML；取消或预算耗尽后不再发送设备命令。
        if adb is not None and adb.serial and not result["source_artifact_paths"] and result["status"] != "cancelled":
            errors = []
            for kind in ("png", "xml"):
                try:
                    _check(adb.deadline, cancel_file)
                    path = directory / ("screen.png" if kind == "png" else "hierarchy.xml")
                    if kind == "png":
                        adb.screenshot(path)
                    else:
                        xml = _ui_call(adb.serial, adb.deadline, "dump", cancel_file=cancel_file)
                        ET.fromstring(xml)
                        with path.open("x", encoding="utf-8") as stream:
                            stream.write(xml)
                    result["source_artifact_paths"].append(str(path.resolve()))
                    result["artifact_paths"].append(str(path.resolve()))
                except Exception as exc:
                    errors.append(f"{kind}: {exc}")
            if errors:
                result["evidence_errors"] = errors
        for path in sorted(directory.glob("navigation-*.xml")):
            result["artifact_paths"].append(str(path.resolve()))
            result["source_artifact_paths"].append(str(path.resolve()))
        report = directory / "result.json"
        result["artifact_paths"].append(str(report.resolve()))
        with report.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
    return result
