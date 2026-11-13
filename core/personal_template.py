"""
个人发送「模板消息」文件解析。

模板文件包含三列（顺序任意）：姓名 / 部门 / 消息；
可选第四列「附件」用于「每人附件不同」模式（多个路径用 ; 分隔）。

按后缀分发到 CSV 或 Excel 解析，返回 (rows, errors)：

    rows   = [{"name": str, "unit": str, "content": str, "files": [str, …]}, …]
    errors = ["第N行: …", …]          成功数 = len(rows)

表头支持别名（容忍首尾空白）：
    姓名：姓名 / 人员名称 / 联系人 / 名字
    部门：部门 / 所在单位 / 单位 / 部门名称
    消息：消息 / 内容 / 消息内容 / 通知内容 / 正文
    附件：附件 / 文件 / 附件文件 / 文件路径（可选）
"""

from typing import List, Tuple

NAME_ALIASES = ("姓名", "人员名称", "联系人", "名字")
UNIT_ALIASES = ("部门", "所在单位", "单位", "部门名称")
CONTENT_ALIASES = ("消息", "内容", "消息内容", "通知内容", "正文")
FILE_ALIASES = ("附件", "文件", "附件文件", "文件路径")

# 附件路径分隔符（半角/全角分号、竖线、换行）
_FILE_SEPARATORS = ("；", "|", "\n", "\r")


def _split_files(raw: str) -> List[str]:
    """把一个单元格里的多个附件路径拆成列表"""
    if not raw:
        return []
    for sep in _FILE_SEPARATORS:
        raw = raw.replace(sep, ";")
    return [p.strip().strip('"').strip("'") for p in raw.split(";") if p.strip()]


def parse_template_file(path: str) -> Tuple[List[dict], List[str]]:
    """按文件后缀分发解析。支持 .csv / .xlsx。"""
    lower = path.lower()
    if lower.endswith(".csv"):
        return _parse_csv(path)
    if lower.endswith(".xlsx"):
        return _parse_excel(path)
    return [], ["仅支持 .xlsx / .csv 格式的模板文件"]


def _pick_column(col_names: List[str], aliases: tuple):
    """在表头列表中按别名找列名，返回原始列名（去空白后匹配）；找不到返回 None"""
    for raw in col_names:
        if raw is not None and str(raw).strip() in aliases:
            return str(raw).strip()
    return None


def _validate_rows(raw_rows: List[Tuple[int, str, str, str, List[str]]]) -> Tuple[List[dict], List[str]]:
    """对 (行号, 姓名, 部门, 消息, 附件列表) 逐行校验、去重。

    去重规则：(姓名, 部门) 完全重复时后一条报错并跳过；
    同姓名不同部门允许保留（OCR 的 find_contact(name, unit) 能区分）。
    """
    rows: List[dict] = []
    errors: List[str] = []
    seen = {}  # (name, unit) -> 首次出现的行号

    for line_num, name, unit, content, files in raw_rows:
        if not name:
            errors.append(f"第{line_num}行: 姓名为空")
            continue
        if not content:
            errors.append(f"第{line_num}行: 消息内容为空")
            continue
        key = (name, unit)
        if key in seen:
            errors.append(f"第{line_num}行: 与第{seen[key]}行重复（姓名+部门）")
            continue
        seen[key] = line_num
        rows.append({"name": name, "unit": unit, "content": content, "files": files})

    return rows, errors


def _parse_csv(path: str) -> Tuple[List[dict], List[str]]:
    import csv

    rows: List[dict] = []
    errors: List[str] = []
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            fieldnames = list(reader.fieldnames or [])
            name_col = _pick_column(fieldnames, NAME_ALIASES)
            content_col = _pick_column(fieldnames, CONTENT_ALIASES)
            unit_col = _pick_column(fieldnames, UNIT_ALIASES)
            file_col = _pick_column(fieldnames, FILE_ALIASES)

            if name_col is None:
                return [], ["未识别到姓名列，请检查表头（需包含：姓名/人员名称/联系人）"]
            if content_col is None:
                return [], ["未识别到消息内容列，请检查表头（需包含：消息/内容/消息内容）"]

            raw_rows: List[Tuple[int, str, str, str, List[str]]] = []
            for line_num, row in enumerate(reader, start=2):
                # 过滤全空行
                if not any((v or "").strip() for v in row.values()):
                    continue
                name = (row.get(name_col) or "").strip()
                unit = (row.get(unit_col) or "").strip() if unit_col else ""
                content = (row.get(content_col) or "").strip()
                files = _split_files(row.get(file_col) or "") if file_col else []
                raw_rows.append((line_num, name, unit, content, files))

        rows, errors = _validate_rows(raw_rows)

    except Exception as e:
        errors.append(f"读取文件失败: {e}")

    return rows, errors


def _parse_excel(path: str) -> Tuple[List[dict], List[str]]:
    try:
        from openpyxl import load_workbook
    except ImportError:
        return [], ["需要安装 openpyxl 库才能读取 Excel 文件"]

    rows: List[dict] = []
    errors: List[str] = []
    try:
        wb = load_workbook(path, read_only=True)
        try:
            ws = wb.active
            header_row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), None)
            if not header_row:
                return [], ["Excel 文件为空或没有表头"]

            col_names = [str(c).strip() if c is not None else "" for c in header_row]
            name_idx = _find_index(col_names, NAME_ALIASES)
            content_idx = _find_index(col_names, CONTENT_ALIASES)
            unit_idx = _find_index(col_names, UNIT_ALIASES)
            file_idx = _find_index(col_names, FILE_ALIASES)

            if name_idx is None:
                return [], ["未识别到姓名列，请检查表头（需包含：姓名/人员名称/联系人）"]
            if content_idx is None:
                return [], ["未识别到消息内容列，请检查表头（需包含：消息/内容/消息内容）"]

            def cell(row, idx):
                if idx is None or idx >= len(row) or row[idx] is None:
                    return ""
                return str(row[idx]).strip()

            raw_rows: List[Tuple[int, str, str, str, List[str]]] = []
            for line_num, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
                if not row or not any((v is not None and str(v).strip()) for v in row):
                    continue
                raw_rows.append((
                    line_num,
                    cell(row, name_idx),
                    cell(row, unit_idx),
                    cell(row, content_idx),
                    _split_files(cell(row, file_idx)),
                ))

            rows, errors = _validate_rows(raw_rows)
        finally:
            wb.close()

    except Exception as e:
        errors.append(f"读取文件失败: {e}")

    return rows, errors


def _find_index(col_names: List[str], aliases: tuple):
    for idx, col in enumerate(col_names):
        if col in aliases:
            return idx
    return None