import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from scheduled_notifier.delivery import DeliveryRunner, RateLimiter
from scheduled_notifier.storage import Store
from scheduled_notifier.tests.helpers import task_data


class FakeClock:
    def __init__(self, now):
        self.now = float(now)

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "tasks.sqlite3")
        self.url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret-value"
        self.group_id = self.store.save_group("业务群", self.url)
        self.task = task_data(group_ids=[self.group_id])
        self.clock = FakeClock(self.task["starts_at"])

    def create_run(self, **changes):
        self.task.update(changes)
        task_id = self.store.save_task(self.task, enable=True, now=int(self.clock()))
        return task_id, self.store.claim_due(self.clock())

    def runner(self, transport):
        return DeliveryRunner(self.store, transport=transport, clock=self.clock,
                              sleep=self.clock.sleep,
                              limiter=RateLimiter(self.clock, self.clock.sleep))

    def test_successful_run_uses_snapshot_and_completes_count(self):
        task_id, run_id = self.create_run(max_runs=1)
        transport = Mock(return_value={"errcode": 0})
        self.runner(transport).run(run_id)
        transport.assert_called_once_with(self.url, self.task["content"], timeout=10)
        self.assertEqual(self.store.get_task(task_id)["status"], "completed")
        self.assertEqual(self.store.run_targets(run_id)[0]["status"], "success")

    def test_explicit_failure_retries_twice_in_same_round(self):
        task_id, run_id = self.create_run()
        transport = Mock(return_value={"errcode": 40058, "errmsg": "invalid parameter"})
        self.runner(transport).run(run_id)
        self.assertEqual(transport.call_count, 3)
        self.assertEqual(self.store.get_task(task_id)["run_count"], 1)
        self.assertEqual(self.store.run_targets(run_id)[0]["attempts"], 3)

    def test_retry_wait_respects_deadline(self):
        task_id, run_id = self.create_run(ends_at=int(self.clock()) + 1)
        transport = Mock(return_value={"errcode": 45009, "errmsg": "limited"})
        self.runner(transport).run(run_id)
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(self.store.get_task(task_id)["status"], "expired")

    def test_timeout_is_unknown_and_key_is_redacted(self):
        _, run_id = self.create_run()
        transport = Mock(side_effect=requests.Timeout("timeout " + self.url))
        self.runner(transport).run(run_id)
        target = self.store.run_targets(run_id)[0]
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(target["status"], "unknown")
        self.assertNotIn("secret-value", target["message"])

    def test_pause_during_request_stops_next_group(self):
        group2 = self.store.save_group("第二群", self.url)
        task_id, run_id = self.create_run(group_ids=[self.group_id, group2])

        def send(*args, **kwargs):
            self.store.set_status(task_id, "paused", int(self.clock()))
            return {"errcode": 0}

        transport = Mock(side_effect=send)
        self.runner(transport).run(run_id)
        self.assertEqual(transport.call_count, 1)
        self.assertEqual([t["status"] for t in self.store.run_targets(run_id)], ["success", "skipped"])

    def test_expired_queued_round_sends_nothing(self):
        _, run_id = self.create_run(ends_at=int(self.clock()) + 10)
        self.clock.sleep(10)
        transport = Mock()
        self.runner(transport).run(run_id)
        transport.assert_not_called()

    def test_database_commit_delay_cannot_cross_deadline_before_request(self):
        _, run_id = self.create_run(ends_at=int(self.clock()) + 1)
        original = self.store.record_target

        def delayed_record(target_id, status, message, attempts, now):
            original(target_id, status, message, attempts, now)
            if status == "sending":
                self.clock.sleep(2)

        transport = Mock(return_value={"errcode": 0})
        with patch.object(self.store, "record_target", side_effect=delayed_record):
            self.runner(transport).run(run_id)
        transport.assert_not_called()
        self.assertEqual(self.store.run_targets(run_id)[0]["attempts"], 0)

    def test_pause_during_database_commit_prevents_request(self):
        task_id, run_id = self.create_run()
        original = self.store.record_target

        def pause_after_record(target_id, status, message, attempts, now):
            original(target_id, status, message, attempts, now)
            if status == "sending":
                self.store.set_status(task_id, "paused", int(self.clock()))

        transport = Mock(return_value={"errcode": 0})
        with patch.object(self.store, "record_target", side_effect=pause_after_record):
            self.runner(transport).run(run_id)
        transport.assert_not_called()

    def test_restart_retains_rate_limit_even_after_task_history_deleted(self):
        groups = [self.group_id]
        for index in range(19):
            groups.append(self.store.save_group(f"群{index}", self.url))
        task_id, run_id = self.create_run(group_ids=groups)
        first = Mock(return_value={"errcode": 0})
        self.runner(first).run(run_id)
        self.assertEqual(first.call_count, 20)
        self.store.delete_task(task_id)
        self.task["group_ids"] = [self.group_id]
        _, second_run = self.create_run(ends_at=int(self.clock()) + 5)
        second = Mock(return_value={"errcode": 0})
        recent = self.store.recent_requests(self.clock())
        limiter = RateLimiter(self.clock, self.clock.sleep, recent=recent, wall_now=self.clock())
        DeliveryRunner(self.store, transport=second, clock=self.clock,
                       sleep=self.clock.sleep, limiter=limiter).run(second_run)
        second.assert_not_called()

    def test_shared_webhook_rate_wait_stops_at_deadline(self):
        groups = [self.group_id]
        for index in range(20):
            groups.append(self.store.save_group(f"群{index}", self.url))
        _, run_id = self.create_run(group_ids=groups, ends_at=int(self.clock()) + 5)
        transport = Mock(return_value={"errcode": 0})
        self.runner(transport).run(run_id)
        self.assertEqual(transport.call_count, 20)
        self.assertEqual(self.store.run_targets(run_id)[-1]["status"], "skipped")

    def test_limiter_is_shared_across_rounds(self):
        limiter = RateLimiter(self.clock, self.clock.sleep)
        for _ in range(20):
            self.assertTrue(limiter.wait_for_slot(self.url, lambda: True))
        before = self.clock()
        self.assertTrue(limiter.wait_for_slot(self.url, lambda: True))
        self.assertGreaterEqual(self.clock() - before, 60)

    def test_old_sender_preserves_post_payload_and_return_value(self):
        from core.sender import WebhookSender
        response = Mock()
        response.json.return_value = {"errcode": 0}
        with patch("core.webhook.requests.post", return_value=response) as post:
            self.assertEqual(WebhookSender(retries=0).send_message(self.url, "通知"), (True, "发送成功"))
        post.assert_called_once_with(self.url, json={"msgtype": "markdown", "markdown": {"content": "通知"}}, timeout=10)


if __name__ == "__main__":
    unittest.main()
