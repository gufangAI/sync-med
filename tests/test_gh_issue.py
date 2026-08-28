# -*- coding: utf-8 -*-
"""gh_issue.upsert 的行为钉死在这里。

这份实现要同时做对两件事——**复用同一个 Issue** 且 **真的出声**——
而它替代的那四份手抄代码，恰好是"复用做对了、出声漏了"。所以这里守的重点是：

  ① 该出声的时候真的发了评论（只 PATCH 正文 = GitHub 不通知任何人 = 等于没写）
  ② 不该出声的时候真的没发（gateway-sentry 每 2 小时一轮 = 360 条/月，
     吵到被静音之后，比现在"没人被通知"还死）
  ③ PR 不许被当成常驻 Issue 去 PATCH（issues 列表里混着 PR，这是 GitHub API 的坑）

全部用打桩，不联网、不碰真 Issue。

用法: python -m pytest tests/test_gh_issue.py -q
"""
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import gh_issue  # noqa: E402

_REAL_API = gh_issue._api


def tearDownModule():
    """把打的桩还回去。

    本文件整份都在替换模块级的 gh_issue._api。不还原的话，同一次
    `pytest tests/` 里后面任何 import 了 gh_issue 的测试都会拿到一个假的 API ——
    那种污染出的错会指向完全无关的文件，是最难查的一类。
    """
    gh_issue._api = _REAL_API


class FakeApi(object):
    """替掉 gh_issue._api，记下每一次调用。"""

    def __init__(self, listing):
        self.listing = listing
        self.calls = []          # [(method, path, payload), ...]

    def __call__(self, token, path, method="GET", payload=None, timeout=45):
        self.calls.append((method, path, payload))
        if method == "GET" and "/issues?" in path:
            return self.listing
        if method == "POST" and path.endswith("/issues"):
            return {"number": 999, "html_url": "https://x/999"}
        if method == "PATCH":
            return {"number": 1, "html_url": "https://x/1"}
        return {}

    def posted_comment(self):
        return any(m == "POST" and p.endswith("/comments") for m, p, _ in self.calls)

    def patched_body(self):
        for m, _, payload in self.calls:
            if m == "PATCH":
                return (payload or {}).get("body")
        return None

    def created_body(self):
        for m, p, payload in self.calls:
            if m == "POST" and p.endswith("/issues"):
                return (payload or {}).get("body")
        return None


def run(listing, **kw):
    fake = FakeApi(listing)
    gh_issue._api = fake
    url = gh_issue.upsert("o/r", "lbl", "PFX", "PFX 标题", "正文", token="t", **kw)
    return fake, url


EXISTING = [{"number": 1, "title": "PFX 昨天", "body": "旧正文", "html_url": "https://x/1"}]


class TestNotifying(unittest.TestCase):
    """① 该出声就要真的出声 —— 这是这份实现存在的全部理由。"""

    def test_default_posts_a_comment(self):
        fake, _ = run(EXISTING)
        self.assertTrue(fake.posted_comment(),
                        "默认必须发评论 —— 只 PATCH 正文 GitHub 不通知任何人，"
                        "那正是被它替换掉的那四份代码的病")

    def test_notify_false_is_absolute_silence(self):
        fake, _ = run(EXISTING, notify=False)
        self.assertFalse(fake.posted_comment())

    def test_creating_does_not_also_comment(self):
        """新建 Issue 本身就会通知订阅者，再追一条是重复吵人。"""
        fake, url = run([])
        self.assertFalse(fake.posted_comment())
        self.assertEqual(url, "https://x/999")


class TestStateGate(unittest.TestCase):
    """② 变了才出声。稳定绿的轮次照常更新正文，但不吵人。"""

    def test_same_state_updates_silently(self):
        listing = [{"number": 1, "title": "PFX x",
                    "body": "旧正文\n\n<!-- gh_issue-state: ok -->",
                    "html_url": "https://x/1"}]
        fake, _ = run(listing, state="ok")
        self.assertFalse(fake.posted_comment(), "状态没变不该发评论")
        self.assertTrue(any(m == "PATCH" for m, _, _ in fake.calls),
                        "但正文还是要更新 —— 要查的时候得查得到")

    def test_changed_state_speaks_up(self):
        listing = [{"number": 1, "title": "PFX x",
                    "body": "旧正文\n\n<!-- gh_issue-state: ok -->",
                    "html_url": "https://x/1"}]
        fake, _ = run(listing, state="fail:3")
        self.assertTrue(fake.posted_comment(), "状态变了必须发评论")

    def test_missing_previous_state_errs_toward_noise(self):
        """读不到旧签名(第一次带 state / 正文被人手改过)→ 按"变了"处理。

        方向是刻意选的：多吵一次的代价是一条评论，静音一次的代价是
        真出事那轮没人知道。
        """
        fake, _ = run(EXISTING, state="ok")   # EXISTING 的正文里没有签名
        self.assertTrue(fake.posted_comment())

    def test_notify_false_still_wins_over_state(self):
        """notify=False 是绝对静音，state 不能把它推翻。"""
        fake, _ = run(EXISTING, state="fail:9", notify=False)
        self.assertFalse(fake.posted_comment())

    def test_state_is_written_into_the_body_invisibly(self):
        fake, _ = run(EXISTING, state="ok")
        body = fake.patched_body()
        self.assertIn("<!-- gh_issue-state: ok -->", body)
        self.assertTrue(body.startswith("正文"), "签名要追在正文后面，不能顶掉正文")

    def test_state_round_trips(self):
        """写进去的签名，下一轮必须读得回来 —— 否则"变了才出声"永远判成"变了"。"""
        fake, _ = run(EXISTING, state="fail:3")
        written = fake.patched_body()
        listing = [{"number": 1, "title": "PFX x", "body": written,
                    "html_url": "https://x/1"}]
        fake2, _ = run(listing, state="fail:3")
        self.assertFalse(fake2.posted_comment(),
                         "同一个签名写进去又读出来应判定为'没变' —— "
                         "读不回来的话每轮都会当成变化，静音档就形同虚设")


class TestCreateFlag(unittest.TestCase):
    """create=False：有就更新，没有就算了 —— 报平安那一路专用。"""

    def test_no_issue_and_create_false_does_nothing(self):
        fake, url = run([], create=False, state="ok")
        self.assertIsNone(url)
        self.assertFalse(any(m == "POST" and p.endswith("/issues")
                             for m, p, _ in fake.calls),
                         "全绿时不该为了报平安凭空开一个 Issue —— 那是纯噪音")

    def test_existing_issue_still_gets_the_recovery_notice(self):
        """fail → ok 是一次状态变化，必须发出恢复通知。

        原来那四份哨兵都没有这个能力：恢复后只是不再更新，
        一个写着"挂了"的 Issue 就那么一直开着 —— 比不报还误导人。
        """
        listing = [{"number": 1, "title": "PFX x",
                    "body": "旧正文\n\n<!-- gh_issue-state: head_down:nvidia -->",
                    "html_url": "https://x/1"}]
        fake, url = run(listing, create=False, state="ok")
        self.assertTrue(fake.posted_comment(), "fail→ok 必须出声")
        self.assertEqual(url, "https://x/1")

    def test_create_true_is_the_default(self):
        fake, url = run([])
        self.assertEqual(url, "https://x/999")


class TestPullRequestsAreNotIssues(unittest.TestCase):
    """③ GitHub 的 issues 列表里混着 PR。认错了会去 PATCH 别人 PR 的标题。"""

    def test_pr_with_matching_prefix_is_skipped(self):
        listing = [
            {"number": 7, "title": "PFX 其实我是个 PR", "pull_request": {"url": "..."},
             "body": "", "html_url": "https://x/pr7"},
        ]
        fake, url = run(listing)
        self.assertFalse(any(m == "PATCH" for m, _, _ in fake.calls),
                         "不许 PATCH 一个 PR")
        self.assertEqual(url, "https://x/999", "应当另建一个真 Issue")


class TestNoToken(unittest.TestCase):
    def test_no_token_returns_none_without_calling_api(self):
        calls = []
        gh_issue._api = lambda *a, **k: calls.append(a) or {}
        old = os.environ.pop("GITHUB_TOKEN", None)
        try:
            self.assertIsNone(
                gh_issue.upsert("o/r", "lbl", "PFX", "t", "b", token=""))
            self.assertEqual(calls, [], "没 token 就别去打 API")
        finally:
            if old is not None:
                os.environ["GITHUB_TOKEN"] = old


if __name__ == "__main__":
    unittest.main()
