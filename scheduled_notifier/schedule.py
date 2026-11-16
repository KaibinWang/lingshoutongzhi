"""北京时间的通知计划计算，独立于桌面界面和发送接口。"""

import re
from datetime import datetime, time, timedelta, timezone


BEIJING = timezone(timedelta(hours=8))
WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def parse_times(text):
    values = sorted(set(filter(None, re.split(r"[,，、;；\s]+", text.strip()))))
    if not values:
        raise ValueError("请至少填写一个发送时点，例如 09:00、15:00")
    for value in values:
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("发送时点请使用 24 小时制 HH:MM，例如 09:00")
    return values


def validate_task(task):
    if not task.get("name", "").strip():
        raise ValueError("请输入任务名称")
    content = task.get("content", "")
    if not content.strip():
        raise ValueError("请输入通知内容")
    if len(content.encode("utf-8")) > 4096:
        raise ValueError("通知内容最多为 4096 个 UTF-8 字节，请缩短内容")
    group_ids = task.get("group_ids")
    if not isinstance(group_ids, list) or not group_ids:
        raise ValueError("请至少选择一个目标群组")
    if any(type(group_id) is not int or group_id < 1 for group_id in group_ids):
        raise ValueError("目标群组标识无效，请重新选择")
    start = task.get("starts_at")
    end = task.get("ends_at")
    if type(start) is not int:
        raise ValueError("请设置开始日期时间")
    if end is not None and (type(end) is not int or end <= start):
        raise ValueError("截止时间必须晚于开始时间")
    maximum = task.get("max_runs")
    if maximum is not None and (type(maximum) is not int or maximum < 1):
        raise ValueError("总发送轮次必须为正整数")
    rule = task.get("rule", {})
    kind = rule.get("kind")
    if kind == "interval":
        minutes = rule.get("minutes")
        if type(minutes) is not int or minutes < 1:
            raise ValueError("发送间隔至少为一分钟")
    elif kind in ("daily", "weekly"):
        times = rule.get("times", [])
        if not isinstance(times, list) or not times:
            raise ValueError("请填写发送时点")
        parse_times(",".join(times))
        if kind == "weekly":
            days = rule.get("weekdays", [])
            if not days or any(type(day) is not int or day not in range(7) for day in days):
                raise ValueError("请至少选择一个有效的星期日期")
    else:
        raise ValueError("请选择有效的发送规则")


def next_time(task, after, inclusive=False):
    maximum = task.get("max_runs")
    if maximum is not None and task.get("run_count", 0) >= maximum:
        return None
    threshold = max(task["starts_at"], int(after) + (0 if inclusive else 1))
    end = task.get("ends_at")
    if end is not None and threshold >= end:
        return None
    rule = task["rule"]
    if rule["kind"] == "interval":
        step = rule["minutes"] * 60
        offset = threshold - task["starts_at"]
        candidate = task["starts_at"] + ((offset + step - 1) // step) * step
        if end is None or candidate < end:
            return candidate
        return None

    first_date = datetime.fromtimestamp(threshold, BEIJING).date()
    for offset in range(8):
        date = first_date + timedelta(days=offset)
        if rule["kind"] == "weekly" and date.weekday() not in rule["weekdays"]:
            continue
        for value in sorted(set(rule["times"])):
            hour, minute = map(int, value.split(":"))
            candidate = int(datetime.combine(date, time(hour, minute), BEIJING).timestamp())
            if candidate >= threshold and (end is None or candidate < end):
                return candidate
    return None


def preview_times(task, after, count=5):
    maximum = task.get("max_runs")
    if maximum is not None:
        count = min(count, max(0, maximum - task.get("run_count", 0)))
    values = []
    candidate = next_time(task, after, inclusive=True)
    while candidate is not None and len(values) < count:
        values.append(candidate)
        candidate = next_time(task, candidate)
    return values


def rule_label(rule):
    if rule["kind"] == "interval":
        minutes = rule["minutes"]
        if minutes % 60 == 0:
            return f"每隔 {minutes // 60} 小时"
        return f"每隔 {minutes} 分钟"
    times = "、".join(sorted(set(rule["times"])))
    if rule["kind"] == "weekly":
        days = "、".join(WEEKDAYS[day] for day in sorted(set(rule["weekdays"])))
        return f"每{days} {times}"
    return f"每天 {times}"


def format_time(value):
    if value is None:
        return "—"
    return datetime.fromtimestamp(value, BEIJING).strftime("%Y-%m-%d %H:%M")
