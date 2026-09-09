"""SQLite业务批次存储；图片按精确字段关联，计划触发采用唯一键防重复。"""

import json
import hashlib
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime
from pathlib import Path
from .scheduling import TIMEZONE


def _now() -> str:
    return datetime.now(TIMEZONE).isoformat()


class Store:
    """短生命周期数据库连接；一个执行工作线程拥有自己的连接。"""

    def __init__(self, path: str | Path):
        target = Path(path).resolve()
        self.path = target
        target.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(target, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS batches(
                id TEXT PRIMARY KEY, trigger_key TEXT UNIQUE NOT NULL,
                planned_at TEXT NOT NULL, config_json TEXT NOT NULL, status TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tasks(
                id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES batches(id),
                platform TEXT NOT NULL, client TEXT NOT NULL, capture_kind TEXT NOT NULL,
                keyword_id TEXT NOT NULL, keyword TEXT NOT NULL, status TEXT NOT NULL,
                result_json TEXT,
                UNIQUE(batch_id,platform,client,capture_kind,keyword_id));
            CREATE TABLE IF NOT EXISTS artifacts(
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
                path TEXT UNIQUE NOT NULL, captured_at TEXT NOT NULL, sha256 TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS attempts(
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
                number INTEGER NOT NULL, status TEXT NOT NULL, started_at TEXT NOT NULL,
                finished_at TEXT, result_json TEXT, workdir TEXT, UNIQUE(task_id,number));
            CREATE TABLE IF NOT EXISTS composites(
                id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES batches(id),
                path TEXT UNIQUE NOT NULL, metadata_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS schedule_cursors(plan_key TEXT PRIMARY KEY,last_scanned TEXT NOT NULL);
        """)
        # 增列迁移保留旧config、结果和产物；不重新初始化已有数据库。
        for table, additions in {
            "batches": {"plan_name": "TEXT NOT NULL DEFAULT ''", "sequence": "INTEGER NOT NULL DEFAULT 0",
                "started_at": "TEXT", "finished_at": "TEXT", "report_path": "TEXT", "reason": "TEXT",
                "kind": "TEXT NOT NULL DEFAULT 'manual'"},
            "tasks": {"ordinal": "INTEGER NOT NULL DEFAULT 0", "updated_at": "TEXT"},
            "artifacts": {"attempt_id": "TEXT", "role": "TEXT NOT NULL DEFAULT 'primary'"},
        }.items():
            columns = {row["name"] for row in self.db.execute(f"PRAGMA table_info({table})")}
            for name, declaration in additions.items():
                if name not in columns:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
        for row in self.db.execute("SELECT rowid,* FROM batches WHERE plan_name='' ORDER BY rowid").fetchall():
            name = json.loads(row["config_json"]).get("name", "旧计划")
            number = self.db.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM batches WHERE plan_name=? AND substr(planned_at,1,10)=?", (name, row["planned_at"][:10])).fetchone()[0]
            self.db.execute("UPDATE batches SET plan_name=?,sequence=? WHERE id=?", (name, number, row["id"]))
        self.db.execute("PRAGMA user_version=2")
        self.db.commit()

    def create_batch(self, trigger_key: str, planned_at: datetime, config: dict,
                     *, kind: str = "manual", status: str = "queued") -> tuple[str, bool]:
        """原子登记触发，返回(批次ID,是否新建)，避免重启后重复执行。"""
        if planned_at.tzinfo is None:
            raise ValueError("计划时间缺少时区")
        identity = uuid.uuid4().hex
        planned = planned_at.astimezone(TIMEZONE).isoformat()
        name = config.get("name", "未命名计划")
        with self.db:
            cursor = self.db.execute(
                """INSERT OR IGNORE INTO batches(id,trigger_key,planned_at,config_json,status,plan_name,sequence,kind)
                VALUES(?,?,?,?,?,?,(SELECT COALESCE(MAX(sequence),0)+1 FROM batches WHERE plan_name=? AND substr(planned_at,1,10)=?),?)""",
                (identity, trigger_key, planned, json.dumps(config, ensure_ascii=False), status, name, name, planned[:10], kind))
            fresh = cursor.rowcount == 1
            row = self.db.execute("SELECT id FROM batches WHERE trigger_key=?", (trigger_key,)).fetchone()
        return row["id"], fresh

    def add_task(self, batch_id: str, platform: str, client: str, kind: str, keyword: str, keyword_id: str, ordinal: int = 0) -> str:
        """按完整身份建任务；关键词模糊匹配不参与任何查询。"""
        identity = uuid.uuid4().hex
        with self.db:
            self.db.execute("""INSERT OR IGNORE INTO tasks
                (id,batch_id,platform,client,capture_kind,keyword_id,keyword,status,result_json,ordinal,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (identity, batch_id, platform, client, kind, keyword_id, keyword, "pending", None, ordinal, _now()))
            row = self.db.execute("SELECT id FROM tasks WHERE batch_id=? AND platform=? AND client=? AND capture_kind=? AND keyword_id=?",
                                  (batch_id, platform, client, kind, keyword_id)).fetchone()
        return row["id"]

    def finish_task(self, task_id: str, result: dict) -> None:
        """保存包含实际时间、阻塞原因及证据清单的采集结果。"""
        with self.db:
            self.db.execute("UPDATE tasks SET status=?,result_json=?,updated_at=? WHERE id=?",
                            (result["status"], json.dumps(result, ensure_ascii=False), _now(), task_id))

    def add_artifact(self, task_id: str, artifact: dict, captured_at: str,
                     attempt_id: str | None = None, role: str = "primary") -> None:
        """原图与任务强关联，同一路径再次登记会被数据库拒绝。"""
        with self.db:
            self.db.execute("INSERT INTO artifacts(id,task_id,path,captured_at,sha256,attempt_id,role) VALUES(?,?,?,?,?,?,?)",
                            (uuid.uuid4().hex, task_id, artifact["path"], captured_at, artifact["sha256"], attempt_id, role))

    def finish_batch(self, batch_id: str, status: str, report_path: str | None = None, reason: str | None = None) -> None:
        with self.db:
            self.db.execute("UPDATE batches SET status=?,finished_at=?,report_path=COALESCE(?,report_path),reason=? WHERE id=?",
                            (status, _now(), report_path, reason, batch_id))

    def batch(self, batch_id: str) -> dict:
        """读取批次，不存在则明确报错。"""
        row = self.db.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if row is None:
            raise ValueError("批次不存在")
        return dict(row)

    def start_batch(self, batch_id: str) -> None:
        with self.db:
            self.db.execute("UPDATE batches SET status='running',started_at=COALESCE(started_at,?),finished_at=NULL,reason=NULL WHERE id=?", (_now(), batch_id))

    def tasks(self, batch_id: str) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM tasks WHERE batch_id=? ORDER BY ordinal,rowid", (batch_id,))]

    def begin_attempt(self, task_id: str, workdir: str) -> dict:
        """每次重试创建新尝试，旧失败快照和原始文件不覆盖。"""
        identity, started = uuid.uuid4().hex, _now()
        with self.db:
            number = self.db.execute("SELECT COALESCE(MAX(number),0)+1 FROM attempts WHERE task_id=?", (task_id,)).fetchone()[0]
            self.db.execute("INSERT INTO attempts(id,task_id,number,status,started_at,workdir) VALUES(?,?,?,'running',?,?)", (identity, task_id, number, started, workdir))
            self.db.execute("UPDATE tasks SET status='running',updated_at=? WHERE id=?", (started, task_id))
        return {"id": identity, "number": number, "started_at": started, "workdir": workdir}

    def finish_attempt(self, attempt_id: str, result: dict) -> None:
        with self.db:
            self.db.execute("UPDATE attempts SET status=?,finished_at=?,result_json=? WHERE id=? AND status='running'",
                            (result["status"], _now(), json.dumps(result, ensure_ascii=False), attempt_id))

    def recover_inflight(self, batch_id: str) -> int:
        """仅在已获得批次OS锁后调用，记载失去持有者的未完成尝试。"""
        with self.db:
            completed = self.db.execute("""SELECT t.*,a.result_json AS attempt_result FROM tasks t
                JOIN attempts a ON a.task_id=t.id WHERE t.batch_id=? AND a.status='success'
                AND a.number=(SELECT MAX(number) FROM attempts WHERE task_id=t.id)
                AND (t.status!='success' OR t.result_json IS NULL)""", (batch_id,)).fetchall()
            for row in completed:
                try:
                    result = json.loads(row["attempt_result"])
                    if not result.get("exports") or any(result.get(k) != row[k] for k in ("platform","client","capture_kind","keyword")):
                        continue
                    valid = True
                    for artifact in result["exports"]:
                        digest = hashlib.sha256()
                        with Path(artifact["path"]).open("rb") as stream:
                            for chunk in iter(lambda: stream.read(1024*1024), b""):
                                digest.update(chunk)
                        valid = valid and digest.hexdigest() == artifact["sha256"]
                    if valid:
                        self.db.execute("UPDATE tasks SET status='success',result_json=?,updated_at=? WHERE id=?", (row["attempt_result"], _now(), row["id"]))
                except (OSError, ValueError, KeyError, TypeError):
                    continue
            rows = self.db.execute("SELECT attempts.id FROM attempts JOIN tasks ON tasks.id=attempts.task_id WHERE tasks.batch_id=? AND attempts.status='running'", (batch_id,)).fetchall()
            for row in rows:
                self.db.execute("UPDATE attempts SET status='interrupted',finished_at=?,result_json=? WHERE id=?", (_now(), json.dumps({"status":"interrupted","reason":"previous_process_ended_before_completion"}), row["id"]))
            self.db.execute("UPDATE tasks SET status='interrupted',updated_at=? WHERE batch_id=? AND status='running'", (_now(), batch_id))
        return len(rows)

    def add_composite(self, batch_id: str, item: dict) -> None:
        with self.db:
            self.db.execute("INSERT INTO composites VALUES(?,?,?,?)", (uuid.uuid4().hex, batch_id, item["path"], json.dumps(item, ensure_ascii=False)))

    def complete_attempt(self, attempt_id: str, task_id: str, result: dict) -> None:
        """尝试和当前任务结果同事务提交，不留下成功后恢复重采的提交窗口。"""
        encoded, finished = json.dumps(result, ensure_ascii=False), _now()
        with self.db:
            cursor = self.db.execute("UPDATE attempts SET status=?,finished_at=?,result_json=? WHERE id=? AND task_id=? AND status='running'",
                                    (result["status"], finished, encoded, attempt_id, task_id))
            if cursor.rowcount != 1:
                raise ValueError("尝试状态已变化，未覆盖历史")
            self.db.execute("UPDATE tasks SET status=?,result_json=?,updated_at=? WHERE id=?", (result["status"], encoded, finished, task_id))

    def get_cursor(self, plan_key: str) -> datetime | None:
        row = self.db.execute("SELECT last_scanned FROM schedule_cursors WHERE plan_key=?", (plan_key,)).fetchone()
        return datetime.fromisoformat(row[0]) if row else None

    def set_cursor(self, plan_key: str, at: datetime) -> None:
        with self.db:
            self.db.execute("INSERT INTO schedule_cursors VALUES(?,?) ON CONFLICT(plan_key) DO UPDATE SET last_scanned=excluded.last_scanned", (plan_key, at.isoformat()))

    def recoverable_batches(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM batches WHERE status IN ('queued','running') ORDER BY planned_at,rowid")]

    def close(self) -> None:
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def list_history(output_root: str | Path, limit: int = 100) -> list[dict]:
    """只读列出批次；未建库时返回空列表，不因GUI刷新创建文件。"""
    root = Path(output_root).resolve()
    path = root / ".capture-state" / "history.sqlite3"
    if not path.exists():
        return []
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("""SELECT b.*,COUNT(t.id) total,
            SUM(CASE WHEN t.status NOT IN ('pending','running','interrupted') THEN 1 ELSE 0 END) completed
            FROM batches b LEFT JOIN tasks t ON t.batch_id=b.id GROUP BY b.id ORDER BY b.rowid DESC LIMIT ?""",
            (max(1, min(int(limit), 1000)),)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item.update(batch_id=item["id"], scheduled_at=item["planned_at"], output_root=str(root))
            item["config"] = json.loads(item["config_json"])
            item["completed"] = int(item["completed"] or 0) if item["total"] else 0
            result.append(item)
        return result


list_batches = list_history
