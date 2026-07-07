"""
配置管理模块
管理群组列表、消息模板的读写和持久化
"""

import json
import os
import csv
import sys
from pathlib import Path
from typing import List, Dict, Optional


# 获取数据目录路径。打包后不写入程序包/临时解压目录，避免配置和日志无法持久保存。
if getattr(sys, "frozen", False) and sys.platform == "darwin":
    BASE_DIR = Path.home() / "Library" / "Application Support" / "零售通知工具"
elif getattr(sys, "frozen", False) and sys.platform == "win32":
    BASE_DIR = Path(os.getenv("APPDATA", Path.home())) / "零售通知工具"
else:
    BASE_DIR = Path(__file__).parent.parent

DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"

# 确保目录存在
DATA_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

GROUPS_FILE = DATA_DIR / "groups.json"
TEMPLATES_FILE = DATA_DIR / "templates.json"


class GroupConfig:
    """群组配置管理"""

    def __init__(self, file_path: Path = GROUPS_FILE):
        self.file_path = file_path
        self.groups: List[Dict] = []
        self.load()

    def load(self):
        """从文件加载群组列表"""
        if self.file_path.exists():
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    self.groups = json.load(f)
            except (json.JSONDecodeError, IOError):
                self.groups = []
        else:
            self.groups = []

    def save(self):
        """保存群组列表到文件"""
        with open(self.file_path, "w", encoding="utf-8") as f:
            json.dump(self.groups, f, ensure_ascii=False, indent=2)

    def add_group(self, name: str, webhook: str, category: str = "默认") -> Dict:
        """添加群组"""
        group = {
            "id": len(self.groups) + 1,
            "name": name,
            "webhook": webhook,
            "category": category,
            "enabled": True
        }
        # 重新分配ID
        if self.groups:
            group["id"] = max(g["id"] for g in self.groups) + 1
        self.groups.append(group)
        self.save()
        return group

    def update_group(self, group_id: int, name: str = None, webhook: str = None,
                     category: str = None, enabled: bool = None) -> bool:
        """更新群组信息"""
        for group in self.groups:
            if group["id"] == group_id:
                if name is not None:
                    group["name"] = name
                if webhook is not None:
                    group["webhook"] = webhook
                if category is not None:
                    group["category"] = category
                if enabled is not None:
                    group["enabled"] = enabled
                self.save()
                return True
        return False

    def delete_group(self, group_id: int) -> bool:
        """删除群组"""
        for i, group in enumerate(self.groups):
            if group["id"] == group_id:
                self.groups.pop(i)
                self.save()
                return True
        return False

    def delete_groups(self, group_ids: List[int]) -> int:
        """批量删除群组，返回实际删除数量。"""
        ids_to_delete = set(group_ids)
        if not ids_to_delete:
            return 0

        original_count = len(self.groups)
        self.groups = [group for group in self.groups if group.get("id") not in ids_to_delete]
        deleted_count = original_count - len(self.groups)
        if deleted_count:
            self.save()
        return deleted_count

    def get_group(self, group_id: int) -> Optional[Dict]:
        """获取单个群组"""
        for group in self.groups:
            if group["id"] == group_id:
                return group
        return None

    def get_enabled_groups(self) -> List[Dict]:
        """获取所有启用的群组"""
        return [g for g in self.groups if g.get("enabled", True)]

    def get_categories(self) -> List[str]:
        """获取所有分类"""
        categories = set(g.get("category", "默认") for g in self.groups)
        return sorted(categories)

    def import_from_csv(self, file_path: str) -> tuple:
        """
        从CSV文件批量导入群组
        CSV格式: 群名称, webhook地址, 分类(可选)
        返回: (成功数, 失败数, 错误信息列表)
        """
        success_count = 0
        fail_count = 0
        errors = []

        try:
            with open(file_path, "r", encoding="utf-8-sig") as f:
                reader = csv.reader(f)
                header = next(reader, None)

                for line_num, row in enumerate(reader, start=2):
                    try:
                        if len(row) < 2:
                            errors.append(f"第{line_num}行: 数据不完整")
                            fail_count += 1
                            continue

                        name = row[0].strip()
                        webhook = row[1].strip()
                        category = row[2].strip() if len(row) > 2 and row[2].strip() else "默认"

                        if not name or not webhook:
                            errors.append(f"第{line_num}行: 群名称或webhook为空")
                            fail_count += 1
                            continue

                        self.add_group(name, webhook, category)
                        success_count += 1
                    except Exception as e:
                        errors.append(f"第{line_num}行: {str(e)}")
                        fail_count += 1

        except Exception as e:
            errors.append(f"读取文件失败: {str(e)}")

        return success_count, fail_count, errors

    def import_from_excel(self, file_path: str) -> tuple:
        """
        从Excel文件批量导入群组
        Excel格式: 第一列群名称, 第二列webhook地址, 第三列分类(可选)
        返回: (成功数, 失败数, 错误信息列表)
        """
        try:
            from openpyxl import load_workbook
        except ImportError:
            return 0, 0, ["需要安装 openpyxl 库才能读取 Excel 文件"]

        success_count = 0
        fail_count = 0
        errors = []

        try:
            wb = load_workbook(file_path, read_only=True)
            ws = wb.active

            for line_num, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
                try:
                    if not row or len(row) < 2:
                        errors.append(f"第{line_num}行: 数据不完整")
                        fail_count += 1
                        continue

                    name = str(row[0]).strip() if row[0] else ""
                    webhook = str(row[1]).strip() if row[1] else ""
                    category = str(row[2]).strip() if len(row) > 2 and row[2] else "默认"

                    if not name or not webhook:
                        errors.append(f"第{line_num}行: 群名称或webhook为空")
                        fail_count += 1
                        continue

                    self.add_group(name, webhook, category)
                    success_count += 1
                except Exception as e:
                    errors.append(f"第{line_num}行: {str(e)}")
                    fail_count += 1

            wb.close()

        except Exception as e:
            errors.append(f"读取文件失败: {str(e)}")

        return success_count, fail_count, errors

    def export_to_csv(self, file_path: str) -> bool:
        """导出群组列表为CSV"""
        try:
            with open(file_path, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["群名称", "Webhook地址", "分类", "启用状态"])
                for group in self.groups:
                    writer.writerow([
                        group["name"],
                        group["webhook"],
                        group.get("category", "默认"),
                        "是" if group.get("enabled", True) else "否"
                    ])
            return True
        except Exception:
            return False

    def export_to_excel(self, file_path: str) -> bool:
        """导出群组列表为Excel"""
        try:
            from openpyxl import Workbook
            wb = Workbook()
            ws = wb.active
            ws.title = "群组列表"

            # 写入表头
            ws.append(["群名称", "Webhook地址", "分类", "启用状态"])

            # 写入数据
            for group in self.groups:
                ws.append([
                    group["name"],
                    group["webhook"],
                    group.get("category", "默认"),
                    "是" if group.get("enabled", True) else "否"
                ])

            # 调整列宽
            ws.column_dimensions["A"].width = 25
            ws.column_dimensions["B"].width = 60
            ws.column_dimensions["C"].width = 15
            ws.column_dimensions["D"].width = 10

            wb.save(file_path)
            return True
        except Exception:
            return False


class TemplateConfig:
    """消息模板管理"""

    def __init__(self, file_path: Path = TEMPLATES_FILE):
        self.file_path = file_path
        self.templates: List[Dict] = []
        self.load()

    def load(self):
        """从文件加载模板列表"""
        if self.file_path.exists():
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    self.templates = json.load(f)
            except (json.JSONDecodeError, IOError):
                self.templates = []
        else:
            self.templates = []
            # 添加默认模板
            self._add_default_templates()

    def _add_default_templates(self):
        """添加默认模板"""
        default_templates = [
            {
                "id": 1,
                "name": "会议通知",
                "content": """<font color="info">【会议通知】</font>

> **会议主题：** {topic}
> **会议时间：** {time}
> **会议地点：** {location}
> **参会人员：** {attendees}

{notes}"""
            },
            {
                "id": 2,
                "name": "培训通知",
                "content": """<font color="warning">【培训通知】</font>

> **培训主题：** {topic}
> **培训时间：** {time}
> **培训地点：** {location}
> **参训人员：** {attendees}

{notes}"""
            },
            {
                "id": 3,
                "name": "紧急通知",
                "content": """<font color="warning">【紧急通知】</font>

{content}"""
            }
        ]
        self.templates = default_templates
        self.save()

    def save(self):
        """保存模板列表到文件"""
        with open(self.file_path, "w", encoding="utf-8") as f:
            json.dump(self.templates, f, ensure_ascii=False, indent=2)

    def add_template(self, name: str, content: str) -> Dict:
        """添加模板"""
        template = {
            "id": max([t["id"] for t in self.templates], default=0) + 1,
            "name": name,
            "content": content
        }
        self.templates.append(template)
        self.save()
        return template

    def update_template(self, template_id: int, name: str = None, content: str = None) -> bool:
        """更新模板"""
        for template in self.templates:
            if template["id"] == template_id:
                if name is not None:
                    template["name"] = name
                if content is not None:
                    template["content"] = content
                self.save()
                return True
        return False

    def delete_template(self, template_id: int) -> bool:
        """删除模板"""
        for i, template in enumerate(self.templates):
            if template["id"] == template_id:
                self.templates.pop(i)
                self.save()
                return True
        return False

    def get_template(self, template_id: int) -> Optional[Dict]:
        """获取单个模板"""
        for template in self.templates:
            if template["id"] == template_id:
                return template
        return None

    def get_template_names(self) -> List[str]:
        """获取所有模板名称"""
        return [t["name"] for t in self.templates]
