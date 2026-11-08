"""chat_style —— 文本清洗 + 断句分段 + 拟人延迟（一条发送链）

原来三个插件（tidy / human_delay）合并到这里，只挂一个 on_decorating_result。
命令：/style（状态、延迟档位、分段模式）
"""

import asyncio
import difflib
import json
import os
import random
import re
import shutil

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Plain
from astrbot.api.star import Context

from .paths import astrbot_data_root, data_path

CFG_NAME = "chat_style_config.json"
DEFAULT_CFG = {
    "enabled": True,
    "distribution": "normal",
    "mean": 2.5,
    "sigma": 1.3,
    "min_delay": 0.0,
    "max_delay": 5.0,
    "base_delay": 1.0,
    "per_char_seconds": 0.05,
    "segment_min_interval": 1.5,
    "segment_max": 6,
    "max_total": 15.0,
    "split": "auto",
    "clean": True,
    "leak_action": "deflect",
}

# 聊天模式
MAX_SEG_CHARS = 40
SOFT_MAX_CHARS = 12
MAX_SEGMENTS = 6
# 长文模式
LONG_CHARS = 120
LONG_AVG_LINE = 30
LONG_MAX_SEG = 150
LONG_MIN_SEG = 40
LONG_MAX_SEGMENTS = 4

TOOL_LEAK_PATTERNS = [
    r"<\|+\s*/?\s*DSML",
    r"<｜",
    r"invoke\s+name\s*=",
    r"tool[▁_ ]?calls",
    r"<tool_call",
    r"</?function_call",
    r'"name"\s*:\s*"[a-z0-9_]*(execute|shell|read|write|grep|search|tool)',
    r'"arguments"\s*:',
    r"astrbot_[a-z_]+",
    r'"command"\s*:\s*"',
]

DEFLECT_LINES = ["啊这 我弄不了这个", "这个我真做不到", "想啥呢 我哪有这本事", "emm 这个我帮不上", "我只会聊天 干不了这个"]

MONOLOGUE_HEAD = ("（", "(", "[", "【")
MONOLOGUE_WORDS = ("草稿", "我心里", "我想想", "我心里想", "其实我", "内心", "旁白", "OS：", "os：")

_LAST_SENT: dict[str, list[str]] = {}


def _plugin_data_path() -> str:
    return os.path.dirname(data_path())


def _data(*parts: str) -> str:
    return data_path(*parts)


def _legacy_data(*parts: str) -> str:
    return os.path.join(astrbot_data_root(), *parts)


def _legacy_plugin_data(*parts: str) -> str:
    return os.path.join(
        _plugin_data_path(),
        "astrbot_plugin_reply_style",
        *parts,
    )


def _common_data(*parts: str) -> str:
    return os.path.join(data_path(), *parts)


def _pad(s: str, width: int) -> str:
    w = sum(2 if ord(c) > 127 else 1 for c in s)
    return s + " " * max(1, width - w)


def _read_json(path: str, default) -> dict:
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8-sig") as f:
                d = json.load(f)
            if isinstance(d, dict):
                return d
    except Exception:
        pass
    return default


def _write_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _level_cfg(level: int) -> dict:
    level = max(0, min(10, level))
    if level == 0:
        return {**DEFAULT_CFG, "enabled": False, "mean": 0.0, "sigma": 0.0, "max_delay": 0.0,
                "base_delay": 0.0, "per_char_seconds": 0.0, "segment_min_interval": 0.0}
    return {
        **DEFAULT_CFG,
        "enabled": True,
        "distribution": "normal",
        "mean": round(level * 0.5, 2),
        "sigma": round(level * 0.26, 2),
        "min_delay": 0.0,
        "max_delay": float(level),
        "base_delay": round(level * 0.2, 2),
        "per_char_seconds": round(level * 0.01, 3),
        "segment_min_interval": 1.5,
    }


class ChatStyle:
    def __init__(self, context: Context, config=None):
        self.context = context
        self.config = config or {}
        self._init_data()
        self._write_cmd_list()

    def _write_cmd_list(self) -> None:
        """把本插件命令表写入 common 的共享运行数据。"""
        try:
            d = _common_data("cmd_list")
            os.makedirs(d, exist_ok=True)
            lines = [
                "/style                发送节奏状态",
                "/style on|off         开关清洗、分段与延迟",
                "/style delay 0-10     档位（0=秒发，5=默认）",
            ]
            with open(os.path.join(d, "reply_style.txt"), "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except Exception as e:
            logger.error(f"reply_style: 写命令清单失败 {e}")

    def _init_data(self) -> None:
        """首次启动创建配置，并兼容旧版 data 根目录配置。"""
        root = _data()
        os.makedirs(root, exist_ok=True)
        target = _data(CFG_NAME)
        if os.path.exists(target):
            return

        for legacy in (_legacy_plugin_data(CFG_NAME), _legacy_data(CFG_NAME)):
            if os.path.isfile(legacy):
                shutil.copy2(legacy, target)
                return

        old = _read_json(
            _legacy_data("config", "astrbot_plugin_human_delay_config.json"),
            {},
        )
        if old:
            merged = {**DEFAULT_CFG, **{k: v for k, v in old.items() if k in DEFAULT_CFG}}
            _write_json(_data(CFG_NAME), merged)
            logger.info("reply_style: 已从旧 human_delay 配置迁移")
            return
        _write_json(target, DEFAULT_CFG)

    # ---------- 配置 ----------
    def _cfg(self) -> dict:
        cfg = dict(DEFAULT_CFG)
        d = _read_json(_data(CFG_NAME), {})
        cfg.update({k: v for k, v in d.items() if k in DEFAULT_CFG})
        return cfg

    def _per_char(self) -> float:
        try:
            return float(self._cfg().get("per_char_seconds", 0.05))
        except Exception:
            return 0.05

    def _floor(self) -> float:
        try:
            return float(self._cfg().get("segment_min_interval", 1.5))
        except Exception:
            return 1.5

    def _delay_level(self) -> int:
        cfg = self._cfg()
        if not cfg.get("enabled", True):
            return 0
        try:
            return int(round(float(cfg.get("max_delay", 5))))
        except Exception:
            return 5

    # ---------- 延迟 ----------
    def compute_delay(self, chars: int) -> float:
        cfg = self._cfg()
        lo = float(cfg.get("min_delay", 0))
        hi = float(cfg.get("max_delay", 5))
        if hi < lo:
            lo, hi = hi, lo
        if str(cfg.get("distribution", "normal")).lower() == "uniform":
            base = random.uniform(lo, hi)
        else:
            base = random.gauss(float(cfg.get("mean", 2.5)), float(cfg.get("sigma", 1.3)))
        base = min(max(base, lo), hi)
        cap = float(cfg.get("max_total", 15))
        return max(0.0, min(base + max(0, chars) * self._per_char(), cap))

    def segment_delay(self, chars: int) -> float:
        cfg = self._cfg()
        cap = float(cfg.get("max_total", 15))
        return max(0.0, min(max(self._floor(), float(cfg.get("base_delay", 1.0)) + chars * self._per_char()), cap))

    # ---------- 清洗 ----------
    def _is_tool_leak(self, text: str) -> bool:
        return any(re.search(p, text, re.I) for p in TOOL_LEAK_PATTERNS)

    @staticmethod
    def _similar(a: str, b: str, ratio: float = 0.8) -> bool:
        a, b = a.strip(), b.strip()
        if not a or not b:
            return False
        if a == b:
            return True
        return difflib.SequenceMatcher(None, a, b).ratio() >= ratio

    def clean(self, text: str, umo: str) -> str:
        cfg = self._cfg()
        t = text.replace("。", "\n")
        if cfg.get("clean", True):
            for ch in ("，", ",", "；", ";", "、"):
                t = t.replace(ch, " ")
        lines = [ln.strip() for ln in t.split("\n") if ln.strip()]
        kept = [ln for ln in lines if not (ln.startswith(MONOLOGUE_HEAD) or any(w in ln for w in MONOLOGUE_WORDS))]

        dedup: list[str] = []
        for ln in kept:
            if dedup and self._similar(dedup[-1], ln, 0.7):
                dedup[-1] = ln
                continue
            dedup.append(ln)

        prev = _LAST_SENT.get(umo, [])
        final = [ln for ln in dedup if not any(self._similar(p, ln, 0.8) for p in prev)]

        merged: list[str] = []
        for ln in final:
            if merged and len(ln) <= 3:
                merged[-1] = merged[-1] + " " + ln
            else:
                merged.append(ln)
        if len(merged) >= 2 and len(merged[0]) <= 3:
            merged[1] = merged[0] + " " + merged[1]
            merged.pop(0)
        return re.sub(r"[ \t]{2,}", " ", "\n".join(merged)).strip()

    # ---------- 分段 ----------
    @staticmethod
    def _structured(text: str) -> bool:
        if "```" in text:
            return True
        for line in text.splitlines():
            s = line.strip()
            if not s:
                continue
            if s.startswith(("- ", "* ", "> ", "|")) or re.match(r"^\d+[.、)]\s+", s):
                return True
        return text.count("|") >= 3

    def _mode_of(self, text: str, lines: list[str]) -> str:
        override = str(self._cfg().get("split", "auto")).lower()
        if override in ("chat", "long"):
            return override
        no_ws = re.sub(r"\s", "", text)
        if len(no_ws) <= 10:
            return "single"
        avg = len(no_ws) / max(1, len(lines))
        if len(no_ws) > LONG_CHARS or avg > LONG_AVG_LINE:
            return "long"
        return "chat"

    def _split_long(self, text: str) -> list[str]:
        paras = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
        if len(paras) <= 1:
            paras = [p.strip() for p in re.split(r"[\r\n]+", text) if p.strip()]
        segs: list[str] = []
        for para in paras:
            if len(para) <= LONG_MAX_SEG:
                segs.append(para)
                continue
            buf = ""
            for piece in [p.strip() for p in re.split(r"(?<=[。！？!?])", para) if p.strip()]:
                while len(piece) > LONG_MAX_SEG:
                    cut = max(piece.rfind("，", 0, LONG_MAX_SEG), piece.rfind(",", 0, LONG_MAX_SEG))
                    cut = cut if cut > 20 else LONG_MAX_SEG
                    segs.append(piece[:cut].strip())
                    piece = piece[cut:].strip()
                cand = f"{buf}{piece}"
                if buf and len(cand) > LONG_MAX_SEG:
                    segs.append(buf)
                    buf = piece
                else:
                    buf = cand
            if buf:
                segs.append(buf)
        merged: list[str] = []
        for seg in segs:
            if merged and len(merged[-1]) < LONG_MIN_SEG and len(merged[-1]) + len(seg) <= LONG_MAX_SEG:
                merged[-1] = merged[-1] + "\n" + seg
            else:
                merged.append(seg)
        if len(merged) > LONG_MAX_SEGMENTS:
            head = merged[: LONG_MAX_SEGMENTS - 1]
            head.append("\n".join(merged[LONG_MAX_SEGMENTS - 1:]))
            return head
        return merged

    def split_segments(self, text: str) -> list[str]:
        text = (text or "").strip()
        if not text:
            return []
        if self._structured(text):
            return [text]
        lines = [s.strip() for s in re.split(r"[\r\n]+", text) if s.strip()]
        mode = self._mode_of(text, lines)
        if mode == "single":
            return [text.replace("\n", " ")]
        if mode == "long":
            return self._split_long(text)

        parts: list[str] = []
        for line in lines:
            for piece in [p.strip() for p in re.split(r"(?<=[，。！？；~…])", line) if p.strip()] or [line]:
                words = piece.split()
                if len(words) > 1:
                    buf = ""
                    for word in words:
                        cand = f"{buf} {word}".strip()
                        if buf and len(cand) > SOFT_MAX_CHARS:
                            parts.append(buf)
                            buf = word
                        else:
                            buf = cand
                    if buf:
                        parts.append(buf)
                elif len(piece) > MAX_SEG_CHARS:
                    parts.extend(piece[i : i + MAX_SEG_CHARS] for i in range(0, len(piece), MAX_SEG_CHARS))
                else:
                    parts.append(piece)
        merged: list[str] = []
        for seg in parts:
            if merged and len(seg) <= 2 and len(merged[-1]) <= 8:
                merged[-1] = merged[-1] + seg
            else:
                merged.append(seg)
        out = merged[:MAX_SEGMENTS]
        if len(merged) > MAX_SEGMENTS:
            out[-1] = " ".join(merged[MAX_SEGMENTS - 1:])
        return out

    # ---------- 发送链 ----------
    async def handle_result(self, event: AstrMessageEvent):
        try:
            cfg = self._cfg()
            if not cfg.get("enabled", True):
                return
            if (event.message_str or "").strip().startswith("/"):
                return
            if event.get_extra("handlers_parsed_params", {}) or event.get_extra("parsed_params", None):
                return
            res = event.get_result()
            if res is None:
                return
            chain = list(getattr(res, "chain", []) or [])
            if not chain or not all(isinstance(c, Plain) for c in chain):
                return
            raw = "".join(c.text or "" for c in chain)
            if not raw.strip():
                return

            umo = str(event.unified_msg_origin)

            if self._is_tool_leak(raw):
                action = str(cfg.get("leak_action", "deflect")).lower()
                logger.warning(f"reply_style: 工具调用残留（{action}）{raw[:80]!r}")
                res.chain = [] if action == "silent" else [Plain(random.choice(DEFLECT_LINES))]
                return

            if raw.lstrip().startswith(("/语音", "／语音")):
                return

            text = self.clean(raw, umo) if cfg.get("clean", True) else raw.strip()
            if not text:
                res.chain = []
                return
            _LAST_SENT[umo] = [s.strip() for s in text.split("\n") if s.strip()][-6:]

            if not cfg.get("enabled", True):
                res.chain = [Plain(text)]
                return

            segs = self.split_segments(text)
            if len(segs) <= 1:
                delay = self.compute_delay(len(text))
                res.chain = [Plain(text)]
                if delay > 0.05:
                    logger.info(f"reply_style: 整条等 {delay:.2f}s（{len(text)} 字）")
                    await asyncio.sleep(delay)
                return

            first = segs[0]
            res.chain = [Plain(first)]
            no_ws = len(re.sub(r"\s", "", text))
            mode = "长文" if no_ws > LONG_CHARS else "聊天"
            delay = self.compute_delay(len(first))
            logger.info(
                f"reply_style: {mode}模式 {no_ws} 字 → {len(segs)} 段，首条 {len(first)} 字等 {delay:.2f}s"
            )
            if delay > 0.05:
                await asyncio.sleep(delay)
            asyncio.create_task(self._send_rest(event, segs[1:]))
        except Exception as e:
            logger.error(f"reply_style: 处理失败 {e}")

    async def _send_rest(self, event: AstrMessageEvent, segs: list[str]) -> None:
        try:
            for seg in segs:
                if not seg.strip():
                    continue
                await asyncio.sleep(self.segment_delay(len(seg)))
                await event.send(MessageChain([Plain(text=seg)]))
                logger.info(f"reply_style: 已发送分段 {len(seg)} 字")
        except Exception as e:
            logger.error(f"reply_style: 分段发送失败 {e}")

    # ---------- 指令 ----------
    @staticmethod
    def _arg(event: AstrMessageEvent) -> str:
        text = (event.message_str or "").strip()
        for token in ("/style", "style", "/节奏"):
            if text.lower().startswith(token):
                return text[len(token):].strip()
        return text.strip()

    def _help(self) -> str:
        cfg = self._cfg()
        return (
            "🎚 发送节奏\n"
            "────────────────────────\n"
            f"{_pad('开关', 8)}{'开' if cfg.get('enabled', True) else '关'}\n"
            f"{_pad('档位', 8)}{self._delay_level()}（0=秒发，5=默认，10=最慢）\n"
            f"{_pad('首条', 8)}正态 μ={cfg.get('mean')} σ={cfg.get('sigma')}\n"
            f"{_pad('每字', 8)}{cfg.get('per_char_seconds')} 秒\n"
            f"{_pad('分段地板', 8)}{self._floor()} 秒｜单条上限 {cfg.get('max_total')} 秒\n"
            f"{_pad('分段', 8)}{cfg.get('split')}｜清洗 {'开' if cfg.get('clean', True) else '关'}\n"
            "────────────────────────\n"
            "/style on|off            模块开关\n"
            "/style delay 0-10          档位"
        )
    async def style_cmd(self, event: AstrMessageEvent):
        cfg = self._cfg()
        arg = self._arg(event)
        if not arg:
            yield event.plain_result(self._help())
            return
        parts = arg.split(maxsplit=1)
        sub = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""

        if sub in ("on", "off", "开", "关"):
            enabled = sub in ("on", "开")
            _write_json(_data(CFG_NAME), {**cfg, "enabled": enabled})
            yield event.plain_result(
                "✅ 回复样式已开启" if enabled else "已关闭回复样式：回复将原样发送"
            )
            return

        if sub == "delay":
            if not rest:
                yield event.plain_result(f"当前 {self._delay_level()} 档。用法：/style delay 0-10")
                return
            try:
                level = int(rest)
            except ValueError:
                yield event.plain_result("参数必须是 0-10 的整数")
                return
            if level < 0 or level > 10:
                yield event.plain_result("参数必须在 0-10 之间")
                return
            new = {**cfg, **_level_cfg(level)}
            _write_json(_data(CFG_NAME), new)
            yield event.plain_result(
                "✅ 已设为 0（立刻发送，分段也不等）" if level == 0 else
                f"✅ 已设为 {level} 档：首条 0-{level:g} 秒正态 + 每字 {level*0.01:.2f} 秒，"
                f"分段地板 {self._floor():.1f} 秒"
            )
            return

        if sub in ("perchar", "typing", "per_char"):
            try:
                v = float(rest)
            except ValueError:
                yield event.plain_result("用法：/style perchar 0-0.3（每字秒数）")
                return
            if v < 0 or v > 0.3:
                yield event.plain_result("每字延迟只能在 0-0.3 秒之间")
                return
            _write_json(_data(CFG_NAME), {**cfg, "per_char_seconds": round(v, 3)})
            yield event.plain_result(f"✅ 每字 {v:.3f} 秒")
            return

        if sub == "floor":
            try:
                v = float(rest)
            except ValueError:
                yield event.plain_result("用法：/style floor 0.5-10（分段之间的最低等待）")
                return
            if v < 0.2 or v > 10:
                yield event.plain_result("地板只能在 0.2-10 秒之间")
                return
            _write_json(_data(CFG_NAME), {**cfg, "segment_min_interval": round(v, 2)})
            yield event.plain_result(f"✅ 分段地板 {v:.2f} 秒")
            return

        if sub in ("normal", "gauss"):
            parts = rest.split()
            if not parts:
                yield event.plain_result("用法：/style normal 2.5 [1.3]（均值 [标准差]）")
                return
            try:
                mean = float(parts[0])
                sigma = float(parts[1]) if len(parts) > 1 else float(cfg.get("sigma", 1.3))
            except ValueError:
                yield event.plain_result("用法：/style normal 2.5 [1.3]")
                return
            if not (0 <= mean <= 10) or not (0 <= sigma <= 5):
                yield event.plain_result("均值 0-10，标准差 0-5")
                return
            _write_json(_data(CFG_NAME), {**cfg, "mean": round(mean, 2), "sigma": round(sigma, 2)})
            yield event.plain_result(f"✅ 首条延迟 正态(μ={mean:.2f}, σ={sigma:.2f})")
            return

        if sub == "total":
            try:
                v = float(rest)
            except ValueError:
                yield event.plain_result("用法：/style total 1-60（单条上限秒数）")
                return
            if v < 1 or v > 60:
                yield event.plain_result("上限只能在 1-60 秒之间")
                return
            _write_json(_data(CFG_NAME), {**cfg, "max_total": round(v, 1)})
            yield event.plain_result(f"✅ 单条延迟上限 {v:.1f} 秒")
            return

        if sub == "split":
            val = rest.lower()
            if val not in ("auto", "chat", "long"):
                yield event.plain_result("用法：/style split auto|chat|long")
                return
            _write_json(_data(CFG_NAME), {**cfg, "split": val})
            yield event.plain_result(f"✅ 分段模式：{val}")
            return

        if sub == "clean":
            val = rest.lower()
            if val not in ("on", "off", "开", "关"):
                yield event.plain_result("用法：/style clean on|off")
                return
            on = val in ("on", "开")
            _write_json(_data(CFG_NAME), {**cfg, "clean": on})
            yield event.plain_result(f"✅ 文本清洗：{'开' if on else '关'}")
            return

        yield event.plain_result("可用：/style、/style on|off、/style delay 0-10")
