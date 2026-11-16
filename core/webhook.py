"""企业微信群机器人 Markdown 请求，共用的无状态传输函数。"""

import requests


def post_markdown(webhook_url, content, timeout=10):
    response = requests.post(
        webhook_url,
        json={"msgtype": "markdown", "markdown": {"content": content}},
        timeout=timeout,
    )
    return response.json()
