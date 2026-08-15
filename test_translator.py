# -*- coding: utf-8 -*-
"""翻译助手插件单元测试：自动方向、显式方向解析、非法方向、失败降级、超长截断、参数解析、配置脏值防御

测试通过 mock `_translate` 返回固定文本进行，全程不联网。
"""

import asyncio
import sys
import unittest

sys.path.insert(0, r"D:\astrbot\data\plugins")

from astrbot_plugin_translator.main import (  # noqa: E402
    HELP_TEXT,
    LANGUAGES,
    TranslatorPlugin,
)


class FakeEvent:
    """最小事件替身：仅提供 message_str 与 chain_result"""

    def __init__(self, message_str=""):
        self.message_str = message_str
        self.sent = []

    def chain_result(self, chain):
        return chain

    async def send(self, chain):
        self.sent.append(chain)
        return None


def make_plugin(config=None):
    """构造插件实例（context 传 None 即可，本插件不依赖 context）"""
    return TranslatorPlugin(None, config or {})


def run(coro):
    """同步运行协程"""
    return asyncio.run(coro)


def reply_text(result):
    """从回复链中提取纯文本（result 为 [Plain(...)] 列表）"""
    return "".join(getattr(c, "text", "") for c in result)


def mock_translate(plugin, calls, reply="【译文】"):
    """把 _translate 替换为记录调用参数的假实现，返回固定文本（async，与插件签名一致）"""
    async def fake_translate(text, src, dst):
        calls.append((text, src, dst))
        return reply + text
    plugin._translate = fake_translate


class TestNormalizeLang(unittest.TestCase):
    """语种规范化：代码/中文全称/简称/英文名"""

    def test_codes(self):
        p = make_plugin()
        self.assertEqual(p._normalize_lang("en"), "en")
        self.assertEqual(p._normalize_lang("zh"), "zh")
        self.assertEqual(p._normalize_lang("ja"), "ja")

    def test_chinese_full_and_short(self):
        p = make_plugin()
        self.assertEqual(p._normalize_lang("英语"), "en")
        self.assertEqual(p._normalize_lang("英"), "en")
        self.assertEqual(p._normalize_lang("中文"), "zh")
        self.assertEqual(p._normalize_lang("中"), "zh")

    def test_english_name_and_whitespace(self):
        p = make_plugin()
        self.assertEqual(p._normalize_lang(" English "), "en")
        self.assertEqual(p._normalize_lang("chinese"), "zh")

    def test_unknown(self):
        p = make_plugin()
        self.assertIsNone(p._normalize_lang("xx"))
        self.assertIsNone(p._normalize_lang(""))


class TestParseDirection(unittest.TestCase):
    """显式方向解析的多种写法"""

    def test_code_arrow(self):
        p = make_plugin()
        self.assertEqual(p._parse_direction("en>zh hello world"), ("en", "zh", "hello world"))

    def test_short_chinese_arrow(self):
        p = make_plugin()
        self.assertEqual(p._parse_direction("英>中 hello"), ("en", "zh", "hello"))

    def test_full_chinese_arrow(self):
        p = make_plugin()
        self.assertEqual(p._parse_direction("英语>中文 hello"), ("en", "zh", "hello"))

    def test_fullwidth_arrow(self):
        p = make_plugin()
        self.assertEqual(p._parse_direction("en＞zh hello"), ("en", "zh", "hello"))

    def test_arrow_symbol(self):
        p = make_plugin()
        self.assertEqual(p._parse_direction("英→中 hello"), ("en", "zh", "hello"))

    def test_no_direction_keeps_full_text(self):
        p = make_plugin()
        self.assertEqual(p._parse_direction("hello world"), (None, None, "hello world"))

    def test_other_language_pair(self):
        p = make_plugin()
        self.assertEqual(p._parse_direction("zh>ja 你好"), ("zh", "ja", "你好"))

    def test_invalid_lang_returns_none(self):
        p = make_plugin()
        src, dst, rest = p._parse_direction("xx>yy hello")
        self.assertIsNone(src)
        self.assertIsNone(dst)
        self.assertEqual(rest, "hello")

    def test_one_side_invalid(self):
        p = make_plugin()
        src, dst, _ = p._parse_direction("英>xx hello")
        self.assertIsNone(src)
        self.assertIsNone(dst)


class TestDetectLang(unittest.TestCase):
    """语言自动检测"""

    def test_english(self):
        self.assertEqual(make_plugin()._detect_lang("hello world"), "en")

    def test_chinese(self):
        self.assertEqual(make_plugin()._detect_lang("你好世界"), "zh")

    def test_japanese(self):
        self.assertEqual(make_plugin()._detect_lang("こんにちは"), "ja")

    def test_korean(self):
        self.assertEqual(make_plugin()._detect_lang("안녕하세요"), "ko")

    def test_mixed_mostly_chinese(self):
        self.assertEqual(make_plugin()._detect_lang("今天是星期三，天气很好 hello"), "zh")

    def test_empty(self):
        self.assertEqual(make_plugin()._detect_lang(""), "zh")


class TestAutoDirection(unittest.TestCase):
    """自动方向选择：英文→中文，中文→英文"""

    def _translate_auto(self, args):
        calls = []
        p = make_plugin()
        mock_translate(p, calls)
        result = run(p.translate_cmd(FakeEvent(args)))
        return calls, reply_text(result)

    def test_english_to_chinese(self):
        calls, text = self._translate_auto("/翻译 hello world")
        self.assertEqual(calls[0][1], "en")
        self.assertEqual(calls[0][2], "zh")
        self.assertIn("英语 → 中文", text)
        self.assertIn("【译文】hello world", text)

    def test_chinese_to_english(self):
        calls, text = self._translate_auto("/翻译 你好世界")
        self.assertEqual(calls[0][1], "zh")
        self.assertEqual(calls[0][2], "en")
        self.assertIn("中文 → 英语", text)

    def test_japanese_to_chinese(self):
        calls, _ = self._translate_auto("/翻译 こんにちは")
        self.assertEqual(calls[0][1], "ja")
        self.assertEqual(calls[0][2], "zh")


class TestExplicitDirection(unittest.TestCase):
    """端到端显式方向翻译"""

    def _translate_explicit(self, args):
        calls = []
        p = make_plugin()
        mock_translate(p, calls, reply="你好")
        result = run(p.translate_cmd(FakeEvent(args)))
        return calls, reply_text(result)

    def test_explicit_short_chinese(self):
        calls, text = self._translate_explicit("/翻译 英>中 hello world")
        self.assertEqual(calls[0], ("hello world", "en", "zh"))
        self.assertIn("英语 → 中文", text)

    def test_explicit_code(self):
        calls, _ = self._translate_explicit("/翻译 en>zh hello")
        self.assertEqual(calls[0], ("hello", "en", "zh"))

    def test_explicit_full_name(self):
        calls, _ = self._translate_explicit("/翻译 英语>中文 hello")
        self.assertEqual(calls[0], ("hello", "en", "zh"))

    def test_explicit_zh_to_ja(self):
        calls, text = self._translate_explicit("/翻译 中文>日语 你好")
        self.assertEqual(calls[0], ("你好", "zh", "ja"))
        self.assertIn("中文 → 日语", text)


class TestInvalidDirection(unittest.TestCase):
    """非法方向提示"""

    def test_invalid_direction_hint(self):
        p = make_plugin()
        result = run(p.translate_cmd(FakeEvent("/翻译 xx>yy hello")))
        text = reply_text(result)
        self.assertIn("无法识别", text)
        self.assertIn("en", text)
        self.assertIn("英语", text)

    def test_one_side_invalid_hint(self):
        p = make_plugin()
        result = run(p.translate_cmd(FakeEvent("/翻译 英>xx hello")))
        self.assertIn("无法识别", reply_text(result))


class TestFailureFallback(unittest.TestCase):
    """翻译接口失败降级"""

    def test_translate_none_fallback(self):
        p = make_plugin()
        async def fake(text, src, dst):
            return None
        p._translate = fake
        result = run(p.translate_cmd(FakeEvent("/翻译 hello world")))
        self.assertIn("翻译服务暂时不可用", reply_text(result))

    def test_translate_exception_fallback(self):
        p = make_plugin()
        async def boom(text, src, dst):
            raise RuntimeError("network down")
        p._translate = boom
        result = run(p.translate_cmd(FakeEvent("/翻译 hello")))
        self.assertIn("翻译服务暂时不可用", reply_text(result))

    def test_translate_empty_string_fallback(self):
        p = make_plugin()
        async def fake(text, src, dst):
            return ""
        p._translate = fake
        result = run(p.translate_cmd(FakeEvent("/翻译 hello")))
        self.assertIn("翻译服务暂时不可用", reply_text(result))


class TestMaxChars(unittest.TestCase):
    """超长文本截断"""

    def test_too_long_truncated(self):
        calls = []
        p = make_plugin({"trans_max_chars": 10})
        mock_translate(p, calls)
        long_text = "a" * 30
        result = run(p.translate_cmd(FakeEvent(f"/翻译 {long_text}")))
        text = reply_text(result)
        self.assertEqual(len(calls[0][0]), 10)
        self.assertIn("截取前 10 字", text)

    def test_within_limit_not_truncated(self):
        calls = []
        p = make_plugin({"trans_max_chars": 100})
        mock_translate(p, calls)
        run(p.translate_cmd(FakeEvent("/翻译 hello world")))
        self.assertEqual(calls[0][0], "hello world")
        self.assertNotIn("截取", reply_text(run(p.translate_cmd(FakeEvent("/翻译 hello")))))

    def test_explicit_direction_respects_limit(self):
        calls = []
        p = make_plugin({"trans_max_chars": 5})
        mock_translate(p, calls)
        run(p.translate_cmd(FakeEvent("/翻译 英>中 abcdefghij")))
        self.assertEqual(calls[0][0], "abcde")


class TestSlashParsing(unittest.TestCase):
    """参数解析兼容带/不带斜杠"""

    def test_with_slash(self):
        self.assertEqual(TranslatorPlugin._parse_command("/翻译 hello world"), "hello world")

    def test_without_slash(self):
        self.assertEqual(TranslatorPlugin._parse_command("翻译 hello world"), "hello world")

    def test_fullwidth_slash(self):
        self.assertEqual(TranslatorPlugin._parse_command("／翻译 hello"), "hello")

    def test_backslash(self):
        self.assertEqual(TranslatorPlugin._parse_command("\\翻译 hello"), "hello")

    def test_args_only(self):
        self.assertEqual(TranslatorPlugin._parse_command("hello world"), "hello world")

    def test_empty_command(self):
        self.assertEqual(TranslatorPlugin._parse_command("/翻译"), "")

    def test_end_to_end_without_slash(self):
        calls = []
        p = make_plugin()
        mock_translate(p, calls)
        result = run(p.translate_cmd(FakeEvent("翻译 hello world")))
        self.assertEqual(calls[0][0], "hello world")
        self.assertIn("【译文】", reply_text(result))

    def test_end_to_end_fullwidth_slash(self):
        calls = []
        p = make_plugin()
        mock_translate(p, calls)
        run(p.translate_cmd(FakeEvent("／翻译 hello")))
        self.assertEqual(calls[0][0], "hello")


class TestHelp(unittest.TestCase):
    """空参数与帮助关键词"""

    def test_empty_args_shows_help(self):
        p = make_plugin()
        result = run(p.translate_cmd(FakeEvent("/翻译")))
        text = reply_text(result)
        self.assertIn("帮助", text)
        self.assertIn("en", text)
        self.assertIn("英语", text)

    def test_help_keyword(self):
        p = make_plugin()
        result = run(p.translate_cmd(FakeEvent("/翻译 帮助")))
        text = reply_text(result)
        self.assertIn("用法", text)
        self.assertIn("英语", text)

    def test_help_contains_all_langs(self):
        for code in LANGUAGES:
            self.assertIn(code, HELP_TEXT)


class TestDirtyConfig(unittest.TestCase):
    """配置脏值防御"""

    def test_max_chars_non_numeric(self):
        calls = []
        p = make_plugin({"trans_max_chars": "abc"})
        mock_translate(p, calls)
        run(p.translate_cmd(FakeEvent("/翻译 hello world")))
        self.assertEqual(calls[0][0], "hello world")

    def test_max_chars_negative(self):
        self.assertEqual(make_plugin({"trans_max_chars": -5})._max_chars(), 2000)

    def test_max_chars_zero(self):
        self.assertEqual(make_plugin({"trans_max_chars": 0})._max_chars(), 2000)

    def test_max_chars_none(self):
        self.assertEqual(make_plugin({"trans_max_chars": None})._max_chars(), 2000)

    def test_max_chars_float(self):
        self.assertEqual(make_plugin({"trans_max_chars": 3.7})._max_chars(), 3)

    def test_default_target_invalid(self):
        self.assertIsNone(make_plugin({"default_target": "zzz"})._default_target())

    def test_default_target_none(self):
        self.assertIsNone(make_plugin({"default_target": None})._default_target())

    def test_default_target_valid(self):
        self.assertEqual(make_plugin({"default_target": " 英语 "})._default_target(), "en")

    def test_default_target_applies(self):
        calls = []
        p = make_plugin({"default_target": "en"})
        mock_translate(p, calls)
        run(p.translate_cmd(FakeEvent("/翻译 你好世界")))
        self.assertEqual(calls[0][1], "zh")
        self.assertEqual(calls[0][2], "en")

    def test_default_target_invalid_falls_back_to_auto(self):
        calls = []
        p = make_plugin({"default_target": "zzz"})
        mock_translate(p, calls)
        run(p.translate_cmd(FakeEvent("/翻译 你好世界")))
        self.assertEqual(calls[0][2], "en")  # 中文文本自动译为英文


if __name__ == "__main__":
    unittest.main(verbosity=2)
