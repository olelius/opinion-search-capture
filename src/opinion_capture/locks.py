"""跨进程资源锁：操作系统句柄是占用证据，文件存在不是。"""

import hashlib
import os
import tempfile
import time
from pathlib import Path


class ResourceBusy(RuntimeError):
    """资源在超时时间内仍有活动持有者。"""


class ResourceLock:
    """批次/设备/profile的OS文件锁，进程退出自动释放，不删除锁文件。"""

    def __init__(self, key: str, *, timeout: float = 0, cancel_event=None):
        self.key, self.timeout, self.cancel_event = key, timeout, cancel_event
        root = Path(tempfile.gettempdir()) / "opinion-capture-resource-locks"
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / (hashlib.sha256(key.encode()).hexdigest() + ".lock")
        self._stream = None

    def acquire(self):
        """排他锁定首字节；超时/取消只停止等待，不抢占活动锁。"""
        if self._stream is not None:
            raise RuntimeError("该锁实例已经持有资源")
        stream = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            stream.write(b"0")
            stream.flush()
        deadline = time.monotonic() + max(0, self.timeout)
        while True:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._stream = stream
                return self
            except (OSError, BlockingIOError):
                if (self.cancel_event is not None and self.cancel_event.is_set()) or time.monotonic() >= deadline:
                    stream.close()
                    raise ResourceBusy(f"资源仍被占用：{self.key}")
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))

    def release(self):
        """只释放自身句柄，保留锁文件但不将其当占用标记。"""
        if self._stream is None:
            return
        stream, self._stream = self._stream, None
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_):
        self.release()
