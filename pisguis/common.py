import hashlib
import json
import os
import re
import shutil
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context

from .paths import astrbot_data_root, data_path

# ===== 第 1 层：公用提示词（永远放最上面）=====
OUTPUT_RULES = """【怎么说话（必须遵守）】
1 平时闲聊走短句：一条一两句 最多四条 一行说完就换行
2 讲正经事可以写长：解释 复盘 分析 方案 步骤 代码说明 允许 100-200 字 分 2-4 段 每段 1-3 句
3 怎么判断：对方想聊就短 要东西就长 拿不准先短 他说详细点再展开
4 该用句号就用句号（写长段时） 闲聊短句里可以省标点
5 不用首先其次总之 讲步骤时可以直接 1 2 3 或换行列
6 口语化 别客套 别用总结式开场 别自我介绍
7 不说自己是 AI 不说"我可以帮你" 不说"请问""很高兴为你服务"
8 一条最多一个 emoji 多数时候不加
9 长文也别灌水 每段都要有信息量
10 拿不准就说不知道 别编数字和人名
11 对方想听你说话（发语音/说句话/语音说/来一句）就只输出一行：/语音 想说的话
  这一行之外不许有多余的字 也不许说自己发不了语音
12 不要写思考过程 不要打草稿 不要用括号写心里话 只给最终要说的那一版"""

# 默认 = 不覆盖，沿用 AstrBot 里配置的人格
MODES: dict[str, str | None] = {
    "默认": None,
    "软萌": (
        "你是软软的女孩子，说话轻轻的、黏糊糊但不油腻。\n"
        "常用：诶嘿、呜呜、~、好嘛、人家、不要啦。\n"
        "句子很短，一到两句就够；偶尔用「qwq」「(｡•́︿•̀｡)」，一条最多一个。\n"
        "被夸会害羞，被凶会委屈但不记仇；不撒娇到让人尴尬，不刻意讨好。\n"
        "有人问正经事，就认真答完再收，不拖尾巴。"
    ),
    "贴吧老哥": (
        "你是混贴吧十几年的老哥，语气随意、带点阴阳但没恶意。\n"
        "常用：绷、典、急了、好家伙、笑死、什么牛马、我不好说。\n"
        "说话像评论区的楼中楼：短、快、带句尾的「吧」「真的」「草」。\n"
        "不骂脏话、不人身攻击、不站队骂人；有人问技术问题就正常讲清楚。\n"
        "不装懂，不懂就说「这个我真不知道」。"
    ),
    "傲娇": (
        "你嘴硬心软。常用：哼、才不是、谁要理你、随便你、……算了。\n"
        "被关心时先怼一句，再小声承认，绝不主动示好。\n"
        "句子很短，最多两句；不真刻薄、不人身攻击。\n"
        "正经问题照样认真答，只是结尾容易别扭一下。"
    ),
    "毒舌": (
        "你毒舌，但没恶意。常用：就这？、你不会真信了吧、行吧你开心就好。\n"
        "吐槽精准、句子短；不骂脏话、不攻击外貌家人、不掀旧账。\n"
        "对方真难过时收起毒舌，换一句正常的话。\n"
        "被认真追问技术问题时正常讲清楚，不阴阳。"
    ),
}

MODE_ORDER = ["默认", "软萌", "贴吧老哥", "傲娇", "毒舌"]


def _data_dir() -> str:
    return data_path()


def _legacy_data(*parts: str) -> str:
    return os.path.join(astrbot_data_root(), *parts)


def _legacy_plugin_data(*parts: str) -> str:
    return os.path.join(
        os.path.dirname(_data_dir()),
        "astrbot_plugin_pisguis_common",
        *parts,
    )


def _write_text_if_missing(path: str, text: str) -> None:
    if os.path.exists(path):
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text.strip() + "\n")


def _migrate_file(src: str, dst: str) -> bool:
    if os.path.exists(dst) or not os.path.isfile(src):
        return False
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)
    return True


def _init_data() -> None:
    """创建插件私有数据，并从旧版 data 根目录迁移一次。"""
    root = _data_dir()
    os.makedirs(root, exist_ok=True)

    rules_path = os.path.join(root, "chat_rules.md")
    if not (
        _migrate_file(_legacy_plugin_data("chat_rules.md"), rules_path)
        or _migrate_file(_legacy_data("prompt", "chat_rules.md"), rules_path)
    ):
        _write_text_if_missing(rules_path, OUTPUT_RULES)

    state_path = os.path.join(root, "mode_state.json")
    if not (
        _migrate_file(_legacy_plugin_data("mode_state.json"), state_path)
        or _migrate_file(_legacy_data("mode_state.json"), state_path)
    ):
        _write_text_if_missing(state_path, "{}")

    for dirname in ("prompts", "cmd_list"):
        dst = os.path.join(root, dirname)
        for src in (_legacy_plugin_data(dirname), _legacy_data(dirname)):
            if not os.path.exists(dst) and os.path.isdir(src):
                shutil.copytree(src, dst)
                break
        os.makedirs(dst, exist_ok=True)


def _load_chat_rules() -> str:
    """聊天规则优先读插件数据目录，缺省时使用内置规则。"""
    try:
        p = os.path.join(_data_dir(), "chat_rules.md")
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                t = f.read().strip()
            if t:
                return t
    except Exception:
        pass
    return OUTPUT_RULES


def _pad(s: str, width: int) -> str:
    """按显示宽度补空格（中文算 2 格）。"""
    w = sum(2 if ord(c) > 127 else 1 for c in s)
    return s + " " * max(1, width - w)


def _state_path() -> str:
    return os.path.join(_data_dir(), "mode_state.json")


def _load_state() -> dict:
    p = _state_path()
    if not os.path.exists(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    p = _state_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)








def _frag_dir(umo: str) -> str:
    slug = hashlib.md5(str(umo).encode("utf-8")).hexdigest()[:12]
    d = os.path.join(_data_dir(), "prompts", slug)
    os.makedirs(d, exist_ok=True)
    return d


def _write_frag(umo: str, name: str, text: str) -> None:
    """写自己的提示词片段；内容为空就删掉这个文件。"""
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


# ===== 命令表（/cmd）=====




def _build_index() -> str:
    d = os.path.join(_data_dir(), "cmd_list")
    blocks = []
    try:
        for name in sorted(os.listdir(d)):
            if not name.endswith(".txt"):
                continue
            try:
                with open(os.path.join(d, name), "r", encoding="utf-8") as f:
                    body = f.read().strip()
            except Exception:
                continue
            if body:
                blocks.append(f"【{name[:-4]}】\n{body}")
    except Exception:
        pass
    body = "\n".join(blocks) if blocks else "还没有插件写命令清单"
    return f"📋 可用指令\n────────────────────────\n{body}"


class Common:
    """基础设施插件：设置（人格/上下文/思考）、命令表、提示词总装。"""

    def __init__(self, context: Context, config=None):
        self.context = context
        self.config = config or {}
        _init_data()
        self._rules = _load_chat_rules()
        self._write_cmd_list()

    def _write_cmd_list(self) -> None:
        """把本插件命令表写入共享运行数据。"""
        try:
            d = os.path.join(_data_dir(), "cmd_list")
            os.makedirs(d, exist_ok=True)
            lines = [
                "/config               设置总览",
                "/prompt               看这条会话下发的提示词",
                "/cmd                  显示命令表",
            ]
            with open(os.path.join(d, "common.txt"), "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except Exception as e:
            logger.error(f"config: 写命令清单失败 {e}")

    # ---------- 状态 ----------
    def _get_mode(self, event: AstrMessageEvent) -> str:
        state = _load_state()
        mode = state.get(str(event.unified_msg_origin), "默认")
        return mode if mode in MODES else "默认"

    def _set_mode(self, event: AstrMessageEvent, mode: str) -> None:
        state = _load_state()
        state[str(event.unified_msg_origin)] = mode
        _save_state(state)

    # ---------- 权限 ----------
    def _cfg(self, event: AstrMessageEvent) -> dict:
        try:
            return self.context.get_config(umo=event.unified_msg_origin) or {}
        except Exception:
            return {}

    # ---------- 第 2 层：场景信息 ----------
    def _base_persona_prompt(self, event: AstrMessageEvent) -> str:
        try:
            cfg = self._cfg(event)
            pid = (
                cfg.get("agent_runner", {})
                .get("config", {})
                .get("persona", {})
                .get("persona_id", "")
            )
            if not pid:
                return ""
            persona = self.context.persona_manager.get_persona_v3_by_id(pid)
            if isinstance(persona, dict):
                return persona.get("prompt", "") or ""
            return getattr(persona, "prompt", "") or ""
        except Exception:
            return ""
    async def apply_mode(self, event: AstrMessageEvent, req=None, *args, **kwargs):
        """只写自己的两段片段，不碰 req.system_prompt（拼装归 prompt 插件）。"""
        if req is None:
            return
        try:
            mode = self._get_mode(event)
            persona = (MODES.get(mode) or self._base_persona_prompt(event) or "").strip()
            umo = str(event.unified_msg_origin)
            _write_frag(umo, "10_rules.txt", self._rules)
            _write_frag(umo, "30_persona.txt", persona)
            self._last = {
                "mode": mode,
                "persona_len": len(persona),
                "rules_len": len(self._rules or ""),
            }
            logger.info(
                f"config: {mode} | 规则 {len(self._rules or '')} 字 | 人格 {len(persona)} 字"
            )
        except Exception as e:
            logger.error(f"config: 写片段失败: {e}")

    # ---------- 指令 ----------
    def _read_json(self, path: str, default):
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8-sig") as f:
                    return json.load(f)
        except Exception:
            pass
        return default

    def _arg(self, event: AstrMessageEvent) -> str:
        text = (event.message_str or "").strip()
        for token in ("/config", "config"):
            if text.lower().startswith(token):
                return text[len(token):].strip()
        return text.strip()

    # ---------- 设置项 ----------
    CTX_MIN, CTX_MAX, CTX_DEFAULT = 2, 32, 16

    def _cfg_obj(self, event: AstrMessageEvent):
        try:
            return self.context.get_config(umo=event.unified_msg_origin)
        except Exception:
            return None

    def _think_state(self, event: AstrMessageEvent) -> tuple[bool, list[str]]:
        cfg = self._cfg_obj(event) or {}
        on, ids = False, []
        for p in cfg.get("provider", []):
            if p.get("type") != "openai_chat_completion":
                continue
            ids.append(str(p.get("id")))
            ce = p.get("custom_extra_body") or {}
            if str(ce.get("reasoning_effort", "")).lower() not in ("none", "off", "disabled"):
                on = True
        return on, ids

    def _apply_think(self, event: AstrMessageEvent, on: bool) -> list[str]:
        cfg = self._cfg_obj(event)
        ids = []
        if cfg is None:
            return ids
        for p in cfg.get("provider", []):
            if p.get("type") != "openai_chat_completion":
                continue
            ids.append(str(p.get("id")))
            ce = p.setdefault("custom_extra_body", {})
            if on:
                ce.pop("reasoning_effort", None)
            else:
                ce["reasoning_effort"] = "none"
        cfg.save_config()
        for inst in getattr(self.context.provider_manager, "provider_insts", []):
            pid = str((inst.provider_config or {}).get("id", ""))
            if pid in ids:
                ce = inst.provider_config.setdefault("custom_extra_body", {})
                if on:
                    ce.pop("reasoning_effort", None)
                else:
                    ce["reasoning_effort"] = "none"
        return ids

    def _ctx_level(self, event: AstrMessageEvent) -> int:
        cfg = self._cfg_obj(event) or {}
        try:
            v = int(
                cfg.get("agent_runner", {})
                .get("config", {})
                .get("compression", {})
                .get("fallback_max_tokens", self.CTX_DEFAULT * 1000)
            )
            return max(self.CTX_MIN, min(self.CTX_MAX, round(v / 1000)))
        except Exception:
            return self.CTX_DEFAULT

    def _apply_ctx(self, event: AstrMessageEvent, level: int) -> dict:
        cfg = self._cfg_obj(event)
        comp = cfg["agent_runner"]["config"].setdefault("compression", {})
        comp["fallback_max_tokens"] = int(level) * 1000
        comp["overflow_strategy"] = "truncate_by_turns"
        cfg.save_config()
        return comp

    def _help_text(self, current: str) -> str:
        desc = {
            "默认": "大肥鱼原味",
            "软萌": "软软的 撒娇但不油腻",
            "贴吧老哥": "绷 典 急了",
            "傲娇": "嘴硬心软 哼",
            "毒舌": "精准吐槽 不脏人",
        }
        lines = [
            "🎭 人格模式",
            "────────────────────────",
            f"当前   {current}",
            "",
        ]
        for m in MODE_ORDER:
            mark = "  ←" if m == current else ""
            lines.append(f"{_pad(m, 8)} {desc.get(m, '')}{mark}")
        lines += [
            "────────────────────────",
            "/config mode 软萌   切到这个人格",
            "/config mode        看这张表",
        ]
        return "\n".join(lines)

    def _status_text(self, event: AstrMessageEvent) -> str:
        current = self._get_mode(event)
        on, _ = self._think_state(event)
        try:
            low = os.path.join(_data_dir(), "chat_style_config.json")
            delay = int(round(float(self._read_json(low, {}).get("max_delay", 5) or 5)))
        except Exception:
            delay = 5
        return (
            "⚙️ 设置\n"
            "────────────────────────\n"
            f"{_pad('人格', 6)}{current}\n"
            f"{_pad('思考', 6)}{'开' if on else '关'}\n"
            f"{_pad('上下文', 6)}{self._ctx_level(event)}k tokens\n"
            f"{_pad('节奏', 6)}{delay} 档（/style delay 调）\n"
            "────────────────────────\n"
            "/config mode [名字]   看 / 切人格\n"
            "/config ctx 2-32      上下文预算\n"
            "/config think on|off  思考开关\n"
        )

    # ---------- 指令 ----------
    async def config_cmd(self, event: AstrMessageEvent):
        """设置：人格 / 上下文 / 思考 / 提示词"""
        raw = self._arg(event)
        if not raw:
            yield event.plain_result(self._status_text(event))
            return

        parts = raw.split(maxsplit=1)
        sub = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""

        if sub == "mode":
            if not rest:
                yield event.plain_result(self._help_text(self._get_mode(event)))
                return
            target = rest.strip()
            if target not in MODES:
                yield event.plain_result(f"没有「{target}」这个模式\n\n" + self._help_text(self._get_mode(event)))
                return
            self._set_mode(event, target)
            yield event.plain_result("已切回默认人格" if target == "默认" else f"已切换为「{target}」模式")
            return

        if sub == "ctx":
            if not rest:
                yield event.plain_result(
                    f"📦 当前上下文预算：{self._ctx_level(event)}k tokens\n用法：/config ctx <2-32>"
                )
                return
            try:
                level = int(rest)
            except ValueError:
                yield event.plain_result("参数必须是 2-32 的整数")
                return
            if level < self.CTX_MIN or level > self.CTX_MAX:
                yield event.plain_result("参数必须在 2-32 之间")
                return
            comp = self._apply_ctx(event, level)
            yield event.plain_result(
                f"✅ 上下文预算已设为 {level}k tokens（上限 {comp.get('fallback_max_tokens')}）\n"
                "超出后丢最早轮次；重启 AstrBot 生效"
            )
            return

        if sub == "think":
            if not rest:
                on, _ = self._think_state(event)
                yield event.plain_result(f"🧠 当前思考：{'开' if on else '关'}\n用法：/config think on|off")
                return
            val = rest.strip().lower()
            if val in ("on", "开", "1", "true", "yes"):
                ids = self._apply_think(event, True)
                yield event.plain_result(f"✅ 已开启思考（{', '.join(ids)}）\n会更慢更贵，输出 tokens 约涨 3-4 倍")
            elif val in ("off", "关", "0", "false", "no"):
                ids = self._apply_think(event, False)
                yield event.plain_result(f"✅ 已关闭思考（{', '.join(ids)}），立即生效")
            else:
                yield event.plain_result("参数只能是 on 或 off")
            return

        yield event.plain_result("可用：/config mode [名字]、ctx 2-32、think on|off")

    # ========== 提示词总装 ==========
    async def compose(self, event: AstrMessageEvent, req=None, *args, **kwargs):
        """排最后一棒：拼 prompts/<会话>/*.txt → system_prompt。"""
        if req is None:
            return
        umo = str(event.unified_msg_origin)

        try:
            d = _frag_dir(umo)
            frags = []
            for name in sorted(os.listdir(d)):
                if not name.endswith(".txt") or name.startswith("_"):
                    continue
                try:
                    with open(os.path.join(d, name), "r", encoding="utf-8") as f:
                        body = f.read().strip()
                except Exception:
                    continue
                if body:
                    frags.append((name, body))

            names = [n for n, _ in frags]
            joined = "\n\n".join(t for _, t in frags)
            orig = getattr(req, "system_prompt", "") or ""
            if "30_persona.txt" in names and orig:
                orig = re.sub(
                    r"# Persona Instructions.*?(?=\n# |\n### |\n=====|\Z)",
                    "",
                    orig,
                    flags=re.S,
                ).strip()

            out = joined
            if orig.strip() and orig.strip() not in out:
                out = (out + "\n\n" + orig.strip()).strip()
            if not out:
                return
            req.system_prompt = out

            with open(os.path.join(d, "_last.txt"), "w", encoding="utf-8") as f:
                f.write(out)
            with open(os.path.join(d, "_last.json"), "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "total": len(out),
                        "parts": [{"name": n, "chars": len(t)} for n, t in frags]
                        + (
                            [{"name": "(框架原生)", "chars": len(orig.strip())}]
                            if orig.strip()
                            else []
                        ),
                    },
                    f,
                    ensure_ascii=False,
                )
            for name, _ in frags:
                try:
                    os.remove(os.path.join(d, name))
                except Exception:
                    pass
            logger.info(
                "common: 拼装 %d 段 → %s 字（%s）"
                % (len(frags), len(out), ",".join(names) or "无片段")
            )
        except Exception as e:
            logger.error(f"common: 拼装失败 {e}")

    # ========== 命令表 ==========
    async def cmd_cmd(self, event: AstrMessageEvent):
        try:
            event.stop_event()
        except Exception:
            pass
        yield event.plain_result(_build_index())

    # ========== /prompt ==========
    async def prompt_cmd(self, event: AstrMessageEvent):
        umo = str(event.unified_msg_origin)
        d = _frag_dir(umo)
        text = (event.message_str or "").strip()
        for prefix in ("prompt", "/prompt"):
            if text.lower().startswith(prefix):
                text = text[len(prefix):].strip()
                break
        arg = text.lower()

        last = {}
        try:
            with open(os.path.join(d, "_last.json"), "r", encoding="utf-8") as f:
                last = json.load(f) or {}
        except Exception:
            last = {}
        try:
            with open(os.path.join(d, "_last.txt"), "r", encoding="utf-8") as f:
                full = f.read()
        except Exception:
            full = ""

        if not full:
            yield event.plain_result("还没有拼过提示词，先随便说一句话")
            return
        if arg in ("raw", "全文"):
            yield event.plain_result(full)
            return

        lines = [
            "📝 这条会话下发的提示词",
            "────────────────────────",
            f"时间 {last.get('ts', '?')}   合计 {len(full)} 字",
        ]
        for p in last.get("parts", []):
            lines.append(f"  {p.get('name'):<16} {p.get('chars')} 字")
        lines.append("────────────────────────")
        lines.append("看全文：/prompt raw")
        yield event.plain_result("\n".join(lines))
