"""group_reply —— 群里接不接话，就两条规则

1. 有人 @ 我，或者回复我的消息 → 把这个人记成"热点"，20 秒内他说的每句都接
2. 其他人 / 其他时候 → 掷一次 0.15，接或不接

不做三态、不让模型输出 <不接>、不打提示词片段。
"""

import json
import os
import random
import shutil
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import At, AtAll, Reply
from astrbot.api.star import Context

from .paths import astrbot_data_root, data_path

CFG_NAME = "addressee_config.json"
DEFAULT_CFG = {
    "config_version": 2,
    "enabled": True,
    "probability": 0.15,
    "window_seconds": 20,
    "groups": ["*"],
}


def _plugin_data_path() -> str:
    return os.path.dirname(data_path())


def _data(*parts: str) -> str:
    return data_path(*parts)


def _legacy_data(*parts: str) -> str:
    return os.path.join(astrbot_data_root(), *parts)


def _legacy_plugin_data(*parts: str) -> str:
    return os.path.join(
        _plugin_data_path(),
        "astrbot_plugin_group_reply",
        *parts,
    )


def _common_data(*parts: str) -> str:
    return os.path.join(data_path(), *parts)


def _write_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _clamp_number(value, default: float, low: float, high: float) -> float:
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return default


def _normalize_config_file(path: str) -> None:
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}

    try:
        version = int(data.get("config_version") or 1)
    except (TypeError, ValueError):
        version = 1
    probability = _clamp_number(data.get("probability"), 0.15, 0.1, 1.0)
    window = _clamp_number(data.get("window_seconds"), 20, 10, 60)

    # v1 的 0.5 / 30 秒是旧默认值，不是用户主动调整。
    if version < 2:
        if abs(probability - 0.5) < 1e-9:
            probability = 0.15
        if abs(window - 30) < 1e-9:
            window = 20

    data["config_version"] = 2
    data["enabled"] = bool(data.get("enabled", True))
    data["probability"] = round(probability, 2)
    data["window_seconds"] = int(round(window))
    groups = data.get("groups")
    data["groups"] = (
        [str(group).strip() for group in groups if str(group).strip()]
        if isinstance(groups, list)
        else ["*"]
    )
    if not data["groups"]:
        data["groups"] = ["*"]
    _write_json(path, data)


def _init_data() -> None:
    """首次启动创建配置；已有旧版配置则自动迁移。"""
    root = _data()
    os.makedirs(root, exist_ok=True)
    target = _data(CFG_NAME)
    if not os.path.exists(target):
        for legacy in (_legacy_plugin_data(CFG_NAME), _legacy_data(CFG_NAME)):
            if os.path.isfile(legacy):
                shutil.copy2(legacy, target)
                break
        else:
            _write_json(target, DEFAULT_CFG)
    _normalize_config_file(target)


class GroupReply:
    def __init__(self, context: Context, config=None):
        self.context = context
        self.config = config or {}
        self._focus: dict[str, float] = {}  # "umo|qq" -> 过期时间戳
        _init_data()
        self._write_cmd_list()

    def _write_cmd_list(self) -> None:
        try:
            d = _common_data("cmd_list")
            os.makedirs(d, exist_ok=True)
            lines = [
                "/group                群聊接话状态",
                "/group on|off         自动接话 / 仅回应 @",
                "/group prob 0.1-1.0   设置接话概率",
                "/group window 10-60   设置热点持续秒数",
            ]
            with open(os.path.join(d, "group_reply.txt"), "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except Exception as e:
            logger.error(f"group_reply: 写命令清单失败 {e}")

    # ---------- 配置 ----------
    def _read_cfg(self) -> dict:
        cfg = dict(DEFAULT_CFG)
        try:
            with open(_data(CFG_NAME), "r", encoding="utf-8-sig") as f:
                d = json.load(f)
            if isinstance(d, dict):
                cfg.update({k: v for k, v in d.items() if k in DEFAULT_CFG})
        except Exception:
            pass
        return cfg

    def _cfg(self) -> dict:
        return self._read_cfg()

    def _group_ok(self, umo: str, cfg: dict) -> bool:
        if "GroupMessage" not in umo:
            return False
        groups = [str(x) for x in (cfg.get("groups") or ["*"])]
        gid = umo.rsplit(":", 1)[-1]
        return "*" in groups or gid in groups

    # ---------- 热点（被 @ / 回复后 20 秒）----------
    def _key(self, umo: str, uid: str) -> str:
        return f"{umo}|{uid}"

    def _is_focus(self, umo: str, uid: str) -> bool:
        exp = self._focus.get(self._key(umo, uid), 0)
        if exp and exp > time.time():
            return True
        if exp:
            self._focus.pop(self._key(umo, uid), None)
        return False

    def _set_focus(self, umo: str, uid: str, secs: float) -> None:
        self._focus[self._key(umo, uid)] = time.time() + secs

    def _save_setting(self, key: str, value) -> None:
        cfg = self._cfg()
        cfg[key] = value
        _write_json(_data(CFG_NAME), cfg)

    # ---------- 是不是在叫我 ----------
    def _addressed(self, event: AstrMessageEvent) -> bool:
        me = str(event.get_self_id())
        try:
            comps = list(event.message_obj.message or [])
        except Exception:
            return False
        for c in comps:
            if isinstance(c, AtAll):
                return True
            if isinstance(c, At) and str(getattr(c, "qq", "")) == me:
                return True
            if isinstance(c, Reply) and str(getattr(c, "sender_id", "")) == me:
                return True
        return False

    # ---------- 唯一钩子 ----------
    async def gate(self, event: AstrMessageEvent, req=None, *args, **kwargs):
        if req is None:
            return
        cfg = self._cfg()
        enabled = bool(cfg.get("enabled", True))

        umo = str(event.unified_msg_origin)
        if not self._group_ok(umo, cfg):
            return  # 私聊 / 不在名单的群，不插手

        # 指令（"/xxx"）和裸 "/"（文本为空）不参与概率，交给 common 处理
        text = (event.message_str or "").strip()
        if not text or text.startswith(("/", "／")):
            return

        uid = str(event.get_sender_id())
        win = float(cfg.get("window_seconds", 20))

        if self._addressed(event):
            if enabled:
                self._set_focus(umo, uid, win)
                logger.info(f"group_reply: {uid} @/回复了我 → 全接 {win:.0f}s")
            else:
                logger.info(f"group_reply: {uid} @/回复了我 → 仅本次回复")
            return

        if not enabled:
            logger.info(f"group_reply: {uid} 非 @/回复消息，仅回应模式跳过")
            try:
                event.stop_event()
            except Exception:
                pass
            return

        if self._is_focus(umo, uid):
            self._set_focus(umo, uid, win)  # 聊着就续期
            return

        p = float(cfg.get("probability", 0.15))
        dice = random.random()
        if dice < p:
            logger.info(f"group_reply: {uid} 骰子 {dice:.2f} < {p} → 接")
            return

        logger.info(f"group_reply: {uid} 骰子 {dice:.2f} ≥ {p} → 不接")
        try:
            event.stop_event()
        except Exception:
            pass

    async def group_cmd(self, event: AstrMessageEvent):
        cfg = self._cfg()
        text = (event.message_str or "").strip()
        arg = ""
        for prefix in ("/group", "group"):
            if text.lower().startswith(prefix):
                arg = text[len(prefix) :].strip().lower()
                break

        if arg in ("on", "off", "开", "关"):
            enabled = arg in ("on", "开")
            self._save_setting("enabled", enabled)
            if not enabled:
                self._focus.clear()
            yield event.plain_result(
                "✅ 自动接话已开启"
                if enabled
                else "已切换为仅回应 @/回复，热点已关闭"
            )
            return

        parts = arg.split(maxsplit=1)
        sub = parts[0] if parts else ""
        value = parts[1].strip() if len(parts) > 1 else ""

        if sub in ("prob", "probability"):
            if not value:
                yield event.plain_result(
                    f"当前接话概率 {float(cfg.get('probability', 0.15)):.2f}\n"
                    "用法：/group prob 0.1-1.0"
                )
                return
            try:
                probability = float(value)
            except ValueError:
                yield event.plain_result("用法：/group prob 0.1-1.0")
                return
            if not 0.1 <= probability <= 1.0:
                yield event.plain_result("接话概率必须在 0.1-1.0 之间")
                return
            self._save_setting("probability", round(probability, 2))
            yield event.plain_result(f"✅ 接话概率已设为 {probability:.2f}")
            return

        if sub == "window":
            if not value:
                yield event.plain_result(
                    f"当前热点持续 {float(cfg.get('window_seconds', 20)):.0f} 秒\n"
                    "用法：/group window 10-60"
                )
                return
            try:
                window = int(value)
            except ValueError:
                yield event.plain_result("用法：/group window 10-60（整数秒）")
                return
            if not 10 <= window <= 60:
                yield event.plain_result("热点时间必须在 10-60 秒之间")
                return
            self._save_setting("window_seconds", window)
            yield event.plain_result(f"✅ 热点时间已设为 {window} 秒")
            return

        enabled = bool(cfg.get("enabled", True))
        yield event.plain_result(
            f"接话模式：{'自动接话' if enabled else '仅回应 @/回复'}\n"
            f"基础概率：{float(cfg.get('probability', 0.15)):.2f}\n"
            f"热点持续：{float(cfg.get('window_seconds', 20)):.0f} 秒\n"
            "用法：/group on|off、prob、window"
        )
