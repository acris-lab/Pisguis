"""Shared paths and migration for the single Pisguis plugin."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

PLUGIN_NAME = "astrbot_plugin_pisguis"

_CURRENT_DIRS = (
    "astrbot_plugin_pisguis_common",
    "astrbot_plugin_pisguis_guard",
    "astrbot_plugin_pisguis_group_reply",
    "astrbot_plugin_pisguis_memory",
    "astrbot_plugin_pisguis_reply_style",
)
_OLD_DIRS = (
    "astrbot_plugin_common",
    "astrbot_plugin_group_reply",
    "astrbot_plugin_memory",
    "astrbot_plugin_reply_style",
)
_ROOT_FILES = (
    "access_config.json",
    "addressee_config.json",
    "chat_style_config.json",
    "memory_config.json",
    "chat_rules.md",
    "mode_state.json",
)
_ROOT_DIRS = ("cmd_list", "prompts", "prompt", "mem_archive")


def astrbot_data_root() -> str:
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        return get_astrbot_data_path()
    except Exception:
        return os.path.join(os.getcwd(), "data")


def plugin_data_root() -> str:
    override = os.environ.get("PISGUIS_PLUGIN_DATA_ROOT")
    if override:
        return os.path.abspath(override)
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

        return get_astrbot_plugin_data_path()
    except Exception:
        return os.path.join(astrbot_data_root(), "plugin_data")


def data_path(*parts: str) -> str:
    return os.path.join(plugin_data_root(), PLUGIN_NAME, *parts)


def _merge_tree(src: str, dst: str) -> None:
    if not os.path.isdir(src):
        return
    src_real = os.path.normcase(os.path.realpath(src))
    dst_real = os.path.normcase(os.path.realpath(dst))
    if src_real == dst_real:
        return
    for root, _, files in os.walk(src):
        rel = os.path.relpath(root, src)
        out_dir = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(out_dir, exist_ok=True)
        for name in files:
            target = os.path.join(out_dir, name)
            if not os.path.exists(target):
                shutil.copy2(os.path.join(root, name), target)


def migrate_legacy_data() -> None:
    """Copy missing data from released layouts without overwriting current files."""
    target = data_path()
    os.makedirs(target, exist_ok=True)
    base = plugin_data_root()
    for dirname in _CURRENT_DIRS + _OLD_DIRS:
        _merge_tree(os.path.join(base, dirname), target)

    root = astrbot_data_root()
    for name in _ROOT_FILES:
        src = os.path.join(root, name)
        dst = os.path.join(target, name)
        if os.path.isfile(src) and not os.path.exists(dst):
            shutil.copy2(src, dst)
    for name in _ROOT_DIRS:
        _merge_tree(os.path.join(root, name), os.path.join(target, name))

    old_style = os.path.join(root, "config", "astrbot_plugin_human_delay_config.json")
    new_style = os.path.join(target, "chat_style_config.json")
    if os.path.isfile(old_style) and not os.path.exists(new_style):
        shutil.copy2(old_style, new_style)


def ensure_data_dir() -> str:
    path = Path(data_path())
    path.mkdir(parents=True, exist_ok=True)
    return str(path)
