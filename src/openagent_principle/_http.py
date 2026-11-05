"""共用的 HTTP 传输层：一个 POST、一套错误分级、零额外依赖。

JEV 与 LLM 两个后端都是「POST JSON 拿 JSON」，错误语义也一致
（401/403/404 重试无用，408/429/5xx 才值得等）。这份逻辑只该有一处 ——
两边各抄一遍，迟早有一边走样。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable

#: 传输层签名：(url, headers, payload, timeout) -> (status, body_text)。
#: status=-1 表示「网络层炸了」（格式化后的异常文本在 body 里）。
Transport = Callable[[str, dict[str, str], dict[str, Any] | None, float], tuple[int, str]]

#: 值得等一等的状态码（官方文档：408/429/5xx）。
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


def urllib_transport(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any] | None,
    timeout: float,
) -> tuple[int, str]:
    """默认传输层：stdlib urllib。`payload=None` 时发 GET（探针用得上）。"""
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001  网络异常统一降级成 -1
        return -1, f"{type(exc).__name__}: {exc}"


def error_detail(body: str) -> str:
    """从错误响应里抠出人话（不同网关的 error 结构不一样，这里都兜住）。"""
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return (body or "").strip()[:300]
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err)[:300]
        if err:
            return str(err)[:300]
        detail = data.get("detail") or data.get("message")
        if detail:
            return str(detail)[:300]
    return json.dumps(data, ensure_ascii=False)[:300]


def load_json(body: str) -> Any:
    """把响应体解析成 JSON；失败返回 None（由调用方给出人话错误）。"""
    try:
        return json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return None
