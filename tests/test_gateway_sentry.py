# -*- coding: utf-8 -*-
"""gateway_sentry 接上 gh_issue 之后的行为，钉在这里。

这个哨兵是**每 2 小时一轮的生产脚本**，所以三件事必须钉死：

  ① **状态签名里绝不能有每轮都在变的东西**（延迟毫秒数是最容易混进来的那个）。
     签名一旦每轮都变，"变了才出声"就退化成"每轮都出声" ——
     一次持续三天的故障会刷出 36 条几乎相同的评论，然后被人静音。

  ② **恢复要出声，但不许为了报平安开 Issue**。改之前全绿是直接 return，
     于是恢复之后那个写着「链头挂了」的 Issue 一直开着。

  ③ **标题前缀在所有状态下都一样**。找回同一个 Issue 全靠它；
     哪天有人把"已恢复"的标题写成别的前缀，就会另开一个 Issue，
     两个 Issue 各说各话 —— 而且不会报错。

不联网、不碰真 Issue：HTTP 和 upsert 全部打桩。

用法: python -m pytest tests/test_gateway_sentry.py -q
"""
import io
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

# 这两个是模块级读环境变量的，必须在 import 之前设好
os.environ["XUNMAI_HEAD"] = "nvidia"
os.environ["MIN_HEALTHY"] = "6"
os.environ["GITHUB_TOKEN"] = "t"

import gateway_sentry as GS  # noqa: E402

_REAL_URLOPEN = GS.urllib.request.urlopen
_REAL_UPSERT = GS.upsert


def tearDownModule():
    GS.urllib.request.urlopen = _REAL_URLOPEN
    GS.upsert = _REAL_UPSERT


def health_json(rows):
    """拼出健康端点的返回，形状照 gateway_sentry 里那条正则。"""
    parts = []
    for name, ok, status, ms, err in rows:
        s = ('{"name":"%s","ok":%s,"status":%d,"cost_ms":%d'
             % (name, "true" if ok else "false", status, ms))
        if err:
            s += ',"error":"%s"' % err
        parts.append(s + "}")
    return '{"providers":[' + ",".join(parts) + "]}"


class FakeResp(object):
    def __init__(self, text):
        self._t = text.encode("utf-8")

    def read(self):
        return self._t

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def run(rows):
    """跑一轮，返回 upsert 收到的 kwargs（没调用则 None）。"""
    GS.urllib.request.urlopen = lambda *a, **k: FakeResp(health_json(rows))
    seen = {}

    def fake_upsert(repo, label, title_prefix, title, body, **kw):
        seen.update(dict(repo=repo, label=label, title_prefix=title_prefix,
                         title=title, body=body, **kw))
        return "https://x/1"

    GS.upsert = fake_upsert
    buf, old = io.StringIO(), sys.stdout
    sys.stdout = buf
    try:
        GS.main()
    finally:
        sys.stdout = old
    return (seen or None), buf.getvalue()


# cost_ms 刻意每个都不同 —— 用来验证它没混进签名。
# 刻意给 7 家（MIN_HEALTHY=6），这样"链头挂"和"池子低"能分开单独触发：
# 只有 6 家的话链头一挂就剩 5 家，两个条件会一起中，测不出各自的签名。
ALL_OK = [("nvidia", True, 200, 137, ""), ("groq", True, 200, 88, ""),
          ("cerebras", True, 200, 210, ""), ("a", True, 200, 51, ""),
          ("b", True, 200, 99, ""), ("c", True, 200, 64, ""),
          ("d", True, 200, 173, "")]
HEAD_DOWN = [("nvidia", False, 403, 12, "AppIdNoAuthError")] + ALL_OK[1:]   # 6 家健康,不触发 pool_low
POOL_LOW = ALL_OK[:2] + [(n, False, 500, m, "boom") for n, _, _, m, _ in ALL_OK[2:]]
BOTH = [("nvidia", False, 403, 12, "AppIdNoAuthError")] + \
       [(n, False, 500, m, "boom") for n, _, _, m, _ in ALL_OK[1:]]


class TestStateSignature(unittest.TestCase):
    """① 签名只装"会改变处置动作"的东西。"""

    def test_all_healthy_is_ok(self):
        seen, _ = run(ALL_OK)
        self.assertEqual(seen["state"], "ok")

    def test_head_down_names_the_head_and_status(self):
        seen, _ = run(HEAD_DOWN)
        self.assertEqual(seen["state"], "head_down:nvidia:403")

    def test_pool_low_carries_the_count(self):
        seen, _ = run(POOL_LOW)
        self.assertEqual(seen["state"], "pool_low:2")

    def test_both_conditions_join_with_a_pipe(self):
        seen, _ = run(BOTH)
        self.assertEqual(seen["state"], "head_down:nvidia:403|pool_low:0")

    def test_latency_never_enters_the_signature(self):
        """★ 本文件最重要的一条。

        cost_ms 每轮都在变。它一旦进了签名，签名就永远在变，
        "变了才出声"退化成"每轮都出声"，静音档形同虚设 ——
        而且这件事**不会报错**，只会表现为"这个 Issue 好吵"。
        """
        for rows in (ALL_OK, HEAD_DOWN, POOL_LOW):
            seen, _ = run(rows)
            for _, _, _, ms, _ in rows:
                self.assertNotIn(str(ms), seen["state"],
                                 "延迟 %dms 混进了状态签名:%s" % (ms, seen["state"]))

    def test_same_health_twice_gives_the_same_signature(self):
        """同样的健康状况、不同的延迟 → 签名必须一致。"""
        a, _ = run(HEAD_DOWN)
        jittered = [(n, ok, st, ms * 3 + 7, e) for n, ok, st, ms, e in HEAD_DOWN]
        b, _ = run(jittered)
        self.assertEqual(a["state"], b["state"])


class TestRecovery(unittest.TestCase):
    """② 恢复要出声，但不许为报平安开 Issue。"""

    def test_healthy_round_does_not_create(self):
        seen, _ = run(ALL_OK)
        self.assertIs(seen["create"], False,
                      "全绿时必须 create=False —— 不许为了报平安凭空开 Issue")

    def test_healthy_round_still_calls_upsert(self):
        """改之前这里是直接 return，恢复通知就是这么丢的。"""
        seen, _ = run(ALL_OK)
        self.assertIsNotNone(seen, "全绿时也要调 upsert，否则 fail→ok 发不出恢复通知")
        self.assertIn("已恢复", seen["title"])

    def test_failing_round_may_create(self):
        seen, _ = run(HEAD_DOWN)
        self.assertNotEqual(seen.get("create"), False,
                            "出故障时必须允许新建 Issue")


class TestTitlePrefixStable(unittest.TestCase):
    """③ 三种状态共用同一个前缀，否则会开出两个各说各话的 Issue。"""

    def test_prefix_is_the_same_across_all_states(self):
        titles = []
        for rows in (ALL_OK, HEAD_DOWN, POOL_LOW):
            seen, _ = run(rows)
            self.assertEqual(seen["title_prefix"], GS.TITLE_PREFIX)
            titles.append(seen["title"])
        for t in titles:
            self.assertTrue(t.startswith(GS.TITLE_PREFIX),
                            "标题 %r 不以约定前缀开头 —— 会被当成另一个 Issue" % t)

    def test_label_is_gateway(self):
        seen, _ = run(HEAD_DOWN)
        self.assertEqual(seen["label"], "gateway")


class TestBodyStillHasTheTable(unittest.TestCase):
    def test_provider_table_present_in_both_paths(self):
        for rows in (ALL_OK, HEAD_DOWN):
            seen, _ = run(rows)
            self.assertIn("## 全部供应商", seen["body"])
            self.assertIn("nvidia", seen["body"])


if __name__ == "__main__":
    unittest.main()
