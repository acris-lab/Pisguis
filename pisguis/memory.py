"""群聊/私聊两层记忆 —— 轮驱动的 页 / 本

存储（自建，不再依赖 sqlite 的上下文窗口）：
- data/plugin_data/astrbot_plugin_pisguis/mem_archive/<QQ>/
- 分片满了 → 立刻新开下一个分片，并把刚满的这个分片喂给「页」整理
- 分片目录最多留 archive_keep 个（默认 10），超了删最早的

三层语义：
- 页：当前一页（可增删改）→ 每 book_pages 次页整理，本吸一次，页冻结翻新
- 本：当前小本本（可改）→ 每次页整理都尝试合入
"""

import asyncio
import copy
import json
import hashlib
import os
import re
import shutil
import time

from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context

try:
    from astrbot.api import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger("astrbot_plugin_pisguis")

from .paths import astrbot_data_root, data_path

# ---------------- 配置 ----------------
DEFAULT_CFG = {
    "config_version": 2,
    "enabled": True,
    "group_memory": True,
    # 每页多少轮（1 轮 = 1 问 1 答）；0 = 关掉自动分页，只在 /mem gen 时整理
    "page_turns": 20,
    "book_pages": 3,          # 3-5 次页整理 → 一次本整理
    "idle_seconds": 1800,     # 空闲兜底：这么久没新消息
    "idle_min_turns": 10,     # 且攒了这么多轮 → 提前整理
    "keep_pages": 2,          # 页/本各留 2 格（当前 + 冻结）
    # 群聊按群独立记录；私聊按用户独立记录
    "max_items": {"hour": 20, "day": 50},
    # importance 名额：None=不限，数字=该档最多几条；没列出的档位只当补位
    "importance_quota": {
        "hour": {"5": None, "4": 12, "3": 8},
        "day": {"5": None, "4": 20, "3": 20},
    },
    # 本只收 importance ≥ 这个值的条目
    "min_importance": {"day": 3},
    "inject": {
        "hour_items": 20, "day_items": 30,
        "hour_frozen_items": 10, "day_frozen_items": 10,
    },
}

TIER_CN = {"hour": "页（最近一段记忆）", "day": "本（小本本）"}
TIER_DUTY = {
    "hour": (
        "这一页只记能复用的事实：TA 是谁、喜好雷区、说定的事、关系温度。"
        "原文里的技术讨论、玩笑、临时情绪不算事实，别当条目抄进来；不要写成聊天流水账。"
    ),
    "day": (
        "这一本只写反复出现的模式（作息、情绪周期）、承诺的进展、关系的整体走向。"
        "页里的单条细事不要原样搬上来，看不到模式就少写。"
    ),
}
GROUP_TIER_DUTY = {
    "hour": (
        "这一页记录本群最近发生的事：谁明确说了什么、共同约定、持续待办和稳定偏好。"
        "普通接话、玩笑和一次性情绪不记；每条都必须保留说话人身份。"
    ),
    "day": (
        "小本本记录本群反复出现的模式：稳定成员信息、长期偏好、共同安排和群内关系。"
        "不要把单次发言升级成群体结论，也不要把多个群友合并成一个人。"
    ),
}

MEMO_SYSTEM = """你是「大肥鱼」。现在你要整理一下你和 TA 这段时间聊过的事，写成你自己的记忆。

用第一人称写：提到 TA 时写「TA」，说自己时写「我」。不要写「对方」「用户」这类第三人称。

【本层】<<TIER>>
【这一层的职责】<<DUTY>>
【日期】<<DATE>>
【上限】<<MAX_ITEMS>> 条（上限，不是目标；够用就少写）

【写什么】
1 TA 是谁：称呼、身份、和我的关系
2 TA 的喜好 / 厌恶 / 固定习惯 / 雷区
3 我们说好的事：计划、时间、待办、承诺
4 我和 TA 的关系：熟络度、称呼、最近的气氛

【怎么写】
- 每条一句话，≤40 字，像我自己记的小本子
- 直接写事实，不加"我觉得""可能"这类犹豫词；口吻随意，不用敬语

【importance 怎么打分】
5 = 身份 / 关系定位 / 核心承诺（TA 是谁、我们之间的定位、说死的约定）
4 = 明确偏好、雷区、待办
3 = 稳定事实（能力、职业、作息）
2 = 偏好细节、口味
1 = 可丢的琐事

【怎么改这一页（大胆点）】
- 这页是我的本子，随便增、删、改、合并、重写，不是只许追加
- 每次重写都应该比上一版更短更准：同类合并成一条，废话直接删，模糊的改具体
- **旧条目不是必须保留**：过时的、没被再提到的、价值低的，删掉就是
- 前后说法冲突时以最新的为准，旧的那条删掉，不要两条都留
- 不要为了显得记性好而堆条数；真没东西可写就输出空列表
- 下面【素材】只是参考材料，不是要你复述
- 只输出 content / type / importance 三个字段，**不要写 id、编号、序号**
- 按 importance 5→1 排好序；超过 <<MAX_ITEMS>> 条就丢 importance 最低的

【判断真假】
- 玩笑、夸张、梗、反问都不算事实，也不能拿来推断 TA 的偏好、人格或立场；
  最多记一条"TA 爱拿这类梗说笑"这种风格判断，别往具体内容上写
- 一次性的说法不成模式；只记 TA 明确表达过、或反复出现过的
- 分清"TA 说的"和"我猜的"：拿不准就不记，不要脑补

【不记】
1 寒暄、语气词、表情、玩笑本身的内容
2 一次性的情绪——只有变成稳定模式才记
3 技术细节、代码、模型名（除非 TA 本身做这行，那属于 TA 是谁）
4 我自己说过的话、我的语气、我的口头禅

【输出】只输出 JSON，不要任何解释文字：
{"items":[{"content":"第一人称一句话，≤40字","type":"who|like|deal|rel","importance":1-5}]}"""

GROUP_MEMO_SYSTEM = """你是「Pisguis」。现在整理的是当前群聊的共享小本本，不是某一个人的私聊画像。

【本层】<<TIER>>
【这一层的职责】<<DUTY>>
【日期】<<DATE>>
【上限】<<MAX_ITEMS>> 条（上限，不是目标；够用就少写）

【这是多人群聊】
- 每条记忆必须写清是谁：用“群友昵称（QQ）：内容”的格式；没有昵称就写“群友 QQ：内容”。
- 不得把多个人的偏好、经历或关系合并到同一条。
- 不写“TA”“对方”“用户”这种无法区分是谁的说法。
- 群友之间的互动可以记，但必须写明涉及哪些人。

【写什么】
1 群友明确自述且以后仍可能有用的事实、偏好、雷区和习惯
2 群内共同约定、活动安排、持续待办
3 群聊稳定梗和称呼，但要避免记录一次性的玩笑内容

【怎么写】
- 每条一句话，≤50 字，像群共享笔记
- 只写群聊里公开出现的信息，不参考群外私聊
- 一次性的情绪、寒暄、复述和普通接话不记
- 同一事实反复出现时合并，不按发言次数重复记录
- importance 5=身份/长期约定，4=明确偏好或雷区，3=稳定事实，1-2=可丢细节
- 大胆删改过期内容；不知道是谁的内容直接丢弃

【输出】只输出 JSON，不要任何解释文字：
{"items":[{"content":"群友昵称（QQ）：一句话，≤50字","type":"who|like|deal|rel","importance":1-5}]}"""


def _pad(s: str, width: int) -> str:
    w = sum(2 if ord(c) > 127 else 1 for c in s)
    return s + " " * max(1, width - w)


def _clamp_int(value, default: int, low: int, high: int) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def _day_label(ts: float | None = None) -> str:
    try:
        d = time.localtime(ts if ts else time.time())
        return f"{time.strftime('%Y-%m-%d', d)} 周{'一二三四五六日'[d.tm_wday]}"
    except Exception:
        return time.strftime("%Y-%m-%d", time.localtime())


def _frag_dir(umo: str) -> str:
    slug = hashlib.md5(str(umo).encode("utf-8")).hexdigest()[:12]
    d = os.path.join(_common_data("prompts"), slug)
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


def _plugin_data_path() -> str:
    return os.path.dirname(data_path())


def _data_dir() -> str:
    return data_path()


def _legacy_data_dir() -> str:
    return astrbot_data_root()


def _legacy_plugin_data(*parts: str) -> str:
    return os.path.join(
        _plugin_data_path(),
        "astrbot_plugin_memory",
        *parts,
    )


def _common_data(*parts: str) -> str:
    return os.path.join(data_path(), *parts)


def _init_data() -> None:
    """首次启动创建插件数据，并迁移旧版 data 根目录内容。"""
    root = _data_dir()
    os.makedirs(root, exist_ok=True)

    cfg_path = os.path.join(root, "memory_config.json")
    if not os.path.exists(cfg_path):
        legacy_cfgs = (
            _legacy_plugin_data("memory_config.json"),
            os.path.join(_legacy_data_dir(), "memory_config.json"),
        )
        for legacy_cfg in legacy_cfgs:
            if os.path.isfile(legacy_cfg):
                shutil.copy2(legacy_cfg, cfg_path)
                break
        else:
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_CFG, f, ensure_ascii=False, indent=2)
    _normalize_config_file(cfg_path)

    memo_path = os.path.join(root, "prompt", "memo_rules.md")
    if not os.path.exists(memo_path):
        os.makedirs(os.path.dirname(memo_path), exist_ok=True)
        legacy_memos = (
            _legacy_plugin_data("prompt", "memo_rules.md"),
            os.path.join(_legacy_data_dir(), "prompt", "memo_rules.md"),
        )
        for legacy_memo in legacy_memos:
            if os.path.isfile(legacy_memo):
                shutil.copy2(legacy_memo, memo_path)
                break
        else:
            with open(memo_path, "w", encoding="utf-8") as f:
                f.write(MEMO_SYSTEM.strip() + "\n")

    group_memo_path = os.path.join(root, "prompt", "memo_rules_group.md")
    if not os.path.exists(group_memo_path):
        os.makedirs(os.path.dirname(group_memo_path), exist_ok=True)
        with open(group_memo_path, "w", encoding="utf-8") as f:
            f.write(GROUP_MEMO_SYSTEM.strip() + "\n")

    archive = os.path.join(root, "mem_archive")
    if not os.path.exists(archive):
        for legacy_archive in (
            _legacy_plugin_data("mem_archive"),
            os.path.join(_legacy_data_dir(), "mem_archive"),
        ):
            if os.path.isdir(legacy_archive):
                shutil.copytree(legacy_archive, archive)
                break
    os.makedirs(archive, exist_ok=True)


def _normalize_config_file(path: str) -> None:
    """把旧三层配置就地升级为两层配置。"""
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    old_version = _clamp_int(data.get("config_version"), 1, 1, 2)
    data.setdefault("enabled", True)
    data.setdefault("group_memory", True)
    data.setdefault("page_turns", 20)
    data["book_pages"] = _clamp_int(data.get("book_pages"), 3, 3, 5)
    data["keep_pages"] = 2
    if old_version < 2:
        data["max_items"] = {"hour": 20, "day": 50}
        data["importance_quota"] = copy.deepcopy(DEFAULT_CFG["importance_quota"])
        data["min_importance"] = copy.deepcopy(DEFAULT_CFG["min_importance"])
        data["inject"] = copy.deepcopy(DEFAULT_CFG["inject"])
    else:
        caps = data.get("max_items")
        if not isinstance(caps, dict):
            caps = {}
        caps["hour"] = _clamp_int(caps.get("hour"), 20, 1, 20)
        caps["day"] = _clamp_int(caps.get("day"), 50, 1, 50)
        data["max_items"] = caps
        data.setdefault("importance_quota", copy.deepcopy(DEFAULT_CFG["importance_quota"]))
        data.setdefault("min_importance", copy.deepcopy(DEFAULT_CFG["min_importance"]))
        data.setdefault("inject", copy.deepcopy(DEFAULT_CFG["inject"]))
    data["config_version"] = 2
    data.pop("profile_books", None)
    data.pop("profile_cap", None)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _qq_of_key(key: str) -> str | None:
    if key.startswith("person:"):
        qq = key.split(":", 1)[1].strip()
        return qq if qq.isdigit() else None
    return None


def _context_of_key(key: str) -> str | None:
    if key.startswith("group:"):
        gid = key.split(":", 1)[1].strip()
        return gid or None
    return None


def _storage_name(key: str) -> str:
    person = _qq_of_key(key)
    if person:
        return person
    group = _context_of_key(key)
    if group:
        safe = re.sub(r"[^0-9A-Za-z_-]", "_", group)
        return f"group_{safe}"
    return hashlib.md5(str(key).encode("utf-8")).hexdigest()[:12]


def _person_dir(key: str) -> str:
    """私聊按用户、群聊按群保存一份独立记忆。"""
    return os.path.join(_data_dir(), "mem_archive", _storage_name(key))


def _mem_file(key: str) -> str:
    return os.path.join(_person_dir(key), "memory.json")


def _clean_items(items: list) -> list:
    """旧数据清洗：丢掉 id，按 importance 从高到低排。"""
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        c = str(it.get("content", "")).strip()
        if not c:
            continue
        out.append(
            {
                "content": c[:60],
                "type": str(it.get("type") or "talk"),
                "importance": max(1, min(5, int(it.get("importance") or 3))),
                "updated_at": it.get("updated_at") or time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
    out.sort(key=lambda x: -int(x["importance"]))
    return out


def _umo_person(umo: str) -> str | None:
    """从私聊 umo 末尾取出用户 ID；群聊返回 None。"""
    if "FriendMessage" in umo or "Friend" in umo:
        parts = umo.split(":")
        if parts:
            tail = parts[-1].strip()
            if tail.isdigit():
                return tail
    return None


def _umo_group(umo: str) -> str | None:
    """从群聊 umo 末尾取出群 ID。"""
    if "Group" not in umo:
        return None
    parts = umo.split(":")
    return parts[-1].strip() if parts else None


def _person_key(qq: str) -> str:
    return f"person:{qq}"


def _group_key(gid: str) -> str:
    return f"group:{gid}"


def _turn_speaker(turn: dict) -> str:
    if str(turn.get("src") or "").lower().startswith("group"):
        name = str(turn.get("speaker_name") or "").strip()
        sid = str(turn.get("speaker_id") or "").strip()
        if name and sid:
            return f"群友 {name}（{sid}）"
        if sid:
            return f"群友 {sid}"
        if name:
            return f"群友 {name}"
        return "群友（未知）"
    return "TA"


def _message_id(event: AstrMessageEvent) -> str:
    message_obj = getattr(event, "message_obj", None)
    for obj in (message_obj, getattr(message_obj, "raw_message", None)):
        if obj is None:
            continue
        value = getattr(obj, "message_id", None)
        if value is None:
            try:
                value = obj["message_id"]
            except Exception:
                pass
        if value is not None:
            return str(value)
    return ""


# ---------------- 待整理对话：每人一个 <QQ>.json ----------------
def _turns_path(key: str) -> str:
    return os.path.join(_person_dir(key), f"{_storage_name(key)}.json")


def _turns_blank(key: str) -> dict:
    return {
        "key": key,
        "scope": "group" if key.startswith("group:") else "private",
        "context_id": _context_of_key(key) or _qq_of_key(key),
        "opened_at": time.time(),
        "turns": [],
    }


def _turns_read(key: str) -> dict | None:
    try:
        with open(_turns_path(key), "r", encoding="utf-8-sig") as f:
            d = json.load(f)
        if isinstance(d, dict):
            d.setdefault("turns", [])
            return d
    except Exception:
        return None
    return None


def _turns_write(key: str, node: dict) -> None:
    d = _person_dir(key)
    os.makedirs(d, exist_ok=True)
    p = _turns_path(key)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(node, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def _turns_clear(key: str) -> None:
    node = _turns_read(key) or _turns_blank(key)
    node["turns"] = []
    node["opened_at"] = time.time()
    node["last_consolidated"] = time.time()
    _turns_write(key, node)


def _turns_list(key: str) -> list[dict]:
    node = _turns_read(key) or {}
    turns = node.get("turns")
    if not isinstance(turns, list):
        return []
    changed = False
    for t in turns:
        if isinstance(t, dict) and "src" not in t:
            t["src"] = "private"
            changed = True
    if changed:
        node["turns"] = turns
        _turns_write(key, node)
    return turns


def _turns_to_text(turns: list[dict]) -> str:
    lines = []
    for t in turns:
        when = time.strftime("%m-%d %H:%M", time.localtime(t.get("ts", 0)))
        src = "群" if str(t.get("src") or "").lower().startswith("group") else "私聊"
        if t.get("user"):
            lines.append(f"[{when}|{src}] {_turn_speaker(t)}：{t['user']}")
        if t.get("reply"):
            lines.append(f"[{when}|{src}] 你：{t['reply']}")
    return "\n".join(lines)


def _render_items(items: list) -> str:
    if not items:
        return "[]"
    return json.dumps(
        [{"content": i.get("content"), "type": i.get("type"), "importance": i.get("importance")}
         for i in items],
        ensure_ascii=False,
    )


class _Mem:
    """一个人的记忆一个文件：mem_archive/<QQ>/memory.json。"""

    def __init__(self):
        self._lock = asyncio.Lock()
        self._recs: dict | None = None

    @staticmethod
    def _blank_rec() -> dict:
        return {
            "hour": {"pages": [], "file": None, "seq": 0, "turns_in_file": 0,
                     "last_turn_at": 0, "pages_done": 0, "last_at": 0},
            "day": {"pages": [], "last_at": 0},
        }

    @staticmethod
    def _migrate(rec: dict) -> dict:
        old_profile = rec.pop("profile", None)
        blank = _Mem._blank_rec()
        for k, v in blank.items():
            if not isinstance(rec.get(k), dict):
                rec[k] = v
            else:
                for kk, vv in v.items():
                    rec[k].setdefault(kk, vv)
        for tier in ("hour", "day"):
            if not isinstance(rec[tier].get("pages"), list):
                rec[tier]["pages"] = []
            for page in rec[tier]["pages"]:
                if isinstance(page, dict) and isinstance(page.get("items"), list):
                    page["items"] = _clean_items(page["items"])
        if isinstance(old_profile, dict) and isinstance(old_profile.get("items"), list):
            profile_items = _clean_items(old_profile["items"])
            if profile_items:
                book = _ensure_page(rec, "day")
                book["items"] = _clean_items(book.get("items", []) + profile_items)
        return rec

    # ---------- 读写 ----------
    def _save_rec(self, key: str) -> None:
        rec = (self._recs or {}).get(key)
        if rec is None:
            return
        d = _person_dir(key)
        os.makedirs(d, exist_ok=True)
        p = _mem_file(key)
        tmp = p + ".tmp"
        payload = {
            "key": key,
            "scope": "group" if key.startswith("group:") else "private",
            "context_id": _context_of_key(key) or _qq_of_key(key),
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "rec": rec,
        }
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=1)
            os.replace(tmp, p)
        except Exception as e:
            logger.error(f"memory: 写 {p} 失败 {e}")

    async def save(self, key: str | None = None) -> None:
        async with self._lock:
            if key:
                self._save_rec(key)
            else:
                for k in list((self._recs or {}).keys()):
                    self._save_rec(k)

    def _load(self) -> dict:
        if self._recs is not None:
            return self._recs
        self._recs = {}
        base = os.path.join(_data_dir(), "mem_archive")
        if os.path.isdir(base):
            for name in os.listdir(base):
                if not name.isdigit() and not name.startswith("group_"):
                    continue
                p = os.path.join(base, name, "memory.json")
                try:
                    if os.path.isfile(p):
                        with open(p, "r", encoding="utf-8-sig") as f:
                            d = json.load(f)
                        rec = d.get("rec") if isinstance(d, dict) and isinstance(d.get("rec"), dict) else d
                    else:
                        rec = self._blank_rec()
                except Exception as e:
                    logger.error(f"memory: 读 {name}/memory.json 失败 {e}")
                    rec = self._blank_rec()
                if name.startswith("group_"):
                    self._recs[f"group:{name[len('group_'):]}"] = self._migrate(rec)
                else:
                    self._recs[f"person:{name}"] = self._migrate(rec)
        return self._recs

    def umo(self, key: str) -> dict:
        recs = self._load()
        rec = recs.get(key)
        if not isinstance(rec, dict):
            rec = self._blank_rec()
            recs[key] = rec
        return self._migrate(rec)

    def all_umos(self) -> list[str]:
        return list(self._load().keys())


def _current_page(rec: dict, tier: str) -> dict | None:
    pages = rec[tier]["pages"]
    return pages[-1] if pages else None


def _frozen_page(rec: dict, tier: str) -> dict | None:
    pages = rec[tier]["pages"]
    return pages[-2] if len(pages) >= 2 else None


def _ensure_page(rec: dict, tier: str) -> dict:
    pages = rec[tier]["pages"]
    if pages and pages[-1].get("state") == "mutable":
        return pages[-1]
    page = {"items": [], "opened_at": time.time(), "frozen_at": None, "state": "mutable"}
    pages.append(page)
    return page


def _freeze_page(rec: dict, tier: str) -> None:
    pages = rec[tier]["pages"]
    if pages and pages[-1].get("state") == "mutable":
        pages[-1]["state"] = "frozen"
        pages[-1]["frozen_at"] = time.time()


def _prune_pages(rec: dict, tier: str, keep: int) -> None:
    pages = rec[tier]["pages"]
    if len(pages) > max(1, keep):
        del pages[: len(pages) - max(1, keep)]


class MemV4:
    def __init__(self, context: Context, config=None):
        self.context = context
        self.config = config or {}
        _init_data()
        self.store = _Mem()
        self._memo = self._load_memo("memo_rules.md", MEMO_SYSTEM)
        self._memo_group = self._load_memo("memo_rules_group.md", GROUP_MEMO_SYSTEM)
        self._busy: set[str] = set()
        self._locks: dict[str, asyncio.Lock] = {}
        self._idle_task = None
        self._startup_task = None
        self._write_cmd_list()
        try:
            loop = asyncio.get_running_loop()
            self._idle_task = loop.create_task(self._idle_loop())
            self._startup_task = loop.create_task(self._startup_catchup())
        except RuntimeError:
            self._idle_task = None
            self._startup_task = None

    async def _startup_catchup(self) -> None:
        """启动自检：如果某个分片已经超过 page_turns（比如上次进程被杀），补一次整理。"""
        await asyncio.sleep(20)
        try:
            if not self._enabled():
                return
            for umo in self.store.all_umos():
                await self._after_turn(umo)
        except Exception as e:
            logger.error(f"memory: 启动补整理失败 {e}")

    def _write_cmd_list(self) -> None:
        """把本插件命令表写入 common 的共享运行数据。"""
        try:
            d = _common_data("cmd_list")
            os.makedirs(d, exist_ok=True)
            lines = [
                "/mem                记忆状态（页/本、待整理轮数）",
                "/mem show           看会注入的记忆",
                "/mem clear          清空当前私聊/群聊记忆",
                "/mem page 0-100     多少轮触发一次整理",
                "/mem book 3-5       几次页整理进入本",
                "/mem pagecap 1-20   设置页容量",
                "/mem bookcap 1-50   设置本容量",
                "/mem gen            立刻整理待整理的对话",
            ]
            with open(os.path.join(d, "memory.txt"), "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except Exception as e:
            logger.error(f"memory: 写命令清单失败 {e}")

    # ---------------- 配置 ----------------
    def _load_memo(self, filename: str, default: str) -> str:
        try:
            p = os.path.join(_data_dir(), "prompt", filename)
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    t = f.read().strip()
                if t:
                    return t
        except Exception:
            pass
        return default

    def _cfg(self) -> dict:
        cfg = copy.deepcopy(DEFAULT_CFG)
        try:
            p = os.path.join(_data_dir(), "memory_config.json")
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8-sig") as f:
                    d = json.load(f)
                if isinstance(d, dict):
                    for k, v in d.items():
                        if k in ("max_items", "inject") and isinstance(v, dict):
                            cfg[k] = {**cfg[k], **v}
                        elif k in cfg:
                            cfg[k] = v
        except Exception as e:
            logger.error(f"memory: 读配置失败 {e}")
        cfg["page_turns"] = _clamp_int(cfg.get("page_turns"), 20, 0, 100)
        cfg["book_pages"] = _clamp_int(cfg.get("book_pages"), 3, 3, 5)
        caps = cfg.setdefault("max_items", {})
        caps["hour"] = _clamp_int(caps.get("hour"), 20, 1, 20)
        caps["day"] = _clamp_int(caps.get("day"), 50, 1, 50)
        caps.pop("profile", None)
        inj = cfg.setdefault("inject", {})
        inj["hour_items"] = min(caps["hour"], _clamp_int(inj.get("hour_items"), 20, 1, 20))
        inj["day_items"] = min(caps["day"], _clamp_int(inj.get("day_items"), 30, 1, 50))
        inj.pop("profile_items", None)
        cfg.pop("profile_cap", None)
        cfg.pop("profile_books", None)
        return cfg

    def _save_cfg_value(self, key: str, value) -> None:
        p = os.path.join(_data_dir(), "memory_config.json")
        try:
            d = {}
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8-sig") as f:
                    d = json.load(f)
            if not isinstance(d, dict):
                d = {}
            d[key] = value
            with open(p, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"memory: 写配置失败 {e}")

    def _save_cfg_max(self, tier: str, value: int) -> None:
        p = os.path.join(_data_dir(), "memory_config.json")
        try:
            with open(p, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                data = {}
            caps = data.get("max_items")
            if not isinstance(caps, dict):
                caps = {}
            caps[tier] = value
            data["max_items"] = caps
            with open(p, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"memory: 写容量配置失败 {e}")

    def _enabled(self) -> bool:
        """用了这个插件就是常开，没有开关。"""
        return True

    def _lock(self, umo: str) -> asyncio.Lock:
        lk = self._locks.get(umo)
        if lk is None:
            lk = asyncio.Lock()
            self._locks[umo] = lk
        return lk

    # ---------------- 模型 ----------------
    async def _llm(self, umo: str, tier: str, user_prompt: str, date_label: str = "") -> str:
        cfg = self._cfg()
        try:
            provider = await self.context.get_using_provider_async(umo)
        except Exception as e:
            logger.error(f"memory: 取模型失败 {e}")
            provider = None
        if provider is None:
            return ""
        pc = getattr(provider, "provider_config", None)
        saved = object()
        try:
            if isinstance(pc, dict):
                ce = pc.get("custom_extra_body")
                saved = copy.deepcopy(ce) if ce is not None else None
                pc["custom_extra_body"] = {
                    k: v for k, v in (ce or {}).items() if k not in ("reasoning_effort", "thinking")
                }
            is_group = str(umo).startswith("group:")
            tpl = (
                (self._memo_group or GROUP_MEMO_SYSTEM)
                if is_group
                else (self._memo or MEMO_SYSTEM)
            )
            duties = GROUP_TIER_DUTY if is_group else TIER_DUTY
            sys_prompt = (
                tpl.replace("<<TIER>>", TIER_CN[tier])
                .replace("<<DUTY>>", duties[tier])
                .replace("<<MAX_ITEMS>>", str(cfg["max_items"].get(tier, 10)))
                .replace("<<DATE>>", date_label or _day_label())
            )
            resp = await provider.text_chat(prompt=user_prompt, contexts=[], system_prompt=sys_prompt)
            return (getattr(resp, "completion_text", "") or "").strip()
        except Exception as e:
            logger.error(f"memory: 整理调用失败 {e}")
            return ""
        finally:
            if isinstance(pc, dict):
                if saved is None:
                    pc.pop("custom_extra_body", None)
                elif isinstance(saved, dict):
                    pc["custom_extra_body"] = saved

    @staticmethod
    def _parse_items(text: str) -> list | None:
        if not text:
            return None
        s, e = text.find("{"), text.rfind("}")
        if s != -1 and e != -1:
            try:
                obj = json.loads(text[s : e + 1])
                if isinstance(obj, dict) and isinstance(obj.get("items"), list):
                    return obj["items"]
            except Exception:
                pass
        s, e = text.find("["), text.rfind("]")
        if s != -1 and e != -1:
            try:
                arr = json.loads(text[s : e + 1])
                if isinstance(arr, list):
                    return arr
            except Exception:
                pass
        return None

    @staticmethod
    def _norm(items: list, cap: int, quota: dict | None = None, min_imp: int = 1) -> list:
        """去重 → importance 降序 → 按档位名额录取 → 不足则用低档补位。"""
        cleaned, seen = [], set()
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        for it in items:
            if not isinstance(it, dict):
                continue
            c = str(it.get("content", "")).strip()
            if not c:
                continue
            key = re.sub(r"[\s，。,.！!？?]+", "", c)[:20]
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(
                {
                    "content": c[:60],
                    "type": str(it.get("type") or "talk"),
                    "importance": max(1, min(5, int(it.get("importance") or 3))),
                    "updated_at": now,
                }
            )
        cleaned.sort(key=lambda x: -int(x.get("importance", 3)))

        if not quota:
            return [i for i in cleaned if i["importance"] >= min_imp][:cap]

        kept, used = [], {}
        for idx, it in enumerate(cleaned):
            imp = int(it["importance"])
            if imp < min_imp:
                continue
            lim = quota.get(str(imp), quota.get(imp, 0))
            if lim is None or used.get(imp, 0) < int(lim):
                kept.append(idx)
                used[imp] = used.get(imp, 0) + 1
                if len(kept) >= cap:
                    break
        # 名额没用满 → 用**没设名额的档位**（1-2 分这类）补位；设了名额的档位不再超额
        if len(kept) < cap:
            quoted = {int(k) for k in quota.keys()}
            for idx, it in enumerate(cleaned):
                if idx in kept:
                    continue
                imp = int(it["importance"])
                if imp < min_imp or imp in quoted:
                    continue
                kept.append(idx)
                if len(kept) >= cap:
                    break
        return [cleaned[i] for i in sorted(kept, key=lambda i: -cleaned[i]["importance"])]

    # ---------------- 记忆键：私聊按人、群聊按群 ----------------
    def _key(self, event: AstrMessageEvent, cfg: dict | None = None) -> str | None:
        cfg = cfg or self._cfg()
        umo = str(event.unified_msg_origin)
        if "Group" in umo:
            if not cfg.get("group_memory", True):
                return None
            gid = _umo_group(umo)
            return _group_key(gid) if gid else None
        try:
            sid = str(event.get_sender_id() or "")
        except Exception:
            sid = ""
        qq = sid or _umo_person(umo)
        return _person_key(qq) if qq else None

    # ---------------- 收集群消息 ----------------
    async def collect_group_message(self, event: AstrMessageEvent):
        if not self._enabled() or event.is_stopped():
            return
        cfg = self._cfg()
        if not cfg.get("group_memory", True):
            return
        key = self._key(event, cfg)
        if not key or not key.startswith("group:"):
            return
        text = (event.message_str or "").strip()
        if not text or text.startswith("/"):
            return
        if event.get_extra("handlers_parsed_params") or event.get_extra("parsed_params"):
            return
        try:
            await self._append_turn(
                key,
                text[:300],
                "",
                "group",
                str(event.get_sender_id() or ""),
                str(event.get_sender_name() or ""),
                _message_id(event),
            )
        except Exception as e:
            logger.error(f"memory: 群消息收集失败 {e}")

    # ---------------- 收集一轮 ----------------
    async def collect(self, event: AstrMessageEvent, resp=None, *args, **kwargs):
        if not self._enabled():
            return
        try:
            key = self._key(event)
            if not key:
                return
            user_text = (event.message_str or "").strip()
            reply = (getattr(resp, "completion_text", "") or "").strip()
            if not user_text or not reply:
                return
            umo = str(event.unified_msg_origin)
            src = "group" if ("GroupMessage" in umo or "Group" in umo) else "private"
            try:
                speaker_id = str(event.get_sender_id() or "")
            except Exception:
                speaker_id = ""
            try:
                speaker_name = str(event.get_sender_name() or "")
            except Exception:
                speaker_name = ""
            await self._append_turn(
                key,
                user_text[:300],
                reply[:300],
                src,
                speaker_id,
                speaker_name,
                _message_id(event),
            )
            asyncio.create_task(self._after_turn(key))
        except Exception as e:
            logger.error(f"memory: 收集失败 {e}")

    async def _append_turn(
        self,
        key: str,
        user: str,
        reply: str,
        src: str = "private",
        speaker_id: str = "",
        speaker_name: str = "",
        message_id: str = "",
    ) -> None:
        async with self._lock(key):
            node = _turns_read(key) or _turns_blank(key)
            node.setdefault("turns", [])
            if src == "group" and message_id:
                for turn in reversed(node["turns"]):
                    if str(turn.get("message_id") or "") != message_id:
                        continue
                    turn["reply"] = reply or turn.get("reply", "")
                    turn["speaker_id"] = speaker_id or turn.get("speaker_id", "")
                    turn["speaker_name"] = speaker_name or turn.get("speaker_name", "")
                    break
                else:
                    node["turns"].append(
                        {
                            "ts": time.time(),
                            "user": user,
                            "reply": reply,
                            "src": src,
                            "speaker_id": speaker_id,
                            "speaker_name": speaker_name,
                            "message_id": message_id,
                        }
                    )
            else:
                node["turns"].append(
                    {
                        "ts": time.time(),
                        "user": user,
                        "reply": reply,
                        "src": src,
                        "speaker_id": speaker_id,
                        "speaker_name": speaker_name,
                        "message_id": message_id,
                    }
                )
            node["updated_at"] = time.time()
            _turns_write(key, node)
            rec = self.store.umo(key)
            rec["hour"]["turns_in_file"] = len(node["turns"])
            rec["hour"]["last_turn_at"] = time.time()
            await self.store.save(key)

    async def _after_turn(self, key: str) -> None:
        """每轮结束看一下：待整理的轮数够不够。"""
        try:
            cfg = self._cfg()
            n = int(cfg["page_turns"])
            if n <= 0 or key in self._busy:
                return
            async with self._lock(key):
                rec = self.store.umo(key)
                turns = int(rec["hour"].get("turns_in_file") or 0)
            if turns < n:
                return
            self._busy.add(key)
            try:
                await self._consolidate_page(key)
            finally:
                self._busy.discard(key)
        except Exception as e:
            logger.error(f"memory: 后台整理失败 {e}")
            self._busy.discard(key)

    def _quota(self, cfg: dict, tier: str) -> dict:
        q = (cfg.get("importance_quota") or {}).get(tier)
        if isinstance(q, dict):
            return {int(k): (None if v is None else int(v)) for k, v in q.items()}
        return {}

    def _min_imp(self, cfg: dict, tier: str) -> int:
        return int((cfg.get("min_importance") or {}).get(tier, 1))

    # ---------------- 页整理 ----------------
    async def _consolidate_page(self, key: str) -> bool:
        cfg = self._cfg()
        turns = _turns_list(key)
        if not turns:
            return False
        raw = _turns_to_text(turns)
        rec = self.store.umo(key)
        page = _ensure_page(rec, "hour")
        prev = _frozen_page(rec, "hour")
        prev_txt = _render_items(prev.get("items", [])) if prev else ""
        prompt = (
            f"【这一页现在的内容】\n{_render_items(page.get('items', []))}\n\n"
            + (f"【上一页（已冻结，只读参考，不要改动它）】\n{prev_txt}\n\n" if prev_txt else "")
            + f"【素材：这 {len(turns)} 轮原话】\n{raw}"
        )
        text = await self._llm(key, "hour", prompt, _day_label(turns[-1].get("ts")))
        items = self._parse_items(text)
        if items is None:
            logger.error("memory: 页整理返回无法解析，待整理对话先留着")
            return False
        page["items"] = self._norm(
            items, int(cfg["max_items"].get("hour", 25)), self._quota(cfg, "hour")
        )
        page["state"] = "mutable"
        page["sources"] = sorted({str(t.get("src") or "private") for t in turns})
        rec["hour"]["last_at"] = time.time()
        rec["hour"]["pages_done"] = int(rec["hour"].get("pages_done") or 0) + 1
        rec["hour"]["turns_in_file"] = 0
        _turns_clear(key)          # 触发整理 → 待整理对话清空
        await self.store.save(key)
        logger.info(f"memory: 页整理 {len(turns)} 轮 → {len(page['items'])} 条；待整理已清空")
        await self._maybe_book(key)
        return True

    # ---------------- 本 ----------------
    async def _maybe_book(self, umo: str) -> None:
        cfg = self._cfg()
        rec = self.store.umo(umo)
        done = int(rec["hour"].get("pages_done") or 0)
        if done < int(cfg.get("book_pages", 3)):
            return
        if await self._consolidate_book(umo):
            rec = self.store.umo(umo)
            rec["hour"]["pages_done"] = 0
            await self.store.save()

    async def _consolidate_book(self, umo: str) -> bool:
        cfg = self._cfg()
        rec = self.store.umo(umo)
        page = _current_page(rec, "hour")
        src = list((page or {}).get("items", []))
        if not src:
            logger.info("memory: 页里还没内容，本先不动")
            return False
        book = _ensure_page(rec, "day")
        prompt = (
            f"【这一本现在的内容】\n{_render_items(book.get('items', []))}\n\n"
            f"【刚整理完的这几页（只读参考）】\n{_render_items(src)}"
        )
        text = await self._llm(umo, "day", prompt, _day_label())
        items = self._parse_items(text)
        if items is None:
            logger.error("memory: 本整理返回无法解析")
            return False
        book["items"] = self._norm(
            items,
            int(cfg["max_items"].get("day", 15)),
            self._quota(cfg, "day"),
            self._min_imp(cfg, "day"),
        )
        book["state"] = "mutable"
        _freeze_page(rec, "hour")
        _ensure_page(rec, "hour")
        _prune_pages(rec, "hour", int(cfg.get("keep_pages", 2)))
        rec["day"]["last_at"] = time.time()
        await self.store.save()
        logger.info(f"memory: 本整理 吸 {len(src)} 条 → 本 {len(book['items'])} 条；页冻结翻新")
        return True

    # ---------------- 空闲兜底 ----------------
    async def _idle_consolidate(self, umo: str) -> None:
        """空闲兜底：攒了 ≥idle_min_turns 轮且已经静默够久，就提前整理。"""
        try:
            if umo in self._busy:
                return
            cfg = self._cfg()
            async with self._lock(umo):
                rec = self.store.umo(umo)
                turns = int(rec["hour"].get("turns_in_file") or 0)
                last = float(rec["hour"].get("last_turn_at") or 0)
            if turns < int(cfg.get("idle_min_turns", 10)):
                return
            if time.time() - last < int(cfg.get("idle_seconds", 1800)):
                return
            self._busy.add(umo)
            try:
                logger.info(f"memory: 空闲兜底触发（{turns} 轮没到 {cfg['page_turns']} 轮）")
                await self._consolidate_page(umo)
            finally:
                self._busy.discard(umo)
        except Exception as e:
            logger.error(f"memory: 空闲兜底失败 {e}")
            self._busy.discard(umo)

    async def _idle_loop(self) -> None:
        await asyncio.sleep(30)
        while True:
            try:
                cfg = self._cfg()
                if self._enabled() and int(cfg.get("page_turns", 40)) > 0:
                    for umo in self.store.all_umos():
                        await self._idle_consolidate(umo)
            except Exception as e:
                logger.error(f"memory: 空闲检查失败 {e}")
            await asyncio.sleep(60)

    # ---------------- 注入 ----------------
    def _top(self, items: list, n: int) -> list:
        """按 importance 从高到低取前 n 条。"""
        ordered = sorted(items, key=lambda i: -int(i.get("importance") or 3))
        return ordered[: max(0, n)]

    def _visible(self, rec: dict, cfg: dict) -> dict:
        """当前页/本有内容就用当前的；空了（刚整理完）才拿冻结的填这个空窗。"""
        inj = cfg["inject"]
        hp, hf = _current_page(rec, "hour"), _frozen_page(rec, "hour")
        dp, df = _current_page(rec, "day"), _frozen_page(rec, "day")
        hour = self._top((hp or {}).get("items", []), int(inj.get("hour_items", 25)))
        hour_from_frozen = False
        if not hour:
            hour = self._top((hf or {}).get("items", []), int(inj.get("hour_frozen_items", 10)))
            hour_from_frozen = bool(hour)
        day = self._top((dp or {}).get("items", []), int(inj.get("day_items", 15)))
        day_from_frozen = False
        if not day:
            day = self._top((df or {}).get("items", []), int(inj.get("day_frozen_items", 10)))
            day_from_frozen = bool(day)
        return {
            "hour": hour,
            "day": day,
            "frozen": {"hour": hour_from_frozen, "day": day_from_frozen},
        }

    def _memory_block(self, rec: dict, cfg: dict) -> str:
        v = self._visible(rec, cfg)
        frozen = v.get("frozen") or {}
        parts = []
        keys = (
            ("hour", "上一段（刚整理完，暂时用这个填着）" if frozen.get("hour") else "最近一段"),
            ("day", "上一本（刚整理完，暂时用这个填着）" if frozen.get("day") else "小本本"),
        )
        for key, label in keys:
            items = v.get(key) or []
            if not items:
                continue
            parts.append(
                f"<{key}：{label}>\n"
                + "\n".join(f"- {i.get('content','')}" for i in items)
                + f"\n</{key}>"
            )
        if not parts:
            return ""
        return (
            "\n\n====================\n### [MEMORY] ###\n"
            "（这是当前私聊或群聊的记忆，只在与当前话题相关时才用；不要主动复述）\n"
            + "\n".join(parts)
            + "\n====================\n"
        )
    async def inject_memory(self, event: AstrMessageEvent, req=None, *args, **kwargs):
        """只写自己的 40_memory.txt 片段，不碰 system_prompt。"""
        if req is None or not self._enabled():
            return
        try:
            cfg = self._cfg()
            key = self._key(event, cfg)
            if not key:
                return
            block = self._memory_block(self.store.umo(key), cfg)
            _write_frag(str(event.unified_msg_origin), "40_memory.txt", block)
        except Exception as e:
            logger.error(f"memory: 注入失败 {e}")

    # ---------------- 指令 ----------------
    # 权限统一由 access 插件在处理指令前拦截，这里不再判断

    @staticmethod
    def _arg(event: AstrMessageEvent) -> str:
        text = (event.message_str or "").strip()
        for t in ("/mem", "mem"):
            if text.lower().startswith(t):
                return text[len(t):].strip()
        return text.strip()

    def _status(self, event: AstrMessageEvent, with_help: bool = True) -> str:
        cfg = self._cfg()
        umo = self._key(event, cfg) or str(event.unified_msg_origin)
        rec = self.store.umo(umo)
        hp = _current_page(rec, "hour") or {}
        dp = _current_page(rec, "day") or {}
        cap_h = int(cfg["max_items"].get("hour", 20))
        cap_d = int(cfg["max_items"].get("day", 50))
        n = int(cfg["page_turns"])
        turns = int(rec["hour"].get("turns_in_file") or 0)
        done_h = int(rec["hour"].get("pages_done") or 0)
        cur = _turns_list(umo)
        src_priv = sum(1 for t in cur if str(t.get("src") or "private") == "private")
        src_grp = len(cur) - src_priv
        lines = [
            "🧠 记忆（常开）",
            "────────────────────────",
            f"{_pad('页', 4)}{len(hp.get('items', []))} / {cap_h} 条"
            f"   待整理 {turns} / {n if n else '∞'} 轮",
            f"{_pad('本', 4)}{len(dp.get('items', []))} / {cap_d} 条"
            f"   {done_h} / {int(cfg.get('book_pages', 3))} 页 → 换本",
            "────────────────────────",
            f"冻结 页 {len((_frozen_page(rec,'hour') or {}).get('items', []))} 条"
            f" / 本 {len((_frozen_page(rec,'day') or {}).get('items', []))} 条（也注入）",
            f"存 {_storage_name(umo)}.json"
            f"｜待整理 私聊 {src_priv} 轮 / 群 {src_grp} 轮",
        ]
        if n == 0:
            lines.append("（page 0 = 不自动分页，只在 /mem gen 时整理）")
        if dp.get("items") is None:
            lines.append("")
        if with_help:
            lines += [
                "────────────────────────",
                "/mem show          看现在会带进去的记忆",
                "/mem clear         清掉当前私聊或群聊的页/本",
                "/mem page 0-100    每页多少轮（默认 20）",
                "/mem book 3-5      几次页整理进入本（默认 3）",
                "/mem gen           立刻用当前分片整理一次",
            ]
        return "\n".join(lines)
    async def mem_cmd(self, event: AstrMessageEvent):
        """记忆：查看 / 清空 / 分页 / 手动整理（权限由 access 插件统一拦截）"""
        cfg = self._cfg()
        arg = self._arg(event).lower()
        umo = self._key(event, cfg) or str(event.unified_msg_origin)

        if not arg:
            yield event.plain_result(self._status(event))
            return

        if arg in ("on", "off", "enable", "disable", "开", "关"):
            yield event.plain_result("记忆插件是常开的，没有开关。要清空就 /mem clear。")
            return

        if arg.startswith("pagecap"):
            part = arg[7:].strip()
            if not part:
                yield event.plain_result(
                    f"页上限 {cfg['max_items']['hour']} 条\n用法：/mem pagecap 1-20"
                )
                return
            try:
                n = int(part)
            except ValueError:
                yield event.plain_result("用法：/mem pagecap 1-20")
                return
            if n < 1 or n > 20:
                yield event.plain_result("页上限只能填 1-20")
                return
            self._save_cfg_max("hour", n)
            yield event.plain_result(f"✅ 页上限已设为 {n} 条")
            return

        if arg.startswith("bookcap"):
            part = arg[7:].strip()
            if not part:
                yield event.plain_result(
                    f"本上限 {cfg['max_items']['day']} 条\n用法：/mem bookcap 1-50"
                )
                return
            try:
                n = int(part)
            except ValueError:
                yield event.plain_result("用法：/mem bookcap 1-50")
                return
            if n < 1 or n > 50:
                yield event.plain_result("本上限只能填 1-50")
                return
            self._save_cfg_max("day", n)
            yield event.plain_result(f"✅ 本上限已设为 {n} 条")
            return

        if arg.startswith("page"):
            part = arg[4:].strip()
            if not part:
                yield event.plain_result(
                    f"每页 {cfg['page_turns']} 轮（1 轮 = 1 问 1 答）\n用法：/mem page 0-100（0=不自动分页）"
                )
                return
            try:
                n = int(part)
            except ValueError:
                yield event.plain_result("用法：/mem page 0-100")
                return
            if n < 0 or n > 100:
                yield event.plain_result("只能填 0-100")
                return
            self._save_cfg_value("page_turns", n)
            extra = "（关闭自动分页）" if n == 0 else ""
            yield event.plain_result(f"✅ 每页已设为 {n} 轮{extra}\n下个分片开始生效，当前分片不受影响")
            return

        if arg.startswith("book"):
            part = arg[4:].strip()
            if not part:
                yield event.plain_result(
                    f"每 {cfg['book_pages']} 次页整理进入本\n用法：/mem book 3-5"
                )
                return
            try:
                n = int(part)
            except ValueError:
                yield event.plain_result("用法：/mem book 3-5")
                return
            if n < 3 or n > 5:
                yield event.plain_result("只能填 3-5")
                return
            self._save_cfg_value("book_pages", n)
            yield event.plain_result(f"✅ 已设为每 {n} 次页整理进入本")
            return

        if arg.startswith("gen"):
            turns = _turns_list(umo)
            if not turns:
                yield event.plain_result("还没有待整理的对话（先聊几句）")
                return
            ok = await self._consolidate_page(umo)
            if not ok:
                yield event.plain_result("这次整理没成功（模型没按 JSON 格式返回），对话还留着，可以再试一次")
                return
            rec = self.store.umo(umo)
            hp = _current_page(rec, "hour") or {}
            dp = _current_page(rec, "day") or {}
            yield event.plain_result(
                f"✅ 已用 {len(turns)} 轮对话整理一次（已清空）\n"
                f"页 {len(hp.get('items', []))} 条 · 本 {len(dp.get('items', []))} 条"
            )
            return

        if arg == "show":
            v = self._visible(self.store.umo(umo), cfg)
            frozen = v.get("frozen") or {}
            lines = ["🧠 现在会带进聊天的记忆", "────────────────────────"]
            for k, base in (("hour", "页"), ("day", "本")):
                label = base
                if k in frozen and frozen.get(k):
                    label = base + "（空窗，用刚冻结的填）"
                items = v.get(k, [])
                lines.append(f"【{label}】{len(items)} 条")
                lines += [f"  · {i.get('content')}" for i in items] or ["  （空）"]
                lines.append("")
            total = sum(len(v.get(k, [])) for k in ("hour", "day"))
            lines.append(f"合计 {total} 条（都会注入）")
            yield event.plain_result("\n".join(lines))
            return

        if arg == "clear":
            data = self.store._load()
            data.pop(umo, None)
            await self.store.save()
            yield event.plain_result("✅ 已清空当前私聊或群聊的页/本（待整理对话保留）")
            return

        if arg in ("clearall", "clear_all"):
            yield event.plain_result("清空全部记忆的命令已移除（太危险）。要清就对本会话用 /mem clear。")
            return

        yield event.plain_result(
            "可用：/mem、show、clear、page 0-100、book 3-5、pagecap 1-20、bookcap 1-50、gen"
        )
