# -*- coding: utf-8 -*-
"""AstrBot 翻译助手插件：中英及常见语种互译

功能：
- `/翻译 <文本>` 自动检测语言并翻译为中文（文本为中文则翻译成英文）
- `/翻译 英>中 <文本>` 指定翻译方向（支持语种代码/中英文名/中文简称）
- `/翻译 检测 <文本>` 检测文本语种
- `/翻译 统计` 查看翻译次数与缓存命中统计
- `/翻译 帮助` 查看语种支持列表与用法
- 长文自动分段翻译（可关），译文按序拼接
- 翻译结果内存缓存（相同文本+方向直接复用），统计持久化

翻译接口抽象为 `_translate(text, src_lang, dst_lang)`，可替换：
默认走 Google translate.googleapis.com 免费接口（无需 API key），
失败返回 None 并由上层降级提示"翻译服务暂时不可用"。
"""

import asyncio
import json
import os
import re
import urllib.parse
import urllib.request

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register

# 插件元数据
PLUGIN_NAME = "astrbot_plugin_translator"
PLUGIN_AUTHOR = "云晓"
PLUGIN_DESC = "翻译助手：中英及常见语种互译（语种检测/长文分段/缓存/统计）"
PLUGIN_VERSION = "1.1.1"

# 支持的语种：标准代码 -> 中文名称
LANGUAGES = {
    "zh": "中文",
    "en": "英语",
    "ja": "日语",
    "ko": "韩语",
    "fr": "法语",
    "de": "德语",
    "ru": "俄语",
    "es": "西班牙语",
    "pt": "葡萄牙语",
    "it": "意大利语",
    "ar": "阿拉伯语",
    "th": "泰语",
    "vi": "越南语",
    "id": "印尼语",
    "tr": "土耳其语",
    "hi": "印地语",
    "nl": "荷兰语",
    "pl": "波兰语",
}

# 语种别名 -> 标准代码（中文全称/简称/英文名/历史代码等）
LANG_ALIASES = {
    "中文": "zh", "汉语": "zh", "中": "zh", "chinese": "zh", "cn": "zh",
    "英语": "en", "英文": "en", "英": "en", "english": "en", "eng": "en",
    "日语": "ja", "日文": "ja", "日": "ja", "japanese": "ja", "jp": "ja",
    "韩语": "ko", "韩文": "ko", "韩": "ko", "korean": "ko", "kr": "ko",
    "法语": "fr", "法文": "fr", "法": "fr", "french": "fr",
    "德语": "de", "德文": "de", "德": "de", "german": "de",
    "俄语": "ru", "俄文": "ru", "俄": "ru", "russian": "ru",
    "西班牙语": "es", "西语": "es", "西": "es", "spanish": "es",
    "葡萄牙语": "pt", "葡语": "pt", "葡": "pt", "portuguese": "pt",
    "意大利语": "it", "意语": "it", "意": "it", "italian": "it",
    "阿拉伯语": "ar", "阿语": "ar", "阿": "ar", "arabic": "ar",
    "泰语": "th", "泰": "th", "thai": "th",
    "越南语": "vi", "越语": "vi", "越": "vi", "vietnamese": "vi",
    "印尼语": "id", "印尼": "id", "indonesian": "id",
    "土耳其语": "tr", "土语": "tr", "土": "tr", "turkish": "tr",
    "印地语": "hi", "印地": "hi", "hindi": "hi",
    "荷兰语": "nl", "荷": "nl", "dutch": "nl",
    "波兰语": "pl", "波": "pl", "polish": "pl",
}

# 指令参数解析：兼容带/不带斜杠（AstrBot 已剥离唤醒前缀，指令名可能残留 / 前缀）
COMMAND_RE = re.compile(r"^[\\/／]?\s*翻译\s*(.*)$", re.S)
# 显式方向解析：`英>中 文本` / `en>zh 文本`（支持全角 ＞ 与 →）
DIRECTION_RE = re.compile(
    r"^([\u4e00-\u9fffA-Za-z\-]{1,16})\s*[>＞→]\s*([\u4e00-\u9fffA-Za-z\-]{1,16})\s*(.*)$",
    re.S,
)
DEFAULT_MAX_CHARS = 2000
DEFAULT_SEGMENTS = 5  # 长文分段翻译的最大段数
DEFAULT_CACHE_SIZE = 500  # 翻译缓存最大条目数

HELP_TEXT = """🌐 翻译助手使用帮助
━━━━━━━━━━━━━━━━━━━━━━━
用法：
  /翻译 <文本>             自动翻译（中文→英文，其他→中文）
  /翻译 英>中 <文本>       指定翻译方向（代码/中文全称/简称均可）
  /翻译 检测 <文本>        检测文本语种
  /翻译 统计               查看翻译次数与缓存命中统计
  /翻译 帮助               查看本帮助

支持语种（代码 / 中文名 / 简称，如 英 英语 en）：
  zh 中文 | en 英语 | ja 日语 | ko 韩语 | fr 法语
  de 德语 | ru 俄语 | es 西班牙语 | pt 葡萄牙语 | it 意大利语
  ar 阿拉伯语 | th 泰语 | vi 越南语 | id 印尼语 | tr 土耳其语
  hi 印地语 | nl 荷兰语 | pl 波兰语

方向写法示例：
  /翻译 en>zh hello world
  /翻译 英>中 hello world
  /翻译 英语>中文 hello world

提示：长文本自动分段翻译后拼接；相同内容重复翻译走缓存；翻译服务异常时会提示稍后再试。"""


@register(PLUGIN_NAME, PLUGIN_AUTHOR, PLUGIN_DESC, PLUGIN_VERSION)
class TranslatorPlugin(Star):
    """翻译助手：中英及常见语种互译"""

    def __init__(self, context: Context, config: AstrBotConfig = None, data_dir: str = None):
        super().__init__(context)
        self.config = config or {}
        # 数据目录：默认 plugin_data/<插件名>，测试可注入临时目录
        self.data_dir = data_dir or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "plugin_data",
            PLUGIN_NAME,
        )
        os.makedirs(self.data_dir, exist_ok=True)

        # 翻译缓存：(src, dst, text) -> 译文（插入序，超限淘汰最旧）
        self._cache: dict[tuple, str] = {}
        # 统计：总次数 / 按目标语种 / 缓存命中
        self._stats: dict = {"total": 0, "by_dst": {}, "cache_hits": 0}
        self._load_stats()
        logger.info(f"【{PLUGIN_NAME}】翻译助手插件初始化完成")

    # ========== 统计持久化 ==========

    def _stats_file(self) -> str:
        return os.path.join(self.data_dir, "translator_stats.json")

    def _load_stats(self):
        """从磁盘加载统计；损坏/结构异常时重置，不影响运行"""
        try:
            path = self._stats_file()
            if not os.path.exists(path):
                return
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                return
            total = raw.get("total", 0)
            by_dst = raw.get("by_dst", {})
            hits = raw.get("cache_hits", 0)
            if isinstance(total, (int, float)) and total >= 0:
                self._stats["total"] = int(total)
            if isinstance(by_dst, dict):
                self._stats["by_dst"] = {
                    str(k): int(v) for k, v in by_dst.items()
                    if isinstance(v, (int, float)) and v >= 0
                }
            if isinstance(hits, (int, float)) and hits >= 0:
                self._stats["cache_hits"] = int(hits)
        except (json.JSONDecodeError, OSError, TypeError, ValueError) as e:
            logger.warning("【%s】统计数据损坏（%s），已重置", PLUGIN_NAME, e)
            self._stats = {"total": 0, "by_dst": {}, "cache_hits": 0}

    def _save_stats(self):
        """保存统计到磁盘；失败仅告警不影响运行"""
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            tmp = self._stats_file() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._stats, f, ensure_ascii=False)
            os.replace(tmp, self._stats_file())
        except OSError as e:
            logger.warning("【%s】保存统计数据失败: %s", PLUGIN_NAME, e)

    def _record_translation(self, dst_lang: str):
        """记录一次成功翻译（统计开关开启时）并持久化"""
        if not self._cfg_bool("trans_stats_enabled", True):
            return
        self._stats["total"] += 1
        by = self._stats["by_dst"]
        by[dst_lang] = by.get(dst_lang, 0) + 1
        self._save_stats()

    def _record_cache_hit(self):
        """记录一次缓存命中（统计开关开启时）并持久化"""
        if not self._cfg_bool("trans_stats_enabled", True):
            return
        self._stats["cache_hits"] += 1
        self._save_stats()

    # ========== 指令处理 ==========

    @staticmethod
    def _parse_command(message_str: str) -> str:
        """从事件消息中提取指令参数，兼容带/不带斜杠；不匹配时按整条消息处理"""
        text = (message_str or "").strip()
        m = COMMAND_RE.match(text)
        if m:
            return m.group(1).strip()
        return text

    @staticmethod
    def _normalize_lang(token) -> str | None:
        """把语种代码/中英文名/简称规范化为标准代码；无法识别返回 None"""
        if token is None:
            return None
        t = str(token).strip().lower()
        if t in LANGUAGES:
            return t
        return LANG_ALIASES.get(t)

    def _parse_direction(self, args: str) -> tuple[str | None, str | None, str]:
        """解析显式方向 `英>中 文本`，返回 (src, dst, 剩余文本)。

        非方向格式返回 (None, None, 原文)；方向分隔符存在但语种无法识别时返回 (None, None, 剩余文本)。
        """
        m = DIRECTION_RE.match(args)
        if not m:
            return None, None, args
        src = self._normalize_lang(m.group(1))
        dst = self._normalize_lang(m.group(2))
        rest = m.group(3).strip()
        if src is None or dst is None:
            return None, None, rest
        return src, dst, rest

    @staticmethod
    def _looks_like_direction(args: str) -> bool:
        """是否命中方向结构（两侧各 ≤16 字符 + >／＞／→ 分隔符）。

        仅靠分隔符判断会把「我用 > 符号」这类普通文本误报为非法方向，
        因此要求整体结构与方向写法一致。
        """
        return bool(DIRECTION_RE.match(args))

    @staticmethod
    def _detect_lang(text: str) -> str:
        """简单启发式语言检测：按字符集比例判断中/日/韩，其余视为英文"""
        if not text:
            return "zh"
        letters = [c for c in text if c.isalpha()]
        if not letters:
            return "zh"
        n = len(letters)
        han = sum(1 for c in letters if "\u4e00" <= c <= "\u9fff")
        kana = sum(1 for c in letters if "\u3040" <= c <= "\u30ff")
        hangul = sum(1 for c in letters if "\uac00" <= c <= "\ud7af")
        if han / n > 0.5:
            return "zh"
        if kana / n > 0.3:
            return "ja"
        if hangul / n > 0.3:
            return "ko"
        return "en"

    def _default_target(self) -> str | None:
        """读取 default_target 配置并规范化为代码；非法值（非字符串/无法识别）返回 None 表示自动"""
        try:
            v = self.config.get("default_target", "") or ""
        except Exception:  # 配置容器异常时安全回退
            return None
        if not isinstance(v, str):
            return None
        return self._normalize_lang(v)

    def _max_chars(self) -> int:
        """读取 trans_max_chars 配置；脏值（非数字/非正数/异常）回退默认 2000"""
        try:
            v = self.config.get("trans_max_chars", DEFAULT_MAX_CHARS)
        except Exception:
            return DEFAULT_MAX_CHARS
        try:
            v = int(v)
        except (TypeError, ValueError):
            return DEFAULT_MAX_CHARS
        if v <= 0:
            return DEFAULT_MAX_CHARS
        return v

    # ========== 配置读取（脏值防御） ==========

    def _cfg_bool(self, key: str, default: bool) -> bool:
        """读取布尔配置，非 bool 值按字符串解析，无法解析时回退默认值"""
        v = self.config.get(key, default)
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.strip().lower() in ("1", "true", "yes", "on")
        return default

    def _cfg_int(self, key: str, default: int) -> int:
        """读取整数配置，脏值回退默认值"""
        try:
            return int(self.config.get(key, default))
        except (TypeError, ValueError):
            return default

    def _cache_enabled(self) -> bool:
        return self._cfg_bool("trans_cache_enabled", True)

    def _cache_limit(self) -> int:
        """缓存上限（1~10000），脏值回退默认 500"""
        return min(10000, max(1, self._cfg_int("trans_cache_size", DEFAULT_CACHE_SIZE)))

    def _segment_enabled(self) -> bool:
        return self._cfg_bool("trans_segment_enabled", True)

    def _max_segments(self) -> int:
        """长文分段最大段数（1~20），脏值回退默认 5"""
        return min(20, max(1, self._cfg_int("trans_max_segments", DEFAULT_SEGMENTS)))

    # ========== 缓存 ==========

    def _cache_get(self, src: str, dst: str, text: str) -> str | None:
        if not self._cache_enabled():
            return None
        return self._cache.get((src, dst, text))

    def _cache_put(self, src: str, dst: str, text: str, result: str):
        if not self._cache_enabled():
            return
        limit = self._cache_limit()
        while len(self._cache) >= limit:
            # 淘汰最早插入的条目
            first = next(iter(self._cache))
            self._cache.pop(first, None)
        self._cache[(src, dst, text)] = result

    # ========== 长文分段 ==========

    @staticmethod
    def _split_text(text: str, size: int) -> list[str]:
        """把文本按 size 切分为若干段；优先在换行处断开，避免拆散句子"""
        if len(text) <= size:
            return [text]
        segments = []
        remaining = text
        while remaining:
            if len(remaining) <= size:
                segments.append(remaining)
                break
            window = remaining[: size + 1]
            # 在窗口内找最后一个换行或句末标点作为切点
            cut = -1
            for sep in ("\n", "。", "！", "？", ".", "!", "?", "；", ";"):
                pos = window.rfind(sep)
                if pos > 0:
                    cut = max(cut, pos + len(sep))
            if cut <= 0:
                cut = size
            segments.append(remaining[:cut])
            remaining = remaining[cut:]
        return [s for s in segments if s]

    def _translate_sync(self, text: str, src_lang: str, dst_lang: str) -> str | None:
        """同步调用翻译接口（可被替换/测试），返回译文；失败返回 None（由上层降级提示）。

        默认实现：Google translate.googleapis.com 免费接口（无需 API key），
        仅使用标准库 urllib.request，国内可直连访问。
        """
        try:
            url = "https://translate.googleapis.com/translate_a/single"
            params = {
                "client": "gtx",
                "sl": src_lang,
                "tl": dst_lang,
                "dt": "t",
                "q": text,
            }
            req = urllib.request.Request(
                url + "?" + urllib.parse.urlencode(params),
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            segments = data[0] if data else []
            result = "".join(seg[0] for seg in segments if seg and seg[0])
            return result or None
        except Exception as e:  # 网络异常/解析失败等一律降级
            logger.warning(f"【{PLUGIN_NAME}】翻译请求失败: {e}")
            return None

    async def _translate(self, text: str, src_lang: str, dst_lang: str) -> str | None:
        """异步包装：网络请求在线程中执行，避免阻塞 AstrBot 事件循环"""
        return await asyncio.to_thread(self._translate_sync, text, src_lang, dst_lang)

    def _invalid_direction_msg(self) -> str:
        """构造非法方向提示（含支持语种列表）"""
        codes = " | ".join(f"{code}({name})" for code, name in LANGUAGES.items())
        return (
            "❌ 无法识别的翻译方向\n"
            "用法示例：/翻译 英>中 hello world 或 /翻译 en>zh hello\n"
            f"支持语种：{codes}"
        )

    def _send_text(self, event: AstrMessageEvent, text: str):
        """构造纯文本回复结果"""
        return event.chain_result([Plain(text)])

    async def _do_translate(self, event: AstrMessageEvent, args: str):
        """执行翻译：解析方向 → 检测语言 → 缓存/分段翻译 → 记录统计"""
        src, dst, rest = self._parse_direction(args)
        if src is None and self._looks_like_direction(args):
            # 存在方向分隔符但语种无法识别，给出明确提示
            return self._send_text(event, self._invalid_direction_msg())
        if not rest:
            return self._send_text(event, HELP_TEXT)

        # 确定源/目标语言
        if src is None:
            src = self._detect_lang(rest)
        if dst is None:
            dst = self._default_target() or ("zh" if src != "zh" else "en")

        max_chars = self._max_chars()
        truncated = False
        segments = [rest]
        if len(rest) > max_chars:
            if self._segment_enabled():
                segments = self._split_text(rest, max_chars)[: self._max_segments()]
                if "".join(segments) != rest:
                    truncated = True  # 段数上限导致尾部被截断
            else:
                segments = [rest[:max_chars]]
                truncated = True

        # 逐段翻译：优先命中缓存
        translated_parts: list[str] = []
        cache_hit_count = 0
        for seg in segments:
            cached = self._cache_get(src, dst, seg)
            if cached is not None:
                translated_parts.append(cached)
                cache_hit_count += 1
                continue
            # _translate 为可替换接口，外部实现可能抛异常，统一兜底降级
            try:
                result = await self._translate(seg, src, dst)
            except Exception as e:
                logger.warning(f"【{PLUGIN_NAME}】翻译接口异常: {e}")
                result = None
            if not result:
                return self._send_text(event, "❌ 翻译服务暂时不可用，请稍后再试")
            self._cache_put(src, dst, seg, result)
            translated_parts.append(result)

        final_text = "".join(translated_parts) if dst == "zh" else " ".join(translated_parts)
        if cache_hit_count:
            self._record_cache_hit()
        # 整体命中全部缓存时不重复计总次数？——仍计入总次数（一次翻译请求）
        self._record_translation(dst)

        src_name = LANGUAGES.get(src, src)
        dst_name = LANGUAGES.get(dst, dst)
        lines = [f"🌐 翻译（{src_name} → {dst_name}）", "━━━━━━━━━━━━━━━━━━━━━"]
        if len(segments) > 1:
            lines.append(f"🧩 长文已分段翻译：共 {len(segments)} 段")
        if truncated:
            if self._segment_enabled():
                lines.append("⚠️ 文本过长，超出分段上限，已截取部分内容翻译")
            else:
                lines.append(f"⚠️ 文本过长，已截取前 {max_chars} 字翻译")
        preview = rest if len(rest) <= 120 else rest[:120] + "…"
        lines.append(f"📄 原文：{preview}")
        lines.append(f"💬 译文：{final_text}")
        return self._send_text(event, "\n".join(lines))

    def _cmd_detect(self, event: AstrMessageEvent, args: str):
        """/翻译 检测 <文本>：显示检测出的语种"""
        text = args.strip()
        if not text:
            return self._send_text(event, "❌ 用法: /翻译 检测 <文本>")
        code = self._detect_lang(text)
        name = LANGUAGES.get(code, code)
        preview = text if len(text) <= 60 else text[:60] + "…"
        return self._send_text(
            event,
            f"🔍 语种检测结果\n━━━━━━━━━━━━━━━━━━━━━\n📄 文本：{preview}\n💬 语种：{name} ({code})",
        )

    def _cmd_stats(self, event: AstrMessageEvent):
        """/翻译 统计：查看翻译次数、语种分布与缓存命中"""
        if not self._cfg_bool("trans_stats_enabled", True):
            return self._send_text(event, "📊 统计功能未开启（trans_stats_enabled = false）")
        total = int(self._stats.get("total", 0))
        hits = int(self._stats.get("cache_hits", 0))
        by_dst = dict(self._stats.get("by_dst") or {})
        lines = ["📊 翻译统计", "━━━━━━━━━━━━━━━━━━━━━"]
        if total <= 0 and hits <= 0:
            lines.append("暂无翻译记录")
            return self._send_text(event, "\n".join(lines))
        rate = (hits / total * 100) if total > 0 else 0.0
        lines.append(f"🔢 累计翻译：{total} 次")
        lines.append(f"⚡ 缓存命中：{hits} 次（{rate:.0f}%）")
        if by_dst:
            lines.append("🗣️ 目标语种分布：")
            ranked = sorted(by_dst.items(), key=lambda kv: -kv[1])
            medals = ["🥇", "🥈", "🥉"]
            for i, (code, cnt) in enumerate(ranked[:5]):
                name = LANGUAGES.get(code, code)
                icon = medals[i] if i < 3 else f"{i + 1}."
                lines.append(f"  {icon} {name}({code})：{cnt} 次")
        return self._send_text(event, "\n".join(lines))

    @filter.command("翻译", priority=200)
    async def translate_cmd(self, event: AstrMessageEvent):
        """翻译助手主入口"""
        args = self._parse_command(event.message_str)
        if not args:
            return self._send_text(event, HELP_TEXT)
        if args in ("帮助", "help", "Help", "HELP"):
            return self._send_text(event, HELP_TEXT)
        # 子命令：检测 / 统计
        if args.startswith("检测"):
            return self._cmd_detect(event, args[2:])
        if args.startswith("统计"):
            return self._cmd_stats(event)
        return await self._do_translate(event, args)

    async def terminate(self):
        """插件卸载时释放内存缓存并按配置保存统计"""
        try:
            if hasattr(self, "_cache"):
                self._cache.clear()
            if self._cfg_bool("trans_stats_enabled", True) and self._stats.get("total", 0) > 0:
                self._save_stats()
        except Exception:
            pass
