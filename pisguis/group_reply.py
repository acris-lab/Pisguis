"""group_reply —— 群里接不接话，就两条规则

1. 有人 @ 我，或者回复我的消息 → 把这个人记成"热点"，30 秒内他说的每句都接
2. 其他人 / 其他时候 → 掷一次 0.5，接或不接

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
    "enabled": True,
    "probability": 0.5,
    "window_seconds": 30,
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


def _init_data() -> None:
    """首次启动创建配置；已有旧版配置则自动迁移。"""
    root = _data()
    os.makedirs(root, exist_ok=True)
    target = _data(CFG_NAME)
    if os.path.exists(target):
        return
    for legacy in (_legacy_plugin_data(CFG_NAME), _legacy_data(CFG_NAME)):
        if os.path.isfile(legacy):
            shutil.copy2(legacy, target)
            return
    _write_json(target, DEFAULT_CFG)


class GroupReply:
    def __init__(self, context: Context, config=None):
        self.context = context
        self.config = config or {}
        self._focus: dict[str, float] = {}  # "umo|qq" -> 过期时间戳
        _init_data()
        self._remove_cmd_list()

    def _remove_cmd_list(self) -> None:
        try:
            path = _common_data("cmd_list", "group_reply.txt")
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass

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

    # ---------- 热点（被 @ / 回复后 30 秒）----------
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
        if not cfg.get("enabled", True):
            return

        umo = str(event.unified_msg_origin)
        if not self._group_ok(umo, cfg):
            return  # 私聊 / 不在名单的群，不插手

        # 指令（"/xxx"）和裸 "/"（文本为空）不参与概率，交给 common 处理
        text = (event.message_str or "").strip()
        if not text or text.startswith(("/", "／")):
            return

        uid = str(event.get_sender_id())
        win = float(cfg.get("window_seconds", 30))

        if self._addressed(event):
            self._set_focus(umo, uid, win)
            logger.info(f"group_reply: {uid} @/回复了我 → 全接 {win:.0f}s")
            return

        if self._is_focus(umo, uid):
            self._set_focus(umo, uid, win)  # 聊着就续期
            return

        p = float(cfg.get("probability", 0.5))
        dice = random.random()
        if dice < p:
            logger.info(f"group_reply: {uid} 骰子 {dice:.2f} < {p} → 接")
            return

        logger.info(f"group_reply: {uid} 骰子 {dice:.2f} ≥ {p} → 不接")
        try:
            event.stop_event()
        except Exception:
            pass
