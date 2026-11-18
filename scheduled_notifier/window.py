"""独立工具的任务、群组与发送记录窗口。"""

import csv
import io
import logging
import sqlite3

from PyQt5.QtCore import Qt, QUrl
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtWidgets import (
    QAbstractItemView, QAction, QApplication, QButtonGroup, QFileDialog,
    QHBoxLayout, QHeaderView, QLabel, QMainWindow, QMenu, QMessageBox,
    QPushButton, QSplitter, QStackedWidget, QStyle, QSystemTrayIcon,
    QTableWidget, QTableWidgetItem, QTextBrowser, QVBoxLayout, QWidget,
)

from scheduled_notifier.group_dialog import GroupDialog
from scheduled_notifier.runtime import Scheduler
from scheduled_notifier.schedule import format_time, rule_label
from scheduled_notifier.task_dialog import TaskDialog


logger = logging.getLogger(__name__)
STATUS_LABELS = {
    "active": "运行中", "paused": "已暂停", "ended": "已结束",
    "completed": "已完成", "expired": "已到期", "running": "发送中",
    "success": "成功", "failed": "失败", "partial": "部分成功",
    "interrupted": "已中断", "unknown": "结果未知", "pending": "等待发送",
    "sending": "发送中", "skipped": "已跳过",
}

STYLE = """
QMainWindow, QDialog { background: #f5f7fa; color: #202a3b; }
QWidget#taskForm { background: #f5f7fa; }
QWidget { font-family: 'Microsoft YaHei', 'PingFang SC'; font-size: 17px; }
QLabel { color: #202a3b; }
QLabel#pageTitle { font-size: 26px; font-weight: 600; padding-bottom: 4px; }
QLabel#secondary { color: #66748a; }
QLabel#planPreview { background: #eaf3ee; color: #24563f; padding: 12px; border-radius: 6px; }
QWidget#sidebar { background: #1f2c40; }
QLabel#brand { color: #ffffff; font-size: 24px; font-weight: 600; }
QLabel#sidebarNote { color: #becbdd; font-size: 15px; }
QPushButton { background: #ffffff; color: #29374d; border: 1px solid #d8dfe8;
    border-radius: 5px; padding: 8px 14px; }
QPushButton:hover { background: #eef3f7; }
QPushButton:disabled { color: #9aa5b3; background: #f0f2f5; }
QPushButton#primary { background: #157653; color: #ffffff; border-color: #157653; }
QPushButton#primary:hover { background: #106443; }
QPushButton#nav { text-align: left; background: transparent; color: #d4deeb;
    border: 0; padding: 12px 16px; border-radius: 6px; }
QPushButton#nav:checked { background: #34465f; color: #ffffff; }
QTableWidget { background: #ffffff; alternate-background-color: #f9fbfd; color: #202a3b;
    border: 1px solid #e0e6ed; border-radius: 6px; gridline-color: #edf0f4;
    selection-background-color: #e5f0eb; selection-color: #164d37; }
QTableWidget::item { padding: 7px; }
QHeaderView::section { background: #edf1f6; color: #556379; border: 0;
    border-bottom: 1px solid #dfe5ee; padding: 10px 8px; }
QLineEdit, QTextEdit, QTextBrowser, QListWidget, QComboBox, QSpinBox, QDateTimeEdit {
    background: #ffffff; color: #202a3b; border: 1px solid #d8dfe8;
    border-radius: 5px; padding: 7px; selection-background-color: #cee8db;
    selection-color: #143f2b; }
QListWidget::item { padding: 5px; }
QCheckBox { color: #29374d; spacing: 7px; }
QTabWidget::pane { border: 0; }
QTabBar::tab { padding: 7px 14px; background: #e9edf3; color: #536178; }
QTabBar::tab:selected { background: #ffffff; color: #157653; }
QStatusBar { background: #ffffff; color: #66748a; border-top: 1px solid #e0e6ed; }
"""


def make_table(headers):
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.setSelectionMode(QAbstractItemView.SingleSelection)
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.setAlternatingRowColors(True)
    table.setShowGrid(False)
    table.verticalHeader().hide()
    table.verticalHeader().setDefaultSectionSize(52)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
    table.horizontalHeader().setStretchLastSection(True)
    return table


def selected_id(table):
    row = table.currentRow()
    item = table.item(row, 0) if row >= 0 else None
    return item.data(Qt.UserRole) if item else None


def populate(table, rows, values):
    selected = selected_id(table)
    table.blockSignals(True)
    table.setRowCount(len(rows))
    selected_row = 0
    for row_index, row in enumerate(rows):
        for column, value in enumerate(values(row)):
            item = QTableWidgetItem(str(value))
            item.setToolTip(str(value))
            if column == 0:
                item.setData(Qt.UserRole, row["id"])
            table.setItem(row_index, column, item)
        if row["id"] == selected:
            selected_row = row_index
    if rows:
        table.selectRow(selected_row)
    table.blockSignals(False)


class MainWindow(QMainWindow):
    def __init__(self, store, start_scheduler=True):
        super().__init__()
        self.store = store
        self.scheduler = None
        self.quitting = False
        self.setWindowTitle("企业微信群定时通知工具")
        self.setMinimumSize(760, 440)
        available = self.screen().availableGeometry()
        self.resize(min(1280, available.width()), min(820, available.height() - 48))
        self.setStyleSheet(STYLE)
        self.setWindowIcon(self.style().standardIcon(QStyle.SP_MessageBoxInformation))
        central = QWidget()
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(196)
        navigation = QVBoxLayout(sidebar)
        navigation.setContentsMargins(18, 28, 18, 22)
        navigation.setSpacing(12)
        brand = QLabel("定时通知")
        brand.setObjectName("brand")
        navigation.addWidget(brand)
        scope = QLabel("企业微信群")
        scope.setObjectName("sidebarNote")
        navigation.addWidget(scope)
        navigation.addSpacing(24)
        self.pages = QStackedWidget()
        self.nav_buttons = QButtonGroup(self)
        for index, name in enumerate(("通知任务", "群组管理", "发送记录")):
            button = QPushButton(name)
            button.setObjectName("nav")
            button.setCheckable(True)
            self.nav_buttons.addButton(button, index)
            button.clicked.connect(lambda checked, page=index: self.pages.setCurrentIndex(page))
            navigation.addWidget(button)
        self.nav_buttons.button(0).setChecked(True)
        navigation.addStretch()
        condition = QLabel("工具运行期间执行\n北京时间 UTC+8")
        condition.setObjectName("sidebarNote")
        condition.setWordWrap(True)
        navigation.addWidget(condition)
        self.pages.addWidget(self.build_tasks_page())
        self.pages.addWidget(self.build_groups_page())
        self.pages.addWidget(self.build_history_page())
        root.addWidget(sidebar)
        root.addWidget(self.pages, 1)
        self.setCentralWidget(central)
        self.create_tray()
        data_action = QAction("打开数据目录", self)
        data_action.triggered.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.store.path.parent))))
        self.menuBar().addAction(data_action)
        self.refresh()
        if start_scheduler:
            self.scheduler = Scheduler(self.store, parent=self)
            self.scheduler.changed.connect(self.safe_refresh)
            self.scheduler.failed.connect(self.show_runtime_failure)
            self.scheduler.start()
            self.refresh()

    def page(self, title, action_name=None, action=None):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 24, 24, 20)
        layout.setSpacing(16)
        heading = QHBoxLayout()
        label = QLabel(title)
        label.setObjectName("pageTitle")
        heading.addWidget(label)
        heading.addStretch()
        if action_name:
            button = QPushButton(action_name)
            button.setObjectName("primary")
            button.clicked.connect(action)
            heading.addWidget(button)
        layout.addLayout(heading)
        return page, layout

    def build_tasks_page(self):
        page, layout = self.page("通知任务", "新建任务", self.new_task)
        actions = QHBoxLayout()
        for label, action in (
            ("编辑", self.edit_task), ("暂停", lambda: self.change_status("paused")),
            ("恢复", lambda: self.change_status("active")),
            ("结束", lambda: self.change_status("ended")), ("删除", self.delete_task),
        ):
            button = QPushButton(label)
            button.clicked.connect(action)
            actions.addWidget(button)
        actions.addStretch()
        layout.addLayout(actions)
        self.task_table = make_table(["任务名称", "状态", "发送规则", "已执行 / 上限", "下次发送", "截止时间", "目标群"])
        self.task_table.itemSelectionChanged.connect(self.show_task_details)
        self.task_table.doubleClicked.connect(self.edit_task)
        layout.addWidget(self.task_table, 3)
        self.task_detail = QTextBrowser()
        self.task_detail.setMinimumHeight(105)
        self.task_detail.setMaximumHeight(210)
        layout.addWidget(self.task_detail, 1)
        return page

    def build_groups_page(self):
        page, layout = self.page("群组管理", "添加群组", self.add_group)
        actions = QHBoxLayout()
        for label, action in (("编辑群组", self.edit_group), ("删除群组", self.delete_group),
                              ("导入 CSV", self.import_csv)):
            button = QPushButton(label)
            button.clicked.connect(action)
            actions.addWidget(button)
        actions.addStretch()
        layout.addLayout(actions)
        self.group_table = make_table(["群组名称", "状态", "机器人地址"])
        self.group_table.doubleClicked.connect(self.edit_group)
        layout.addWidget(self.group_table)
        note = QLabel("在目标企业微信群中添加群机器人，将其 Webhook 地址配置到这里。")
        note.setObjectName("secondary")
        note.setWordWrap(True)
        layout.addWidget(note)
        return page

    def build_history_page(self):
        page, layout = self.page("发送记录")
        splitter = QSplitter(Qt.Vertical)
        self.history_table = make_table(["任务名称", "计划时间", "实际开始", "结果", "成功 / 总群数"])
        self.history_table.itemSelectionChanged.connect(self.show_run_details)
        self.result_table = make_table(["群组名称", "结果", "请求次数", "记录时间", "结果说明"])
        splitter.addWidget(self.history_table)
        splitter.addWidget(self.result_table)
        splitter.setSizes([380, 230])
        layout.addWidget(splitter)
        note = QLabel("显示最近 200 轮。选择一轮查看逐群结果，完整记录保存在本机数据库中。")
        note.setObjectName("secondary")
        layout.addWidget(note)
        return page

    def create_tray(self):
        self.tray = QSystemTrayIcon(self.windowIcon(), self)
        self.tray.setToolTip("企业微信群定时通知工具")
        menu = QMenu(self)
        show_action = menu.addAction("显示窗口")
        show_action.triggered.connect(self.show_window)
        exit_action = menu.addAction("退出并停止任务")
        exit_action.triggered.connect(self.close)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self.show_window() if reason == QSystemTrayIcon.DoubleClick else None)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()

    def show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def refresh(self):
        tasks = self.store.tasks()
        populate(self.task_table, tasks, lambda task: (
            task["name"], STATUS_LABELS.get(task["status"], task["status"]), rule_label(task["rule"]),
            f"{task['run_count']} / {task['max_runs'] if task['max_runs'] is not None else '不限'}",
            "发送中" if task["status"] == "active" and task["next_at"] is None else format_time(task["next_at"]),
            format_time(task["ends_at"]), f"{len(task['group_ids'])} 个",
        ))
        populate(self.group_table, self.store.groups(), lambda group: (
            group["name"], "已启用" if group["enabled"] else "已停用", "qyapi.weixin.qq.com / key=***",
        ))
        populate(self.history_table, self.store.history(), lambda run: (
            run["task_name"], format_time(run["scheduled_at"]), format_time(run["started_at"]),
            STATUS_LABELS.get(run["status"], run["status"]), f"{run['success']} / {run['total']}",
        ))
        self.show_task_details()
        self.show_run_details()
        active = sum(task["status"] == "active" for task in tasks)
        if self.scheduler and self.scheduler.failed_state:
            self.statusBar().showMessage("调度已停止，请检查数据目录的 app.log 并重新打开工具")
        else:
            self.statusBar().showMessage(f"已保存 {len(tasks)} 个任务 · 运行中 {active} 个 · 北京时间 UTC+8")

    def safe_refresh(self):
        try:
            self.refresh()
        except (sqlite3.Error, OSError, ValueError):
            logger.exception("刷新界面失败")
            if self.scheduler:
                self.scheduler.on_failed("读取本地数据失败，调度已停止。请检查数据目录的 app.log。")
            else:
                self.show_runtime_failure("读取本地数据失败，请检查数据目录的 app.log。")

    def show_runtime_failure(self, message):
        self.statusBar().showMessage("调度已停止，请检查本地数据")
        QMessageBox.critical(self, "调度已停止", message)

    def show_task_details(self):
        task_id = selected_id(self.task_table)
        task = self.store.get_task(task_id) if task_id is not None else None
        if task is None:
            self.task_detail.setPlainText("添加群组后新建通知任务，或选择已有任务查看内容。")
            return
        names = {group["id"]: group["name"] for group in self.store.groups()}
        targets = "、".join(names.get(group_id, f"#{group_id}（已删除）") for group_id in task["group_ids"])
        lines = [task["name"], f"目标群组  {targets}", f"开始时间  {format_time(task['starts_at'])}"]
        if task["last_note"]:
            lines.append(task["last_note"])
        lines.extend(["", task["content"]])
        self.task_detail.setPlainText("\n".join(lines))

    def show_run_details(self):
        run_id = selected_id(self.history_table)
        targets = self.store.run_targets(run_id) if run_id is not None else []
        populate(self.result_table, targets, lambda target: (
            target["group_name"], STATUS_LABELS.get(target["status"], target["status"]),
            target["attempts"], format_time(target["updated_at"]), target["message"],
        ))

    def new_task(self):
        if not self.store.groups():
            QMessageBox.information(self, "先添加群组", "请先在群组管理中添加目标企业微信群及机器人地址。")
            self.pages.setCurrentIndex(1)
            self.nav_buttons.button(1).setChecked(True)
            return
        TaskDialog(self.store, parent=self).exec_()
        self.refresh()

    def edit_task(self):
        task_id = selected_id(self.task_table)
        task = self.store.get_task(task_id) if task_id is not None else None
        if task is None:
            return
        if task["status"] != "paused":
            QMessageBox.information(self, "先暂停任务", "请先暂停任务并等待当前轮次结束。已完成的任务可通过新建开始新的计数。")
            return
        if any(run["status"] == "running" for run in self.store.history(task_id)):
            QMessageBox.information(self, "正在结束当前轮次", "请等待当前轮次结束后再编辑。")
            return
        TaskDialog(self.store, task=task, parent=self).exec_()
        self.refresh()

    def change_status(self, status):
        task_id = selected_id(self.task_table)
        if task_id is None:
            return
        try:
            self.store.set_status(task_id, status)
        except (ValueError, sqlite3.Error, OSError) as exc:
            QMessageBox.warning(self, "操作未完成", str(exc))
        self.refresh()

    def delete_task(self):
        task_id = selected_id(self.task_table)
        if task_id is None:
            return
        answer = QMessageBox.question(self, "删除任务", "删除该任务及其全部发送记录？",
                                      QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        try:
            self.store.delete_task(task_id)
        except (ValueError, sqlite3.Error, OSError) as exc:
            QMessageBox.warning(self, "删除未完成", str(exc))
        self.refresh()

    def add_group(self):
        GroupDialog(self.store, parent=self).exec_()
        self.refresh()

    def edit_group(self):
        group_id = selected_id(self.group_table)
        group = next((group for group in self.store.groups() if group["id"] == group_id), None)
        if group:
            GroupDialog(self.store, group=group, parent=self).exec_()
            self.refresh()

    def delete_group(self):
        group_id = selected_id(self.group_table)
        if group_id is None:
            return
        answer = QMessageBox.question(self, "删除群组", "删除该群组？后续轮次会跳过此群。",
                                      QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer == QMessageBox.Yes:
            try:
                self.store.delete_group(group_id)
            except (sqlite3.Error, OSError) as exc:
                QMessageBox.warning(self, "删除失败", str(exc))
            self.refresh()

    def import_csv(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入群组", "", "CSV 文件 (*.csv)")
        if not path:
            return
        try:
            with open(path, "rb") as source:
                raw = source.read()
            try:
                content = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                content = raw.decode("gb18030")
            success = 0
            failures = []
            for line, row in enumerate(csv.reader(io.StringIO(content)), 1):
                if not row or not any(cell.strip() for cell in row):
                    continue
                if line == 1 and row[0].strip().lower() in ("群名称", "群组名称", "name"):
                    continue
                try:
                    if len(row) < 2:
                        raise ValueError("缺少群名称或 Webhook 地址")
                    enabled = len(row) < 4 or row[3].strip().lower() not in ("否", "false", "0", "停用")
                    self.store.save_group(row[0], row[1], enabled)
                    success += 1
                except ValueError as exc:
                    failures.append(f"第 {line} 行 {exc}")
            summary = f"已导入 {success} 个群组。"
            if failures:
                summary += "\n" + "\n".join(failures[:10])
            QMessageBox.information(self, "导入结果", summary)
        except (OSError, UnicodeError, csv.Error, sqlite3.Error) as exc:
            QMessageBox.warning(self, "导入失败", str(exc))
        self.refresh()

    def shutdown(self):
        if self.scheduler and not self.scheduler.shutdown():
            return False
        self.quitting = True
        self.tray.hide()
        self.hide()
        return True

    def closeEvent(self, event):
        if self.quitting:
            event.accept()
            return
        dialog = QMessageBox(self)
        dialog.setWindowTitle("关闭定时通知工具")
        dialog.setText("退出程序后，定时任务随程序停止。")
        tray_button = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            tray_button = dialog.addButton("在托盘继续运行", QMessageBox.AcceptRole)
        exit_button = dialog.addButton("退出并停止任务", QMessageBox.DestructiveRole)
        dialog.addButton("取消", QMessageBox.RejectRole)
        dialog.exec_()
        if tray_button is not None and dialog.clickedButton() == tray_button:
            self.hide()
            event.ignore()
        elif dialog.clickedButton() == exit_button:
            if self.shutdown():
                event.accept()
                QApplication.instance().quit()
            else:
                QMessageBox.information(self, "正在结束发送", "正在等待已发起的请求返回，请稍后再次退出。")
                event.ignore()
        else:
            event.ignore()
