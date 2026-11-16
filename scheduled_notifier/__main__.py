"""独立桌面入口，可通过 python -m scheduled_notifier 启动。"""

import logging
import sys
from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory

from PyQt5.QtCore import QLockFile, QStandardPaths, QTimer
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import QApplication, QMessageBox

from scheduled_notifier.storage import Store
from scheduled_notifier.window import MainWindow


def acquire_instance_lock(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(directory / "instance.lock"))
    lock.setStaleLockTime(0)
    return lock if lock.tryLock(100) else None


def run_application(app, directory, smoke_test=False):
    lock = None
    window = None
    handler = None
    root_logger = logging.getLogger()
    try:
        lock = acquire_instance_lock(directory)
        if lock is None:
            if smoke_test:
                return 1
            QMessageBox.information(None, "工具已在运行", "工具已在运行，请查看已打开的窗口或系统托盘。")
            return 0
        handler = logging.FileHandler(directory / "app.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s %(message)s"))
        root_logger.setLevel(logging.INFO)
        root_logger.addHandler(handler)
        store = Store(directory / "tasks.sqlite3")
        window = MainWindow(store, start_scheduler=not smoke_test)
        window.show()
        app.aboutToQuit.connect(window.shutdown)
        if smoke_test:
            QTimer.singleShot(500, app.quit)
        return app.exec_()
    except Exception as exc:
        logging.exception("启动失败")
        if not smoke_test:
            QMessageBox.critical(None, "启动失败", f"无法打开本地任务数据，请检查数据目录的权限与 app.log。\n{exc}")
        return 1
    finally:
        if window is not None:
            window.shutdown()
        if lock is not None:
            lock.unlock()
        if handler is not None:
            root_logger.removeHandler(handler)
            handler.close()


def main():
    smoke_test = "--smoke-test" in sys.argv
    app = QApplication([argument for argument in sys.argv if argument != "--smoke-test"])
    app.setOrganizationName("RetailTools")
    app.setApplicationName("WeComScheduledNotifier")
    app.setApplicationDisplayName("企业微信群定时通知工具")
    app.setFont(QFont("Microsoft YaHei", 10))
    app.setStyle("Fusion")
    app.setQuitOnLastWindowClosed(False)
    if smoke_test:
        data_directory = TemporaryDirectory(prefix="wecom-notifier-smoke-")
    else:
        data_directory = nullcontext(QStandardPaths.writableLocation(QStandardPaths.AppLocalDataLocation))
    with data_directory as directory:
        return run_application(app, Path(directory), smoke_test)


if __name__ == "__main__":
    raise SystemExit(main())
