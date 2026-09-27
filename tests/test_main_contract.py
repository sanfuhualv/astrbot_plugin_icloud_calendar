import asyncio
import importlib
import inspect
import json
import logging
import sys
import types
from dataclasses import dataclass
from typing import Any

import pytest
from astrbot_plugin_icloud_calendar.errors import AccessDeniedError


class FakeStar:
    def __init__(self, context):
        self.context = context


@dataclass
class FakeFunctionTool:
    name: str
    description: str
    parameters: dict
    handler: Any = None
    handler_module_path: str | None = None
    active: bool = True
    is_background_task: bool = False

    @classmethod
    def __class_getitem__(cls, _item):
        return cls

    async def call(self, _context, **_kwargs):
        raise NotImplementedError


class FakeContextWrapper:
    def __init__(self, context):
        self.context = context


class FakeAstrAgentContext:
    def __init__(self, event):
        self.event = event


class FakeContext:
    def __init__(self):
        self.tools = []

    def add_llm_tools(self, *tools):
        self.tools.extend(tools)


def install_astrbot_stubs(monkeypatch, tmp_path):
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event = types.ModuleType("astrbot.api.event")
    star = types.ModuleType("astrbot.api.star")
    core = types.ModuleType("astrbot.core")
    agent = types.ModuleType("astrbot.core.agent")
    run_context = types.ModuleType("astrbot.core.agent.run_context")
    tool = types.ModuleType("astrbot.core.agent.tool")
    astr_agent_context = types.ModuleType("astrbot.core.astr_agent_context")
    utils = types.ModuleType("astrbot.core.utils")
    astrbot_path = types.ModuleType("astrbot.core.utils.astrbot_path")

    class AstrMessageEvent:
        pass

    class Context:
        pass

    class AstrBotConfig(dict):
        pass

    def register(*_args, **_kwargs):
        return lambda cls: cls

    event.AstrMessageEvent = AstrMessageEvent
    star.Context = Context
    star.Star = FakeStar
    star.register = register
    api.AstrBotConfig = AstrBotConfig
    api.logger = logging.getLogger("astrbot-test")
    run_context.ContextWrapper = FakeContextWrapper
    tool.FunctionTool = FakeFunctionTool
    tool.ToolExecResult = str
    astr_agent_context.AstrAgentContext = FakeAstrAgentContext
    astrbot_path.get_astrbot_data_path = lambda: str(tmp_path)

    modules = {
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.event": event,
        "astrbot.api.star": star,
        "astrbot.core": core,
        "astrbot.core.agent": agent,
        "astrbot.core.agent.run_context": run_context,
        "astrbot.core.agent.tool": tool,
        "astrbot.core.astr_agent_context": astr_agent_context,
        "astrbot.core.utils": utils,
        "astrbot.core.utils.astrbot_path": astrbot_path,
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)


def load_plugin(monkeypatch, tmp_path, config=None):
    install_astrbot_stubs(monkeypatch, tmp_path)
    sys.modules.pop("astrbot_plugin_icloud_calendar.main", None)
    module = importlib.import_module("astrbot_plugin_icloud_calendar.main")
    context = FakeContext()
    plugin = module.ICloudCalendarPlugin(context, config or {})
    return module, context, plugin


def test_registers_eight_function_tools_with_explicit_schemas(monkeypatch, tmp_path):
    _module, context, _plugin = load_plugin(monkeypatch, tmp_path)
    assert len(context.tools) == 8
    assert {tool.name for tool in context.tools} == {
        "icloud_list_calendars", "icloud_list_events", "icloud_get_event",
        "icloud_create_event", "icloud_update_event", "icloud_delete_event",
        "icloud_refresh_index", "icloud_index_status",
    }
    for registered_tool in context.tools:
        assert registered_tool.parameters["type"] == "object"
        assert registered_tool.parameters["additionalProperties"] is False
        assert registered_tool.handler is None
        assert inspect.iscoroutinefunction(type(registered_tool).call)
    create_tool = next(item for item in context.tools if item.name == "icloud_create_event")
    assert "calendar_id" not in create_tool.parameters["properties"]
    assert set(create_tool.parameters["required"]) == {"summary", "start", "end", "confirmed"}


def test_tool_returns_data_to_model_without_sending_user_message(monkeypatch, tmp_path):
    _module, context, plugin = load_plugin(monkeypatch, tmp_path)

    class FakeService:
        async def list_calendars(self, _refresh):
            return [{"id": "cal-1", "name": "工作"}]

    plugin._service = FakeService()

    class Event:
        message_obj = types.SimpleNamespace(group_id="")
        unified_msg_origin = "private:1"

        def plain_result(self, _text):
            raise AssertionError("FunctionTool 不应调用 event.plain_result")

        async def send(self, _result):
            raise AssertionError("FunctionTool 不应直接向用户发送消息")

    tool = next(item for item in context.tools if item.name == "icloud_list_calendars")
    run_context = FakeContextWrapper(FakeAstrAgentContext(Event()))
    raw_result = asyncio.run(tool.call(run_context, refresh=False))
    payload = json.loads(raw_result)
    assert payload["ok"] is True
    assert payload["data"]["calendars"][0]["name"] == "工作"
    assert "自然语言" in payload["model_instruction"]


def test_access_controls_default_to_no_group_and_no_write(monkeypatch, tmp_path):
    _module, _context, plugin = load_plugin(monkeypatch, tmp_path)
    group_event = types.SimpleNamespace(
        message_obj=types.SimpleNamespace(group_id="123"), unified_msg_origin="group:123"
    )
    with pytest.raises(AccessDeniedError):
        plugin._authorize(group_event)
    private_event = types.SimpleNamespace(
        message_obj=types.SimpleNamespace(group_id=""), unified_msg_origin="private:1"
    )
    with pytest.raises(AccessDeniedError):
        plugin._authorize(private_event, write=True, confirmed=True)
