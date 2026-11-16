"""可停止的逐群发送，共享机器人限速并检查截止时间。"""

import re
import threading
import time
from collections import defaultdict, deque
from urllib.parse import parse_qs, urlparse

from core.webhook import post_markdown
from scheduled_notifier.storage import webhook_identity


def safe_message(message, webhook):
    value = str(message).replace(webhook, "[机器人地址]")
    for key in parse_qs(urlparse(webhook).query).get("key", []):
        value = value.replace(key, "***")
    return re.sub(r"key=[^&\s\"')]+", "key=***", value)[:1000]


class RateLimiter:
    def __init__(self, clock=time.monotonic, sleep=time.sleep, recent=(), wall_now=None):
        self.clock = clock
        self.sleep = sleep
        self.requests = defaultdict(deque)
        if wall_now is None:
            wall_now = time.time()
        now = self.clock()
        for row in sorted(recent, key=lambda value: value["started_at"]):
            age = max(0, wall_now - row["started_at"])
            if age < 60:
                self.requests[row["robot_id"]].append(now - age)

    def wait_for_slot(self, webhook, allowed):
        key = webhook_identity(webhook)
        recent = self.requests[key]
        while allowed():
            now = self.clock()
            while recent and now - recent[0] >= 60:
                recent.popleft()
            if len(recent) < 20:
                recent.append(now)
                return True
            self.sleep(min(0.2, max(0.001, 60 - (now - recent[0]))))
        return False

    def confirm_slot(self, webhook):
        self.requests[webhook_identity(webhook)][-1] = self.clock()


class DeliveryRunner:
    def __init__(self, store, transport=post_markdown, clock=time.time,
                 sleep=time.sleep, limiter=None):
        self.store = store
        self.transport = transport
        self.clock = clock
        self.sleep = sleep
        if limiter is None:
            now = clock()
            limiter = RateLimiter(sleep=sleep, recent=store.recent_requests(now), wall_now=now)
        self.limiter = limiter
        self.stopped = threading.Event()

    def request_stop(self):
        self.stopped.set()

    def allowed(self, task_id):
        if self.stopped.is_set():
            return False
        task = self.store.get_task(task_id)
        if task is None or task["status"] != "active":
            return False
        return task["ends_at"] is None or self.clock() < task["ends_at"]

    def wait(self, seconds, task_id):
        until = self.clock() + seconds
        while self.clock() < until:
            if not self.allowed(task_id):
                return False
            self.sleep(min(0.2, until - self.clock()))
        return self.allowed(task_id)

    def record(self, target, status, message, attempts):
        self.store.record_target(target["id"], status,
                                 safe_message(message, target["webhook"]),
                                 attempts, self.clock())

    def send_target(self, run, target):
        allowed = lambda: self.allowed(run["task_id"])
        attempts = 0
        last_failure = ""
        for attempt in range(1, 4):
            if not self.limiter.wait_for_slot(target["webhook"], allowed) or not allowed():
                status = "failed" if attempts else "skipped"
                message = last_failure + " 任务已暂停、结束或到达截止时间"
                self.record(target, status, message.strip(), attempts)
                return
            attempts = attempt
            self.record(target, "sending", "正在发送", attempts)
            request_id = self.store.begin_request(target["webhook"], self.clock())
            if not allowed():
                self.store.discard_request(request_id)
                self.record(target, "failed" if attempt > 1 else "skipped",
                            last_failure + " 任务已暂停、结束或到达截止时间", attempt - 1)
                return
            started_at = self.clock()
            self.limiter.confirm_slot(target["webhook"])
            try:
                response = self.transport(target["webhook"], run["content"], timeout=10)
            except Exception as exc:
                self.record(target, "unknown", f"发送结果未知，请核对群消息。{exc}", attempts)
                return
            finally:
                self.store.finish_request(request_id, started_at)
            if not isinstance(response, dict) or type(response.get("errcode")) is not int:
                self.record(target, "unknown", "接口响应无法确认发送结果，请核对群消息", attempts)
                return
            if response["errcode"] == 0:
                self.record(target, "success", "发送成功", attempts)
                return
            last_failure = f"接口错误 {response['errcode']}，{response.get('errmsg', '发送失败')}"
            self.record(target, "failed", last_failure, attempts)
            if attempt < 3 and not self.wait(2, run["task_id"]):
                return

    def run(self, run_id):
        run = self.store.get_run(run_id)
        if run is None or run["status"] != "running":
            return
        try:
            for target in self.store.run_targets(run_id):
                if target["status"] == "pending":
                    self.send_target(run, target)
        finally:
            self.store.finish_run(run_id, self.clock())
