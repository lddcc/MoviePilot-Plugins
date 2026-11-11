import dataclasses
import json
from datetime import date, datetime
from enum import Enum
from pathlib import PurePath
from typing import Any

from app.schemas.types import EventType
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.sdk.network import RequestUtils
from app.sdk.plugin import _PluginBase

# 详情页保留的最近发送记录条数
HISTORY_LIMIT = 50
# 事件数据序列化的最大递归深度，防止循环引用或超深对象拖垮事件线程
MAX_DEPTH = 12


class WebhookPlus(_PluginBase):
    """事件发生时向第三方地址发送请求，支持自定义请求头和事件过滤。"""

    plugin_name = "Webhook增强版"
    plugin_desc = "事件发生时向第三方地址发送请求，支持自定义请求头（如鉴权 Token）和按事件类型过滤。"
    plugin_icon = "https://raw.githubusercontent.com/lddcc/MoviePilot-Plugins/main/icons/webhookplus.png"
    plugin_version = "1.0.0"
    plugin_label = "消息通知"
    plugin_author = "lddcc"
    author_url = "https://github.com/lddcc"
    plugin_config_prefix = "webhookplus_"
    plugin_order = 14
    auth_level = 1

    _enabled = False
    _webhook_url = ""
    _method = "POST"
    _headers: dict[str, str] = {}
    _event_types: list[str] = []
    _timeout = 10

    def init_plugin(self, config: dict | None = None) -> None:
        """读取配置；可重复调用。"""
        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._webhook_url = str(config.get("webhook_url") or "").strip()
        self._method = "GET" if config.get("request_method") == "GET" else "POST"
        self._headers = self._parse_headers(config.get("headers"))
        self._event_types = [str(t) for t in (config.get("event_types") or [])]
        try:
            self._timeout = max(1, int(config.get("timeout") or 10))
        except (TypeError, ValueError):
            self._timeout = 10

    def get_state(self) -> bool:
        """返回插件是否启用。"""
        return self._enabled

    @staticmethod
    def get_command() -> list[dict[str, Any]]:
        """不注册远程命令。"""
        return []

    def get_api(self) -> list[dict[str, Any]]:
        """不注册后端 API。"""
        return []

    def get_form(self) -> tuple[list[dict], dict[str, Any]]:
        """返回配置页面和默认配置。"""
        event_items = [{"title": f"{e.value}（{e.name}）", "value": e.value} for e in EventType]
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}}
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "request_method",
                                            "label": "请求方式",
                                            "items": ["POST", "GET"],
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 7},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {"model": "webhook_url", "label": "Webhook 地址"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 2},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {"model": "timeout", "label": "超时（秒）", "type": "number"},
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "event_types",
                                            "label": "发送的事件类型（不选 = 全部事件）",
                                            "items": event_items,
                                            "multiple": True,
                                            "chips": True,
                                            "clearable": True,
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "headers",
                                            "label": "自定义请求头",
                                            "rows": 3,
                                            "placeholder": "每行一个，格式 Key: Value\nAuthorization: Bearer xxxxx",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "POST 以 JSON 发送 {\"type\": 事件类型, \"data\": 事件数据}；"
                                                    "GET 以查询参数 type 和 data（JSON 字符串）发送。",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "request_method": "POST",
            "webhook_url": "",
            "timeout": 10,
            "event_types": [],
            "headers": "",
        }

    def get_page(self) -> list[dict]:
        """详情页：最近的发送记录。"""
        history = self.get_data("history") or []
        if not history:
            return [{"component": "div", "props": {"class": "text-center"}, "text": "暂无发送记录"}]
        rows = [
            {
                "component": "tr",
                "content": [
                    {"component": "td", "text": item.get("time")},
                    {"component": "td", "text": item.get("type")},
                    {
                        "component": "td",
                        "props": {"class": "text-success" if item.get("ok") else "text-error"},
                        "text": item.get("result"),
                    },
                ],
            }
            for item in reversed(history)
        ]
        return [
            {
                "component": "VTable",
                "props": {"hover": True, "density": "compact"},
                "content": [
                    {
                        "component": "thead",
                        "content": [
                            {
                                "component": "tr",
                                "content": [
                                    {"component": "th", "text": "时间"},
                                    {"component": "th", "text": "事件"},
                                    {"component": "th", "text": "结果"},
                                ],
                            }
                        ],
                    },
                    {"component": "tbody", "content": rows},
                ],
            }
        ]

    @eventmanager.register(EventType)
    def send(self, event: Event) -> None:
        """把事件发送到第三方地址。"""
        if not self._enabled or not self._webhook_url:
            return
        if not event or not event.event_type:
            return
        event_type = event.event_type.value if isinstance(event.event_type, Enum) else str(event.event_type)
        if self._event_types and event_type not in self._event_types:
            return

        event_info = {"type": event_type, "data": self._to_jsonable(event.event_data)}
        requester = RequestUtils(headers=self._headers or None, timeout=self._timeout,
                                 content_type="application/json")
        try:
            if self._method == "POST":
                ret = requester.post_res(self._webhook_url, json=event_info)
            else:
                ret = requester.get_res(self._webhook_url, params={
                    "type": event_type,
                    "data": json.dumps(event_info["data"], ensure_ascii=False),
                })
            if ret is None:
                ok, result = False, "请求失败，未获取到返回信息"
            elif ret.ok:
                ok, result = True, f"HTTP {ret.status_code}"
            else:
                ok, result = False, f"HTTP {ret.status_code} {ret.text[:200]}"
        except Exception as e:
            ok, result = False, f"请求异常：{e}"

        if ok:
            logger.info(f"Webhook 发送成功：{event_type} -> {result}")
        else:
            logger.error(f"Webhook 发送失败：{event_type} -> {result}")
        self._append_history(event_type, ok, result)

    def stop_service(self) -> None:
        """没有后台资源需要释放。"""
        return None

    @staticmethod
    def _parse_headers(text: Any) -> dict[str, str]:
        """把每行一个的 `Key: Value` 文本解析成请求头字典。"""
        headers = {}
        for line in str(text or "").splitlines():
            key, sep, value = line.partition(":")
            if sep and key.strip():
                headers[key.strip()] = value.strip()
        return headers

    @classmethod
    def _to_jsonable(cls, obj: Any, depth: int = 0, seen: set[int] | None = None) -> Any:
        """
        递归把事件数据转换成可 JSON 序列化的结构。

        生成新对象，不改动原事件数据——事件数据会继续传给其它插件和宿主的处理器。
        """
        if isinstance(obj, (str, int, float, bool, type(None))):
            return obj
        if isinstance(obj, Enum):
            return obj.value
        if isinstance(obj, (datetime, date)):
            return obj.isoformat()
        if isinstance(obj, PurePath):
            return str(obj)
        if depth >= MAX_DEPTH:
            return str(obj)
        seen = seen if seen is not None else set()
        if id(obj) in seen:
            return "<循环引用>"
        seen = seen | {id(obj)}
        if isinstance(obj, dict):
            return {str(k): cls._to_jsonable(v, depth + 1, seen) for k, v in obj.items()}
        if isinstance(obj, (list, tuple, set, frozenset)):
            return [cls._to_jsonable(v, depth + 1, seen) for v in obj]
        if hasattr(obj, "model_dump"):
            try:
                return cls._to_jsonable(obj.model_dump(), depth + 1, seen)
            except Exception:
                pass
        if hasattr(obj, "to_dict"):
            try:
                return cls._to_jsonable(obj.to_dict(), depth + 1, seen)
            except Exception:
                pass
        if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
            return cls._to_jsonable(
                {f.name: getattr(obj, f.name, None) for f in dataclasses.fields(obj)}, depth + 1, seen)
        if hasattr(obj, "__dict__"):
            return cls._to_jsonable(
                {k: v for k, v in vars(obj).items() if not k.startswith("_")}, depth + 1, seen)
        return str(obj)

    def _append_history(self, event_type: str, ok: bool, result: str) -> None:
        """追加一条发送记录，只保留最近 HISTORY_LIMIT 条。"""
        try:
            history = self.get_data("history") or []
            history.append({
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "type": event_type,
                "ok": ok,
                "result": result,
            })
            self.save_data("history", history[-HISTORY_LIMIT:])
        except Exception as e:
            logger.warning(f"保存 Webhook 发送记录失败：{e}")
