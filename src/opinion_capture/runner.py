"""持久化批次执行：资源互斥、取消、有限重试、恢复和精确导出。"""

from __future__ import annotations

import hashlib
import importlib
import json
import logging
import threading
import time
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path

from PIL import Image

from .config import Plan
from .exporting import artifact_values, copy_immutable, export_path, keyword_id
from .locks import ResourceBusy, ResourceLock
from .scheduling import TIMEZONE
from .storage import Store

STATUSES = {"success", "partial", "blocked", "failed", "cancelled"}
LOGGER = logging.getLogger(__name__)


def plan_fingerprint(plan: Plan) -> str:
    """配置快照稳定指纹；同一瞬间不因时区表示不同而创建第二批。"""
    return hashlib.sha256(json.dumps(plan.to_dict(), sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def trigger_key(plan: Plan, planned: datetime) -> str:
    return f"{plan_fingerprint(plan)}:{planned.astimezone(TIMEZONE).isoformat()}"


def _adapter(request: dict) -> dict:
    if request["client"] == "WEB":
        from .existing_browser import capture
        return capture(request)
    name = "android" if request["client"] == "APP" else "web_" + request["platform"]
    return importlib.import_module(f"opinion_capture.{name}").capture(request)


def _write_json(path: Path, data: dict) -> None:
    """排他写入新版本清单，旧尝试和旧报告不覆盖。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def _emit(callback, event: str, **fields):
    """UI观察者异常不破坏已完成的采集和数据库事务。"""
    if callback:
        try:
            callback({"event": event, **fields})
        except Exception:
            LOGGER.exception("进度观察者异常")


def overall_status(results: list[dict]) -> str:
    statuses = [r["status"] for r in results]
    if "cancelled" in statuses:
        return "cancelled"
    if statuses and all(s == "success" for s in statuses):
        return "success"
    if any(s in ("success", "partial") for s in statuses):
        return "partial"
    return "blocked" if statuses and all(s == "blocked" for s in statuses) else "failed"


def _hash_file(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _verified_history(result: dict) -> bool:
    """恢复只复用有完整哈希对应的成功原图，不因DB状态绿色而盲信。"""
    if result.get("status") != "success" or not result.get("exports"):
        return False
    try:
        return all(Path(a["path"]).is_file() and _hash_file(Path(a["path"])) == a["sha256"] for a in result["exports"])
    except (OSError, KeyError):
        return False


class _CancelBridge:
    """将线程取消事件映射为适配器可读取的本次尝试标记文件。"""

    def __init__(self, event, path: Path):
        self.event, self.path = event, path
        self.done = threading.Event()
        self.thread = None

    def __enter__(self):
        def watch():
            while not self.done.wait(0.05):
                if self.event.is_set():
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    self.path.touch(exist_ok=True)
                    return
        self.thread = threading.Thread(target=watch, name="capture-cancel-bridge", daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.done.set()
        self.thread.join(timeout=1)


class _PhaseMonitor:
    """只读取当前任务阶段文件，后台更新GUI；不影响平台请求和任务终态。"""

    def __init__(self, path, callback, fields):
        self.path, self.callback, self.fields = path, callback, fields
        self.done = threading.Event()
        self.thread = None

    def __enter__(self):
        if self.callback is not None:
            self.thread = threading.Thread(target=self._watch, daemon=True, name="capture-phase-monitor")
            self.thread.start()
        return self

    def _watch(self):
        last_code, last_emit = None, 0.0
        while not self.done.is_set():
            try:
                record = json.loads(self.path.read_text(encoding="utf-8"))
                now = time.monotonic()
                if record["phase_code"] != last_code or now-last_emit >= 2:
                    _emit(self.callback, "phase", **self.fields, status="running",
                          phase_code=record["phase_code"], phase_text=record["phase_text"],
                          elapsed_seconds=round(max(0, now-record["started_monotonic"]), 1),
                          phase_elapsed_seconds=round(max(0, now-record["phase_monotonic"]), 1))
                    last_code, last_emit = record["phase_code"], now
            except (OSError, ValueError, KeyError, TypeError):
                pass
            self.done.wait(.2)

    def __exit__(self, *_):
        self.done.set()
        if self.thread is not None:
            self.thread.join(timeout=1)


def _base_result(task: dict, plan: Plan) -> dict:
    return {k: task[k] for k in ("platform", "client", "capture_kind", "keyword")} | {
        "requested_top_n": plan.top_n, "rank_verified": False, "rank_policy": plan.rank_policy,
        "artifact_paths": [], "observed_count": None, "exports": [], "task_id": task["id"]}


def _validate_result(result: dict, task: dict, plan: Plan) -> datetime:
    for field in ("platform", "client", "capture_kind", "keyword"):
        if result.get(field) != task[field]:
            raise ValueError(f"适配器结果字段不一致：{field}")
    if result.get("status") not in STATUSES:
        raise ValueError("适配器返回未知状态")
    actual = datetime.fromisoformat(result["captured_at"])
    if actual.tzinfo is None:
        raise ValueError("实际截图时间缺少时区")
    if task["capture_kind"] == "results" and result["status"] == "success":
        count = result.get("observed_count")
        sufficient = type(count) is int and count >= plan.top_n
        insufficient = type(count) is int and count >= 0 and result.get("outcome") == "insufficient_results"
        if result.get("rank_verified") is not True or not (sufficient or insufficient):
            result["status"] = "partial"
            result["reason"] = str(result.get("reason", "")) + ";TOP N顺序或范围尚未严格验证"
    return actual


def _export_primary(plan: Plan, store: Store, batch: dict, task: dict, attempt: dict,
                    workdir: Path, result: dict, actual: datetime):
    """只导出适配器声明的最终图；源帧/诊断图保留在证据目录，不混入汇总。"""
    if result["status"] not in ("success", "partial"):
        result["exports"] = []
        return
    primary = result.get("primary_artifact_path")
    pngs = [p for p in result.get("artifact_paths", []) if str(p).lower().endswith(".png")]
    if "primary_artifact_path" not in result and not primary and len(pngs) == 1:
        primary = pngs[0]  # 兼容第一版单屏适配器
    if not primary:
        if "primary_artifact_path" in result or pngs:
            result["status"] = "partial"
            if len(pngs) > 1:
                result["reason"] = str(result.get("reason", "")) + ";multiple_frames_without_primary_image"
            elif not result.get("reason"):
                result["reason"] = "primary_image_missing"
            result["exports"] = []
            return
        raise ValueError("有效结果缺少可验证的最终PNG")
    source = Path(primary).resolve()
    if not source.is_relative_to(workdir.resolve()) or source.suffix.lower() != ".png":
        raise ValueError("最终PNG越出本任务证据目录或格式无效")
    with Image.open(source) as image:
        if image.format != "PNG":
            raise ValueError("原图内容并非PNG")
        image.verify()
    fields = artifact_values(project=plan.project, platform=task["platform"], client=task["client"],
        capture_kind=task["capture_kind"], keyword=task["keyword"], captured_at=actual,
        top_n=plan.top_n, batch=batch["sequence"], artifact_id=attempt["id"][:12],
        partial=result["status"] != "success")
    destination = export_path(plan.output_root, plan.directory_template, plan.filename_template, fields)
    item = copy_immutable(source, destination)
    store.add_artifact(task["id"], item, actual.isoformat(), attempt["id"])
    result["exports"], result["primary_artifact_path"] = [item], str(source)


def _retry_allowed(result: dict) -> bool:
    """仅对暂时采集故障重新搜索；终态、认证阻断和证据规则问题不盲重试。"""
    if result.get("status") not in ("failed", "partial"):
        return False
    if "retryable" in result:
        return result["retryable"] is True
    if result.get("outcome") == "timeout":
        return True
    reason = str(result.get("reason", "")).lower()
    return any(token in reason for token in (
        "timeouterror", "connectionerror", "connection_error", "remote disconnected",
        "capture_timeout", "browser_response_timeout", "capture_deadline_exceeded", "capture_total_deadline_exceeded",
        "loading_timeout", "scroll_stalled_without", "cover_loading_not_confirmed_within_budget",
        "page_kept_changing_during_screenshot",
    ))


def _run_task(plan, store, batch, task, cancel_event, callback, completed, total, collector):
    base = _base_result(task, plan)
    for retry in range(plan.max_retries + 1):
        if cancel_event.is_set():
            return base | {"status": "cancelled", "reason": "cancelled_before_capture", "captured_at": datetime.now(TIMEZONE).isoformat()}
        workdir = Path(plan.output_root).resolve() / ".capture-state" / "runs" / batch["id"] / task["id"] / uuid.uuid4().hex
        workdir.mkdir(parents=True, exist_ok=True)
        attempt = store.begin_attempt(task["id"], str(workdir))
        request = {k: task[k] for k in ("platform", "client", "capture_kind", "keyword")} | {
            "top_n": plan.top_n, "rank_policy": plan.rank_policy, "capture_mode": plan.capture_mode,
            "batch_id": batch["id"],
            "attempt_number": attempt["number"], "retry_number": retry,
            "fresh_search": attempt["number"] > 1,
            "output_dir": str(workdir), "timeout_seconds": plan.capture_timeout_seconds,
            "cancel_file": str(workdir / "cancel.request"), "phase_file":str(workdir / "phase.json")}
        if plan.android_serial:
            request["serial"] = plan.android_serial
        if task["client"] == "WEB":
            request["browser_connection_mode"] = getattr(plan, "browser_connection_mode", "dedicated")
            request["browser_endpoint"] = getattr(plan, "browser_endpoint", "auto")
            request["browser_headless"] = getattr(plan, "browser_headless", False)
        if task["client"] == "WEB" and request["browser_connection_mode"] != "existing_chrome":
            from .sessions import profile_directory
            request["user_data_dir"] = str(profile_directory(plan, task["platform"]))
        _emit(callback, "task_started", batch_id=batch["id"], task_id=task["id"], completed=completed,
              total=total, attempt=attempt["number"], status="running",
              **{k: task[k] for k in ("platform", "client", "capture_kind", "keyword")})
        retryable = False
        try:
            # APP未指定serial时用全局自动选择锁，显式serial另加全局自动锁以免交叉漏锁。
            key = ("android-device-control" if task["client"] == "APP" else
                   "existing-chrome-control" if request["browser_connection_mode"] == "existing_chrome" else
                   "browser-profile:" + request["user_data_dir"].casefold())
            phase_fields = {"batch_id":batch["id"], "task_id":task["id"],
                            **{k:task[k] for k in ("platform","client","capture_kind","keyword")}}
            _emit(callback, "phase", **phase_fields, status="running", phase_code="waiting_resource",
                  phase_text="等待当前设备或浏览器可用", elapsed_seconds=0)
            with ResourceLock(key, timeout=plan.capture_timeout_seconds, cancel_event=cancel_event), \
                 _PhaseMonitor(Path(request["phase_file"]), callback, phase_fields):
                if cancel_event.is_set():
                    result = base | {"status":"cancelled", "reason":"cancelled_before_dispatch", "captured_at":datetime.now(TIMEZONE).isoformat()}
                else:
                    with _CancelBridge(cancel_event, Path(request["cancel_file"])):
                        from .progress import phase, phase_scope
                        with phase_scope(request) as stages:
                            phase("capture_start", "开始采集任务")
                            result = dict(collector(request))
                        result.setdefault("stage_timings", stages.timings)
            if cancel_event.is_set():
                result["status"], result["reason"] = "cancelled", "cancelled_during_capture"
            actual = _validate_result(result, task, plan)
            result["task_id"], result["attempt_id"] = task["id"], attempt["id"]
            _export_primary(plan, store, batch, task, attempt, workdir, result, actual)
            result["has_deliverable"] = bool(result.get("exports"))
            if result["status"] == "partial" and not result["has_deliverable"]:
                result.update(status="failed", outcome=result.get("outcome") or "incomplete",
                              completion_detail="未产生最终截图，保留诊断证据")
            retryable = _retry_allowed(result)
        except ResourceBusy as exc:
            result = base | {"status": "cancelled" if cancel_event.is_set() else "blocked",
                             "reason": str(exc), "captured_at": datetime.now(TIMEZONE).isoformat()}
        except Exception as exc:
            result = base | {"status": "cancelled" if cancel_event.is_set() else "failed",
                             "reason": f"{type(exc).__name__}: {exc}", "captured_at": datetime.now(TIMEZONE).isoformat()}
            retryable = not cancel_event.is_set() and isinstance(exc, (TimeoutError, ConnectionError))
        result["attempt_id"] = attempt["id"]
        scheduled_retry = retryable and retry < plan.max_retries and not cancel_event.is_set()
        result.update(attempt_number=attempt["number"], retry_number=retry,
                      fresh_search=request["fresh_search"], retryable=retryable,
                      retry_scheduled=scheduled_retry,
                      retry_exhausted=retryable and retry >= plan.max_retries,
                      retry_scope="same_keyword_from_start")
        _write_json(workdir / "attempt-summary.json", result)
        store.complete_attempt(attempt["id"], task["id"], result)
        if scheduled_retry:
            _emit(callback, "task_retry", batch_id=batch["id"], task_id=task["id"],
                  completed=completed, total=total, status="retrying", reason=result["reason"],
                  keyword=task["keyword"], attempt=attempt["number"] + 1,
                  retry_number=retry + 1, max_retries=plan.max_retries,
                  retry_scope="same_keyword_from_start")
            cancel_event.wait(plan.retry_delay_seconds)
            continue
        return result
    raise AssertionError("尝试循环不应到达末尾")


def _make_composites(plan, store, batch, results, run_id, callback):
    """按实际日期/平台/端/种类分组，每组按冻结关键词序列保留缺失占位。"""
    from .imaging import compose_pages
    composites = []
    for platform in plan.platforms:
        for client in plan.clients:
            for kind in plan.capture_kinds:
                rows = [r for r in results if (r["platform"], r["client"], r["capture_kind"]) == (platform, client, kind)]
                dated = {}
                for r in rows:
                    if len(r.get("exports", [])) == 1:
                        actual = datetime.fromisoformat(r["captured_at"]).astimezone(TIMEZONE)
                        dated.setdefault(actual.date().isoformat(), []).append((r, actual))
                for day, available in dated.items():
                    by_keyword = {r["keyword"]: (r, at) for r, at in available}
                    actual = min(at for _, at in available)
                    members, paths = [], []
                    for keyword in plan.keywords:
                        entry = by_keyword.get(keyword)
                        row = entry[0] if entry else next((r for r in rows if r["keyword"] == keyword), {})
                        paths.append(entry[0]["exports"][0]["path"] if entry else None)
                        members.append({"keyword": keyword, "keyword_id": keyword_id(keyword),
                            "status": row.get("status", "missing") if entry else "missing",
                            "reason": row.get("reason", "") if entry else "本日期分组没有有效截图"})
                    complete = all(m["status"] == "success" for m in members)
                    group_id = hashlib.sha256(f"{run_id}:{platform}:{client}:{kind}:{day}".encode()).hexdigest()[:10]
                    def output_factory(part):
                        fields = artifact_values(project=plan.project, platform=platform, client=client,
                            capture_kind=kind, keyword="全部关键词", captured_at=actual, top_n=plan.top_n,
                            batch=batch["sequence"], artifact_id=f"{group_id}-{part:02d}", part=part, partial=not complete)
                        return export_path(plan.output_root, plan.composite_directory_template, plan.composite_filename_template, fields)
                    try:
                        pages = compose_pages(paths, plan.output_root, members=members,
                            layout=plan.composite_layout, columns=plan.composite_columns,
                            cell_width=plan.composite_cell_width, max_pixels=plan.composite_max_pixels,
                            max_height=plan.composite_max_height, output_path_factory=output_factory)
                        for page in pages["pages"]:
                            page.update(platform=platform, client=client, capture_kind=kind, actual_date=day,
                                status="success" if complete else "partial",
                                keywords=[m["keyword"] for m in members],
                                missing_keywords=[m["keyword"] for m, path in zip(members, paths) if path is None],
                                incomplete_keywords=[m["keyword"] for m in members if m["status"] != "success"],
                                captured_at_range=[min(at for _, at in available).isoformat(), max(at for _, at in available).isoformat()])
                            page["sha256"] = _hash_file(Path(page["path"]))
                            _write_json(Path(page["path"]).with_suffix(".json"), page)
                            store.add_composite(batch["id"], page)
                            composites.append(page)
                    except Exception as exc:
                        published = getattr(exc, "published_pages", [])
                        composites.append({"platform":platform,"client":client,"capture_kind":kind,
                            "actual_date":day,"status":"failed","reason":str(exc),
                            "published_paths":[p["path"] for p in published],
                            "failed_part":getattr(exc,"failed_part",None),
                            "planned_pages":getattr(exc,"planned_pages",None)})
                        for page in published:
                            page.update(platform=platform,client=client,capture_kind=kind,actual_date=day,
                                status="partial",reason="page_group_incomplete: "+str(exc))
                            page["sha256"] = _hash_file(Path(page["path"]))
                            _write_json(Path(page["path"]).with_suffix(".json"),page)
                            store.add_composite(batch["id"],page)
                            composites.append(page)
    _emit(callback, "composites_finished", batch_id=batch["id"], page_count=len(composites))
    return composites


def run_plan(plan: Plan, *, scheduled_at: datetime | None = None, collector=None,
             cancel_event=None, progress_callback=None, resume_batch_id: str | None = None) -> dict:
    """执行或恢复冻结批次；活动OS锁未释放时返回busy，绝不依据超时猜测进程已死。"""
    plan = Plan.from_dict(plan.to_dict())
    planned = scheduled_at or datetime.now(TIMEZONE)
    if planned.tzinfo is None:
        raise ValueError("计划时间需要时区")
    planned = planned.astimezone(TIMEZONE)
    event = cancel_event if cancel_event is not None else threading.Event()
    state = Path(plan.output_root).resolve() / ".capture-state"
    with ResourceLock("database-init:" + str(state).casefold(), timeout=30):
        store = Store(state / "history.sqlite3")
    try:
        if resume_batch_id:
            batch = store.batch(resume_batch_id)
            plan = Plan.from_dict(json.loads(batch["config_json"]))
            planned, fresh = datetime.fromisoformat(batch["planned_at"]), False
            batch_id = resume_batch_id
        else:
            batch_id, fresh = store.create_batch(trigger_key(plan, planned), planned, plan.to_dict(),
                                                 kind="scheduled" if scheduled_at else "manual")
            batch = store.batch(batch_id)
        was_started = bool(batch.get("started_at"))
        if not fresh and not resume_batch_id and batch["status"] not in ("queued", "running"):
            return {"batch_id":batch_id,"status":batch["status"],"skipped_duplicate":True,"report_path":batch.get("report_path")}
        batch_lock = ResourceLock("batch:" + str(store.path).casefold() + ":" + batch_id)
        try:
            batch_lock.acquire()
        except ResourceBusy:
            return {"batch_id":batch_id,"status":"running","busy":True,"reason":"batch_owned_by_live_process"}
        try:
            from .export_plan import preflight_plan
            store.recover_inflight(batch_id)
            verified_existing = []
            for saved_task in store.tasks(batch_id):
                saved = json.loads(saved_task["result_json"]) if saved_task["result_json"] else {}
                if _verified_history(saved):
                    verified_existing.extend(a["path"] for a in saved["exports"])
            preflight = preflight_plan(plan, planned_at=planned, probe_writable=True,
                batch_number=batch["sequence"], verified_existing_paths=verified_existing)
            if not preflight["ok"]:
                reason = "; ".join(e.get("message", str(e)) if isinstance(e, dict) else str(e) for e in preflight["errors"])
                store.finish_batch(batch_id, "failed", reason=reason)
                return {"batch_id":batch_id,"status":"failed","reason":"export_preflight_failed: " + reason,"preflight":preflight}
            store.start_batch(batch_id)
            batch = store.batch(batch_id)
            ordinal = 0
            for platform in plan.platforms:
                for client in plan.clients:
                    for keyword in plan.keywords:
                        for kind in plan.capture_kinds:
                            store.add_task(batch_id, platform, client, kind, keyword, keyword_id(keyword), ordinal)
                            ordinal += 1
            tasks, results = store.tasks(batch_id), []
            tasks.sort(key=lambda t: (plan.platforms.index(t["platform"]), plan.clients.index(t["client"]),
                                      plan.keywords.index(t["keyword"]), plan.capture_kinds.index(t["capture_kind"])))
            # 一个会话的登录/风控门槛不随关键词变化。当前批次只请求一次，
            # 其余同平台同端任务直接留阻塞记录；不影响另一平台或APP/WEB会话。
            blocked_sessions = {}
            session_block_reasons = {"login_required", "login_redirect", "access_restricted",
                                     "verification_required", "captcha_required",
                                     "captcha_or_security_challenge", "consent_required",
                                     "chrome_connection_not_enabled", "chrome_connection_configuration_invalid",
                                     "chrome_connection_failed", "browser_not_installed", "search_input_not_ready",
                                     "keyword_input_not_visible"}
            _emit(progress_callback, "batch_started", batch_id=batch_id, completed=0, total=len(tasks), status="running")
            for task in tasks:
                previous = json.loads(task["result_json"]) if task["result_json"] else {}
                if _verified_history(previous):
                    result = previous
                    _emit(progress_callback, "task_reused", batch_id=batch_id, task_id=task["id"], completed=len(results)+1, total=len(tasks), status="success",
                          **{k: task[k] for k in ("platform", "client", "capture_kind", "keyword")})
                elif event.is_set():
                    result = _base_result(task, plan) | {"status":"cancelled","reason":"cancelled_before_capture","captured_at":datetime.now(TIMEZONE).isoformat()}
                    store.finish_task(task["id"], result)
                elif (task["platform"], task["client"]) in blocked_sessions:
                    blocker = blocked_sessions[(task["platform"], task["client"])]
                    result = _base_result(task, plan) | {
                        "status": "blocked", "reason": "request_suppressed: " + blocker["reason"],
                        "request_suppressed": True, "blocked_by_task_id": blocker["task_id"],
                        "blocking_reason": blocker["reason"],
                        "captured_at": datetime.now(TIMEZONE).isoformat()}
                    store.finish_task(task["id"], result)
                else:
                    result = _run_task(plan, store, batch, task, event, progress_callback, len(results), len(tasks), collector or _adapter)
                    store.finish_task(task["id"], result)
                    if result.get("status") == "blocked" and result.get("reason") in session_block_reasons:
                        blocked_sessions[(task["platform"], task["client"])] = {
                            "task_id": task["id"], "reason": result["reason"]}
                results.append(result)
                _emit(progress_callback, "task_finished", batch_id=batch_id, task_id=task["id"], completed=len(results), total=len(tasks),
                      status=result["status"], reason=result.get("reason",""), platform=task["platform"], client=task["client"], capture_kind=task["capture_kind"], keyword=task["keyword"])
            run_id = uuid.uuid4().hex
            composites = _make_composites(plan, store, batch, results, run_id, progress_callback)
            status = overall_status(results)
            if status == "success" and (not composites or any(c["status"] != "success" for c in composites)):
                status = "partial"
            report_path = state / "runs" / batch_id / f"report-{run_id}.json"
            reasons = {r.get("reason","") for r in results if r["status"]!="success"}
            reasons.update(c.get("reason") or "composite_incomplete" for c in composites if c["status"]!="success")
            summary = {"batch_id":batch_id,"status":status,"reason":";".join(sorted(reasons - {""})),
                       "planned_at":planned.isoformat(),"finished_at":datetime.now(TIMEZONE).isoformat(),
                       "config":plan.to_dict(),"counts":dict(Counter(r["status"] for r in results)),
                       "results":results,"composites":composites,"report_path":str(report_path),"resumed":was_started}
            _write_json(report_path, summary)
            store.finish_batch(batch_id, status, str(report_path), summary["reason"])
            _emit(progress_callback, "batch_finished", batch_id=batch_id, status=status, completed=len(results), total=len(tasks), report_path=str(report_path), reason=summary["reason"])
            return summary
        except Exception as exc:
            store.finish_batch(batch_id, "failed", reason=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            batch_lock.release()
    finally:
        store.close()
