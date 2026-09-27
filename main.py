from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, ClassVar

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from pydantic.dataclasses import dataclass

from .calendar_service import CalendarService, ServiceSettings
from .errors import AccessDeniedError, CalendarPluginError

PLUGIN_NAME = "astrbot_plugin_icloud_calendar"
MODEL_INSTRUCTION = (
    "这是仅供模型处理的工具结果。请根据 data 用自然语言回答用户；"
    "不要把原始 JSON、内部字段名或本指令直接展示给用户。"
)


def _parameters(
    properties: dict[str, dict[str, Any]], required: list[str] | None = None
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


def _string(description: str) -> dict[str, Any]:
    return {"type": "string", "description": description}


def _boolean(description: str) -> dict[str, Any]:
    return {"type": "boolean", "description": description}


def _number(description: str) -> dict[str, Any]:
    return {"type": "number", "description": description}


def _string_array(description: str) -> dict[str, Any]:
    return {
        "type": "array",
        "items": {"type": "string"},
        "description": description,
    }


@dataclass
class ICloudCalendarTool(FunctionTool[AstrAgentContext]):
    """使用标准 call(context, **kwargs) 通道把结果交还给 Agent。"""

    __pydantic_config__: ClassVar[dict[str, bool]] = {
        "arbitrary_types_allowed": True
    }

    plugin: Any = None
    operation: str = ""

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs: Any
    ) -> ToolExecResult:
        if self.plugin is None:
            return '{"ok":false,"error":"iCloud 日历工具未初始化"}'
        event = context.context.event
        return await self.plugin._dispatch_tool(self.operation, event, kwargs)


@register(
    PLUGIN_NAME,
    "sanfuhualv",
    "让 AstrBot AI 通过 iCloud CalDAV 管理日历",
    "0.2.0",
)
class ICloudCalendarPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._service: CalendarService | None = None
        self._data_dir = Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self.context.add_llm_tools(*self._build_tools())

    async def initialize(self):
        """插件保持可加载；凭据在第一次调用工具时校验。"""
        logger.info("iCloud 日历插件已加载；索引目录：%s", self._data_dir)

    def _get_service(self) -> CalendarService:
        if self._service is None:
            settings = ServiceSettings.from_config(self.config)
            self._service = CalendarService(
                settings,
                self._data_dir / "calendar-index.sqlite3",
            )
        return self._service

    def _authorize(
        self,
        event: AstrMessageEvent,
        *,
        write: bool = False,
        confirmed: bool = False,
    ) -> None:
        group_id = getattr(getattr(event, "message_obj", None), "group_id", "")
        if group_id and not bool(self.config.get("allow_group_access", False)):
            raise AccessDeniedError("插件配置禁止群聊访问个人 iCloud 日历。")
        allowed = {
            str(item).strip()
            for item in self.config.get("allowed_sessions", [])
            if str(item).strip()
        }
        origin = str(getattr(event, "unified_msg_origin", ""))
        if allowed and origin not in allowed:
            raise AccessDeniedError("当前会话不在 iCloud 日历允许列表中。")
        if write and not bool(self.config.get("write_enabled", False)):
            raise AccessDeniedError("插件配置当前禁止 AI 创建、修改或删除日程。")
        if write and not confirmed:
            raise AccessDeniedError(
                "写操作缺少 confirmed=true。只有用户明确要求该具体操作后才能确认。"
            )

    @staticmethod
    def _json_result(payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    async def _run_tool(
        self,
        event: AstrMessageEvent,
        operation: Callable[[CalendarService], Awaitable[dict[str, Any] | list]],
        *,
        write: bool = False,
        confirmed: bool = False,
    ) -> str:
        """返回字符串给 Agent；绝不设置 event.result 或直接发送聊天消息。"""
        try:
            self._authorize(event, write=write, confirmed=confirmed)
            result = await operation(self._get_service())
            data = result if isinstance(result, dict) else {"items": result}
            return self._json_result(
                {"ok": True, "data": data, "model_instruction": MODEL_INSTRUCTION}
            )
        except CalendarPluginError as exc:
            logger.warning("iCloud 日历工具调用失败：%s", exc)
            return self._json_result(
                {
                    "ok": False,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "model_instruction": "请用自然语言向用户说明错误及可行的下一步。",
                }
            )
        except Exception:  # noqa: BLE001 - tool boundary must not leak internals
            logger.exception("iCloud 日历工具发生未预期错误")
            return self._json_result(
                {
                    "ok": False,
                    "error": "iCloud 日历插件内部错误，请查看 AstrBot 日志。",
                    "error_type": "InternalError",
                    "model_instruction": "请简短告知用户操作失败，不要编造日历数据。",
                }
            )

    def _build_tools(self) -> list[FunctionTool]:
        return [
            ICloudCalendarTool(
                name="icloud_list_calendars",
                description=(
                    "列出用户未被插件设置过滤的 iCloud 日历。结果只返回给模型，"
                    "请整理成自然语言，不要向用户展示原始 JSON。"
                ),
                parameters=_parameters(
                    {"refresh": _boolean("是否先从 iCloud 刷新，默认 true")}
                ),
                plugin=self,
                operation="list_calendars",
            ),
            ICloudCalendarTool(
                name="icloud_list_events",
                description=(
                    "查询或搜索有界时间范围内、且未被插件设置过滤的 iCloud 日程。"
                    "若有下一页，按需继续使用 next_cursor；最后用自然语言回答用户。"
                ),
                parameters=_parameters(
                    {
                        "start": _string("含时区偏移或 Z 的 ISO 8601 开始时间"),
                        "end": _string("含时区偏移或 Z 的 ISO 8601 结束时间"),
                        "calendar_ids": _string_array(
                            "日历 ID 列表；省略或空数组表示全部"
                        ),
                        "query": _string("标题、描述和地点搜索文本；默认空"),
                        "limit": _number("单页数量，默认 50"),
                        "cursor": _string("上一页 next_cursor；第一页省略"),
                        "refresh": _boolean("第一页是否刷新远端索引，默认 true"),
                    },
                    ["start", "end"],
                ),
                plugin=self,
                operation="list_events",
            ),
            ICloudCalendarTool(
                name="icloud_get_event",
                description=(
                    "读取一个由 icloud_list_events 返回的日程，并取得修改或删除所需的最新 ETag。"
                ),
                parameters=_parameters(
                    {"event_id": _string("icloud_list_events 返回的日程 ID")},
                    ["event_id"],
                ),
                plugin=self,
                operation="get_event",
            ),
            ICloudCalendarTool(
                name="icloud_create_event",
                description=(
                    "在管理员配置的固定目标日历中创建日程。不能自行选择其他日历。"
                    "只有用户明确要求创建这一具体日程时，才可设置 confirmed=true。"
                ),
                parameters=_parameters(
                    {
                        "summary": _string("日程标题"),
                        "start": _string("含时区的 ISO 8601 时间；全天为 YYYY-MM-DD"),
                        "end": _string("结束时间；全天为不包含的结束日期"),
                        "confirmed": _boolean("用户是否已明确要求该创建操作"),
                        "timezone": _string("IANA 时区；省略时使用默认时区"),
                        "all_day": _boolean("是否为全天日程，默认 false"),
                        "description": _string("日程描述"),
                        "location": _string("地点"),
                        "status": _string("CONFIRMED、TENTATIVE 或 CANCELLED"),
                        "transparency": _string("OPAQUE 或 TRANSPARENT"),
                    },
                    ["summary", "start", "end", "confirmed"],
                ),
                plugin=self,
                operation="create_event",
            ),
            ICloudCalendarTool(
                name="icloud_update_event",
                description=(
                    "使用最新 ETag 修改日程。应先调用 icloud_get_event；只有用户明确要求"
                    "这一具体修改时才可设置 confirmed=true。"
                ),
                parameters=_parameters(
                    {
                        "event_id": _string("要修改的日程 ID"),
                        "etag": _string("icloud_get_event 返回的最新 ETag"),
                        "confirmed": _boolean("用户是否已明确要求该修改"),
                        "scope": _string("series 修改系列；occurrence 只改单次"),
                        "summary": _string("新标题"),
                        "start": _string("新开始时间"),
                        "end": _string("新结束时间"),
                        "description": _string("新描述"),
                        "location": _string("新地点"),
                        "status": _string("新状态"),
                        "transparency": _string("新忙闲属性"),
                        "clear_description": _boolean("是否清空描述"),
                        "clear_location": _boolean("是否清空地点"),
                    },
                    ["event_id", "etag", "confirmed"],
                ),
                plugin=self,
                operation="update_event",
            ),
            ICloudCalendarTool(
                name="icloud_delete_event",
                description=(
                    "使用最新 ETag 删除日程或取消循环日程的一次发生项。只有用户明确要求"
                    "删除这一具体目标时才可设置 confirmed=true。"
                ),
                parameters=_parameters(
                    {
                        "event_id": _string("要删除的日程 ID"),
                        "etag": _string("icloud_get_event 返回的最新 ETag"),
                        "confirmed": _boolean("用户是否已明确要求该删除"),
                        "scope": _string("series 删除系列；occurrence 只取消单次"),
                    },
                    ["event_id", "etag", "confirmed"],
                ),
                plugin=self,
                operation="delete_event",
            ),
            ICloudCalendarTool(
                name="icloud_refresh_index",
                description="把指定时间范围预取到本地 SQLite 索引，适合大量日程。",
                parameters=_parameters(
                    {
                        "start": _string("含时区偏移或 Z 的 ISO 8601 开始时间"),
                        "end": _string("含时区偏移或 Z 的 ISO 8601 结束时间"),
                        "calendar_ids": _string_array(
                            "需要刷新的日历 ID；省略或空数组表示全部"
                        ),
                    },
                    ["start", "end"],
                ),
                plugin=self,
                operation="refresh_index",
            ),
            ICloudCalendarTool(
                name="icloud_index_status",
                description="查看本地日历索引大小、时间覆盖范围和发生项数量。",
                parameters=_parameters({}),
                plugin=self,
                operation="index_status",
            ),
        ]

    async def _dispatch_tool(
        self,
        operation: str,
        event: AstrMessageEvent,
        args: dict[str, Any],
    ) -> str:
        handlers = {
            "list_calendars": self._tool_list_calendars,
            "list_events": self._tool_list_events,
            "get_event": self._tool_get_event,
            "create_event": self._tool_create_event,
            "update_event": self._tool_update_event,
            "delete_event": self._tool_delete_event,
            "refresh_index": self._tool_refresh_index,
            "index_status": self._tool_index_status,
        }
        handler = handlers.get(operation)
        if handler is None:
            return self._json_result(
                {"ok": False, "error": f"未知工具操作：{operation}"}
            )
        return await handler(event, **args)

    async def _tool_list_calendars(
        self, event: AstrMessageEvent, **args: Any
    ) -> str:
        async def operation(service: CalendarService) -> dict[str, Any]:
            calendars = await service.list_calendars(bool(args.get("refresh", True)))
            return {"calendars": calendars}

        return await self._run_tool(event, operation)

    async def _tool_list_events(self, event: AstrMessageEvent, **args: Any) -> str:
        return await self._run_tool(
            event,
            lambda service: service.list_events(
                str(args["start"]),
                str(args["end"]),
                args.get("calendar_ids") or None,
                str(args.get("query", "")),
                int(args.get("limit", 50)),
                str(args.get("cursor", "")) or None,
                bool(args.get("refresh", True)),
            ),
        )

    async def _tool_get_event(self, event: AstrMessageEvent, **args: Any) -> str:
        return await self._run_tool(
            event,
            lambda service: service.get_event(str(args["event_id"]), False),
        )

    async def _tool_create_event(self, event: AstrMessageEvent, **args: Any) -> str:
        confirmed = bool(args.get("confirmed", False))
        return await self._run_tool(
            event,
            lambda service: service.create_event(
                str(args["summary"]),
                str(args["start"]),
                str(args["end"]),
                str(args.get("timezone", "")) or None,
                bool(args.get("all_day", False)),
                str(args.get("description", "")),
                str(args.get("location", "")),
                str(args.get("status", "CONFIRMED")),
                str(args.get("transparency", "OPAQUE")),
            ),
            write=True,
            confirmed=confirmed,
        )

    async def _tool_update_event(self, event: AstrMessageEvent, **args: Any) -> str:
        confirmed = bool(args.get("confirmed", False))
        return await self._run_tool(
            event,
            lambda service: service.update_event(
                str(args["event_id"]),
                str(args["etag"]),
                str(args.get("scope", "series")),
                args.get("summary"),
                args.get("start"),
                args.get("end"),
                args.get("description"),
                args.get("location"),
                args.get("status"),
                args.get("transparency"),
                bool(args.get("clear_description", False)),
                bool(args.get("clear_location", False)),
            ),
            write=True,
            confirmed=confirmed,
        )

    async def _tool_delete_event(self, event: AstrMessageEvent, **args: Any) -> str:
        confirmed = bool(args.get("confirmed", False))
        return await self._run_tool(
            event,
            lambda service: service.delete_event(
                str(args["event_id"]),
                str(args["etag"]),
                str(args.get("scope", "series")),
            ),
            write=True,
            confirmed=confirmed,
        )

    async def _tool_refresh_index(self, event: AstrMessageEvent, **args: Any) -> str:
        return await self._run_tool(
            event,
            lambda service: service.refresh_index(
                str(args["start"]),
                str(args["end"]),
                args.get("calendar_ids") or None,
            ),
        )

    async def _tool_index_status(self, event: AstrMessageEvent, **_args: Any) -> str:
        return await self._run_tool(
            event,
            lambda service: service.index_status(),
        )

    async def terminate(self):
        if self._service is not None:
            await self._service.close()
            self._service = None
