"""Qt 定时派发与单个持久后台线程。"""

import logging
import time

from PyQt5.QtCore import QObject, QThread, QTimer, pyqtSignal, pyqtSlot

from scheduled_notifier.delivery import DeliveryRunner


logger = logging.getLogger(__name__)


class DeliveryWorker(QObject):
    finished = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(self, runner):
        super().__init__()
        self.runner = runner

    @pyqtSlot(int)
    def execute(self, run_id):
        try:
            self.runner.run(run_id)
        except Exception:
            logger.exception("后台执行或保存记录失败，已停止派发")
            self.failed.emit("后台执行或保存记录失败，调度已停止。请检查数据目录的 app.log 后重新打开工具。")
        finally:
            self.finished.emit()


class Scheduler(QObject):
    requested = pyqtSignal(int)
    changed = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(self, store, runner=None, parent=None):
        super().__init__(parent)
        self.store = store
        self.runner = runner or DeliveryRunner(store)
        self.thread = QThread(self)
        self.worker = DeliveryWorker(self.runner)
        self.worker.moveToThread(self.thread)
        self.requested.connect(self.worker.execute)
        self.worker.finished.connect(self.on_finished)
        self.worker.failed.connect(self.on_failed)
        self.thread.finished.connect(self.worker.deleteLater)
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.tick)
        self.busy = False
        self.failed_state = False
        self.last_poll = time.time()

    def start(self):
        self.store.recover(time.time())
        self.last_poll = time.time()
        self.thread.start()
        self.timer.start()

    def tick(self):
        if self.failed_state:
            return
        now = time.time()
        try:
            if now - self.last_poll > 30 or now < self.last_poll:
                self.store.reschedule_waiting(now)
                self.changed.emit()
            self.last_poll = now
            if self.busy:
                return
            run_id = self.store.claim_due(now)
            if run_id is not None:
                self.busy = True
                self.changed.emit()
                self.requested.emit(run_id)
            else:
                self.changed.emit()
        except Exception:
            logger.exception("读取或派发任务失败")
            self.on_failed("读取或派发任务失败，调度已停止。请检查数据目录的 app.log。")

    @pyqtSlot()
    def on_finished(self):
        self.busy = False
        self.changed.emit()

    @pyqtSlot(str)
    def on_failed(self, message):
        self.failed_state = True
        self.timer.stop()
        self.runner.request_stop()
        self.failed.emit(message)

    def shutdown(self):
        self.timer.stop()
        self.runner.request_stop()
        self.thread.quit()
        return self.thread.wait(12000)
