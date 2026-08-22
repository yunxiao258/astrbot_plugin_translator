# -*- coding: utf-8 -*-
"""翻译插件新功能测试（v1.1.0）：语种检测指令、长文分段、缓存、统计持久化。

所有测试 mock _translate，不联网；数据目录注入临时目录。
"""

import asyncio
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, r"D:\astrbot\data\plugins")

from astrbot_plugin_translator.main import TranslatorPlugin  # noqa: E402


class FakeEvent:
    def __init__(self, message_str=""):
        self.message_str = message_str

    def chain_result(self, chain):
        return chain


def make_plugin(config=None, data_dir=None):
    tmp = data_dir or tempfile.mkdtemp(prefix="trans_extra_test_")
    return TranslatorPlugin(None, config or {}, data_dir=tmp), tmp


def run(coro):
    return asyncio.run(coro)


def chain_text(chain) -> str:
    return chain[0].text


class TestSplitText(unittest.TestCase):
    def test_short_text_single_segment(self):
        parts = TranslatorPlugin._split_text("短文本", 100)
        self.assertEqual(parts, ["短文本"])

    def test_split_at_sentence_punctuation(self):
        text = "第一句话。" * 30  # 150 字
        parts = TranslatorPlugin._split_text(text, 20)
        self.assertGreater(len(parts), 1)
        self.assertEqual("".join(parts), text)
        # 除最后一段外都应以句号结尾（在标点处切分）
        for p in parts[:-1]:
            self.assertTrue(p.endswith("。"), f"段未按句号切分: {p[-5:]}")

    def test_split_newline_priority(self):
        text = "abc\n" + "x" * 15
        parts = TranslatorPlugin._split_text(text, 10)
        self.assertEqual("".join(parts), text)
        self.assertEqual(parts[0], "abc\n")

    def test_hard_split_when_no_seps(self):
        text = "a" * 35
        parts = TranslatorPlugin._split_text(text, 10)
        self.assertEqual("".join(parts), text)
        for p in parts:
            self.assertLessEqual(len(p), 10)

    def test_no_content_loss(self):
        import random
        random.seed(42)
        text = "".join(random.choice("你好世界，今天天气不错。abc xyz!\n") for _ in range(500))
        parts = TranslatorPlugin._split_text(text, 50)
        self.assertEqual("".join(parts), text)


class TestCache(unittest.TestCase):
    def test_same_text_uses_cache(self):
        plugin, _ = make_plugin()
        calls = []

        async def fake_translate(text, src, dst):
            calls.append(text)
            return "译文"

        plugin._translate = fake_translate
        r1 = run(plugin._do_translate(FakeEvent("/翻译 hello"), "hello"))
        r2 = run(plugin._do_translate(FakeEvent("/翻译 hello"), "hello"))
        self.assertIn("译文", chain_text(r2))
        # 第二次应命中缓存，不再调用接口
        self.assertEqual(len(calls), 1)

    def test_cache_disabled_always_calls(self):
        plugin, _ = make_plugin({"trans_cache_enabled": False})
        calls = []

        async def fake_translate(text, src, dst):
            calls.append(text)
            return "译文"

        plugin._translate = fake_translate
        run(plugin._do_translate(FakeEvent("/翻译 hello"), "hello"))
        run(plugin._do_translate(FakeEvent("/翻译 hello"), "hello"))
        self.assertEqual(len(calls), 2)

    def test_cache_limit_eviction(self):
        plugin, _ = make_plugin({"trans_cache_size": 2})

        async def fake_translate(text, src, dst):
            return f"译:{text}"

        plugin._translate = fake_translate
        for t in ("aaa", "bbb", "ccc"):
            run(plugin._do_translate(FakeEvent(f"/翻译 {t}"), t))
        self.assertLessEqual(len(plugin._cache), 2)
        # 最旧的 aaa 已被淘汰
        self.assertIsNone(plugin._cache_get("en", "zh", "aaa"))
        self.assertIsNotNone(plugin._cache_get("en", "zh", "ccc"))

    def test_dirty_cache_size(self):
        plugin, _ = make_plugin({"trans_cache_size": "bad"})
        self.assertEqual(plugin._cache_limit(), 500)


class TestSegmentTranslation(unittest.TestCase):
    def test_long_text_segmented(self):
        plugin, _ = make_plugin({"trans_max_chars": 10, "trans_max_segments": 5})
        seg_calls = []

        async def fake_translate(text, src, dst):
            seg_calls.append(len(text))
            return "[段]"

        plugin._translate = fake_translate
        # 每句内容不同，避免相同段落命中缓存干扰调用计数
        text = "".join(f"这是第{i}句话。" for i in range(12))
        r = run(plugin._do_translate(FakeEvent(f"/翻译 {text}"), text))
        out = chain_text(r)
        self.assertIn("长文已分段翻译", out)
        self.assertGreater(len(seg_calls), 1)
        for n in seg_calls:
            self.assertLessEqual(n, 11)  # 每段不超过 max_chars+1

    def test_segment_disabled_truncates(self):
        plugin, _ = make_plugin({"trans_max_chars": 10, "trans_segment_enabled": False})

        async def fake_translate(text, src, dst):
            return "[译]"

        plugin._translate = fake_translate
        text = "a" * 50
        r = run(plugin._do_translate(FakeEvent(f"/翻译 {text}"), text))
        out = chain_text(r)
        self.assertNotIn("已分段", out)
        self.assertIn("已截取前 10 字翻译", out)

    def test_max_segments_cap(self):
        plugin, _ = make_plugin({"trans_max_chars": 5, "trans_max_segments": 2})

        async def fake_translate(text, src, dst):
            return "[译]"

        plugin._translate = fake_translate
        text = "句子内容测试。" * 20
        calls = []

        async def counting(text_, s, d):
            calls.append(text_)
            return "[译]"

        plugin._translate = counting
        run(plugin._do_translate(FakeEvent(f"/翻译 {text}"), text))
        self.assertLessEqual(len(calls), 2)


class TestDetectCommand(unittest.TestCase):
    def test_detect_chinese(self):
        plugin, _ = make_plugin()
        r = plugin._cmd_detect(FakeEvent("/翻译 检测 你好世界"), "你好世界")
        self.assertIn("中文 (zh)", chain_text(r))

    def test_detect_japanese(self):
        plugin, _ = make_plugin()
        r = plugin._cmd_detect(FakeEvent("/翻译 检测 こんにちは"), "こんにちは")
        self.assertIn("(ja)", chain_text(r))

    def test_detect_korean(self):
        plugin, _ = make_plugin()
        r = plugin._cmd_detect(FakeEvent("/翻译 检测 안녕하세요"), "안녕하세요")
        self.assertIn("(ko)", chain_text(r))

    def test_detect_english(self):
        plugin, _ = make_plugin()
        r = plugin._cmd_detect(FakeEvent("/翻译 检测 hello world"), "hello world")
        self.assertIn("英语 (en)", chain_text(r))

    def test_detect_empty(self):
        plugin, _ = make_plugin()
        r = plugin._cmd_detect(FakeEvent("/翻译 检测"), "")
        self.assertIn("用法", chain_text(r))


class TestStats(unittest.TestCase):
    def test_stats_recorded_and_persisted(self):
        plugin, tmp = make_plugin()

        async def fake_translate(text, src, dst):
            return "译文"

        plugin._translate = fake_translate
        run(plugin._do_translate(FakeEvent("/翻译 hello world"), "hello world"))
        self.assertEqual(plugin._stats["total"], 1)
        self.assertEqual(plugin._stats["by_dst"].get("zh"), 1)
        # 重新加载后统计仍在
        plugin2, _ = make_plugin(data_dir=tmp)
        self.assertEqual(plugin2._stats["total"], 1)

    def test_stats_command_output(self):
        plugin, _ = make_plugin()

        async def fake_translate(text, src, dst):
            return "译文"

        plugin._translate = fake_translate
        run(plugin._do_translate(FakeEvent("/翻译 hello"), "hello"))
        r = plugin._cmd_stats(FakeEvent("/翻译 统计"))
        text = chain_text(r)
        self.assertIn("累计翻译：1 次", text)

    def test_stats_empty_hint(self):
        plugin, _ = make_plugin()
        r = plugin._cmd_stats(FakeEvent("/翻译 统计"))
        self.assertIn("暂无翻译记录", chain_text(r))

    def test_stats_disabled(self):
        plugin, _ = make_plugin({"trans_stats_enabled": False})

        async def fake_translate(text, src, dst):
            return "译文"

        plugin._translate = fake_translate
        run(plugin._do_translate(FakeEvent("/翻译 hello"), "hello"))
        self.assertEqual(plugin._stats["total"], 0)
        r = plugin._cmd_stats(FakeEvent("/翻译 统计"))
        self.assertIn("未开启", chain_text(r))

    def test_cache_hit_counted(self):
        plugin, _ = make_plugin()

        async def fake_translate(text, src, dst):
            return "译文"

        plugin._translate = fake_translate
        run(plugin._do_translate(FakeEvent("/翻译 hello"), "hello"))
        run(plugin._do_translate(FakeEvent("/翻译 hello"), "hello"))
        self.assertEqual(plugin._stats["cache_hits"], 1)
        r = plugin._cmd_stats(FakeEvent("/翻译 统计"))
        self.assertIn("缓存命中：1 次", chain_text(r))

    def test_by_dst_ranking(self):
        plugin, _ = make_plugin()

        async def fake_translate(text, src, dst):
            return "t"

        plugin._translate = fake_translate
        run(plugin._do_translate(FakeEvent("/翻译 en>ja one"), "en>ja one"))
        run(plugin._do_translate(FakeEvent("/翻译 en>ja two"), "en>ja two"))
        run(plugin._do_translate(FakeEvent("/翻译 hello"), "hello"))
        r = plugin._cmd_stats(FakeEvent("/翻译 统计"))
        text = chain_text(r)
        self.assertIn("日语(ja)：2 次", text)
        self.assertIn("中文(zh)：1 次", text)

    def test_corrupted_stats_reset(self):
        tmp = tempfile.mkdtemp(prefix="trans_corrupt_")
        with open(os.path.join(tmp, "translator_stats.json"), "w", encoding="utf-8") as f:
            f.write("{broken json")
        plugin, _ = make_plugin(data_dir=tmp)
        self.assertEqual(plugin._stats["total"], 0)


class TestDispatcher(unittest.TestCase):
    def test_dispatch_detect(self):
        plugin, _ = make_plugin()
        r = run(plugin.translate_cmd(FakeEvent("/翻译 检测 你好")))
        self.assertIn("语种", chain_text(r))

    def test_dispatch_stats(self):
        plugin, _ = make_plugin()
        r = run(plugin.translate_cmd(FakeEvent("/翻译 统计")))
        self.assertIn("翻译统计", chain_text(r) + "" if "暂无" not in chain_text(r) else chain_text(r))

    def test_normal_translation_unaffected(self):
        plugin, _ = make_plugin()

        async def fake_translate(text, src, dst):
            return "hi"

        plugin._translate = fake_translate
        r = run(plugin.translate_cmd(FakeEvent("/翻译 检查一下这条消息")))
        self.assertIn("译文：hi", chain_text(r))


if __name__ == "__main__":
    unittest.main(verbosity=1)
