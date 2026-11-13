"""
个人发送面板
通过企业微信 PC 端模拟操作，向指定联系人逐一发送消息。

设计对齐 group_panel.py + send_panel.py 的 PyQt5 风格。
"""

import os
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QPushButton, QLabel, QLineEdit, QComboBox, QCheckBox,
    QTextEdit, QPlainTextEdit,
    QDialog, QFormLayout, QDialogButtonBox,
    QMessageBox, QFileDialog, QGroupBox, QSpinBox, QDoubleSpinBox,
    QFrame, QListWidget, QRadioButton, QButtonGroup
)
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont, QColor
from typing import List, Dict, Callable, Optional
from core.config import PersonConfig
from core.personal_sender import PersonalSender
from core.personal_template import parse_template_file
from core.sender import format_results
from PyQt5.QtWidgets import QToolButton, QMenu, QWidgetAction, QCheckBox
from PyQt5.QtCore import pyqtSignal


class MultiCheckFilterButton(QToolButton):
    """多选筛选按钮：点击弹出带复选框的菜单，可勾选多项；不勾选=全部"""
    changed = pyqtSignal()

    def __init__(self, label="全部", parent=None):
        super().__init__(parent)
        self._label = label
        self._checkboxes = {}   # text -> QCheckBox
        self.setPopupMode(QToolButton.InstantPopup)
        self.setText(label)
        self.setFixedWidth(110)          # ← 新增：固定宽度，不随文字变化
        self.setStyleSheet(              # ← 新增：让它看起来更像下拉框而不是普通按钮
            "QToolButton { text-align: left; padding: 3px 6px; }"
            "QToolButton::menu-indicator { subcontrol-position: right center; }"
        )
        self.menu = QMenu(self)
        self.setMenu(self.menu)

    def set_options(self, options: list):
        """重建菜单选项，尽量保留之前已勾选的项"""
        old_checked = set(self.checked_items())
        self.menu.clear()
        self._checkboxes.clear()
        self.menu.setMinimumWidth(self.width()) 
        for text in options:
            cb = QCheckBox(text, self.menu)
            cb.setChecked(text in old_checked)
            cb.stateChanged.connect(self._on_changed)
            action = QWidgetAction(self.menu)
            action.setDefaultWidget(cb)
            self.menu.addAction(action)
            self._checkboxes[text] = cb
        self._update_text()

    def checked_items(self) -> list:
        return [text for text, cb in self._checkboxes.items() if cb.isChecked()]

    def _on_changed(self, _state):
        self._update_text()
        self.changed.emit()

    def _update_text(self):
        n = len(self.checked_items())
        self.setText(f"{self._label}({n})" if n else self._label)


# ── 联系人编辑对话框 ───────────────────────────────────────────────────────

class PersonEditDialog(QDialog):
    """添加 / 编辑单个联系人"""

    def __init__(self, parent=None, person: dict = None, units: list = None):
        super().__init__(parent)
        self.person = person
        self.units  = units or []
        self.init_ui()

    def init_ui(self):
        self.setWindowTitle("编辑联系人" if self.person else "添加联系人")
        self.setMinimumWidth(380)

        layout = QFormLayout(self)
        layout.setSpacing(10)

        # 姓名
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("请输入姓名")
        if self.person:
            self.name_edit.setText(self.person.get("name", ""))
        layout.addRow("姓名：", self.name_edit)

        # 所在单位
        self.unit_combo = QComboBox()
        self.unit_combo.setEditable(True)
        self.unit_combo.addItem("")
        self.unit_combo.addItems(self.units)
        if self.person:
            unit = self.person.get("unit", "")
            idx  = self.unit_combo.findText(unit)
            self.unit_combo.setCurrentIndex(idx if idx >= 0 else 0)
            if idx < 0:
                self.unit_combo.setCurrentText(unit)
        self.unit_combo.setPlaceholderText("选择或输入所在单位")
        layout.addRow("所在单位：", self.unit_combo)

        #自定义标签
        self.tag_edit = QLineEdit()
        self.tag_edit.setPlaceholderText("多个标签用英文逗号分隔")
        if self.person:
            self.tag_edit.setText(self.person.get("tag", ""))
        layout.addRow("自定义标签：", self.tag_edit)


        # 按钮
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def _validate_and_accept(self):
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "提示", "请输入姓名")
            return
        self.accept()

    def get_data(self) -> dict:
        #selector = str(self.selector_spin.value()) if self.has_selector_cb.isChecked() else None
        return {
            "name":     self.name_edit.text().strip(),
            "unit":     self.unit_combo.currentText().strip(),
            "tag":  self.tag_edit.text().strip(),
            # "selector": selector,
        }


# ── 主面板 ─────────────────────────────────────────────────────────────────

class PersonalPanel(QWidget):
    """个人发送面板"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.person_config = PersonConfig()
        self.template_rows: List[dict] = []   # 模板消息模式下解析出的逐人数据
        self.init_ui()
        self.refresh_table()
        self.refresh_tasks()

    # ── UI 初始化 ──────────────────────────────────────────────────────────

    def init_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        splitter = QSplitter(Qt.Horizontal)

        # ── 左侧：联系人管理 ──────────────────────────────────────────────
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 4, 0)
        left_layout.setSpacing(6)

        # 工具栏
        toolbar = QHBoxLayout()
        self.add_btn    = QPushButton("➕ 添加")
        self.edit_btn   = QPushButton("✏️ 编辑")
        self.delete_btn = QPushButton("🗑️ 删除")
        for btn in (self.add_btn, self.edit_btn, self.delete_btn):
            toolbar.addWidget(btn)
        toolbar.addStretch()
        self.import_csv_btn   = QPushButton("📥 CSV")
        self.import_excel_btn = QPushButton("📥 Excel")
        self.export_btn       = QPushButton("📤 导出")
        for btn in (self.import_csv_btn, self.import_excel_btn, self.export_btn):
            toolbar.addWidget(btn)
        left_layout.addLayout(toolbar)

        # 筛选栏
        filter_row = QHBoxLayout()
        # filter_row.addWidget(QLabel("所在单位："))
        # # self.unit_filter = QComboBox()
        # # self.unit_filter.addItem("全部")
        # # self.unit_filter.setMinimumWidth(140)
        # # filter_row.addWidget(self.unit_filter)
        # self.unit_filter = MultiCheckFilterButton("所在单位")
        # filter_row.addWidget(self.unit_filter)

        # filter_row.addWidget(QLabel("搜索："))
        # self.search_edit = QLineEdit()
        # self.search_edit.setPlaceholderText("姓名关键词…")
        # self.search_edit.setMaximumWidth(160)
        # filter_row.addWidget(self.search_edit)

        self.select_all_btn = QPushButton("全选")
        self.select_all_btn.setFixedWidth(58)
        self.select_all_btn.setToolTip("全选/取消全选当前可见联系人的启用复选框")
        # filter_row.addWidget(self.select_all_btn)

        # filter_row.addWidget(QLabel("标签："))
        # # self.tag_filter = QComboBox()
        # # self.tag_filter.addItem("全部")
        # # self.tag_filter.setMinimumWidth(120)
        # # filter_row.addWidget(self.tag_filter)
        # self.tag_filter = MultiCheckFilterButton("标签")
        # filter_row.addWidget(self.tag_filter)
        self.unit_filter = MultiCheckFilterButton("所在单位")
        filter_row.addWidget(self.unit_filter)

        filter_row.addWidget(QLabel("搜索："))
        self.search_edit = QLineEdit()
        ...
        filter_row.addWidget(self.search_edit)

        filter_row.addWidget(self.select_all_btn)

        self.tag_filter = MultiCheckFilterButton("标签")
        filter_row.addWidget(self.tag_filter)

        filter_row.addStretch()
        left_layout.addLayout(filter_row)

        

        # 联系人表格
        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["✓", "姓名", "所在单位", "自定义标签"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.setColumnWidth(0, 36)
        self.table.setColumnWidth(3, 70)
        self.table.setColumnHidden(4, True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        # self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        left_layout.addWidget(self.table)

        # 统计行
        self.stats_label = QLabel("共 0 位联系人")
        self.stats_label.setStyleSheet("color: #666; font-size: 12px;")
        left_layout.addWidget(self.stats_label)

        splitter.addWidget(left)

        # ── 右侧：消息 + 发送控制 ─────────────────────────────────────────
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(4, 0, 0, 0)
        right_layout.setSpacing(8)

        # 发送模式开关（统一消息 / 模板消息）
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("发送模式："))
        self.mode_unified_radio  = QRadioButton("统一消息")
        self.mode_template_radio = QRadioButton("模板消息")
        self.mode_group = QButtonGroup(self)
        self.mode_group.addButton(self.mode_unified_radio)
        self.mode_group.addButton(self.mode_template_radio)
        self.mode_unified_radio.setChecked(True)
        self.mode_unified_radio.setToolTip("所有联系人发送同一段消息")
        self.mode_template_radio.setToolTip("从模板文件导入，每人内容独立")
        mode_row.addWidget(self.mode_unified_radio)
        mode_row.addWidget(self.mode_template_radio)
        mode_row.addStretch()
        right_layout.addLayout(mode_row)

        # 消息编辑区
        self.msg_group = QGroupBox("消息内容")
        msg_inner  = QVBoxLayout(self.msg_group)
        self.msg_edit = QPlainTextEdit()
        self.msg_edit.setPlaceholderText(
            "请输入发送内容（纯文本）。\n\n"
            "个人会话不支持 Markdown 格式，内容将原文发送。"
        )
        self.msg_edit.setMinimumHeight(140)
        msg_inner.addWidget(self.msg_edit)

        char_row = QHBoxLayout()
        self.char_count_label = QLabel("0 字")
        self.char_count_label.setStyleSheet("color: #888; font-size: 11px;")
        char_row.addStretch()
        char_row.addWidget(self.char_count_label)
        msg_inner.addLayout(char_row)
        right_layout.addWidget(self.msg_group)

        # 模板消息区（模板模式下显示）
        self.tpl_group = QGroupBox("模板消息")
        tpl_inner = QVBoxLayout(self.tpl_group)

        tpl_btn_row = QHBoxLayout()
        self.download_tpl_btn = QPushButton("📄 下载模板")
        self.download_tpl_btn.setToolTip("导出一份空白模板（含表头与示例行），照填后再点「导入模板」")
        self.import_tpl_btn = QPushButton("📥 导入模板")
        self.clear_tpl_btn  = QPushButton("🗑️ 清空模板")
        tpl_btn_row.addWidget(self.download_tpl_btn)
        tpl_btn_row.addWidget(self.import_tpl_btn)
        tpl_btn_row.addWidget(self.clear_tpl_btn)
        tpl_btn_row.addStretch()
        self.tpl_count_label = QLabel("共 0 条")
        self.tpl_count_label.setStyleSheet("color: #666; font-size: 12px;")
        tpl_btn_row.addWidget(self.tpl_count_label)
        tpl_inner.addLayout(tpl_btn_row)

        # 附件来源：所有人相同（用下方附件列表） / 每人不同（读模板「附件」列）
        attach_row = QHBoxLayout()
        attach_row.addWidget(QLabel("附件来源："))
        self.att_same_radio     = QRadioButton("所有人相同")
        self.att_per_person_radio = QRadioButton("每人不同（读模板「附件」列）")
        self.att_group = QButtonGroup(self)
        self.att_group.addButton(self.att_same_radio)
        self.att_group.addButton(self.att_per_person_radio)
        self.att_same_radio.setChecked(True)
        self.att_same_radio.setToolTip("所有收件人发送下方「附件文件」列表里的同一份文件")
        self.att_per_person_radio.setToolTip("每人的附件取模板「附件」列（多个路径用 ; 分隔）")
        attach_row.addWidget(self.att_same_radio)
        attach_row.addWidget(self.att_per_person_radio)
        attach_row.addStretch()
        tpl_inner.addLayout(attach_row)

        self.tpl_table = QTableWidget()
        self.tpl_table.setColumnCount(4)
        self.tpl_table.setHorizontalHeaderLabels(["姓名", "部门", "附件", "消息预览"])
        self.tpl_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.tpl_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.tpl_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.tpl_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.tpl_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tpl_table.setAlternatingRowColors(True)
        self.tpl_table.setMaximumHeight(140)
        tpl_inner.addWidget(self.tpl_table)

        tpl_tip = QLabel(
            "模板文件需包含三列：姓名 / 部门 / 消息；"
            "「每人不同」时另需一列「附件」（多个路径用 ; 分隔，支持 .xlsx、.csv）"
        )
        tpl_tip.setStyleSheet("color: #888; font-size: 11px;")
        tpl_tip.setWordWrap(True)
        tpl_inner.addWidget(tpl_tip)

        self.tpl_group.setVisible(False)
        right_layout.addWidget(self.tpl_group)

        # 文件上传区
        self.file_group = QGroupBox("附件文件（可选）")
        file_inner = QVBoxLayout(self.file_group)
        self.file_group_hint = QLabel("")   # 模板模式下「每人不同」时提示禁用原因
        self.file_group_hint.setStyleSheet("color: #e65100; font-size: 11px;")
        self.file_group_hint.setWordWrap(True)
        self.file_group_hint.setVisible(False)
        file_inner.addWidget(self.file_group_hint)

        file_btn_row = QHBoxLayout()
        self.add_file_btn = QPushButton("➕ 添加文件")
        self.clear_file_btn = QPushButton("🗑️ 清空")
        file_btn_row.addWidget(self.add_file_btn)
        file_btn_row.addWidget(self.clear_file_btn)
        file_btn_row.addStretch()
        file_inner.addLayout(file_btn_row)
 
        self.file_list = QListWidget()
        self.file_list.setMaximumHeight(100)
        self.file_list.setAlternatingRowColors(True)
        file_inner.addWidget(self.file_list)
 
        right_layout.addWidget(self.file_group)
        # 文件上传区end

        # 发送参数
        param_group = QGroupBox("发送参数")
        param_grid  = QHBoxLayout(param_group)

        def _param_row(label, widget):
            row = QHBoxLayout()
            row.addWidget(QLabel(label))
            row.addWidget(widget)
            return row

        self.search_delay_spin = QDoubleSpinBox()
        self.search_delay_spin.setRange(0.5, 10.0)
        self.search_delay_spin.setValue(1.5)
        self.search_delay_spin.setSuffix(" 秒")
        self.search_delay_spin.setSingleStep(0.5)

        self.send_interval_spin = QDoubleSpinBox()
        self.send_interval_spin.setRange(1.0, 30.0)
        self.send_interval_spin.setValue(2.0)
        self.send_interval_spin.setSuffix(" 秒")
        self.send_interval_spin.setSingleStep(0.5)

        param_grid.addLayout(_param_row("搜索等待：", self.search_delay_spin))
        param_grid.addSpacing(16)
        param_grid.addLayout(_param_row("发送间隔：", self.send_interval_spin))
        param_grid.addStretch()
        right_layout.addWidget(param_group)

        # 发送前提示
        tip_frame = QFrame()
        tip_frame.setStyleSheet(
            "QFrame { background: #fff8e1; border: 1px solid #ffe082; border-radius: 4px; padding: 4px; }"
        )
        tip_layout = QVBoxLayout(tip_frame)
        tip_layout.setContentsMargins(8, 6, 8, 6)
        tip_label = QLabel(
            "⚠️  本机自动化发送前请确认：企业微信 PC 端已打开并登录。\n"
            "点击「提交审批」只登记任务，审批人通过后才自动发送。\n"
            "自动发送期间请勿操作鼠标和键盘；紧急停止：将鼠标快速移到屏幕<b>左上角</b>。"
        )
        tip_label.setTextFormat(Qt.RichText)
        tip_label.setStyleSheet("font-size: 12px; color: #5d4037;")
        tip_label.setWordWrap(True)
        tip_layout.addWidget(tip_label)
        right_layout.addWidget(tip_frame)

        # 控制按钮
        ctrl_row = QHBoxLayout()
        self.send_btn = QPushButton("🚀 提交审批")
        self.send_btn.setMinimumHeight(34)
        self.send_btn.setStyleSheet(
            "QPushButton { background: #1976d2; color: white; border-radius: 4px; font-size: 13px; }"
            "QPushButton:hover { background: #1565c0; }"
            "QPushButton:disabled { background: #bdbdbd; }"
        )
        ctrl_row.addWidget(self.send_btn)
        right_layout.addLayout(ctrl_row)

        # 我的任务区（默认只看本人发送面板提交的个人任务）
        task_group = QGroupBox("我的任务")
        task_inner = QVBoxLayout(task_group)

        task_btn_row = QHBoxLayout()
        self.refresh_task_btn = QPushButton("🔄 刷新")
        self.refresh_task_btn.clicked.connect(self.refresh_tasks)
        task_btn_row.addWidget(self.refresh_task_btn)

        self.task_type_filter = QComboBox()
        self.task_type_filter.addItem("个人发送", "personal")
        self.task_type_filter.addItem("群组发送", "group")
        self.task_type_filter.addItem("全部", None)
        self.task_type_filter.currentIndexChanged.connect(self.refresh_tasks)
        task_btn_row.addWidget(self.task_type_filter)

        hint = QLabel("（双击任务行查看逐条结果与失败截图）")
        hint.setStyleSheet("color: #757575;")
        task_btn_row.addWidget(hint)
        task_btn_row.addStretch()
        task_inner.addLayout(task_btn_row)

        self.task_table = QTableWidget()
        self.task_table.setColumnCount(5)
        self.task_table.setHorizontalHeaderLabels(["ID", "类型", "状态", "提交时间", "结果"])
        self.task_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.task_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.task_table.setColumnWidth(0, 40)
        self.task_table.setColumnWidth(1, 50)
        self.task_table.setColumnWidth(3, 130)
        self.task_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.task_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.task_table.setAlternatingRowColors(True)
        self.task_table.setMaximumHeight(180)
        self.task_table.itemDoubleClicked.connect(self._show_task_detail)
        task_inner.addWidget(self.task_table)

        right_layout.addWidget(task_group)

        splitter.addWidget(right)
        splitter.setSizes([480, 420])
        root.addWidget(splitter)

        # ── 信号连接 ──────────────────────────────────────────────────────
        self.add_btn.clicked.connect(self.add_person)
        self.edit_btn.clicked.connect(self.edit_person)
        self.delete_btn.clicked.connect(self.delete_person)
        self.import_csv_btn.clicked.connect(self.import_csv)
        self.import_excel_btn.clicked.connect(self.import_excel)
        self.export_btn.clicked.connect(self.export_persons)
        # self.unit_filter.currentIndexChanged.connect(self.filter_table)
        self.search_edit.textChanged.connect(self.filter_table)
        # new
        # self.tag_filter.currentIndexChanged.connect(self.filter_table)
        self.unit_filter.changed.connect(self.filter_table)
        self.tag_filter.changed.connect(self.filter_table)

        self.send_btn.clicked.connect(self.start_send)
        self.msg_edit.textChanged.connect(self._update_char_count)
        self.table.doubleClicked.connect(self.edit_person)
        # new
        self.add_file_btn.clicked.connect(self.add_files)
        self.clear_file_btn.clicked.connect(self.file_list.clear)
        self.select_all_btn.clicked.connect(self.toggle_select_all)
        # 发送模式 / 模板
        self.mode_unified_radio.toggled.connect(self._on_mode_changed)
        self.att_per_person_radio.toggled.connect(self._sync_file_group_state)
        self.download_tpl_btn.clicked.connect(self.download_template)
        self.import_tpl_btn.clicked.connect(self.import_template)
        self.clear_tpl_btn.clicked.connect(self.clear_template)

    # ── 表格管理 ──────────────────────────────────────────────────────────

    def refresh_table(self):
        """刷新联系人表格"""
        self.table.setRowCount(0)
        persons = self.person_config.persons
        self.table.setRowCount(len(persons))

        for i, p in enumerate(persons):
            # 启用复选框
            cb = QCheckBox()
            cb.setChecked(p.get("enabled", True))
            cb.stateChanged.connect(
                lambda state, pid=p["id"]: self.person_config.update_person(pid, enabled=(state == Qt.Checked))
            )
            cell = QWidget()
            cb_lay = QHBoxLayout(cell)
            cb_lay.addWidget(cb)
            cb_lay.setAlignment(Qt.AlignCenter)
            cb_lay.setContentsMargins(0, 0, 0, 0)
            self.table.setCellWidget(i, 0, cell)

            self.table.setItem(i, 1, QTableWidgetItem(p.get("name", "")))
            self.table.setItem(i, 2, QTableWidgetItem(p.get("unit", "")))

            tag_edit = QLineEdit(p.get("tag", ""))
            tag_edit.setPlaceholderText("标签1,标签2")
            tag_edit.setFrame(False)
            tag_edit.editingFinished.connect(
                lambda pid=p["id"], w=tag_edit: self._on_tag_edited(pid, w)
            )
            self.table.setCellWidget(i, 3, tag_edit)
            # selector = p.get("selector")
            # sel_item = QTableWidgetItem(f"第{selector}条" if selector else "")
            # sel_item.setTextAlignment(Qt.AlignCenter)
            # self.table.setItem(i, 3, sel_item)

            self.table.setItem(i, 4, QTableWidgetItem(str(p["id"])))

        self._update_stats()
        self._update_unit_filter()
        self._update_tag_filter()

    # def filter_table(self):
    #     unit_sel    = self.unit_filter.currentText()
    #     tag_sel     = self.tag_filter.currentText()
    #     search_text = self.search_edit.text().strip().lower()

    #     for row in range(self.table.rowCount()):
    #         show = True

    #         if unit_sel and unit_sel != "全部":
    #             unit_item = self.table.item(row, 2)
    #             if not unit_item or unit_item.text() != unit_sel:
    #                 show = False

    #         tag_widget = self.table.cellWidget(row, 3)
    #         tag_text = tag_widget.text() if tag_widget else ""
    #         tags = [t.strip() for t in tag_text.split(",") if t.strip()]

    #         if show and tag_sel and tag_sel != "全部":
    #             if tag_sel not in tags:
    #                 show = False

    #         if show and search_text:
    #             name_item = self.table.item(row, 1)
    #             name_text = name_item.text().lower() if name_item else ""
    #             if search_text not in name_text and search_text not in tag_text.lower():
    #                 show = False

    #         self.table.setRowHidden(row, not show)

    #     self._update_stats()
    #     self.select_all_btn.setText("全选")
    def filter_table(self):
        units_sel   = self.unit_filter.checked_items()   # 空列表 = 不筛选(全部)
        tags_sel    = self.tag_filter.checked_items()
        search_text = self.search_edit.text().strip().lower()

        for row in range(self.table.rowCount()):
            show = True

            if units_sel:
                unit_item = self.table.item(row, 2)
                unit_text = unit_item.text() if unit_item else ""
                if unit_text not in units_sel:
                    show = False

            tag_widget = self.table.cellWidget(row, 3)
            tag_text = tag_widget.text() if tag_widget else ""
            tags = [t.strip() for t in tag_text.split(",") if t.strip()]

            if show and tags_sel:
                if not any(t in tags_sel for t in tags):
                    show = False

            if show and search_text:
                name_item = self.table.item(row, 1)
                name_text = name_item.text().lower() if name_item else ""
                if search_text not in name_text and search_text not in tag_text.lower():
                    show = False

            self.table.setRowHidden(row, not show)

        self._update_stats()
        self.select_all_btn.setText("全选")

    def _update_stats(self):
        visible = sum(
            1 for row in range(self.table.rowCount())
            if not self.table.isRowHidden(row)
        )
        total   = len(self.person_config.persons)
        enabled = len(self.person_config.get_enabled_persons())
        self.stats_label.setText(
            f"共 {total} 位联系人，已启用 {enabled} 位"
            + (f"，当前筛选 {visible} 位" if visible != total else "")
        )

    # def _update_unit_filter(self):
    #     current = self.unit_filter.currentText()
    #     self.unit_filter.blockSignals(True)
    #     self.unit_filter.clear()
    #     self.unit_filter.addItem("全部")
    #     self.unit_filter.addItems(self.person_config.get_units())
    #     idx = self.unit_filter.findText(current)
    #     self.unit_filter.setCurrentIndex(idx if idx >= 0 else 0)
    #     self.unit_filter.blockSignals(False)

    # def _update_tag_filter(self):
    #     current = self.tag_filter.currentText()
    #     self.tag_filter.blockSignals(True)
    #     self.tag_filter.clear()
    #     self.tag_filter.addItem("全部")
    #     self.tag_filter.addItems(self.person_config.get_tags())
    #     idx = self.tag_filter.findText(current)
    #     self.tag_filter.setCurrentIndex(idx if idx >= 0 else 0)
    #     self.tag_filter.blockSignals(False)
    def _update_unit_filter(self):
        self.unit_filter.set_options(self.person_config.get_units())

    def _update_tag_filter(self):
        self.tag_filter.set_options(self.person_config.get_tags())

    def toggle_select_all(self):
        """全选 / 取消全选当前可见行的启用复选框"""
        # 判断当前可见行是否全部已启用，决定这次点击是"全选"还是"取消全选"
        visible_rows = [
            row for row in range(self.table.rowCount())
            if not self.table.isRowHidden(row)
        ]
        if not visible_rows:
            return

        # 统计可见行中已启用的数量
        all_checked = all(
            self._get_row_checkbox(row).isChecked()
            for row in visible_rows
        )
        target_state = not all_checked  # 全部已选则取消，否则全选

        for row in visible_rows:
            cb = self._get_row_checkbox(row)
            if cb:
                cb.setChecked(target_state)

        self.select_all_btn.setText("取消全选" if target_state else "全选")

    # def _get_row_checkbox(self, row: int) -> QCheckBox | None:
    def _get_row_checkbox(self, row: int) -> Optional[QCheckBox]:
        """取某行第 0 列的 QCheckBox"""
        cell = self.table.cellWidget(row, 0)
        if cell is None:
            return None
        for child in cell.children():
            if isinstance(child, QCheckBox):
                return child
        return None
    def _update_char_count(self):
        n = len(self.msg_edit.toPlainText())
        self.char_count_label.setText(f"{n} 字")

    # ── 联系人 CRUD ────────────────────────────────────────────────────────

    def add_person(self):
        dlg = PersonEditDialog(self, units=self.person_config.get_units())
        if dlg.exec_() == QDialog.Accepted:
            data = dlg.get_data()
            self.person_config.add_person(data["name"], data["unit"], data["tag"])
            self.refresh_table()

    def edit_person(self):
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, "提示", "请先选择要编辑的联系人")
            return
        id_item = self.table.item(row, 4)
        if not id_item:
            return
        person = self.person_config.get_person(int(id_item.text()))
        if not person:
            return

        dlg = PersonEditDialog(self, person=person, units=self.person_config.get_units())
        if dlg.exec_() == QDialog.Accepted:
            data = dlg.get_data()
            self.person_config.update_person(
                person["id"],
                name=data["name"],
                unit=data["unit"],
                tag=data["tag"],
                # selector=data["selector"],
            )
            self.refresh_table()
    def _on_tag_edited(self, pid, widget):
        self.person_config.update_person(pid, tag=widget.text().strip())
        self._update_tag_filter()   # 新标签要能马上出现在筛选下拉框里

    # def delete_person(self):
    #     row = self.table.currentRow()
    #     if row < 0:
    #         QMessageBox.information(self, "提示", "请先选择要删除的联系人")
    #         return
    #     id_item = self.table.item(row, 4)
    #     if not id_item:
    #         return
    #     person = self.person_config.get_person(int(id_item.text()))
    #     if not person:
    #         return

    #     reply = QMessageBox.question(
    #         self, "确认删除",
    #         f"确定要删除联系人【{person['name']}】吗？",
    #         QMessageBox.Yes | QMessageBox.No,
    #     )
    #     if reply == QMessageBox.Yes:
    #         self.person_config.delete_person(person["id"])
    #         self.refresh_table()
    def delete_person(self):
        """删除选中的联系人（支持多选）"""
        selected_rows = list(set(idx.row() for idx in self.table.selectedIndexes()))
        if not selected_rows:
            QMessageBox.information(self, "提示", "请先选择要删除的联系人")
            return

        reply = QMessageBox.question(
            self, "确认删除",
            f"确定要删除选中的 {len(selected_rows)} 位联系人吗？",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        # 收集要删除的 ID
        ids_to_delete = []
        for row in selected_rows:
            id_item = self.table.item(row, 4)
            if id_item:
                ids_to_delete.append(int(id_item.text()))

        for pid in ids_to_delete:
            self.person_config.delete_person(pid)

        self.refresh_table()

    # ── 导入 / 导出 ────────────────────────────────────────────────────────

    def import_csv(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择 CSV 文件", "", "CSV 文件 (*.csv);;所有文件 (*)")
        if not path:
            return
        ok, fail, errors = self.person_config.import_from_csv(path)
        self._show_import_result(ok, fail, errors)
        self.refresh_table()

    def import_excel(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择 Excel 文件", "", "Excel 文件 (*.xlsx *.xls);;所有文件 (*)")
        if not path:
            return
        ok, fail, errors = self.person_config.import_from_excel(path)
        self._show_import_result(ok, fail, errors)
        self.refresh_table()

    def _show_import_result(self, ok, fail, errors, unit="位"):
        msg = f"导入完成\n成功：{ok} {unit}\n失败：{fail} {unit}"
        if errors:
            msg += "\n\n错误详情：\n" + "\n".join(errors[:10])
            if len(errors) > 10:
                msg += f"\n…还有 {len(errors) - 10} 条错误"
        QMessageBox.information(self, "导入结果", msg)

    def export_persons(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "导出联系人列表", "联系人列表", "CSV 文件 (*.csv)"
        )
        if not path:
            return
        if not path.endswith(".csv"):
            path += ".csv"
        if self.person_config.export_to_csv(path):
            QMessageBox.information(self, "导出成功", f"已导出到：\n{path}")
        else:
            QMessageBox.warning(self, "导出失败", "导出时发生错误，请检查文件路径")

    # ── 模板消息 ───────────────────────────────────────────────────────────

    def _on_mode_changed(self):
        """统一消息 / 模板消息 互斥切换（只让一个组可见，右侧高度不膨胀）"""
        unified = self.mode_unified_radio.isChecked()
        self.msg_group.setVisible(unified)
        self.tpl_group.setVisible(not unified)
        self._sync_file_group_state()

    def _attachments_per_person(self) -> bool:
        """当前是否处于「模板消息 + 每人附件不同」"""
        return self.mode_template_radio.isChecked() and self.att_per_person_radio.isChecked()

    def _sync_file_group_state(self):
        """「每人不同」时全局附件列表无意义，禁用并给出提示"""
        per_person = self._attachments_per_person()
        self.file_group.setEnabled(not per_person)
        self.file_group_hint.setVisible(per_person)
        self.file_group_hint.setText(
            "当前为『每人不同』，附件改由模板「附件」列指定，此列表已停用。" if per_person else ""
        )

    def download_template(self):
        """导出空白模板（表头 + 示例行），供用户照填后导入。

        模板内容随「附件来源」而不同：
          · 所有人相同 → 三列（姓名/部门/消息）
          · 每人不同   → 四列，并在「附件」列给出示例路径
        """
        default_name = "个人发送模板_含附件" if self._attachments_per_person() else "个人发送模板"
        path, selected = QFileDialog.getSaveFileName(
            self, "保存模板文件", default_name,
            "Excel 模板 (*.xlsx);;CSV 模板 (*.csv)",
        )
        if not path:
            return
        if not path.lower().endswith((".xlsx", ".csv")):
            path += ".csv" if "CSV" in selected else ".xlsx"

        with_files = self._attachments_per_person()
        if path.lower().endswith(".xlsx"):
            ok = self._export_template_excel(path, with_files=with_files)
        else:
            ok = self._export_template_csv(path, with_files=with_files)

        if ok:
            if with_files:
                tip = ("请按「姓名 / 部门 / 消息 / 附件」四列填写，保存后点「导入模板」。\n"
                       "「附件」列填文件的完整路径，多个用 ; 分隔；留空表示此人不发附件。")
            else:
                tip = ("请按「姓名 / 部门 / 消息」三列填写，保存后点「导入模板」。\n"
                       "（当前附件来源为「所有人相同」，如要每人不同，请先切换后再下载模板。）")
            QMessageBox.information(self, "模板已保存", f"模板已保存到：\n{path}\n\n{tip}")

    def _export_template_csv(self, path, with_files: bool = False) -> bool:
        import csv
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f)
                if with_files:
                    writer.writerow(["姓名", "部门", "消息", "附件"])
                    writer.writerow([
                        "张三", "一部门", "这是发给张三的示例消息，请整段替换。",
                        r"D:\通知\张三的附件.pdf",
                    ])
                    writer.writerow([
                        "李四", "二部门", "这是发给李四的示例消息，请整段替换。",
                        r"D:\通知\李四的附件.pdf;D:\通知\李四的补充说明.docx",
                    ])
                else:
                    writer.writerow(["姓名", "部门", "消息"])
                    writer.writerow(["张三", "一部门", "这是发给张三的示例消息，请整段替换。"])
                    writer.writerow(["李四", "二部门", "这是发给李四的示例消息，请整段替换。"])
            return True
        except Exception as e:
            QMessageBox.warning(self, "保存失败", f"保存模板时发生错误：\n{e}")
            return False

    def _export_template_excel(self, path, with_files: bool = False) -> bool:
        try:
            from openpyxl import Workbook
        except ImportError:
            QMessageBox.warning(
                self, "缺少依赖",
                "需要安装 openpyxl 库才能生成 Excel 模板，请改用 CSV 模板。"
            )
            return False
        try:
            wb = Workbook()
            ws = wb.active
            ws.title = "模板消息"
            if with_files:
                ws.append(["姓名", "部门", "消息", "附件"])
                ws.append([
                    "张三", "一部门", "这是发给张三的示例消息，请整段替换。",
                    r"D:\通知\张三的附件.pdf",
                ])
                ws.append([
                    "李四", "二部门", "这是发给李四的示例消息，请整段替换。",
                    r"D:\通知\李四的附件.pdf;D:\通知\李四的补充说明.docx",
                ])
            else:
                ws.append(["姓名", "部门", "消息"])
                ws.append(["张三", "一部门", "这是发给张三的示例消息，请整段替换。"])
                ws.append(["李四", "二部门", "这是发给李四的示例消息，请整段替换。"])
            ws.column_dimensions["A"].width = 14
            ws.column_dimensions["B"].width = 16
            ws.column_dimensions["C"].width = 50
            if with_files:
                ws.column_dimensions["D"].width = 46
            wb.save(path)
            return True
        except Exception as e:
            QMessageBox.warning(self, "保存失败", f"保存模板时发生错误：\n{e}")
            return False

    def import_template(self):
        """导入模板文件（姓名 / 部门 / 消息）"""
        path, _ = QFileDialog.getOpenFileName(
            self, "选择模板文件", "", "模板文件 (*.xlsx *.csv);;所有文件 (*)"
        )
        if not path:
            return
        rows, errors = parse_template_file(path)
        if not rows:
            self._show_import_result(0, len(errors), errors, unit="条")
            return
        self.template_rows = rows
        self._refresh_template_table()
        self._show_import_result(len(rows), len(errors), errors, unit="条")

    def clear_template(self):
        """清空已导入的模板数据"""
        if not self.template_rows:
            return
        reply = QMessageBox.question(
            self, "确认清空",
            f"确定要清空已导入的 {len(self.template_rows)} 条模板消息吗？",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        self.template_rows = []
        self._refresh_template_table()

    def _refresh_template_table(self):
        """刷新模板消息表格与计数"""
        self.tpl_table.setRowCount(len(self.template_rows))
        for i, r in enumerate(self.template_rows):
            files = r.get("files") or []
            attach_text = f"{len(files)} 个" if files else "—"
            self.tpl_table.setItem(i, 0, QTableWidgetItem(r.get("name", "")))
            self.tpl_table.setItem(i, 1, QTableWidgetItem(r.get("unit", "")))
            self.tpl_table.setItem(i, 2, QTableWidgetItem(attach_text))
            self.tpl_table.setItem(i, 3, QTableWidgetItem(r.get("content", "")))
        self.tpl_count_label.setText(f"共 {len(self.template_rows)} 条")

    # new
    def add_files(self):
        """选择要发送的文件"""
        from PyQt5.QtWidgets import QFileDialog
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择附件文件", "", "所有文件 (*.*)"
        )
        for path in paths:
            # 避免重复添加
            existing = [self.file_list.item(i).text() for i in range(self.file_list.count())]
            if path not in existing:
                self.file_list.addItem(path)
 
    def get_file_paths(self) -> list:
        """获取已选文件路径列表"""
        return [self.file_list.item(i).text() for i in range(self.file_list.count())]
    # new

    # ── 发送控制 ───────────────────────────────────────────────────────────

    # def _get_target_persons(self) -> list:
    #     """
    #     收集当前筛选条件下所有启用的联系人，转换为 sender 所需格式。
    #     """
    #     unit_sel = self.unit_filter.currentText()
    #     if unit_sel == "全部":
    #         persons = self.person_config.get_enabled_persons()
    #     else:
    #         persons = self.person_config.get_by_unit(unit_sel)

    #     # 只取表格中可见行（尊重姓名搜索进一步过滤）
    #     visible_ids = set()
    #     for row in range(self.table.rowCount()):
    #         if not self.table.isRowHidden(row):
    #             id_item = self.table.item(row, 4)
    #             if id_item:
    #                 visible_ids.add(int(id_item.text()))

    #     return [
    #         {"name": p["name"], "unit": p.get("unit", ""), "selector": p.get("selector")}
    #         for p in persons
    #         if p["id"] in visible_ids
    #     ]
    def _get_target_persons(self) -> list:
        units_sel = self.unit_filter.checked_items()
        if not units_sel:
            persons = self.person_config.get_enabled_persons()
        else:
            persons = [p for p in self.person_config.get_enabled_persons()
                    if p.get("unit") in units_sel]

        visible_ids = set()
        for row in range(self.table.rowCount()):
            if not self.table.isRowHidden(row):
                id_item = self.table.item(row, 4)
                if id_item:
                    visible_ids.add(int(id_item.text()))

        return [
            {"name": p["name"], "unit": p.get("unit", "")}
            for p in persons
            if p["id"] in visible_ids
        ]

    def start_send(self):
        """提交审批任务（不再直接发送，交由审批人审批后执行）"""
        template_mode = self.mode_template_radio.isChecked()

        if template_mode:
            if not self.template_rows:
                QMessageBox.warning(self, "提示", "请先导入模板文件")
                return
            per_person_att = self._attachments_per_person()
            targets = []
            for r in self.template_rows:
                t = {"name": r["name"], "unit": r["unit"], "content": r["content"]}
                if per_person_att:
                    # 明确给出每人的附件（空列表=此人只发文字）
                    t["file_paths"] = list(r.get("files") or [])
                targets.append(t)
            if per_person_att:
                content = f"（模板消息）共 {len(targets)} 人，每人内容与附件独立"
            else:
                content = f"（模板消息）共 {len(targets)} 人，每人内容独立"
        else:
            content = self.msg_edit.toPlainText().strip()
            if not content:
                QMessageBox.warning(self, "提示", "请输入消息内容")
                return
            targets = self._get_target_persons()
            if not targets:
                QMessageBox.warning(self, "提示", "没有符合条件的启用联系人\n请检查所在单位筛选或联系人列表")
                return

        # 检查依赖
        ok, dep_msg = PersonalSender.check_dependencies()
        if not ok:
            QMessageBox.critical(self, "缺少依赖", dep_msg)
            return

        # 确认对话框（两模式分别展示）
        if template_mode:
            first = targets[0]
            first_preview = first["content"][:50] + ("…" if len(first["content"]) > 50 else "")
            if per_person_att:
                n_with_files = sum(1 for t in targets if t.get("file_paths"))
                total_files = sum(len(t.get("file_paths") or []) for t in targets)
                att_desc = f"每人不同（{n_with_files} 人带附件，共 {total_files} 个）"
            else:
                att_desc = f"所有人相同（{len(self.get_file_paths())} 个）"
            confirm_text = (
                f"即将提交以下范围的通知：\n\n"
                f"  目标人数：{len(targets)} 位（模板导入，每人内容独立）\n"
                f"  附件：{att_desc}\n\n"
                f"首条示例（{first['name']}）：\n{first_preview}\n\n"
                f"确认提交审批？"
            )
        else:
            units_sel = self.unit_filter.checked_items()
            unit_desc = f"【{'、'.join(units_sel)}】" if units_sel else "【全部】"
            confirm_text = (
                f"即将提交以下范围的通知：\n\n"
                f"  部门筛选：{unit_desc}\n"
                f"  目标人数：{len(targets)} 位（已启用且可见）\n\n"
                f"消息内容（前50字）：\n{content[:50]}{'…' if len(content) > 50 else ''}\n\n"
                f"确认提交审批？"
            )
        reply = QMessageBox.question(self, "确认提交", confirm_text, QMessageBox.Yes | QMessageBox.No)
        if reply != QMessageBox.Yes:
            return

        # 组装审批任务（复用现有发送内核，由审批人审批后执行）
        from core.approval import PendingTaskStore, AuditLog
        from core.auth import ROLE_OPERATOR, ROLE_LABELS

        task = {
            "type": "personal",
            "content": content,
            "targets": targets,
            # 「每人不同」时附件在 targets[*]["file_paths"]，全局列表置空以免污染
            "file_paths": [] if self._attachments_per_person() else self.get_file_paths(),
            "send_params": {
                "search_delay": self.search_delay_spin.value(),
                "send_interval": self.send_interval_spin.value(),
            },
            "submitter": ROLE_LABELS.get(ROLE_OPERATOR, "经办人"),
            "send_mode": "template" if template_mode else "unified",
        }
        store = PendingTaskStore()
        task = store.add(task)
        audit_kwargs = {
            "task_id": task["id"],
            "operator": task["submitter"],
            "target_summary": f"{len(targets)} 人",
        }
        if template_mode:
            audit_kwargs["detail"] = "模板消息（附件每人不同）" if self._attachments_per_person() else "模板消息"
        AuditLog().log("submit", **audit_kwargs)

        QMessageBox.information(
            self, "已提交审批",
            f"任务已提交审批（编号 #{task['id']}）。\n"
            "审批人通过后才会真实发送。"
        )

        # 刷新任务列表
        self.refresh_tasks()


    def refresh_tasks(self):
        """刷新我的任务列表（按类型筛选：个人发送 / 群组发送 / 全部）"""
        from core.approval import PendingTaskStore

        STATUS_LABELS = {
            "pending": "待审批",
            "approved": "已通过",
            "sending": "发送中",
            "done": "已完成",
            "failed": "失败",
            "rejected": "已驳回",
        }

        store = PendingTaskStore()
        task_type = self.task_type_filter.currentData()   # "personal" / "group" / None
        tasks = [t for t in store.list()
                 if task_type is None or t.get("type") == task_type]

        self.task_table.setRowCount(len(tasks))
        for i, task in enumerate(tasks):
            self.task_table.setItem(i, 0, QTableWidgetItem(str(task.get("id", ""))))
            self.task_table.setItem(i, 1, QTableWidgetItem("个人" if task.get("type") == "personal" else "群组"))

            status = task.get("status", "")
            status_text = STATUS_LABELS.get(status, status)
            status_item = QTableWidgetItem(status_text)
            if status == "done":
                status_item.setForeground(QColor("#2e7d32"))
            elif status == "failed":
                status_item.setForeground(QColor("#c62828"))
            elif status == "pending":
                status_item.setForeground(QColor("#f57f17"))
            self.task_table.setItem(i, 2, status_item)

            self.task_table.setItem(i, 3, QTableWidgetItem(task.get("submitted_at", "")))

            result = task.get("result", "")
            self.task_table.setItem(i, 4, QTableWidgetItem(result))

    def _show_task_detail(self, item: QTableWidgetItem):
        """双击任务行 → 查看发送对象与逐条结果（含失败阶段/备注/截图）"""
        from core.approval import PendingTaskStore

        row = item.row()
        id_item = self.task_table.item(row, 0)
        if id_item is None or not id_item.text().isdigit():
            return
        task = PendingTaskStore().get(int(id_item.text()))
        if task is None:
            return

        STATUS_LABELS = {
            "pending": "待审批", "approved": "已通过", "sending": "发送中",
            "done": "已完成", "failed": "失败", "rejected": "已驳回",
        }
        lines = [
            f"任务 #{task.get('id')}   状态：{STATUS_LABELS.get(task.get('status'), task.get('status', ''))}",
            f"提交时间：{task.get('submitted_at', '')}   经办人：{task.get('submitter', '')}",
        ]

        lines.append("")
        lines.append("── 发送对象 ──")
        for t in task.get("targets") or []:
            lines.append(f"{t.get('name', '')}  [{t.get('unit', '')}]")

        lines.append("")
        lines.append("── 发送结果 ──")
        result_lines = format_results(task)
        lines.extend(result_lines or ["（暂无结果：尚未审批执行，或该任务未产生结果）"])

        # 失败截图：提供按钮用系统默认看图器打开
        shots = [r.get("screenshot") for r in (task.get("results") or []) if r.get("screenshot")]

        dialog = QDialog(self)
        dialog.setWindowTitle(f"任务 #{task.get('id')} 详情")
        dialog.setMinimumSize(680, 480)
        layout = QVBoxLayout(dialog)
        edit = QTextEdit()
        edit.setReadOnly(True)
        edit.setFont(QFont("Consolas", 10))
        edit.setPlainText("\n".join(lines))
        layout.addWidget(edit)

        btn_row = QHBoxLayout()
        open_btn = QPushButton("查看失败截图")
        open_btn.setEnabled(bool(shots))
        open_btn.clicked.connect(lambda: self._open_image(shots[0]))
        btn_row.addWidget(open_btn)
        btn_row.addStretch()
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(dialog.reject)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)
        dialog.exec_()

    @staticmethod
    def _open_image(path: str):
        """用系统默认看图器打开截图"""
        if not path or not os.path.exists(path):
            QMessageBox.information(None, "提示", "截图文件不存在（可能已被清理）。")
            return
        try:
            os.startfile(path)
        except OSError as e:
            QMessageBox.warning(None, "提示", f"无法打开截图：{e}")