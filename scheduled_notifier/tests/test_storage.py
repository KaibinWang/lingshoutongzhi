import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from scheduled_notifier.storage import Store
from scheduled_notifier.tests.helpers import task_data


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "tasks.sqlite3")
        self.group_id = self.store.save_group("业务群", "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test")
        self.task = task_data(group_ids=[self.group_id])
        self.now = self.task["starts_at"]

    def save_active(self, **changes):
        task = dict(self.task, **changes)
        return self.store.save_task(task, enable=True, now=self.now)

    def test_deleted_group_id_is_never_reused(self):
        self.store.delete_group(self.group_id)
        new_id = self.store.save_group("新群", "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=new")
        self.assertGreater(new_id, self.group_id)

    def test_saved_task_and_groups_survive_reopening(self):
        task_id = self.save_active()
        reopened = Store(self.store.path)
        self.assertEqual(reopened.get_task(task_id)["rule"], self.task["rule"])
        self.assertEqual(reopened.groups()[0]["name"], "业务群")

    def test_claim_is_atomic_and_counts_a_round_once(self):
        task_id = self.save_active(max_runs=3)
        with ThreadPoolExecutor(max_workers=2) as pool:
            runs = list(pool.map(lambda _: self.store.claim_due(self.now), range(2)))
        self.assertEqual(sum(run is not None for run in runs), 1)
        self.assertEqual(self.store.get_task(task_id)["run_count"], 1)

    def test_completed_round_schedules_future_and_stops_at_count(self):
        task_id = self.save_active(max_runs=1)
        run_id = self.store.claim_due(self.now)
        target = self.store.run_targets(run_id)[0]
        self.store.record_target(target["id"], "success", "发送成功", 1, self.now)
        self.store.finish_run(run_id, self.now + 1)
        task = self.store.get_task(task_id)
        self.assertEqual(task["status"], "completed")
        self.assertIsNone(task["next_at"])

    def test_deadline_prevents_claim(self):
        task_id = self.save_active(ends_at=self.now + 60)
        self.assertIsNone(self.store.claim_due(self.now + 60))
        self.assertEqual(self.store.get_task(task_id)["status"], "expired")
        self.assertEqual(self.store.get_task(task_id)["run_count"], 0)

    def test_no_valid_target_pauses_without_using_count(self):
        task_id = self.save_active()
        self.store.delete_group(self.group_id)
        self.assertIsNone(self.store.claim_due(self.now))
        task = self.store.get_task(task_id)
        self.assertEqual(task["status"], "paused")
        self.assertEqual(task["run_count"], 0)

    def test_recover_keeps_unknown_delivery_and_skips_missed_times(self):
        task_id = self.save_active()
        run_id = self.store.claim_due(self.now)
        target = self.store.run_targets(run_id)[0]
        self.store.record_target(target["id"], "sending", "正在发送", 1, self.now)
        self.store.recover(self.now + 4000)
        self.assertEqual(self.store.history()[0]["status"], "interrupted")
        self.assertEqual(self.store.run_targets(run_id)[0]["status"], "unknown")
        task = self.store.get_task(task_id)
        self.assertEqual(task["run_count"], 1)
        self.assertGreater(task["next_at"], self.now + 4000)
        self.assertIn("中断", task["last_note"])

    def test_editing_preserves_count_and_requires_paused_idle_task(self):
        task_id = self.save_active()
        run_id = self.store.claim_due(self.now)
        with self.assertRaises(ValueError):
            self.store.save_task(self.store.get_task(task_id), now=self.now)
        self.store.set_status(task_id, "paused", self.now)
        with self.assertRaises(ValueError):
            self.store.save_task(self.store.get_task(task_id), now=self.now)
        self.store.finish_run(run_id, self.now + 1)
        edited = dict(self.store.get_task(task_id), name="更新任务", run_count=0)
        self.store.save_task(edited, now=self.now)
        self.assertEqual(self.store.get_task(task_id)["run_count"], 1)

    def test_resume_skips_elapsed_occurrences(self):
        task_id = self.save_active()
        self.store.set_status(task_id, "paused", self.now)
        self.store.set_status(task_id, "active", self.now + 3601)
        self.assertGreater(self.store.get_task(task_id)["next_at"], self.now + 3601)

    def test_disabled_target_is_logged_alongside_success(self):
        other = self.store.save_group("停用群", "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=off", False)
        self.save_active(group_ids=[self.group_id, other])
        run_id = self.store.claim_due(self.now)
        targets = self.store.run_targets(run_id)
        self.assertEqual([target["status"] for target in targets], ["pending", "skipped"])

    def test_unfinished_request_keeps_quota_after_restart(self):
        url = self.store.groups()[0]["webhook"]
        self.store.begin_request(url, self.now)
        records = self.store.recent_requests(self.now + 7200)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["started_at"], self.now + 7200)


if __name__ == "__main__":
    unittest.main()
