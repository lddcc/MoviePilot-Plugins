import asyncio
import hashlib
import re
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel

from app.schemas import Response
from app.sdk.cache import TTLCache
from app.sdk.logging import logger
from app.sdk.network import AsyncRequestUtils, RequestUtils
from app.sdk.plugin import _PluginBase

# 运行状态码 -> (中文, Vuetify 颜色)
STATUS = {
    0: ("未开始", "grey"),
    1: ("运行中", "info"),
    2: ("失败", "error"),
    3: ("已中止", "warning"),
    4: ("成功", "success"),
    5: ("排队中", "secondary"),
    6: ("部分成功", "warning"),
    7: ("等待操作", "warning"),
    8: ("已拒绝", "error"),
}
# 还会变化的状态：运行列表缓存要短
ACTIVE = {0, 1, 5, 7}
TRIGGERS = {
    "scheduler": "定时",
    "webhook": "Webhook",
    "manual": "手动",
    "retry": "重试",
    "catchup": "补跑",
    "subdag": "子任务",
}
DEFAULT_DAGS = "emby-media-sync=Emby 媒体库同步\ncd2-cache-warm=CD2 缓存预热"

# 缓存时长（秒）。缓存走宿主的缓存后端，MoviePilot 配了 Redis 时就在 Redis 里，多进程 / 重启都能复用
RUNS_TTL = 15            # 运行列表：没有进行中的运行时
RUNS_ACTIVE_TTL = 5      # 运行列表：有运行中 / 排队中的运行时
META_MIN_TTL = 30        # DAG 列表（下次运行时间等）：缓存到最早的下次运行时间，上下限
META_MAX_TTL = 3600
SUMMARY_TTL = 7 * 86400  # 已结束运行的摘要不会再变
DOWN_TTL = 30            # Dagu 连不上时，这段时间内直接显示错误，不再逐个请求等超时

# 去掉日志行开头的时间戳，如 "2026-11-11 12:00:16 "
TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}\S*\s*")


class DaguError(Exception):
    """Dagu 接口请求失败。"""


class RunRequest(BaseModel):
    """立即运行接口的请求体。"""
    dag: str


class DaguPanel(_PluginBase):
    """在 MoviePilot 里查看 Dagu 任务的运行状态，并一键排队运行。"""

    plugin_name = "Dagu任务面板"
    plugin_desc = "在仪表板和详情页查看 Dagu 任务的最近运行状态、结果摘要和下次运行时间，并可一键立即运行。"
    plugin_icon = "https://raw.githubusercontent.com/lddcc/MoviePilot-Plugins/main/icons/dagupanel.png"
    plugin_version = "1.0.0"
    plugin_label = "工具"
    plugin_author = "lddcc"
    author_url = "https://github.com/lddcc"
    plugin_config_prefix = "dagupanel_"
    plugin_order = 30
    auth_level = 1

    _enabled = False
    _url = ""
    _api_key = ""
    _dags: dict[str, str] = {}
    _history = 10
    _refresh = 30
    _timeout = 10
    _prefix = ""
    _cache: TTLCache | None = None
    _client: RequestUtils | None = None
    _aclient: AsyncRequestUtils | None = None
    _executor: ThreadPoolExecutor | None = None

    def init_plugin(self, config: dict | None = None) -> None:
        """读取配置并准备复用的连接、缓存和线程池；可重复调用。"""
        self.stop_service()
        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._url = str(config.get("dagu_url") or "").strip().rstrip("/")
        self._api_key = str(config.get("api_key") or "").strip()
        self._dags = self._parse_dags(config.get("dags") or "")
        self._history = self._int(config.get("history_count"), 10, 1, 50)
        self._refresh = self._int(config.get("refresh"), 30, 5, 3600)
        self._timeout = self._int(config.get("timeout"), 10, 1, 60)
        if not self._enabled or not self._url or not self._api_key:
            return
        # 缓存键带上地址的短哈希：换了 Dagu 地址自然不会读到旧数据，不必清缓存
        self._prefix = hashlib.sha1(self._url.encode()).hexdigest()[:8]
        self._cache = TTLCache(region="DaguPanel", maxsize=512, ttl=RUNS_TTL)
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        # 同步客户端给仪表板 / 详情页（宿主在线程池里同步调用它们），keep-alive 复用连接
        self._client = RequestUtils(headers=headers, timeout=self._timeout, use_session=True)
        # 异步客户端给「立即运行」接口，底层连接池由宿主共享
        self._aclient = AsyncRequestUtils(headers=headers, timeout=self._timeout)
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="DaguPanel")

    def get_state(self) -> bool:
        """返回插件是否启用且配置完整。"""
        return bool(self._enabled and self._client)

    @staticmethod
    def get_command() -> list[dict[str, Any]]:
        """不注册远程命令。"""
        return []

    def get_api(self) -> list[dict[str, Any]]:
        """注册「立即运行」接口，供页面按钮调用。"""
        return [
            {
                "path": "/run",
                "endpoint": self.api_run,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "立即运行（排队）一个已配置的 Dagu DAG",
            }
        ]

    def get_service(self) -> list[dict[str, Any]]:
        """不注册定时服务：数据在打开页面时按需拉取并缓存。"""
        return []

    def get_form(self) -> tuple[list[dict], dict[str, Any]]:
        """返回配置页面和默认配置。"""
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
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "dagu_url",
                                            "label": "Dagu 地址",
                                            "placeholder": "http://10.0.0.8:8080",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "api_key",
                                            "label": "API Key",
                                            "type": "password",
                                            "placeholder": "dagu_xxxx（建议 operator 角色）",
                                        },
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
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "dags",
                                            "label": "显示的 DAG",
                                            "rows": 3,
                                            "placeholder": "每行一个，格式 DAG名=显示名（显示名可省略）",
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
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {"model": "history_count", "label": "详情页每个 DAG 显示条数",
                                                  "type": "number"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {"model": "refresh", "label": "仪表板刷新间隔（秒）",
                                                  "type": "number"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {"model": "timeout", "label": "请求超时（秒）", "type": "number"},
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
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "API Key 在 Dagu「设置 → API Keys」创建，operator 角色即可（只能查看和运行 DAG）。"
                                                    "「立即运行」走 Dagu 的排队接口，任务正在跑时会排在后面。"
                                                    "数据按需拉取并缓存（MoviePilot 配了 Redis 时缓存在 Redis），"
                                                    "已结束运行的结果摘要只取一次。",
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
            "dagu_url": "",
            "api_key": "",
            "dags": DEFAULT_DAGS,
            "history_count": 10,
            "refresh": 30,
            "timeout": 10,
        }

    def get_dashboard_meta(self) -> list[dict[str, str]] | None:
        """注册一个仪表板组件。"""
        if not self.get_state():
            return None
        return [{"key": "tasks", "name": "Dagu 任务"}]

    def get_dashboard(self, key: str | None = None, **kwargs) -> tuple[dict, dict, list[dict]] | None:
        """仪表板：每个 DAG 一块，显示最近一次运行、结果摘要、下次运行和「立即运行」按钮。"""
        if not self.get_state():
            return None
        cols = {"cols": 12, "md": 6}
        attrs = {"refresh": self._refresh, "border": True, "title": "Dagu 任务"}
        meta, runs, error = self._load(1)
        if error:
            return cols, attrs, [self._error_alert(error)]
        # 每个 DAG 最多补一次摘要请求（最近一次已结束的运行），之后 7 天内走缓存
        jobs = {}
        for dag in self._dags:
            done = next((r for r in runs.get(dag) or [] if r.get("status") not in ACTIVE), None)
            if done:
                jobs[dag] = (done, self._executor.submit(self._summary, dag, done))
        elements = []
        for i, (dag, name) in enumerate(self._dags.items()):
            if i:
                elements.append({"component": "VDivider", "props": {"class": "my-3"}})
            dag_runs = runs.get(dag)
            latest = dag_runs[0] if dag_runs else None
            info = meta.get(dag) or {}
            line = []
            if latest:
                line.append(f"上次 {self._fmt_time(self._run_start(latest))}")
                duration = self._duration(latest)
                if duration:
                    line.append(duration)
                line.append(self._trigger(latest))
            elif dag_runs is None:
                line.append("Dagu 上没有这个 DAG")
            else:
                line.append("还没有运行记录")
            if info.get("suspended"):
                line.append("已暂停")
            elif info.get("nextRun"):
                line.append(f"下次 {self._fmt_time(self._dt(info['nextRun']))}")
            summary = ""
            if dag in jobs:
                done, future = jobs[dag]
                summary = self._future_text(future)
                if done is not latest and summary:
                    summary = f"上一次已结束的运行：{summary}"
            elements.append({
                "component": "div",
                "props": {"class": "d-flex align-center ga-2"},
                "content": [
                    {"component": "span", "props": {"class": "text-subtitle-1 font-weight-medium text-truncate"},
                     "text": name},
                    self._status_chip(latest),
                    {"component": "VSpacer"},
                    self._run_button(dag),
                ],
            })
            elements.append({"component": "div", "props": {"class": "text-caption text-medium-emphasis mt-1"},
                             "text": " · ".join(line)})
            if summary:
                elements.append({"component": "div", "props": {"class": "text-body-2 mt-1 text-truncate",
                                                                "title": summary},
                                 "text": summary})
        return cols, attrs, elements

    def get_page(self) -> list[dict]:
        """详情页：每个 DAG 一张表，列出最近的运行记录和结果摘要。"""
        if not self.get_state():
            return [{"component": "div", "props": {"class": "text-center"}, "text": "插件未启用或未配置 Dagu 地址 / API Key"}]
        meta, runs, error = self._load(self._history)
        if error:
            return [self._error_alert(error)]
        # 所有 DAG 的摘要一起并发补齐，已结束的运行之后都走缓存
        futures = {
            (dag, r.get("dagRunId")): self._executor.submit(self._summary, dag, r)
            for dag in self._dags for r in runs.get(dag) or [] if r.get("status") not in ACTIVE
        }
        cards = []
        for dag, name in self._dags.items():
            info = meta.get(dag) or {}
            dag_runs = runs.get(dag)
            subtitle = [dag]
            if info.get("description"):
                subtitle.append(info["description"])
            if info.get("suspended"):
                subtitle.append("已暂停")
            elif info.get("nextRun"):
                subtitle.append(f"下次运行 {self._fmt_time(self._dt(info['nextRun']))}")
            header = {
                "component": "div",
                "props": {"class": "d-flex align-center flex-wrap ga-2 pa-4 pb-2"},
                "content": [
                    {"component": "div", "props": {"class": "text-h6"}, "text": name},
                    {"component": "VSpacer"},
                    {
                        "component": "VBtn",
                        "props": {"size": "small", "variant": "text", "href": f"{self._url}/dags/{quote(dag)}",
                                  "target": "_blank", "prepend-icon": "mdi-open-in-new"},
                        "text": "在 Dagu 中打开",
                    },
                    self._run_button(dag),
                ],
            }
            sub = {"component": "div", "props": {"class": "text-caption text-medium-emphasis px-4 pb-2"},
                   "text": " · ".join(subtitle)}
            if dag_runs is None:
                body = {"component": "div", "props": {"class": "px-4 pb-4 text-error"}, "text": "Dagu 上没有这个 DAG"}
            elif not dag_runs:
                body = {"component": "div", "props": {"class": "px-4 pb-4"}, "text": "还没有运行记录"}
            else:
                rows = []
                for r in dag_runs:
                    future = futures.get((dag, r.get("dagRunId")))
                    detail = {}
                    if future:
                        try:
                            detail = future.result()
                        except Exception as e:
                            detail = {"text": f"摘要获取失败：{e}"}
                    retries = detail.get("retries") or 0
                    rows.append({
                        "component": "tr",
                        "content": [
                            {"component": "td", "content": [self._status_chip(r)]},
                            {"component": "td", "props": {"class": "text-no-wrap"}, "content": [
                                {"component": "a", "props": {
                                    "href": f"{self._url}/dag-runs/{quote(dag)}/{quote(r.get('dagRunId') or '')}",
                                    "target": "_blank"}, "text": self._fmt_time(self._run_start(r))}
                            ]},
                            {"component": "td", "props": {"class": "text-no-wrap"}, "text": self._duration(r) or "-"},
                            {"component": "td", "props": {"class": "text-no-wrap"},
                             "text": self._trigger(r) + (f" · 重试{retries}次" if retries else "")},
                            {"component": "td", "props": {"class": "text-body-2", "style": "white-space: pre-line"},
                             "text": detail.get("text") or ""},
                        ],
                    })
                body = {
                    "component": "VTable",
                    "props": {"hover": True, "density": "compact"},
                    "content": [
                        {"component": "thead", "content": [{"component": "tr", "content": [
                            {"component": "th", "text": t} for t in ("状态", "开始", "耗时", "触发", "结果")
                        ]}]},
                        {"component": "tbody", "content": rows},
                    ],
                }
            cards.append({"component": "VCard", "props": {"variant": "outlined", "class": "mb-4"},
                          "content": [header, sub, body]})
        return cards

    async def api_run(self, req: RunRequest) -> Response:
        """把 DAG 加入 Dagu 运行队列（正在跑时排在后面），并让该 DAG 的运行列表缓存失效。"""
        if not self.get_state():
            return Response(success=False, message="插件未启用或未配置")
        if req.dag not in self._dags:
            return Response(success=False, message=f"未配置的 DAG：{req.dag}")
        name = self._dags[req.dag]
        ret = await self._aclient.post_res(f"{self._url}/api/v1/dags/{quote(req.dag)}/enqueue", json={})
        if ret is None:
            logger.error(f"Dagu 立即运行失败：{name}，连接 Dagu 失败")
            return Response(success=False, message="连接 Dagu 失败")
        if not 200 <= ret.status_code < 300:
            message = f"HTTP {ret.status_code} {ret.text[:200]}"
            logger.error(f"Dagu 立即运行失败：{name}，{message}")
            return Response(success=False, message=message)
        run_id = (ret.json() or {}).get("dagRunId")
        # 同步缓存后端可能是阻塞的 Redis 调用，丢到线程里，不卡事件循环
        await asyncio.to_thread(self._cache.delete, self._key(f"runs:{req.dag}"))
        logger.info(f"Dagu 已排队运行：{name}（{run_id}）")
        return Response(success=True, message=f"已排队运行：{name}", data={"dagRunId": run_id})

    def stop_service(self) -> None:
        """关闭 keep-alive 会话和线程池。"""
        if self._executor:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
        self._aclient = None

    # ---------- 数据 ----------

    def _key(self, name: str) -> str:
        """缓存键加上 Dagu 地址前缀。"""
        return f"{self._prefix}:{name}"

    def _get(self, path: str, **params) -> Any:
        """GET Dagu 接口并返回 JSON；404 返回 None，其它错误抛 DaguError。"""
        ret = self._client.get_res(f"{self._url}/api/v1/{path}", params=params or None)
        if ret is None:
            raise DaguError(f"连接 Dagu 失败：{self._url}")
        if ret.status_code == 404:
            return None
        if not ret.ok:
            raise DaguError(f"Dagu 返回 HTTP {ret.status_code}：{ret.text[:200]}")
        return ret.json()

    def _load(self, limit: int) -> tuple[dict, dict, str | None]:
        """
        并发取 DAG 元信息和每个 DAG 的运行列表（都带缓存）。

        :return: (元信息 {dag: {...}}, 运行列表 {dag: [...] 或 None=DAG 不存在}, 错误信息)
        """
        error = self._cache.get(self._key("down"))
        if error:
            return {}, {}, error
        try:
            meta_future = self._executor.submit(self._meta)
            run_futures = {dag: self._executor.submit(self._runs, dag) for dag in self._dags}
            meta = meta_future.result()
            # 运行列表接口对不存在的 DAG 也返回空列表，按 DAG 列表判断是否存在
            runs = {dag: f.result()[:limit] if dag in meta and f.result() is not None else None
                    for dag, f in run_futures.items()}
        except Exception as e:
            error = str(e) if isinstance(e, DaguError) else f"读取 Dagu 数据出错：{e}"
            self._cache.set(self._key("down"), error, ttl=DOWN_TTL)
            logger.warning(error)
            return {}, {}, error
        return meta, runs, None

    def _meta(self) -> dict[str, dict]:
        """所有 DAG 的描述、下次运行时间、是否暂停；缓存到最早的下次运行时间（30 秒 ~ 1 小时）。"""
        key = self._key("meta")
        meta = self._cache.get(key)
        if meta is not None:
            return meta
        data = self._get("dags", perPage=1000) or {}
        meta = {
            d.get("fileName"): {
                "description": (d.get("dag") or {}).get("description") or "",
                "nextRun": d.get("nextRun") or "",
                "suspended": bool(d.get("suspended")),
            }
            for d in data.get("dags") or []
        }
        now = datetime.now().astimezone()
        upcoming = [t for t in (self._dt(m["nextRun"]) for m in meta.values()) if t and t > now]
        ttl = META_MAX_TTL
        if upcoming:
            ttl = int((min(upcoming) - now).total_seconds()) + 5
        self._cache.set(key, meta, ttl=max(META_MIN_TTL, min(META_MAX_TTL, ttl)))
        return meta

    def _runs(self, dag: str) -> list[dict] | None:
        """DAG 最近的运行列表（新的在前）；有进行中的运行时缓存更短。DAG 不存在返回 None。"""
        key = self._key(f"runs:{dag}")
        cached = self._cache.get(key)
        if cached is not None:
            return cached.get("runs")
        data = self._get(f"dag-runs/{quote(dag)}", limit=self._history)
        runs = None if data is None else [
            {k: r.get(k) for k in ("dagRunId", "status", "triggerType", "queuedAt", "startedAt", "finishedAt")}
            for r in data.get("dagRuns") or []
        ]
        active = any(r.get("status") in ACTIVE for r in runs or [])
        self._cache.set(key, {"runs": runs}, ttl=RUNS_ACTIVE_TTL if active else RUNS_TTL)
        return runs

    def _summary(self, dag: str, run: dict) -> dict:
        """
        已结束运行的结果摘要：最后执行的那一步 stdout 最后一行（去时间戳）；失败时优先取 stderr 里的失败说明。

        摘要结果长期缓存，同一次运行只请求一次。
        """
        run_id = run.get("dagRunId") or ""
        key = self._key(f"sum:{dag}:{run_id}")
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        data = self._get(f"dag-runs/{quote(dag)}/{quote(run_id)}") or {}
        nodes = (data.get("dagRunDetails") or {}).get("nodes") or []
        started = [n for n in nodes if n.get("status") not in (0, None)]
        result = {"text": "", "retries": sum(int(n.get("retryCount") or 0) for n in nodes)}
        if started:
            node = started[-1]
            for n in started:
                if n.get("status") == 2:
                    node = n
                    break
            step = (node.get("step") or {}).get("name") or ""
            failed = run.get("status") == 2 or node.get("status") == 2
            text = ""
            if failed:
                lines = self._log_lines(dag, run_id, step, "stderr", 30)
                text = next((s for s in lines if "失败" in s), lines[-1] if lines else "")
                text = text or node.get("error") or ""
            if not text:
                lines = self._log_lines(dag, run_id, step, "stdout", 5)
                text = lines[-1] if lines else ""
            result["text"] = text[:300]
        self._cache.set(key, result, ttl=SUMMARY_TTL)
        return result

    def _log_lines(self, dag: str, run_id: str, step: str, stream: str, tail: int) -> list[str]:
        """取步骤日志末尾几行，去掉空行和行首时间戳。"""
        data = self._get(f"dag-runs/{quote(dag)}/{quote(run_id)}/steps/{quote(step)}/log",
                         tail=tail, stream=stream) or {}
        lines = []
        for line in str(data.get("content") or "").splitlines():
            line = TIMESTAMP_RE.sub("", line).strip()
            if line:
                lines.append(line)
        return lines

    # ---------- 渲染 ----------

    @staticmethod
    def _status_chip(run: dict | None) -> dict:
        """运行状态标签。"""
        label, color = STATUS.get(run.get("status"), ("未知", "grey")) if run else ("无记录", "grey")
        return {"component": "VChip", "props": {"size": "small", "color": color, "variant": "tonal", "label": True},
                "text": label}

    @staticmethod
    def _run_button(dag: str) -> dict:
        """「立即运行」按钮，调用本插件的 /run 接口。"""
        return {
            "component": "VBtn",
            "props": {"size": "small", "variant": "tonal", "color": "primary", "prepend-icon": "mdi-play"},
            "text": "立即运行",
            "events": {"click": {"api": "plugin/DaguPanel/run", "method": "post", "params": {"dag": dag}}},
        }

    @staticmethod
    def _error_alert(error: str) -> dict:
        """错误提示。"""
        return {"component": "VAlert", "props": {"type": "error", "variant": "tonal", "density": "compact"},
                "text": error}

    @staticmethod
    def _future_text(future: Future) -> str:
        """取摘要线程的结果文本，失败时返回错误说明。"""
        try:
            return future.result().get("text") or ""
        except Exception as e:
            return f"摘要获取失败：{e}"

    @staticmethod
    def _trigger(run: dict) -> str:
        """触发方式的中文名。"""
        trigger = run.get("triggerType") or ""
        return TRIGGERS.get(trigger, trigger or "-")

    @staticmethod
    def _dt(value: Any) -> datetime | None:
        """解析 Dagu 的时间（有 +08:00 也有 Z），转成本地时区；空值或 "-" 返回 None。"""
        if not value or value == "-":
            return None
        try:
            return datetime.fromisoformat(str(value)).astimezone()
        except ValueError:
            return None

    @classmethod
    def _run_start(cls, run: dict) -> datetime | None:
        """运行的开始时间，还没开始就用排队时间。"""
        return cls._dt(run.get("startedAt")) or cls._dt(run.get("queuedAt"))

    @classmethod
    def _duration(cls, run: dict) -> str:
        """运行耗时；还在跑的算到现在。"""
        start = cls._dt(run.get("startedAt"))
        if not start:
            return ""
        end = cls._dt(run.get("finishedAt"))
        if not end:
            if run.get("status") != 1:
                return ""
            end = datetime.now().astimezone()
        seconds = max(0, int((end - start).total_seconds()))
        if seconds < 60:
            return f"{seconds}秒"
        if seconds < 3600:
            return f"{seconds // 60}分{seconds % 60}秒"
        return f"{seconds // 3600}时{seconds % 3600 // 60}分"

    @staticmethod
    def _fmt_time(value: datetime | None) -> str:
        """今天 / 昨天 / 明天 HH:MM，其余 MM-DD HH:MM。"""
        if not value:
            return "-"
        today = datetime.now().astimezone().date()
        names = {today: "今天", today - timedelta(days=1): "昨天", today + timedelta(days=1): "明天"}
        day = names.get(value.date()) or value.strftime("%m-%d")
        return f"{day} {value.strftime('%H:%M')}"

    # ---------- 配置 ----------

    @staticmethod
    def _parse_dags(text: str) -> dict[str, str]:
        """每行 `DAG名=显示名`，显示名可省略。"""
        dags = {}
        for line in str(text).splitlines():
            dag, _, name = line.partition("=")
            if dag.strip():
                dags[dag.strip()] = name.strip() or dag.strip()
        return dags

    @staticmethod
    def _int(value: Any, default: int, low: int, high: int) -> int:
        """把配置值转成限定范围内的整数。"""
        try:
            return max(low, min(high, int(value)))
        except (TypeError, ValueError):
            return default
