"""显式命令行入口：预览、手动执行/恢复、定时服务和桌面配置。"""

import argparse
import json
import threading
from datetime import datetime
from pathlib import Path

from .config import Plan, load_plan, save_plan
from .scheduling import TIMEZONE, daily_slots
from .paths import default_config_path


def main() -> int:
    parser = argparse.ArgumentParser(description="关键词截图：原生APP模拟器与WEB")
    parser.add_argument("action", choices=["preview","run","watch","history","gui","init-config","devices"])
    parser.add_argument("--config", default=str(default_config_path()))
    parser.add_argument("--resume-batch", default=None)
    args = parser.parse_args()
    if args.action == "devices":
        # 设备发现无需关键词或计划配置；不进入任何APP或WEB平台页面。
        import os
        from .android import discover_devices
        try:
            devices = discover_devices()
            print(json.dumps({"status":"success", "devices":devices,
                              "adb_path":os.environ.get("ADBUTILS_ADB_PATH"),
                              "platform_requests":0}, ensure_ascii=False, indent=2))
            return 0
        except Exception as exc:
            print(json.dumps({"status":"blocked", "reason":str(exc), "platform_requests":0}, ensure_ascii=False))
            return 2
    if args.action == "gui":
        from .gui import main as gui_main
        return gui_main(config_path=args.config)
    if args.action == "init-config":
        if Path(args.config).exists():
            parser.error("配置文件已存在，未覆盖")
        save_plan(Plan(), args.config)
        print(f"配置已保存：{Path(args.config).resolve()}")
        return 0
    plan = load_plan(args.config)
    if args.action == "history":
        from .storage import list_history
        print(json.dumps(list_history(plan.output_root), ensure_ascii=False, indent=2))
        return 0
    if args.action == "preview":
        from .export_plan import preflight_plan
        result = {"config":plan.to_dict(), "today_slots":[t.isoformat() for t in daily_slots(datetime.now(TIMEZONE).date(),plan.schedule)],
                  "tasks_per_batch":len(plan.keywords)*len(plan.platforms)*len(plan.clients)*len(plan.capture_kinds),
                  "exports":preflight_plan(plan,probe_writable=False)}
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 0 if result["exports"]["ok"] else 2
    if args.action == "run":
        from .runner import run_plan
        result=run_plan(plan,resume_batch_id=args.resume_batch)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 0 if result["status"]=="success" else 2
    from .service import ScheduleService
    def progress(data):
        if data["event"] in ("schedule_started","schedule_missed","schedule_result","schedule_error","schedule_stopped"):
            print(json.dumps(data,ensure_ascii=False),flush=True)
    service=ScheduleService(plan,progress_callback=progress)
    service.start()
    try:
        while service.is_running:
            threading.Event().wait(.2)
    except KeyboardInterrupt:
        print("已请求停止计划并取消当前批次，等待安全退出。",flush=True)
    finally:
        service.stop(wait=True,cancel_running=True)
    return 2 if service.last_error else 0


if __name__=="__main__":
    raise SystemExit(main())
