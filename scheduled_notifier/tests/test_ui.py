import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication, QDialog, QPushButton, QScrollArea
from PyQt5.QtTest import QTest

from scheduled_notifier.delivery import DeliveryRunner
from scheduled_notifier.__main__ import acquire_instance_lock
from scheduled_notifier.group_dialog import GroupDialog
from scheduled_notifier.runtime import Scheduler
from scheduled_notifier.storage import Store
from scheduled_notifier.task_dialog import TaskDialog
from scheduled_notifier.window import MainWindow
from scheduled_notifier.tests.helpers import task_data


class UiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "tasks.sqlite3")
        self.store.save_group("零售业务群", "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test")

    def fill_task(self):
        dialog = TaskDialog(self.store)
        self.addCleanup(dialog.close)
        dialog.name_edit.setText("材料报送提醒")
        dialog.content_edit.setPlainText("请完成材料报送。")
        dialog.group_list.item(0).setCheckState(Qt.Checked)
        return dialog

    def test_save_paused_task_from_editor(self):
        dialog = self.fill_task()
        dialog.save(False)
        self.assertEqual(dialog.result(), QDialog.Accepted)
        self.assertEqual(self.store.get_task(dialog.saved_task_id)["status"], "paused")

    def test_save_enabled_daily_task_with_multiple_times(self):
        dialog = self.fill_task()
        dialog.mode_combo.setCurrentIndex(1)
        dialog.times_edit.setText("09:00，15:00")
        dialog.count_check.setChecked(True)
        dialog.count_spin.setValue(10)
        dialog.end_check.setChecked(True)
        dialog.save(True)
        task = self.store.get_task(dialog.saved_task_id)
        self.assertEqual(task["status"], "active")
        self.assertEqual(task["rule"]["times"], ["09:00", "15:00"])
        self.assertEqual(task["max_runs"], 10)
        self.assertIsNotNone(task["ends_at"])

    def test_window_has_three_pages_and_reloads_saved_task(self):
        dialog = self.fill_task()
        dialog.save(False)
        window = MainWindow(self.store, start_scheduler=False)
        self.addCleanup(window.shutdown)
        self.assertEqual(window.pages.count(), 3)
        self.assertEqual(window.task_table.rowCount(), 1)
        self.assertEqual(window.group_table.rowCount(), 1)

    def test_larger_font_applies_to_tables_dialogs_and_editor(self):
        window = MainWindow(self.store, start_scheduler=False)
        self.addCleanup(window.shutdown)
        task = TaskDialog(self.store, parent=window)
        group = GroupDialog(self.store, parent=window)
        for widget in (window.task_table, window.task_table.horizontalHeader(),
                       window.nav_buttons.button(0), task.content_edit, group.name_edit):
            widget.ensurePolished()
            self.assertEqual(widget.font().pixelSize(), 17, type(widget).__name__)
        self.assertEqual(task.content_edit.document().defaultFont().pixelSize(), 17)
        self.assertGreater(window.task_table.verticalHeader().defaultSectionSize(), 44)

    def test_task_editor_keeps_save_buttons_outside_form_scroll_area(self):
        window = MainWindow(self.store, start_scheduler=False)
        self.addCleanup(window.shutdown)
        dialog = TaskDialog(self.store, parent=window)
        self.addCleanup(dialog.close)
        dialog.mode_combo.setCurrentIndex(2)
        dialog.resize(760, 465)
        dialog.show()
        self.app.processEvents()
        form = dialog.findChild(QScrollArea)
        self.assertIsNotNone(form)
        self.assertTrue(form.isAncestorOf(dialog.end_edit))
        self.assertGreater(form.verticalScrollBar().maximum(), 0)
        save = dialog.findChild(QPushButton, "primary")
        self.assertIsNotNone(save)
        self.assertFalse(form.isAncestorOf(save))
        self.assertTrue(dialog.rect().contains(save.mapTo(dialog, save.rect().bottomRight())))

    def test_two_instances_cannot_acquire_same_lock(self):
        lock = acquire_instance_lock(Path(self.temp.name))
        self.assertIsNotNone(lock)
        self.addCleanup(lock.unlock)
        self.assertIsNone(acquire_instance_lock(Path(self.temp.name)))

    def test_scheduler_starts_and_shuts_down_thread(self):
        scheduler = Scheduler(self.store, runner=Mock())
        scheduler.start()
        self.assertTrue(scheduler.thread.isRunning())
        self.assertTrue(scheduler.shutdown())
        self.assertFalse(scheduler.thread.isRunning())

    def test_scheduler_failure_stops_delivery_and_keeps_failure_visible(self):
        window = MainWindow(self.store)
        self.addCleanup(window.shutdown)
        with patch("scheduled_notifier.window.QMessageBox.critical"):
            window.scheduler.on_failed("测试保存失败")
        window.refresh()
        self.assertTrue(window.scheduler.runner.stopped.is_set())
        self.assertIn("调度已停止", window.statusBar().currentMessage())

    def test_scheduler_dispatches_real_worker_with_mock_transport(self):
        task = task_data()
        now = task["starts_at"]
        transport = Mock(return_value={"errcode": 0})
        runner = DeliveryRunner(self.store, transport=transport, clock=lambda: now)
        scheduler = Scheduler(self.store, runner=runner)
        self.addCleanup(scheduler.shutdown)
        scheduler.start()
        task_id = self.store.save_task(task, enable=True, now=now)
        scheduler.last_poll = now
        with patch("scheduled_notifier.runtime.time.time", return_value=now):
            scheduler.tick()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            QTest.qWait(20)
            history = self.store.history(task_id)
            if history and history[0]["status"] == "success":
                break
        self.assertEqual(self.store.history(task_id)[0]["status"], "success")
        transport.assert_called_once()

    def test_normal_entry_starts_and_exits_using_temporary_data(self):
        script = textwrap.dedent("""
            import sys
            from unittest.mock import patch
            from PyQt5.QtCore import QTimer
            from PyQt5.QtWidgets import QApplication
            from scheduled_notifier.__main__ import main
            original_exec = QApplication.exec_
            def temporary_exec(app):
                QTimer.singleShot(150, app.quit)
                return original_exec()
            with patch('scheduled_notifier.__main__.QStandardPaths.writableLocation', return_value=sys.argv[1]), patch.object(QApplication, 'exec_', temporary_exec):
                raise SystemExit(main())
        """)
        result = subprocess.run([sys.executable, "-c", script, self.temp.name],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((Path(self.temp.name) / "instance.lock").exists())

    def test_smoke_test_entry_exits_automatically(self):
        script = textwrap.dedent("""
            import sys
            from unittest.mock import patch
            from scheduled_notifier.__main__ import main
            with patch('scheduled_notifier.__main__.QStandardPaths.writableLocation', return_value=sys.argv[1]):
                raise SystemExit(main())
        """)
        result = subprocess.run([sys.executable, "-c", script, self.temp.name, "--smoke-test"],
                                capture_output=True, text=True, timeout=4)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((Path(self.temp.name) / "instance.lock").exists())


if __name__ == "__main__":
    unittest.main()
