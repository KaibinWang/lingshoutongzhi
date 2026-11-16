from datetime import datetime


def timestamp(value):
    return int(datetime.fromisoformat(value + "+08:00").timestamp())


def task_data(**changes):
    task = {
        "name": "材料报送提醒",
        "content": "请于截止时间前完成材料报送。",
        "group_ids": [1],
        "rule": {"kind": "interval", "minutes": 30},
        "starts_at": timestamp("2030-01-01T09:00:00"),
        "ends_at": None,
        "max_runs": None,
        "run_count": 0,
    }
    task.update(changes)
    return task
