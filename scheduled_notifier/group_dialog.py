"""群机器人配置编辑。"""

import sqlite3

from PyQt5.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QLineEdit, QMessageBox,
)


class GroupDialog(QDialog):
    def __init__(self, store, group=None, parent=None):
        super().__init__(parent)
        self.store = store
        self.group = group or {}
        self.setWindowTitle("编辑群组" if group else "添加群组")
        self.setMinimumWidth(560)
        form = QFormLayout(self)
        form.setSpacing(16)
        self.name_edit = QLineEdit(self.group.get("name", ""))
        self.webhook_edit = QLineEdit(self.group.get("webhook", ""))
        self.webhook_edit.setPlaceholderText("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=…")
        self.enabled_check = QCheckBox("启用此群组")
        self.enabled_check.setChecked(bool(self.group.get("enabled", True)))
        form.addRow("群组名称", self.name_edit)
        form.addRow("机器人 Webhook", self.webhook_edit)
        form.addRow("", self.enabled_check)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("保存群组")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def save(self):
        try:
            self.store.save_group(self.name_edit.text(), self.webhook_edit.text(),
                                  self.enabled_check.isChecked(), self.group.get("id"))
        except (ValueError, OSError, sqlite3.Error) as exc:
            QMessageBox.warning(self, "保存失败", str(exc))
            return
        self.accept()
