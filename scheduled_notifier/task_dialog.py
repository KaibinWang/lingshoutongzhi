"""通知任务编辑与未来计划预览。"""

import sqlite3
import time
from datetime import datetime, timedelta

from PyQt5.QtCore import QDate, QDateTime, QTime, Qt
from PyQt5.QtWidgets import (
    QCheckBox, QComboBox, QDateTimeEdit, QDialog, QFormLayout, QGridLayout,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QPushButton, QSpinBox, QStackedWidget, QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

from scheduled_notifier.schedule import (
    BEIJING, WEEKDAYS, format_time, parse_times, preview_times, validate_task,
)


def date_editor(value):
    date = datetime.fromtimestamp(value, BEIJING)
    edit = QDateTimeEdit()
    edit.setTimeSpec(Qt.UTC)
    edit.setDateTime(QDateTime(QDate(date.year, date.month, date.day),
                               QTime(date.hour, date.minute), Qt.UTC))
    edit.setDisplayFormat("yyyy-MM-dd HH:mm")
    edit.setCalendarPopup(True)
    return edit


def date_value(edit):
    return int(edit.dateTime().toPyDateTime().replace(tzinfo=BEIJING).timestamp())


class TaskDialog(QDialog):
    def __init__(self, store, task=None, parent=None):
        super().__init__(parent)
        self.store = store
        self.task = task or {}
        self.saved_task_id = None
        self.setWindowTitle("编辑通知任务" if task else "新建通知任务")
        self.setMinimumSize(880, 660)
        self.resize(960, 740)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(16)
        title = QLabel(self.windowTitle())
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        columns = QHBoxLayout()
        columns.setSpacing(24)
        left = QVBoxLayout()
        left.setSpacing(10)
        left.addWidget(QLabel("任务名称"))
        self.name_edit = QLineEdit(self.task.get("name", ""))
        self.name_edit.setPlaceholderText("例如 材料报送提醒")
        left.addWidget(self.name_edit)
        left.addWidget(QLabel("通知内容"))
        self.content_edit = QTextEdit()
        self.content_edit.setAcceptRichText(False)
        self.content_edit.setPlainText(self.task.get("content", ""))
        self.content_edit.setPlaceholderText("请输入通知内容，支持 Markdown")
        self.preview_edit = QTextEdit()
        self.preview_edit.setReadOnly(True)
        self.content_tabs = QTabWidget()
        self.content_tabs.addTab(self.content_edit, "编辑")
        self.content_tabs.addTab(self.preview_edit, "预览")
        left.addWidget(self.content_tabs, 2)
        self.bytes_label = QLabel()
        self.bytes_label.setObjectName("secondary")
        left.addWidget(self.bytes_label)
        left.addWidget(QLabel("目标群组"))
        self.group_list = QListWidget()
        self.group_list.setMinimumHeight(120)
        selected = set(self.task.get("group_ids", []))
        groups = self.store.groups()
        for group in groups:
            suffix = "" if group["enabled"] else "（已停用）"
            self.add_group_item(group["id"], group["name"] + suffix, group["id"] in selected)
        for group_id in selected.difference(group["id"] for group in groups):
            self.add_group_item(group_id, f"原群组 #{group_id}（已删除）", True)
        left.addWidget(self.group_list, 1)
        columns.addLayout(left, 1)

        right = QVBoxLayout()
        right.setSpacing(12)
        right.addWidget(QLabel("发送计划 · 北京时间 UTC+8"))
        form = QFormLayout()
        form.setSpacing(12)
        start = int((datetime.now(BEIJING).replace(second=0, microsecond=0)
                     + timedelta(minutes=1)).timestamp())
        self.start_edit = date_editor(self.task.get("starts_at", start))
        form.addRow("开始时间", self.start_edit)
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("固定间隔", "interval")
        self.mode_combo.addItem("每天指定时点", "daily")
        self.mode_combo.addItem("每周指定日期", "weekly")
        form.addRow("发送频率", self.mode_combo)
        right.addLayout(form)
        self.rule_pages = QStackedWidget()
        interval_page = QWidget()
        interval_layout = QHBoxLayout(interval_page)
        interval_layout.setContentsMargins(0, 0, 0, 0)
        interval_layout.addWidget(QLabel("每隔"))
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(1, 1000000)
        self.interval_spin.setValue(30)
        self.unit_combo = QComboBox()
        self.unit_combo.addItem("分钟", 1)
        self.unit_combo.addItem("小时", 60)
        interval_layout.addWidget(self.interval_spin, 1)
        interval_layout.addWidget(self.unit_combo)
        self.rule_pages.addWidget(interval_page)
        calendar_page = QWidget()
        calendar_layout = QVBoxLayout(calendar_page)
        calendar_layout.setContentsMargins(0, 0, 0, 0)
        calendar_layout.addWidget(QLabel("发送时点，可用逗号分隔"))
        self.times_edit = QLineEdit("09:00, 15:00")
        self.times_edit.setPlaceholderText("09:00, 15:00")
        calendar_layout.addWidget(self.times_edit)
        self.week_widget = QWidget()
        weekdays = QGridLayout(self.week_widget)
        weekdays.setContentsMargins(0, 0, 0, 0)
        self.day_checks = []
        for index, name in enumerate(WEEKDAYS):
            check = QCheckBox(name)
            check.setChecked(index < 5)
            self.day_checks.append(check)
            weekdays.addWidget(check, index // 4, index % 4)
        calendar_layout.addWidget(self.week_widget)
        self.rule_pages.addWidget(calendar_page)
        right.addWidget(self.rule_pages)
        self.count_check = QCheckBox("设置总发送轮次")
        self.count_spin = QSpinBox()
        self.count_spin.setRange(1, 1000000)
        self.count_spin.setSuffix(" 轮")
        self.count_spin.setValue(self.task.get("max_runs") or 10)
        self.count_check.setChecked(self.task.get("max_runs") is not None)
        count_row = QHBoxLayout()
        count_row.addWidget(self.count_check)
        count_row.addWidget(self.count_spin)
        right.addLayout(count_row)
        count_note = QLabel(f"每轮向各目标群发送一次。已执行 {self.task.get('run_count', 0)} 轮。")
        count_note.setObjectName("secondary")
        count_note.setWordWrap(True)
        right.addWidget(count_note)
        self.end_check = QCheckBox("设置截止日期时间")
        self.end_check.setChecked(self.task.get("ends_at") is not None)
        self.end_edit = date_editor(self.task.get("ends_at") or start + 7 * 86400)
        right.addWidget(self.end_check)
        right.addWidget(self.end_edit)
        right.addWidget(QLabel("接下来的发送时间"))
        self.plan_preview = QLabel()
        self.plan_preview.setObjectName("planPreview")
        self.plan_preview.setWordWrap(True)
        self.plan_preview.setMinimumHeight(110)
        right.addWidget(self.plan_preview)
        right.addStretch()
        columns.addLayout(right, 1)
        layout.addLayout(columns, 1)
        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        paused = QPushButton("保存为暂停")
        paused.clicked.connect(lambda: self.save(False))
        enabled = QPushButton("保存并启用")
        enabled.setObjectName("primary")
        enabled.clicked.connect(lambda: self.save(True))
        for button in (cancel, paused, enabled):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.load_rule()
        self.mode_combo.currentIndexChanged.connect(self.mode_changed)
        self.count_check.toggled.connect(self.refresh_preview)
        self.end_check.toggled.connect(self.refresh_preview)
        self.content_edit.textChanged.connect(self.refresh_preview)
        self.name_edit.textChanged.connect(self.refresh_preview)
        self.group_list.itemChanged.connect(self.refresh_preview)
        self.times_edit.textChanged.connect(self.refresh_preview)
        self.start_edit.dateTimeChanged.connect(self.refresh_preview)
        self.end_edit.dateTimeChanged.connect(self.refresh_preview)
        self.interval_spin.valueChanged.connect(self.refresh_preview)
        self.unit_combo.currentIndexChanged.connect(self.refresh_preview)
        self.count_spin.valueChanged.connect(self.refresh_preview)
        for check in self.day_checks:
            check.toggled.connect(self.refresh_preview)
        self.mode_changed()

    def add_group_item(self, group_id, name, selected):
        item = QListWidgetItem(name)
        item.setData(Qt.UserRole, group_id)
        item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
        item.setCheckState(Qt.Checked if selected else Qt.Unchecked)
        self.group_list.addItem(item)

    def load_rule(self):
        rule = self.task.get("rule", {"kind": "interval", "minutes": 30})
        self.mode_combo.setCurrentIndex(self.mode_combo.findData(rule["kind"]))
        if rule["kind"] == "interval":
            minutes = rule["minutes"]
            hours = minutes % 60 == 0
            self.unit_combo.setCurrentIndex(1 if hours else 0)
            self.interval_spin.setValue(minutes // 60 if hours else minutes)
        else:
            self.times_edit.setText(", ".join(rule["times"]))
            if rule["kind"] == "weekly":
                for index, check in enumerate(self.day_checks):
                    check.setChecked(index in rule["weekdays"])

    def mode_changed(self, *args):
        kind = self.mode_combo.currentData()
        self.rule_pages.setCurrentIndex(0 if kind == "interval" else 1)
        self.week_widget.setVisible(kind == "weekly")
        self.refresh_preview()

    def collect_task(self):
        kind = self.mode_combo.currentData()
        rule = {"kind": kind}
        if kind == "interval":
            rule["minutes"] = self.interval_spin.value() * self.unit_combo.currentData()
        else:
            rule["times"] = parse_times(self.times_edit.text())
            if kind == "weekly":
                rule["weekdays"] = [i for i, check in enumerate(self.day_checks) if check.isChecked()]
        selected = []
        for index in range(self.group_list.count()):
            item = self.group_list.item(index)
            if item.checkState() == Qt.Checked:
                selected.append(item.data(Qt.UserRole))
        return {
            "id": self.task.get("id"), "name": self.name_edit.text().strip(),
            "content": self.content_edit.toPlainText(), "group_ids": selected, "rule": rule,
            "starts_at": date_value(self.start_edit),
            "ends_at": date_value(self.end_edit) if self.end_check.isChecked() else None,
            "max_runs": self.count_spin.value() if self.count_check.isChecked() else None,
            "run_count": self.task.get("run_count", 0),
        }

    def refresh_preview(self, *args):
        self.count_spin.setEnabled(self.count_check.isChecked())
        self.end_edit.setEnabled(self.end_check.isChecked())
        content = self.content_edit.toPlainText()
        size = len(content.encode("utf-8"))
        self.bytes_label.setText(f"{size} / 4096 UTF-8 字节")
        self.preview_edit.setMarkdown(content)
        try:
            task = self.collect_task()
            validate_task(task)
            values = preview_times(task, int(time.time()))
            self.plan_preview.setText("\n".join(format_time(value) for value in values)
                                      or "当前条件下已无未来发送时间，请调整计划。")
        except ValueError as exc:
            self.plan_preview.setText(str(exc))

    def save(self, enable):
        try:
            task = self.collect_task()
            self.saved_task_id = self.store.save_task(task, enable=enable)
        except (ValueError, sqlite3.Error, OSError) as exc:
            QMessageBox.warning(self, "请检查任务设置", str(exc))
            return
        self.accept()
