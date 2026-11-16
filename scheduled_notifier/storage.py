"""事务化保存任务、执行轮次和逐群发送进度。"""

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from scheduled_notifier.schedule import next_time, validate_task


def validate_webhook(value):
    parsed = urlparse(value.strip())
    if (parsed.scheme != "https" or parsed.netloc != "qyapi.weixin.qq.com"
            or parsed.path != "/cgi-bin/webhook/send"
            or not parse_qs(parsed.query).get("key")):
        raise ValueError("请填写完整的企业微信群机器人 Webhook 地址，包含 key 参数")


def webhook_identity(value):
    keys = parse_qs(urlparse(value).query).get("key", [value])
    return hashlib.sha256("\0".join(keys).encode("utf-8")).hexdigest()


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    webhook TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    content TEXT NOT NULL,
                    group_ids TEXT NOT NULL,
                    rule TEXT NOT NULL,
                    starts_at INTEGER NOT NULL,
                    ends_at INTEGER,
                    max_runs INTEGER,
                    run_count INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    next_at INTEGER,
                    last_note TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    task_name TEXT NOT NULL,
                    content TEXT NOT NULL,
                    scheduled_at INTEGER NOT NULL,
                    started_at INTEGER NOT NULL,
                    finished_at INTEGER,
                    status TEXT NOT NULL DEFAULT 'running'
                );
                CREATE TABLE IF NOT EXISTS run_targets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    group_id INTEGER NOT NULL,
                    group_name TEXT NOT NULL,
                    webhook TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    message TEXT NOT NULL DEFAULT '',
                    updated_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS tasks_due ON tasks(status, next_at);
                CREATE INDEX IF NOT EXISTS runs_task ON runs(task_id, status);
                CREATE INDEX IF NOT EXISTS targets_run ON run_targets(run_id);
                CREATE TABLE IF NOT EXISTS request_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    robot_id TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    pending INTEGER NOT NULL DEFAULT 1
                );
                PRAGMA user_version=2;
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(str(self.path), timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def task_row(row):
        if row is None:
            return None
        task = dict(row)
        task["group_ids"] = json.loads(task["group_ids"])
        task["rule"] = json.loads(task["rule"])
        return task

    def groups(self):
        with self.connection() as db:
            return [dict(row) for row in db.execute("SELECT * FROM groups ORDER BY id")]

    def save_group(self, name, webhook, enabled=True, group_id=None):
        if not name.strip():
            raise ValueError("请输入群组名称")
        validate_webhook(webhook)
        with self.connection() as db:
            if group_id is not None:
                cursor = db.execute(
                    "UPDATE groups SET name=?, webhook=?, enabled=? WHERE id=?",
                    (name.strip(), webhook.strip(), int(enabled), group_id),
                )
                if cursor.rowcount != 1:
                    raise ValueError("该群组已经删除，请刷新后重试")
                return group_id
            cursor = db.execute(
                "INSERT INTO groups(name, webhook, enabled) VALUES(?, ?, ?)",
                (name.strip(), webhook.strip(), int(enabled)),
            )
            return cursor.lastrowid

    def delete_group(self, group_id):
        with self.connection() as db:
            db.execute("DELETE FROM groups WHERE id=?", (group_id,))

    def tasks(self):
        with self.connection() as db:
            return [self.task_row(row) for row in db.execute("SELECT * FROM tasks ORDER BY id DESC")]

    def get_task(self, task_id):
        with self.connection() as db:
            return self.task_row(db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())

    @staticmethod
    def has_running(db, task_id):
        return db.execute(
            "SELECT 1 FROM runs WHERE task_id=? AND status='running'", (task_id,)
        ).fetchone() is not None

    @staticmethod
    def terminal_status(task, now):
        if task["max_runs"] is not None and task["run_count"] >= task["max_runs"]:
            return "completed"
        if task["ends_at"] is not None and now >= task["ends_at"]:
            return "expired"
        return "completed"

    def save_task(self, task, enable=False, now=None):
        validate_task(task)
        now = int(time.time() if now is None else now)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = None
            if task.get("id") is not None:
                existing = self.task_row(db.execute(
                    "SELECT * FROM tasks WHERE id=?", (task["id"],)
                ).fetchone())
                if existing is None:
                    raise ValueError("任务已经删除")
                if existing["status"] != "paused" or self.has_running(db, task["id"]):
                    raise ValueError("请先暂停任务，并等待当前轮次结束后再编辑")
            task = dict(task, run_count=existing["run_count"] if existing else 0)
            upcoming = next_time(task, now, inclusive=existing is None)
            if enable and upcoming is None:
                raise ValueError("当前条件下已无未来发送时间，请调整起止时间或总轮次")
            selected = set(task["group_ids"])
            available = {row["id"] for row in db.execute("SELECT id FROM groups WHERE enabled=1")}
            if enable and not selected.intersection(available):
                raise ValueError("请至少选择一个已启用的有效群组")
            status = "active" if enable else "paused"
            values = (
                task["name"].strip(), task["content"],
                json.dumps(list(dict.fromkeys(task["group_ids"]))),
                json.dumps(task["rule"], ensure_ascii=False), task["starts_at"],
                task.get("ends_at"), task.get("max_runs"), status,
                upcoming if enable else None, now,
            )
            if existing:
                db.execute("""
                    UPDATE tasks SET name=?, content=?, group_ids=?, rule=?, starts_at=?,
                        ends_at=?, max_runs=?, status=?, next_at=?, updated_at=?, last_note=''
                    WHERE id=?
                """, values + (existing["id"],))
                return existing["id"]
            cursor = db.execute("""
                INSERT INTO tasks(name, content, group_ids, rule, starts_at, ends_at,
                    max_runs, status, next_at, updated_at, created_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, values + (now,))
            return cursor.lastrowid

    def set_status(self, task_id, status, now=None):
        if status not in ("active", "paused", "ended"):
            raise ValueError("无效的任务状态")
        now = int(time.time() if now is None else now)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            task = self.task_row(db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())
            if task is None:
                raise ValueError("任务已经删除")
            if task["status"] not in ("active", "paused"):
                raise ValueError("该任务已经结束，请新建任务开始新的发送计划")
            upcoming = None
            if status == "active":
                if self.has_running(db, task_id):
                    raise ValueError("当前轮次正在结束，请稍后恢复")
                upcoming = next_time(task, now)
                if upcoming is None:
                    raise ValueError("已经达到截止时间或次数上限，请编辑或新建任务")
                placeholders = ",".join("?" for _ in task["group_ids"])
                valid = db.execute(
                    f"SELECT 1 FROM groups WHERE enabled=1 AND id IN ({placeholders}) LIMIT 1",
                    task["group_ids"],
                ).fetchone()
                if valid is None:
                    raise ValueError("目标群组已删除或停用，请编辑任务重新选择")
            db.execute(
                "UPDATE tasks SET status=?, next_at=?, updated_at=?, last_note='' WHERE id=?",
                (status, upcoming, now, task_id),
            )

    def delete_task(self, task_id):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if self.has_running(db, task_id):
                raise ValueError("请先暂停任务，并等待当前轮次结束后再删除")
            db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def claim_due(self, now):
        now = int(now)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""
                UPDATE tasks SET status='expired', next_at=NULL, updated_at=?
                WHERE status='active' AND ends_at IS NOT NULL AND ends_at<=?
            """, (now, now))
            rows = db.execute("""
                SELECT * FROM tasks WHERE status='active' AND next_at<=?
                ORDER BY next_at, id
            """, (now,)).fetchall()
            groups = {row["id"]: dict(row) for row in db.execute("SELECT * FROM groups")}
            for row in rows:
                task = self.task_row(row)
                if self.has_running(db, task["id"]):
                    continue
                if task["max_runs"] is not None and task["run_count"] >= task["max_runs"]:
                    db.execute("UPDATE tasks SET status='completed', next_at=NULL WHERE id=?", (task["id"],))
                    continue
                targets = [groups.get(group_id) for group_id in task["group_ids"]]
                if not any(group and group["enabled"] for group in targets):
                    db.execute("""
                        UPDATE tasks SET status='paused', next_at=NULL,
                            last_note='目标群组已删除或停用，请重新选择', updated_at=? WHERE id=?
                    """, (now, task["id"]))
                    continue
                cursor = db.execute("""
                    INSERT INTO runs(task_id, task_name, content, scheduled_at, started_at)
                    VALUES(?, ?, ?, ?, ?)
                """, (task["id"], task["name"], task["content"], task["next_at"], now))
                run_id = cursor.lastrowid
                for group_id, group in zip(task["group_ids"], targets):
                    valid = bool(group and group["enabled"])
                    name = group["name"] if group else f"群组 #{group_id}（已删除）"
                    webhook = group["webhook"] if group else ""
                    db.execute("""
                        INSERT INTO run_targets(run_id, group_id, group_name, webhook,
                            status, message, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?)
                    """, (run_id, group_id, name, webhook, "pending" if valid else "skipped",
                          "" if valid else "群组已删除或停用", now))
                db.execute("""
                    UPDATE tasks SET run_count=run_count+1, next_at=NULL, updated_at=?, last_note='正在发送'
                    WHERE id=?
                """, (now, task["id"]))
                return run_id
        return None

    def get_run(self, run_id):
        with self.connection() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            return dict(row) if row else None

    def run_targets(self, run_id):
        with self.connection() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM run_targets WHERE run_id=? ORDER BY id", (run_id,)
            )]

    def record_target(self, target_id, status, message, attempts, now):
        with self.connection() as db:
            db.execute("""
                UPDATE run_targets SET status=?, message=?, attempts=?, updated_at=? WHERE id=?
            """, (status, message, attempts, int(now), target_id))

    def finish_run(self, run_id, now):
        now = int(now)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            run = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if run is None or run["status"] != "running":
                return
            pending = db.execute(
                "SELECT 1 FROM run_targets WHERE run_id=? AND status IN ('pending','sending')", (run_id,)
            ).fetchone()
            db.execute("""
                UPDATE run_targets SET status='skipped', message='本轮执行已停止', updated_at=?
                WHERE run_id=? AND status='pending'
            """, (now, run_id))
            db.execute("""
                UPDATE run_targets SET status='unknown', message='发送结果未知，请核对群消息', updated_at=?
                WHERE run_id=? AND status='sending'
            """, (now, run_id))
            statuses = [row[0] for row in db.execute("SELECT status FROM run_targets WHERE run_id=?", (run_id,))]
            status = "failed"
            if pending:
                status = "interrupted"
            elif all(value == "success" for value in statuses):
                status = "success"
            elif "unknown" in statuses:
                status = "unknown"
            elif "success" in statuses:
                status = "partial"
            db.execute("UPDATE runs SET status=?, finished_at=? WHERE id=?", (status, now, run_id))
            task = self.task_row(db.execute("SELECT * FROM tasks WHERE id=?", (run["task_id"],)).fetchone())
            next_at = None
            task_status = task["status"]
            if task_status == "active":
                next_at = next_time(task, now)
                if next_at is None:
                    task_status = self.terminal_status(task, now)
            db.execute("UPDATE tasks SET status=?, next_at=?, updated_at=?, last_note=? WHERE id=?",
                       (task_status, next_at, now, "最近一轮已记录", task["id"]))

    def recover(self, now):
        now = int(now)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""
                UPDATE run_targets SET status='unknown', message='上次执行中断，发送结果请核对', updated_at=?
                WHERE status='sending' AND run_id IN (SELECT id FROM runs WHERE status='running')
            """, (now,))
            db.execute("""
                UPDATE run_targets SET status='skipped', message='上次执行中断，已跳过', updated_at=?
                WHERE status='pending' AND run_id IN (SELECT id FROM runs WHERE status='running')
            """, (now,))
            db.execute("""
                UPDATE tasks SET last_note='上次执行中断，请查看发送记录'
                WHERE id IN (SELECT task_id FROM runs WHERE status='running')
            """)
            db.execute("UPDATE runs SET status='interrupted', finished_at=? WHERE status='running'", (now,))
            self.reschedule_in_connection(db, now)

    def reschedule_waiting(self, now):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self.reschedule_in_connection(db, int(now))

    def reschedule_in_connection(self, db, now):
        for row in db.execute("SELECT * FROM tasks WHERE status='active'").fetchall():
            task = self.task_row(row)
            if self.has_running(db, task["id"]):
                continue
            next_at = next_time(task, now)
            status = "active" if next_at is not None else self.terminal_status(task, now)
            db.execute("UPDATE tasks SET status=?, next_at=?, updated_at=? WHERE id=?",
                       (status, next_at, now, task["id"]))

    def history(self, task_id=None, limit=200):
        where = "WHERE r.task_id=?" if task_id is not None else ""
        params = (task_id, limit) if task_id is not None else (limit,)
        with self.connection() as db:
            rows = db.execute(f"""
                SELECT r.id, r.task_id, r.task_name, r.scheduled_at, r.started_at,
                    r.finished_at, r.status, COUNT(t.id) AS total,
                    SUM(t.status='success') AS success, SUM(t.status='failed') AS failed,
                    SUM(t.status='unknown') AS unknown, SUM(t.status='skipped') AS skipped
                FROM runs r LEFT JOIN run_targets t ON t.run_id=r.id
                {where} GROUP BY r.id ORDER BY r.id DESC LIMIT ?
            """, params).fetchall()
            return [dict(row) for row in rows]

    def begin_request(self, webhook, now):
        with self.connection() as db:
            db.execute("DELETE FROM request_history WHERE pending=0 AND started_at<=?", (now - 60,))
            cursor = db.execute("INSERT INTO request_history(robot_id, started_at) VALUES(?, ?)",
                                (webhook_identity(webhook), now))
            return cursor.lastrowid

    def finish_request(self, request_id, started_at):
        with self.connection() as db:
            db.execute("UPDATE request_history SET started_at=?, pending=0 WHERE id=?",
                       (started_at, request_id))

    def discard_request(self, request_id):
        with self.connection() as db:
            db.execute("DELETE FROM request_history WHERE id=?", (request_id,))

    def recent_requests(self, now):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            # 中断的请求从本次启动时起保守占用一分钟额度。
            db.execute("UPDATE request_history SET started_at=?, pending=0 WHERE pending=1", (now,))
            db.execute("DELETE FROM request_history WHERE started_at<=?", (now - 60,))
            return [dict(row) for row in db.execute(
                "SELECT robot_id, started_at FROM request_history ORDER BY started_at, id"
            )]
