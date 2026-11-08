"""Pisguis single-plugin entry point."""

import asyncio

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.star import Context, Star

from .pisguis.common import Common
from .pisguis.group_reply import GroupReply
from .pisguis.guard import Access
from .pisguis.memory import MemV4
from .pisguis.paths import ensure_data_dir, migrate_legacy_data
from .pisguis.reply_style import ChatStyle


class PisguisPlugin(Star):
    """Pisguis: commands, permissions, prompt assembly, chat style and memory."""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        migrate_legacy_data()
        ensure_data_dir()
        config = config or {}
        self.common = Common(context, config)
        self.access = Access(context)
        self.group_reply = GroupReply(context, config)
        self.memory = MemV4(context, config)
        self.style = ChatStyle(context, config)

    async def terminate(self):
        tasks = [self.memory._idle_task, self.memory._startup_task]
        for task in tasks:
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in tasks if task is not None),
            return_exceptions=True,
        )

    @filter.event_message_type(EventMessageType.ALL, priority=9999)
    async def filter_commands(self, event: AstrMessageEvent):
        await self.access.filter_commands(event)

    @filter.event_message_type(EventMessageType.ALL, priority=9000)
    async def collect_group_message(self, event: AstrMessageEvent):
        await self.memory.collect_group_message(event)

    @filter.on_llm_request(priority=-2000)
    async def group_gate(self, event: AstrMessageEvent, req=None, *args, **kwargs):
        await self.group_reply.gate(event, req, *args, **kwargs)

    @filter.on_llm_request()
    async def apply_mode(self, event: AstrMessageEvent, req=None, *args, **kwargs):
        await self.common.apply_mode(event, req, *args, **kwargs)

    @filter.on_llm_request()
    async def inject_memory(self, event: AstrMessageEvent, req=None, *args, **kwargs):
        await self.memory.inject_memory(event, req, *args, **kwargs)

    @filter.on_llm_request()
    async def gate_and_scene(self, event: AstrMessageEvent, req=None, *args, **kwargs):
        await self.access.gate_and_scene(event, req, *args, **kwargs)

    @filter.on_llm_request(priority=-1000)
    async def compose_prompt(self, event: AstrMessageEvent, req=None, *args, **kwargs):
        await self.common.compose(event, req, *args, **kwargs)

    @filter.on_llm_response()
    async def collect_memory(self, event: AstrMessageEvent, resp=None, *args, **kwargs):
        await self.memory.collect(event, resp, *args, **kwargs)

    @filter.on_decorating_result()
    async def decorate_result(self, event: AstrMessageEvent):
        await self.style.handle_result(event)

    @filter.command("config", alias={"/config"})
    async def config_cmd(self, event: AstrMessageEvent):
        async for result in self.common.config_cmd(event):
            yield result

    @filter.command("prompt", alias={"/prompt"})
    async def prompt_cmd(self, event: AstrMessageEvent):
        async for result in self.common.prompt_cmd(event):
            yield result

    @filter.command("cmd", alias={"/cmd"})
    async def cmd_cmd(self, event: AstrMessageEvent):
        async for result in self.common.cmd_cmd(event):
            yield result

    @filter.command("guard", alias={"/guard"})
    async def guard_cmd(self, event: AstrMessageEvent):
        async for result in self.access.guard_cmd(event):
            yield result

    @filter.command("style", alias={"/style"})
    async def style_cmd(self, event: AstrMessageEvent):
        async for result in self.style.style_cmd(event):
            yield result

    @filter.command("mem", alias={"/mem"})
    async def mem_cmd(self, event: AstrMessageEvent):
        async for result in self.memory.mem_cmd(event):
            yield result
