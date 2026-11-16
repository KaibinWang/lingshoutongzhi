import unittest

from scheduled_notifier.schedule import (
    next_time, parse_times, preview_times, rule_label, validate_task,
)
from scheduled_notifier.tests.helpers import task_data, timestamp


class ScheduleTests(unittest.TestCase):
    def test_interval_stays_anchored_to_start(self):
        task = task_data()
        self.assertEqual(next_time(task, timestamp("2030-01-01T09:07:00")),
                         timestamp("2030-01-01T09:30:00"))

    def test_equal_time_is_only_included_when_requested(self):
        task = task_data()
        start = task["starts_at"]
        self.assertEqual(next_time(task, start, inclusive=True), start)
        self.assertEqual(next_time(task, start), start + 1800)

    def test_before_start_returns_first_occurrence(self):
        task = task_data()
        self.assertEqual(next_time(task, task["starts_at"] - 1), task["starts_at"])

    def test_daily_times_cross_month(self):
        task = task_data(rule={"kind": "daily", "times": ["09:00", "15:00"]})
        self.assertEqual(next_time(task, timestamp("2030-01-31T15:00:00")),
                         timestamp("2030-02-01T09:00:00"))

    def test_weekly_crosses_week(self):
        task = task_data(rule={"kind": "weekly", "weekdays": [0, 4],
                               "times": ["10:00"]})
        self.assertEqual(next_time(task, timestamp("2030-01-04T10:00:00")),
                         timestamp("2030-01-07T10:00:00"))

    def test_deadline_is_exclusive(self):
        task = task_data(ends_at=timestamp("2030-01-01T09:30:00"))
        self.assertIsNone(next_time(task, task["starts_at"]))
        self.assertEqual(preview_times(task, task["starts_at"]), [task["starts_at"]])

    def test_count_limit_caps_preview_and_stops_exhausted_task(self):
        task = task_data(max_runs=3, run_count=2)
        self.assertEqual(len(preview_times(task, task["starts_at"])), 1)
        task["run_count"] = 3
        self.assertIsNone(next_time(task, task["starts_at"]))

    def test_chinese_content_uses_utf8_byte_limit(self):
        validate_task(task_data(content="中" * 1365))
        with self.assertRaisesRegex(ValueError, "4096"):
            validate_task(task_data(content="中" * 1366))

    def test_rejects_invalid_rules_and_ranges(self):
        cases = [
            {"rule": {"kind": "interval", "minutes": 0}},
            {"rule": {"kind": "weekly", "weekdays": [], "times": ["09:00"]}},
            {"rule": {"kind": "daily", "times": ["24:00"]}},
            {"ends_at": timestamp("2030-01-01T08:59:00")},
            {"max_runs": 0}, {"group_ids": []}, {"name": " "},
        ]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_task(task_data(**changes))

    def test_times_normalize_chinese_delimiters_and_duplicates(self):
        self.assertEqual(parse_times("15:00，09:00、15:00"), ["09:00", "15:00"])

    def test_rule_label_is_readable(self):
        self.assertEqual(rule_label({"kind": "interval", "minutes": 120}), "每隔 2 小时")
        self.assertIn("周一", rule_label({"kind": "weekly", "weekdays": [0],
                                        "times": ["09:00"]}))


if __name__ == "__main__":
    unittest.main()
