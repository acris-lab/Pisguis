"""access —— 指令过滤 / agent 闸门 / 会话身份标记

职责（只做这三件事，不注册任何用户命令）：
1 指令过滤：/ 开头的消息，只有主人放行；其他人 event.stop_event()，AI 完全看不见
2 agent 闸门：非主人清空工具集，并明说"没有工具"
3 会话身份：给每次请求追加 <scene> / <who> 两段，告诉模型现在是私聊还是群、对方是谁

配置 data/plugin_data/astrbot_plugin_pisguis/access_config.json
（明文 QQ，不哈希）。
首次私聊会自动认领主人，后续可通过 /guard 管理二级管理员。
"""

import asyncio
import hashlib
import json
import os
import shutil

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.command_group import CommandGroupFilter

from .paths import astrbot_data_root, data_path

CFG_NAME = "access_config.json"
DEFAULT_CFG = {
    "enabled": True,
    "owner_ids": [],
    "admin_ids": [],
    "filter_commands": True,
    "scene_prompt": True,
}
BUILTIN_COMMANDS = {
    "help": True,
    "sid": True,
    "name": True,
    "reset": True,
    "stop": True,
    "new": True,
    "stats": True,
    "provider": True,
    "dashboard_update": True,
    "set": True,
    "unset": True,
}


def _data_path() -> str:
    return data_path()


def _cfg() -> dict:
    cfg = dict(DEFAULT_CFG)
    try:
        with open(os.path.join(_data_path(), CFG_NAME), "r", encoding="utf-8-sig") as f:
            d = json.load(f)
        if isinstance(d, dict):
            cfg.update({k: v for k, v in d.items() if k in DEFAULT_CFG})
    except Exception:
        pass
    for key in ("owner_ids", "admin_ids"):
        cfg[key] = [str(x).strip() for x in (cfg.get(key) or []) if str(x).strip()]
    return cfg


def _save_cfg(cfg: dict) -> None:
    path = os.path.join(_data_path(), CFG_NAME)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _init_data() -> None:
    root = _data_path()
    os.makedirs(root, exist_ok=True)
    target = os.path.join(root, CFG_NAME)
    if os.path.exists(target):
        return
    legacy = os.path.join(astrbot_data_root(), CFG_NAME)
    if os.path.isfile(legacy):
        shutil.copy2(legacy, target)
    else:
        _save_cfg(dict(DEFAULT_CFG))


def _is_primary(sid, cfg: dict) -> bool:
    return str(sid).strip() in set(cfg.get("owner_ids") or [])


def _ensure_wake_prefix(context: Context) -> None:
    """统一使用 AstrBot 默认唤醒前缀，斜杠由 guard 在鉴权后恢复。"""
    try:
        cfg = context.get_config()
        if cfg.get("wake_prefix") == ["/"]:
            return
        cfg["wake_prefix"] = ["/"]
        cfg.save_config()
        logger.info("guard: wake_prefix 已初始化/修正为 ['/']")
    except Exception as e:
        logger.error(f"guard: 初始化 wake_prefix 失败 {e}")


def _frag_dir(umo: str) -> str:
    slug = hashlib.md5(str(umo).encode("utf-8")).hexdigest()[:12]
    d = os.path.join(
        _data_path(),
        "prompts",
        slug,
    )
    os.makedirs(d, exist_ok=True)
    return d


def _write_frag(umo: str, name: str, text: str) -> None:
    """写提示词片段；空就删。"""
    try:
        p = os.path.join(_frag_dir(umo), name)
        if not (text or "").strip():
            if os.path.exists(p):
                os.remove(p)
            return
        with open(p, "w", encoding="utf-8") as f:
            f.write(text.strip())
    except Exception:
        pass




def _is_owner(sid, cfg: dict) -> bool:
    owners = {str(x).strip() for x in (cfg.get("owner_ids") or []) if str(x).strip()}
    # 首次安装尚未配置主人时开放，避免把机器人锁死；配置任一主人后恢复严格鉴权。
    return not owners or str(sid).strip() in owners


def _is_admin(sid, cfg: dict) -> bool:
    return str(sid).strip() in set(cfg.get("admin_ids") or [])


def _role(sid, cfg: dict) -> str:
    if _is_owner(sid, cfg):
        return "owner"
    if _is_admin(sid, cfg):
        return "admin"
    return "guest"


def _pick(obj, key):
    """raw_message 可能是对象也可能是 dict，两种都试。"""
    v = getattr(obj, key, None)
    if v is None:
        try:
            v = obj[key]
        except Exception:
            pass
    return v


def _text(event: AstrMessageEvent) -> str:
    """原始文本：组件 → OneBot 原始事件（message 段 / raw_message 字符串）。"""
    try:
        t = "".join(
            getattr(c, "text", "") or "" for c in (event.message_obj.message or [])
        )
        if t.strip():
            return t.strip()
    except Exception:
        pass
    rm = getattr(event.message_obj, "raw_message", None)
    if rm is not None:
        segs = _pick(rm, "message")
        if isinstance(segs, list):
            parts = []
            for s in segs:
                if isinstance(s, dict) and s.get("type") == "text":
                    parts.append(str((s.get("data") or {}).get("text", "")))
            t = "".join(parts).strip()
            if t:
                return t
        rs = _pick(rm, "raw_message")
        if isinstance(rs, str) and rs.strip():
            return rs.strip()
    return (event.message_str or "").strip()


def _command_name(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("/"):
        text = text[1:]
    return text.split(maxsplit=1)[0].lower() if text else ""


def _has_activated_command(event: AstrMessageEvent) -> bool:
    handlers = event.get_extra("activated_handlers") or []
    return any(
        any(
            isinstance(f, CommandFilter | CommandGroupFilter)
            for f in handler.event_filters
        )
        for handler in handlers
    )


def _drop_activated_command_handlers(event: AstrMessageEvent) -> None:
    """把无斜杠命令从本轮处理器中移除，让消息继续作为聊天处理。"""
    handlers = event.get_extra("activated_handlers")
    if not handlers:
        return
    removed: set[str] = set()
    kept = []
    for handler in handlers:
        if any(
            isinstance(f, CommandFilter | CommandGroupFilter)
            for f in handler.event_filters
        ):
            removed.add(handler.handler_full_name)
        else:
            kept.append(handler)
    handlers[:] = kept
    params = event.get_extra("handlers_parsed_params")
    if isinstance(params, dict):
        for name in removed:
            params.pop(name, None)


def _scene(event: AstrMessageEvent, role: str) -> str:
    umo = str(event.unified_msg_origin)
    is_group = "Group" in umo
    tail = umo.rsplit(":", 1)[-1] if ":" in umo else ""
    if is_group:
        speakers = "群里说话的人里可能有 TA，也可能只是普通群友；拿不准就按普通群友对待，别把私聊的事说出去"
        scene = f"群聊（群号 {tail}），消息可能来自多个人"
    else:
        speakers = "TA 就是眼前这个人，可以放开聊" if role != "guest" else "对方是陌生人，只闲聊"
        scene = "私聊（一对一）"
    if role == "owner":
        who = "主人（拥有全部权限，可以管理 guard）"
    elif role == "admin":
        who = "二级管理员（可以调用除 /guard 外的命令，不能使用工具）"
    else:
        who = "陌生人（没有权限，只能闲聊）"
    return (
        f"<scene>当前会话：{scene}</scene>\n"
        f"<who>对方是：{who}；{speakers}</who>"
    )


class Access:
    def __init__(self, context: Context):
        self.context = context
        self._claim_lock = asyncio.Lock()
        _init_data()
        _ensure_wake_prefix(context)
        if not _cfg().get("owner_ids"):
            logger.warning("guard: 未配置主人，当前为开放模式，所有用户均按主人处理")
        self._write_cmd_list()

    def _write_cmd_list(self) -> None:
        try:
            d = os.path.join(
                _data_path(),
                "cmd_list",
            )
            os.makedirs(d, exist_ok=True)
            lines = [
                "/guard                权限状态",
                "/guard on|off         开关鉴权与工具限制",
                "/guard add <QQ>       添加二级管理员",
                "/guard remove <QQ>    移除二级管理员",
                "/guard list           查看主人和管理员",
            ]
            with open(os.path.join(d, "guard.txt"), "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except Exception as e:
            logger.error(f"guard: 写命令清单失败 {e}")

    async def _claim_owner(self, event: AstrMessageEvent, cfg: dict) -> dict:
        """未设置主人时，把第一个私聊发送者设为主人。"""
        if cfg.get("owner_ids") or not event.is_private_chat():
            return cfg
        async with self._claim_lock:
            cfg = _cfg()
            if cfg.get("owner_ids"):
                return cfg
            sender = str(event.get_sender_id()).strip()
            if not sender:
                return cfg
            cfg["owner_ids"] = [sender]
            _save_cfg(cfg)
            logger.warning(f"guard: 首次私聊初始化主人 <{sender}>")
            return cfg

    # ---------- 1) 指令过滤 ----------
    async def filter_commands(self, event: AstrMessageEvent):
        """斜杠命令和纯字符串内置命令的统一鉴权入口。

        斜杠命令由内核剥掉 "/"，主人通过鉴权后恢复原文。
        内置命令不带斜杠，通过 BUILTIN_COMMANDS 单独识别；其他
        无斜杠命令使用 WakingCheck 已激活的 CommandFilter 识别。
        """
        cfg = _cfg()
        cfg = await self._claim_owner(event, cfg)
        if not cfg.get("enabled", True) or not cfg.get("filter_commands", True):
            return

        logger.info(f"guard: probe message_str={(event.message_str or '')!r}")
        text = _text(event)
        is_slash = text.startswith("/")
        is_builtin = _command_name(text) in BUILTIN_COMMANDS

        # 所有命令都只接受斜杠形式；纯字符串移除命令处理器后继续聊天。
        if not is_slash:
            if is_builtin or _has_activated_command(event):
                _drop_activated_command_handlers(event)
                logger.info(
                    f"guard: 无斜杠命令按聊天处理 <{event.get_sender_id()}> {text[:24]!r}"
                )
            return

        command = _command_name(text)
        if command == "guard":
            authorized = bool(cfg.get("owner_ids")) and _is_primary(
                event.get_sender_id(), cfg
            )
        else:
            authorized = _is_owner(event.get_sender_id(), cfg) or _is_admin(
                event.get_sender_id(), cfg
            )

        if not authorized or not _has_activated_command(event):
            _drop_activated_command_handlers(event)
            logger.info(
                f"guard: 未授权或未匹配命令按聊天处理 "
                f"<{event.get_sender_id()}> {text[:24]!r}"
            )
            return

        # 自定义插件同时接受有/无斜杠形式；内置命令保留内核处理后的形式。
        if not is_builtin:
            event.message_str = text

    # ---------- 2) agent 闸门 + 3) 会话身份 ----------
    async def gate_and_scene(self, event: AstrMessageEvent, req=None, *args, **kwargs):
        if req is None:
            return
        cfg = _cfg()
        if not cfg.get("enabled", True):
            return
        role = _role(event.get_sender_id(), cfg)

        if role != "owner":
            had_tools = bool(getattr(req, "func_tool", None))
            try:
                from astrbot.core.agent.tool import ToolSet

                req.func_tool = ToolSet()
            except Exception:
                req.func_tool = None
            # 框架把技能说明写在 system_prompt 里，非主人先摘掉（只删不加）
            try:
                sp = getattr(req, "system_prompt", "") or ""
                for marker in ("## Skills", "<skills>"):
                    idx = sp.find(marker)
                    if idx >= 0:
                        req.system_prompt = sp[:idx].rstrip()
                        break
            except Exception:
                pass
            if had_tools:
                logger.info(f"guard: 已剥离 {event.get_sender_id()} 的工具集（非主人角色）")

        if cfg.get("scene_prompt", True):
            block = _scene(event, role)
            if role != "owner":
                block += (
                    "\n\n【没有工具】你现在没有任何工具可用：不能读文件、不能执行命令、"
                    "不能上网、不能建提醒。有人让你做这类事，就直接说做不到，"
                    "绝对不要输出任何工具调用格式。"
                )
            _write_frag(str(event.unified_msg_origin), "20_scene.txt", block)

    # ---------- 4) 主人管理 ----------
    def _arg(self, event: AstrMessageEvent) -> str:
        text = (event.message_str or "").strip()
        for prefix in ("guard", "/guard"):
            if text.lower().startswith(prefix):
                return text[len(prefix) :].strip()
        return ""
    async def guard_cmd(self, event: AstrMessageEvent):
        cfg = _cfg()
        if not _is_primary(event.get_sender_id(), cfg):
            yield event.plain_result("只有主人可以管理 guard")
            return

        arg = self._arg(event)
        parts = arg.split(maxsplit=1)
        sub = parts[0].lower() if parts else ""
        target = parts[1].strip() if len(parts) > 1 else ""

        if sub in ("on", "off", "开", "关"):
            enabled = sub in ("on", "开")
            cfg["enabled"] = enabled
            if enabled:
                cfg["filter_commands"] = True
            _save_cfg(cfg)
            yield event.plain_result(
                "✅ guard 已开启：恢复命令鉴权和工具限制"
                if enabled
                else "已关闭 guard：命令鉴权和工具限制停止生效"
            )
            return

        if not sub or sub in ("status", "list"):
            owners = "、".join(cfg.get("owner_ids") or []) or "未设置"
            admins = "、".join(cfg.get("admin_ids") or []) or "无"
            yield event.plain_result(
                f"guard：{'开' if cfg.get('enabled', True) else '关'}\n"
                f"主人：{owners}\n"
                f"二级管理员：{admins}\n"
                "二级管理员可以调用除 /guard 外的命令和工具"
            )
            return

        if sub in ("add", "remove"):
            if not target.isdigit():
                yield event.plain_result(f"用法：/guard {sub} <QQ>")
                return
            admins = list(dict.fromkeys(cfg.get("admin_ids") or []))
            if sub == "add":
                if target in (cfg.get("owner_ids") or []):
                    yield event.plain_result("该 QQ 已经是主人")
                    return
                if target not in admins:
                    admins.append(target)
                    cfg["admin_ids"] = admins
                    _save_cfg(cfg)
                yield event.plain_result(f"已添加二级管理员 {target}")
            else:
                if target not in admins:
                    yield event.plain_result("该 QQ 不在二级管理员名单")
                    return
                admins.remove(target)
                cfg["admin_ids"] = admins
                _save_cfg(cfg)
                yield event.plain_result(f"已移除二级管理员 {target}")
            return

        yield event.plain_result(
            "用法：/guard、/guard on|off、/guard add <QQ>、"
            "/guard remove <QQ>、/guard list"
        )
