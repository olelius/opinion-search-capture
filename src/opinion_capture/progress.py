"""采集阶段记录：仅写本次任务目录，GUI读取本地状态，不增加平台请求。"""
from contextlib import contextmanager
from contextvars import ContextVar
import json
from pathlib import Path
import time

_active = ContextVar("capture_phase_recorder", default=None)


class PhaseRecorder:
    """记录阶段耗时；同一阶段重复进入不重复写盘。"""

    def __init__(self, request):
        self.started = time.monotonic()
        self.at = self.started
        self.code = self.text = ""
        self.timings = []
        self.path = None
        if request.get("phase_file"):
            candidate = Path(request["phase_file"]).resolve()
            if candidate.parent == Path(request["output_dir"]).resolve():
                self.path = candidate

    def set(self, code, text):
        if (code, text) == (self.code, self.text):
            return
        now = time.monotonic()
        if self.code:
            self.timings.append({"phase_code":self.code, "phase_text":self.text,
                                 "seconds":round(now-self.at, 3)})
        self.at, self.code, self.text = now, code, text
        if self.path:
            temporary = self.path.with_suffix(".tmp")
            payload = {"phase_code":code, "phase_text":text, "started_monotonic":self.started,
                       "phase_monotonic":now, "stage_timings":self.timings}
            temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            temporary.replace(self.path)

    def finish(self):
        if self.code:
            self.timings.append({"phase_code":self.code, "phase_text":self.text,
                                 "seconds":round(time.monotonic()-self.at, 3)})


def phase(code: str, text: str):
    """无阶段上下文时为空操作；兼容直接使用平台适配器。"""
    recorder = _active.get()
    if recorder is not None:
        recorder.set(code, text)


@contextmanager
def phase_scope(request):
    recorder = PhaseRecorder(request)
    token = _active.set(recorder)
    try:
        yield recorder
    finally:
        recorder.finish()
        _active.reset(token)
