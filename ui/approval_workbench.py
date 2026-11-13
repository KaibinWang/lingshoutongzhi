"""
审批工作台
审批人登录后查看待审批队列，勾选批量通过/驳回，通过后执行真实发送。

发送执行复用现有 WebhookSender / PersonalSender 内核，仅在其外围做
"决策先落盘 + 逐条进度落盘"的状态一致性保证，异常中断后可从断点续发。
"""

from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
    QPushButton, QLabel, QTableWidget, QTableWidgetItem, QHeaderView,
    QAbstractItemView, QCheckBox, QTextEdit, QPlainTextEdit, QGroupBox,
    QProgressBar, QMessageBox, QDialog, QDialogButtonBox, QAction,
    QComboBox, QScrollArea,
)
from PyQt5.QtCore import Qt, QThread, QObject, pyqtSignal
from PyQt5.QtGui import QFont, QColor, QPixmap
from pathlib import Path

from core.version import APP_NAME, APP_VERSION, VERSION_DISPLAY
from core.approval import (
    PendingTaskStore, AuditLog, AUDIT_FILE,
    STATUS_PENDING, STATUS_APPROVED, STATUS_SENDING,
    STATUS_DONE, STATUS_FAILED, STATUS_REJECTED,
)
from core.sender import WebhookSender, SendResult, SendLog, STAGE_LABELS, format_results
from core.personal_sender import PersonalSender
from core.auth import ROLE_LABELS


# ── 表格列索引 ─────────────────────────────────────────────
COL_CHECK = 0
COL_ID = 1
COL_TYPE = 2
COL_STATUS = 3
COL_TIME = 4
COL_SUBMITTER = 5
COL_COUNT = 6
COL_HIDDEN_ID = 7

# ── 逐条结果表列索引 ───────────────────────────────────────
COL_R_CHECK = 0
COL_R_NAME = 1
COL_R_UNIT = 2
COL_R_STATUS = 3
COL_R_STAGE = 4
COL_R_DETAIL = 5
COL_R_ELAPSED = 6

TYPE_LABELS = {"group": "群组", "personal": "个人"}
STATUS_LABELS = {
    STATUS_PENDING: "待审批",
    STATUS_APPROVED: "已通过",
    STATUS_SENDING: "发送中",
    STATUS_DONE: "已完成",
    STATUS_FAILED: "失败",
    STATUS_REJECTED: "已驳回",
}


class ApprovalSendWorker(QObject):
    """在子线程中串行执行审批通过的发送任务（复用现有发送内核）。

    改进：由【单个】常驻 QThread 驱动，任务经队列串行处理，
    不再"每任务新建一个 QThread"。旧实现在上一条任务的完成回调里
    同步创建新线程，会因旧线程尚未收尾而丢到新线程的 started→run 投递，
    导致批次中第 2 条任务的 worker.run 不执行、状态卡在 sending。
    """

    progress = pyqtSignal(int, int, str)                 # current, total, target_name
    result = pyqtSignal(object)                          # SendResult
    task_started = pyqtSignal(int)                       # task_id
    task_finished = pyqtSignal(int, int, int, list)      # task_id, success, total, results
    queue_finished = pyqtSignal(bool)                    # 整队列处理完毕，参数=是否被停止

    def __init__(self, store: PendingTaskStore, reviewer: str = "审批人"):
        super().__init__()
        self.store = store
        self.reviewer = reviewer
        self.audit = AuditLog()
        self._queue: list = []
        self._processing = False
        self._stop_flag = False
        self._current_task = None
        self.sender = None

    # ── 主线程经 queued 信号调用的入口 ───────────────────
    def submit(self, task_ids):
        """追加一批任务并开始处理（线程空闲时立即消费队列）"""
        self._stop_flag = False
        self._queue.extend(task_ids)
        self._maybe_start()

    def stop(self):
        """请求停止：结束当前发送，并丢弃后续队列"""
        self._stop_flag = True
        if self.sender is not None:
            self.sender.stop()

    # ── 队列处理（均在 worker 线程内执行）─────────────────
    def _maybe_start(self):
        if self._processing:
            return
        self._processing = True
        try:
            while self._queue:
                if self._stop_flag:
                    self._mark_remaining_failed()
                    self._queue.clear()
                    break
                task_id = self._queue.pop(0)
                self._run_one(task_id)
            self.queue_finished.emit(self._stop_flag)
        finally:
            self._processing = False

    def _mark_remaining_failed(self):
        """停止时把尚未开始的任务标记为失败，避免残留 approved 状态"""
        for tid in self._queue:
            t = self.store.get(tid)
            if t is not None and t.get("status") == STATUS_APPROVED:
                self.store.update_status(tid, STATUS_FAILED, reviewer=self.reviewer,
                                         detail={"result": "已停止"})

    def _run_one(self, task_id: int):
        task = self.store.get(task_id)
        if task is None:
            return
        self._current_task = task

        # 写前提交：先落盘 sending，再执行。
        # 崩溃续发的任务（check_stale_tasks 传入，状态仍为 sending）保留已落盘的
        # sent_targets/remaining_targets，只补发剩余；新任务才重置为全部目标。
        was_sending = task.get("status") == STATUS_SENDING
        self.store.update_status(task_id, STATUS_SENDING, reviewer=self.reviewer)
        if not was_sending:
            self.store.update_progress(task_id, [], list(task.get("targets") or []))
        self.audit.log("execute_start", task_id=task_id, reviewer=self.reviewer,
                       target_summary=self._summary(task))
        self.task_started.emit(task_id)

        remaining = list(task.get("remaining_targets") or task.get("targets") or [])
        try:
            if task.get("type") == "personal":
                self.sender = PersonalSender(
                    search_delay=task.get("send_params", {}).get("search_delay", 1.5),
                    send_interval=task.get("send_params", {}).get("send_interval", 2.0),
                )
                results = self.sender.send_to_persons(
                    remaining,
                    task.get("content", ""),
                    task.get("file_paths") or [],
                    progress_callback=lambda c, t, n: self.progress.emit(c, t, n),
                    result_callback=self._on_result,
                    reviewer=self.reviewer,
                    content_mode="模板" if task.get("send_mode") == "template" else "统一",
                )
            else:
                self.sender = WebhookSender(
                    interval=task.get("send_params", {}).get("interval", 3.0),
                    retries=task.get("send_params", {}).get("retries", 2),
                )
                results = self.sender.send_to_groups(
                    remaining,
                    task.get("content", ""),
                    progress_callback=lambda c, t, n: self.progress.emit(c, t, n),
                    result_callback=self._on_result,
                )
            success = sum(1 for r in results if r.success)
        except Exception as e:
            import traceback
            traceback.print_exc()
            results = [SendResult(task.get("content", ""), False, f"执行异常: {e}")]
            success = 0

        # 结果落盘 + 审计（群组任务复用现有 SendLog 写入历史）
        total = len(results)
        status = STATUS_DONE if success == total else STATUS_FAILED
        self.store.update_status(task_id, status, detail={"result": f"{success}/{total}"})
        if task.get("type") == "group":
            send_log = SendLog()
            for r in results:
                send_log.add(r)
            send_log.save_to_history(
                task.get("content", ""),
                {"total": total, "success": success, "failed": total - success},
            )
        action = "execute_done" if status == STATUS_DONE else "execute_fail"
        self.audit.log(action, task_id=task_id, reviewer=self.reviewer,
                       target_summary=self._summary(task), detail=f"{success}/{total}")

        # 逐条结果落盘：任务结束时一次性写，供界面/事后定位"哪一笔失败、失败原因"
        self.store.update_results(task_id, [self._result_to_dict(r) for r in results])

        self.task_finished.emit(task_id, success, total, list(results))

    @staticmethod
    def _result_to_dict(r) -> dict:
        """SendResult → 纯 dict（JSON 可序列化，供 pending_tasks.json 持久化）"""
        return {
            "name": r.group_name,
            "unit": r.unit,
            "success": r.success,
            "stage": r.stage,
            "message": r.message,
            "detail": r.detail,
            "attempts": r.attempts,
            "elapsed": round(r.elapsed or 0.0, 1),
            "screenshot": r.screenshot,
        }

    def _on_result(self, result: SendResult):
        """每条目标发送完成即落盘进度，供崩溃续发"""
        self.result.emit(result)
        task = self._current_task
        if task is None:
            return
        task_id = task["id"]
        stored = self.store.get(task_id)
        if stored is None:
            return
        sent = list(stored.get("sent_targets") or [])
        remaining = list(stored.get("remaining_targets") or stored.get("targets") or [])
        for t in remaining:
            # 模板模式逐人内容，同姓名跨部门需按 unit 精确匹配；
            # 统一/群组任务 result.unit 为空 → 短路成纯名字匹配（行为不变）。
            if t.get("name") == result.group_name and (not result.unit or t.get("unit", "") == result.unit):
                remaining.remove(t)
                if result.success:
                    sent.append(t)
                break
        self.store.update_progress(task_id, sent, remaining)

    @staticmethod
    def _summary(task: dict) -> str:
        count = len(task.get("targets") or [])
        unit = "群" if task.get("type") == "group" else "人"
        return f"{count} {unit}"


class ApprovalWorkbench(QMainWindow):
    """审批工作台"""

    # 主线程 → worker 线程的任务投递信号（queued 连接，跨线程安全）
    submit_signal = pyqtSignal(list)

    def __init__(self, store=None, auth=None, parent=None):
        super().__init__(parent)
        self.store = store or PendingTaskStore()
        self.auth = auth
        self.audit = AuditLog()
        self.worker = None
        self.thread = None
        self.is_executing = False
        self._live_results: list = []   # 本次执行的逐条结果（dict）
        self._result_rows: list = []    # 结果表当前绑定的数据行（供双击定位）
        self.setWindowTitle(f"审批工作台 - {VERSION_DISPLAY}")
        self.setMinimumSize(960, 640)
        self.init_ui()
        self.check_stale_tasks()
        self.refresh()

    # ── UI 初始化 ─────────────────────────────────────────
    def init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)

        splitter = QSplitter(Qt.Horizontal)

        # 左侧：待审批列表
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 4, 0)

        toolbar = QHBoxLayout()
        self.refresh_btn = QPushButton("🔄 刷新")
        self.refresh_btn.clicked.connect(self.refresh)
        toolbar.addWidget(self.refresh_btn)

        self.status_filter = QComboBox()
        self.status_filter.addItem("待审批", STATUS_PENDING)
        self.status_filter.addItem("全部", None)
        self.status_filter.currentIndexChanged.connect(self.refresh)
        toolbar.addWidget(self.status_filter)

        self.select_all_btn = QPushButton("全选")
        self.select_all_btn.clicked.connect(self.select_all)
        toolbar.addWidget(self.select_all_btn)

        toolbar.addStretch()
        self.stats_label = QLabel("")
        toolbar.addWidget(self.stats_label)
        left_layout.addLayout(toolbar)

        self.table = QTableWidget()
        self.table.setColumnCount(8)
        self.table.setHorizontalHeaderLabels(["✓", "ID", "类型", "状态", "提交时间", "经办人", "目标数", ""])
        self.table.horizontalHeader().setSectionResizeMode(COL_TIME, QHeaderView.Stretch)
        self.table.setColumnWidth(COL_CHECK, 36)
        self.table.setColumnWidth(COL_ID, 50)
        self.table.setColumnWidth(COL_TYPE, 60)
        self.table.setColumnWidth(COL_STATUS, 70)
        self.table.setColumnWidth(COL_SUBMITTER, 70)
        self.table.setColumnWidth(COL_COUNT, 60)
        self.table.setColumnHidden(COL_HIDDEN_ID, True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        left_layout.addWidget(self.table)

        splitter.addWidget(left)

        # 右侧：详情 + 操作
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(4, 0, 0, 0)

        detail_group = QGroupBox("通知内容")
        detail_inner = QVBoxLayout(detail_group)
        self.content_edit = QTextEdit()
        self.content_edit.setReadOnly(True)
        self.content_edit.setMinimumHeight(120)
        detail_inner.addWidget(self.content_edit)
        right_layout.addWidget(detail_group)

        targets_group = QGroupBox("发送对象")
        targets_inner = QVBoxLayout(targets_group)
        self.targets_edit = QPlainTextEdit()
        self.targets_edit.setReadOnly(True)
        targets_inner.addWidget(self.targets_edit)
        right_layout.addWidget(targets_group)

        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        right_layout.addWidget(self.progress_bar)

        # 逐条结果（失败行标红，双击查看失败截图）
        results_group = QGroupBox("逐条结果")
        results_inner = QVBoxLayout(results_group)
        self.results_table = QTableWidget()
        self.results_table.setColumnCount(7)
        self.results_table.setHorizontalHeaderLabels(
            ["", "姓名", "部门", "状态", "失败阶段", "备注", "耗时"]
        )
        self.results_table.setColumnWidth(COL_R_CHECK, 30)
        self.results_table.setColumnWidth(COL_R_NAME, 90)
        self.results_table.setColumnWidth(COL_R_UNIT, 110)
        self.results_table.setColumnWidth(COL_R_STATUS, 50)
        self.results_table.setColumnWidth(COL_R_STAGE, 80)
        self.results_table.setColumnWidth(COL_R_ELAPSED, 60)
        self.results_table.horizontalHeader().setSectionResizeMode(COL_R_DETAIL, QHeaderView.Stretch)
        self.results_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.results_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.results_table.setAlternatingRowColors(True)
        self.results_table.setMinimumHeight(120)
        self.results_table.itemDoubleClicked.connect(self._on_result_row_dclicked)
        results_inner.addWidget(self.results_table)
        right_layout.addWidget(results_group, 1)

        self.status_label = QLabel("就绪")
        right_layout.addWidget(self.status_label)

        btn_row = QHBoxLayout()
        self.approve_btn = QPushButton("✅ 通过并发送")
        self.approve_btn.clicked.connect(self.approve_selected)
        btn_row.addWidget(self.approve_btn)

        self.reject_btn = QPushButton("❌ 驳回")
        self.reject_btn.clicked.connect(self.reject_selected)
        btn_row.addWidget(self.reject_btn)

        self.stop_btn = QPushButton("⏹ 停止发送")
        self.stop_btn.clicked.connect(self.stop_execution)
        self.stop_btn.setEnabled(False)
        btn_row.addWidget(self.stop_btn)

        self.audit_btn = QPushButton("📋 操作日志")
        self.audit_btn.clicked.connect(self.show_audit)
        btn_row.addWidget(self.audit_btn)

        self.token_btn = QPushButton("🔑 修改 Token")
        self.token_btn.clicked.connect(self.change_token)
        btn_row.addWidget(self.token_btn)

        right_layout.addLayout(btn_row)

        splitter.addWidget(right)
        splitter.setSizes([520, 440])
        root.addWidget(splitter)

        self.create_menu_bar()

    def create_menu_bar(self):
        """菜单栏（QMainWindow 原生菜单条，位于 central widget 之上，不挤占布局）"""
        menubar = self.menuBar()
        help_menu = menubar.addMenu("帮助(&H)")
        about = QAction("关于(&A)", self)
        about.triggered.connect(self.show_about)
        help_menu.addAction(about)

    def show_about(self):
        QMessageBox.about(
            self, "关于",
            f"<h3>{APP_NAME}</h3><p>版本: {APP_VERSION}</p>"
            "<p>审批工作台：审核经办人提交的通知任务，通过后执行真实发送；"
            "支持断点续发与全程审计。</p>"
        )

    # ── 列表刷新与勾选 ────────────────────────────────────
    def refresh(self):
        """刷新任务列表；状态筛选：待审批（默认）/ 全部 —— 全部时可选历史任务查看结果"""
        self.table.setRowCount(0)
        status = self.status_filter.currentData()
        tasks = self.store.list(status=status)
        self.table.setRowCount(len(tasks))
        for i, t in enumerate(tasks):
            is_pending = t.get("status") == STATUS_PENDING
            if is_pending:
                # 只有待审批的记录可勾选审批
                cb = QCheckBox()
                cell = QWidget()
                cb_lay = QHBoxLayout(cell)
                cb_lay.addWidget(cb)
                cb_lay.setAlignment(Qt.AlignCenter)
                cb_lay.setContentsMargins(0, 0, 0, 0)
                self.table.setCellWidget(i, COL_CHECK, cell)
            else:
                self.table.removeCellWidget(i, COL_CHECK)
                self.table.setItem(i, COL_CHECK, QTableWidgetItem("—"))

            status_item = QTableWidgetItem(STATUS_LABELS.get(t.get("status"), t.get("status", "")))
            if t.get("status") == STATUS_FAILED:
                status_item.setForeground(QColor("#c62828"))
            elif t.get("status") == STATUS_DONE:
                status_item.setForeground(QColor("#2e7d32"))
            self.table.setItem(i, COL_ID, QTableWidgetItem(str(t.get("id", ""))))
            self.table.setItem(i, COL_TYPE, QTableWidgetItem(TYPE_LABELS.get(t.get("type"), t.get("type", ""))))
            self.table.setItem(i, COL_STATUS, status_item)
            self.table.setItem(i, COL_TIME, QTableWidgetItem(t.get("submitted_at", "")))
            self.table.setItem(i, COL_SUBMITTER, QTableWidgetItem(t.get("submitter", "")))
            self.table.setItem(i, COL_COUNT, QTableWidgetItem(str(len(t.get("targets") or []))))
            self.table.setItem(i, COL_HIDDEN_ID, QTableWidgetItem(str(t.get("id", ""))))
        if status == STATUS_PENDING:
            self.stats_label.setText(f"待审批 {len(tasks)} 条")
        else:
            self.stats_label.setText(f"共 {len(tasks)} 条")

    def select_all(self):
        all_checked = True
        for row in range(self.table.rowCount()):
            cb = self._get_row_checkbox(row)
            if cb is None or not cb.isChecked():
                all_checked = False
                break
        target = not all_checked
        for row in range(self.table.rowCount()):
            cb = self._get_row_checkbox(row)
            if cb is not None:
                cb.setChecked(target)
        self.select_all_btn.setText("取消全选" if target else "全选")

    def _get_row_checkbox(self, row: int):
        cell = self.table.cellWidget(row, COL_CHECK)
        if cell is None:
            return None
        for child in cell.children():
            if isinstance(child, QCheckBox):
                return child
        return None

    def _selected_ids(self) -> list:
        ids = []
        for row in range(self.table.rowCount()):
            cb = self._get_row_checkbox(row)
            if cb is not None and cb.isChecked():
                item = self.table.item(row, COL_HIDDEN_ID)
                if item is not None and item.text().isdigit():
                    ids.append(int(item.text()))
        return ids

    # ── 详情显示 ──────────────────────────────────────────
    def _on_selection_changed(self):
        rows = self.table.selectionModel().selectedRows()
        if rows:
            row = rows[0].row()
            item = self.table.item(row, COL_HIDDEN_ID)
            if item is not None:
                self._show_detail(int(item.text()))

    def _show_detail(self, task_id: int):
        task = self.store.get(task_id)
        if task is None:
            return
        self.content_edit.setPlainText(task.get("content", ""))
        lines = []
        template_mode = task.get("send_mode") == "template"
        for t in task.get("targets") or []:
            if task.get("type") == "group":
                lines.append(f"{t.get('name')}  [{t.get('category', '默认')}]")
            elif template_mode:
                # 模板消息：逐条展示每个人自己的消息，便于审批人核对
                body = t.get("content", "")
                preview = body[:60] + ("…" if len(body) > 60 else "")
                files = t.get("file_paths")
                att = ""
                if files is not None:
                    att = f"  📎 附件 {len(files)} 个" if files else "  （无附件）"
                lines.append(f"{t.get('name')} [{t.get('unit', '')}]\n  {preview}{att}")
            else:
                lines.append(f"{t.get('name')}  [{t.get('unit', '')}]")

        # 发送结果（历史任务）：逐条渲染 成败/阶段/备注/截图
        result_lines = format_results(task)
        if result_lines:
            lines.append("")
            lines.append("── 发送结果 ──")
            lines.extend(result_lines)
        self.targets_edit.setPlainText("\n".join(lines))

        # 非执行中：把该任务已落盘的逐条结果填进结果表（执行中不清屏，避免打断实时结果）
        if not self.is_executing:
            self._refresh_results_table(task.get("results") or [])

    # ── 审批操作 ──────────────────────────────────────────
    def _reviewer(self) -> str:
        role = self.auth.current_role if self.auth else None
        if self.auth and role:
            record = self.auth.records.get(role, {})
            sso_account = record.get("sso_account", "")
            if sso_account:
                return sso_account
        return ROLE_LABELS.get(role, "审批人")

    @staticmethod
    def _summary(task: dict) -> str:
        count = len(task.get("targets") or [])
        unit = "群" if task.get("type") == "group" else "人"
        return f"{count} {unit}"

    def approve_selected(self):
        if self.is_executing:
            QMessageBox.information(self, "提示", "正在执行发送，请稍候")
            return
        ids = self._selected_ids()
        if not ids:
            QMessageBox.information(self, "提示", "请先勾选要审批的记录")
            return
        reply = QMessageBox.question(
            self, "确认通过",
            f"确定通过 {len(ids)} 条记录并立即执行发送吗？",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        reviewer = self._reviewer()
        approved_ids = []
        for tid in ids:
            task = self.store.get(tid)
            if task is None or task.get("status") != STATUS_PENDING:
                continue
            # 决策先落盘，再执行
            self.store.update_status(tid, STATUS_APPROVED, reviewer=reviewer)
            self.audit.log("approve", task_id=tid, reviewer=reviewer,
                           target_summary=self._summary(task))
            approved_ids.append(tid)
        if not approved_ids:
            QMessageBox.information(self, "提示", "没有可审批的记录")
            return
        self.start_execution(approved_ids)

    def reject_selected(self):
        if self.is_executing:
            QMessageBox.information(self, "提示", "正在执行发送，请稍候")
            return
        ids = self._selected_ids()
        if not ids:
            QMessageBox.information(self, "提示", "请先勾选要审批的记录")
            return
        reply = QMessageBox.question(
            self, "确认驳回",
            f"确定驳回 {len(ids)} 条记录吗？",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        reviewer = self._reviewer()
        for tid in ids:
            task = self.store.get(tid)
            if task is None or task.get("status") != STATUS_PENDING:
                continue
            self.store.update_status(tid, STATUS_REJECTED, reviewer=reviewer)
            self.audit.log("reject", task_id=tid, reviewer=reviewer,
                           target_summary=self._summary(task))
        self.refresh()

    # ── 批量执行（单线程串行处理整个队列）─────────────────
    def start_execution(self, task_ids):
        """把任务交给常驻 worker 的队列串行执行"""
        if not task_ids:
            return
        self.is_executing = True
        self._set_controls_enabled(False)
        self._ensure_worker()
        self.submit_signal.emit(list(task_ids))

    def _ensure_worker(self):
        """惰性创建唯一的常驻 QThread + ApprovalSendWorker（只建一次）"""
        if self.thread is not None:
            return
        self.thread = QThread()
        self.worker = ApprovalSendWorker(self.store, reviewer=self._reviewer())
        self.worker.moveToThread(self.thread)
        # 主线程 emit → worker 线程 queued 执行，避免跨线程直接调用
        self.submit_signal.connect(self.worker.submit)
        self.worker.task_started.connect(self._on_task_started)
        self.worker.progress.connect(self._on_progress)
        self.worker.result.connect(self._on_result)
        self.worker.task_finished.connect(self._on_task_finished)
        self.worker.queue_finished.connect(self._on_queue_finished)
        self.thread.start()

    def _on_progress(self, current: int, total: int, name: str):
        self.progress_bar.setValue(current)
        self.status_label.setText(f"[{current}/{total}] {name}")

    def _on_result(self, result):
        """逐条结果实时进表，失败行标红，双击可看失败截图"""
        self._live_results.append(self._norm(result))
        self._refresh_results_table(self._live_results)

    @staticmethod
    def _norm(r) -> dict:
        """SendResult / dict → 统一 dict（结果表与持久化共用同一形状）"""
        if isinstance(r, dict):
            return r
        return {
            "name": r.group_name,
            "unit": r.unit,
            "success": r.success,
            "stage": r.stage,
            "message": r.message,
            "detail": r.detail,
            "elapsed": r.elapsed or 0.0,
            "screenshot": r.screenshot,
        }

    def _refresh_results_table(self, items: list):
        self._result_rows = items
        self.results_table.setRowCount(len(items))
        for i, r in enumerate(items):
            ok = bool(r.get("success"))
            self.results_table.setItem(i, COL_R_CHECK, QTableWidgetItem("✓" if ok else "✗"))
            self.results_table.setItem(i, COL_R_NAME, QTableWidgetItem(str(r.get("name", ""))))
            self.results_table.setItem(i, COL_R_UNIT, QTableWidgetItem(str(r.get("unit", ""))))
            self.results_table.setItem(i, COL_R_STATUS, QTableWidgetItem("成功" if ok else "失败"))
            self.results_table.setItem(
                i, COL_R_STAGE,
                QTableWidgetItem("" if ok else STAGE_LABELS.get(r.get("stage", ""), r.get("stage", "") or "")),
            )
            self.results_table.setItem(
                i, COL_R_DETAIL,
                QTableWidgetItem(str(r.get("detail") or r.get("message") or "")),
            )
            elapsed = r.get("elapsed") or 0
            self.results_table.setItem(i, COL_R_ELAPSED, QTableWidgetItem(f"{elapsed:.1f}"))
            if not ok:
                for c in range(COL_R_ELAPSED + 1):
                    item = self.results_table.item(i, c)
                    if item is not None:
                        item.setForeground(QColor("#c62828"))

    def _on_result_row_dclicked(self, item: QTableWidgetItem):
        """双击结果行 → 失败时弹截图"""
        row = item.row()
        if row < 0 or row >= len(self._result_rows):
            return
        r = self._result_rows[row]
        if r.get("success"):
            QMessageBox.information(self, "提示", "该笔发送成功，无失败截图。")
            return
        self._show_screenshot(str(r.get("screenshot") or ""), str(r.get("name") or ""))

    def _show_screenshot(self, path: str, caption: str = ""):
        """弹窗显示失败截图；路径为空/不存在则提示无截图"""
        if not path or not Path(path).exists():
            QMessageBox.information(self, "失败截图", "无截图（可能当时未打开搜索窗口）。")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(f"失败截图  {caption}".strip())
        dialog.setMinimumSize(640, 480)
        layout = QVBoxLayout(dialog)
        scroll = QScrollArea()
        scroll.setWidgetResizable(False)
        label = QLabel()
        pixmap = QPixmap(path)
        if pixmap.isNull():
            label.setText(f"无法加载截图：{path}")
        else:
            label.setPixmap(pixmap.scaled(1400, 900, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        scroll.setWidget(label)
        layout.addWidget(scroll)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(dialog.reject)
        buttons.clicked.connect(dialog.accept)
        layout.addWidget(buttons)
        dialog.exec_()

    def _on_task_started(self, task_id: int):
        task = self.store.get(task_id)
        total = len(task.get("targets") or []) if task else 0
        self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(True)
        self.status_label.setText(f"正在发送任务 #{task_id}...")
        # 新一次执行开始 → 清空逐条结果表
        self._live_results = []
        self._refresh_results_table([])

    def _on_task_finished(self, task_id: int, success: int, total: int, results: list):
        # 状态与审计已在 worker 内完成，这里仅做界面反馈并刷新列表
        self.status_label.setText(f"任务 #{task_id} 处理完成: 成功 {success}/{total}")
        self.refresh()

    def _on_queue_finished(self, stopped: bool):
        self.is_executing = False
        self._set_controls_enabled(True)
        self.status_label.setText("已停止" if stopped else "执行完成")
        self.progress_bar.setVisible(False)
        self.refresh()

    def stop_execution(self):
        if self.worker is not None:
            self.worker.stop()
        self.status_label.setText("正在停止...")
        self.stop_btn.setEnabled(False)

    def closeEvent(self, event):
        """关闭窗口时停止 worker 并等待线程退出，避免线程销毁告警"""
        if self.worker is not None and self.thread is not None:
            self.worker.stop()
            self.thread.quit()
            self.thread.wait(3000)
        event.accept()

    def _set_controls_enabled(self, enabled: bool):
        self.approve_btn.setEnabled(enabled)
        self.reject_btn.setEnabled(enabled)
        self.refresh_btn.setEnabled(enabled)
        self.select_all_btn.setEnabled(enabled)
        self.stop_btn.setEnabled(not enabled)

    def change_token(self):
        """主动修改当前审批人的 Token"""
        if self.auth is None:
            QMessageBox.information(self, "提示", "当前无登录会话，无法修改")
            return
        from ui.login import ChangeTokenDialog
        from core.auth import ROLE_APPROVER
        dialog = ChangeTokenDialog(self.auth, ROLE_APPROVER, forced=False, parent=self)
        dialog.exec_()

    # ── 崩溃恢复 ──────────────────────────────────────────
    def check_stale_tasks(self):
        """启动时检出中断的 sending 任务，续发或标记失败"""
        stale = self.store.get_stale_sending()
        if not stale:
            return
        resume_ids = []
        for task in stale:
            reply = QMessageBox.question(
                self, "发现中断的发送任务",
                f"任务 #{task['id']} 上次发送被中断。\n"
                f"是否继续发送剩余目标？\n"
                f"（选“是”续发剩余目标，选“否”标记为失败）",
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply == QMessageBox.Yes:
                resume_ids.append(task["id"])
            else:
                self.store.update_status(task["id"], STATUS_FAILED,
                                         detail={"result": "中断标记失败"})
                self.audit.log("recover", task_id=task["id"], reviewer=self._reviewer(),
                               detail="标记失败")
        if resume_ids:
            self.start_execution(resume_ids)

    # ── 审计查看 ──────────────────────────────────────────
    def show_audit(self):
        lines = []
        if AUDIT_FILE.exists():
            with open(AUDIT_FILE, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()[-200:]

        dialog = QDialog(self)
        dialog.setWindowTitle("审计日志")
        dialog.setMinimumSize(720, 480)
        layout = QVBoxLayout(dialog)
        edit = QTextEdit()
        edit.setReadOnly(True)
        edit.setFont(QFont("Consolas", 10))
        edit.setPlainText("\n".join(lines))
        layout.addWidget(edit)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(dialog.reject)
        buttons.clicked.connect(dialog.accept)
        layout.addWidget(buttons)
        dialog.exec_()
