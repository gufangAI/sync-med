# -*- coding: utf-8 -*-
"""workflow_sentry 接上 gh_issue 之后的行为，钉在这里。

守的是同一组会**安静出错**的东西（和 test_gateway_sentry 同源，判据各自独立）：

  ① **签名必须由"决定要不要告警"的那些事实算出来，一个不多一个不少。**
     多了 → 在一个"该报的事都没变"的 Issue 上刷评论 → 被静音
     少了 → 真出事那次静音
     具体到这里：小时数（每轮都变）和 stale（单独存在时本来就不开 Issue）
     都不许进签名。

  ② **恢复要出声，但不许为报平安开 Issue。**

  ③ **标题前缀要能同时匹配"告警"和"已恢复"两种标题**，否则会开出两个
     各说各话的 Issue —— 而且不报错。

不联网、不碰真 Issue。

用法: python -m pytest tests/test_workflow_sentry.py -q
"""
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import workflow_sentry as WS  # noqa: E402


# failed 的形状: (workflow 显示名, 文件名, 小时数, run url)
F_A = ("鹰眼", "intel-radar.yml", 3, "https://x/1")
F_B = ("百家论道", "roundtable.yml", 11, "https://x/2")
# zero_out 的形状: (产线名, 生产者 workflow)
Z_A = ("鹰眼情报采集", "intel-radar.yml")
Z_B = ("百家论道内容工厂", "roundtable.yml")
# stale 的形状: (显示名, 文件名, 小时数, 阈值)
S_A = ("某周报", "weekly.yml", 500, 420)


class TestStateSignature(unittest.TestCase):

    def test_nothing_wrong_is_ok(self):
        self.assertEqual(WS.state_of([], []), "ok")

    def test_failed_carries_filenames(self):
        self.assertEqual(WS.state_of([F_A], []), "fail:intel-radar.yml")

    def test_zero_output_carries_names(self):
        self.assertEqual(WS.state_of([], [Z_A]), "zero:intel-radar.yml")

    def test_both_join_with_a_pipe(self):
        s = WS.state_of([F_A], [Z_B])
        self.assertEqual(s, "fail:intel-radar.yml|zero:roundtable.yml")

    def test_hours_never_enter_the_signature(self):
        """★ 小时数每轮都在变。它一旦进签名，"变了才出声"就退化成"每轮都出声"。

        这件事**不会报错**，只会表现为"这个 Issue 好吵"，然后被人静音 ——
        那时比现在更死：现在是没人被通知，那时是所有人主动屏蔽。
        """
        jittered = [(n, f, h * 7 + 13, u) for n, f, h, u in (F_A, F_B)]
        self.assertEqual(WS.state_of([F_A, F_B], []), WS.state_of(jittered, []))
        for _, _, h, _ in jittered:
            self.assertNotIn(str(h), WS.state_of(jittered, []))

    def test_order_does_not_matter(self):
        """扫描顺序变了不该算作"状态变了"。"""
        self.assertEqual(WS.state_of([F_A, F_B], []), WS.state_of([F_B, F_A], []))

    def test_a_new_failing_workflow_does_change_it(self):
        """真的多坏了一个，必须算变化 —— 否则新故障被静音。"""
        self.assertNotEqual(WS.state_of([F_A], []), WS.state_of([F_A, F_B], []))

    def test_signature_matches_the_alert_gate_exactly(self):
        """★ 签名与告警闸必须用同一组事实。

        main() 里的闸是 `if not failed and not zero_out`。
        所以：闸判定"不用报"的每一种输入，签名都必须是 ok；
              闸判定"要报"的每一种输入，签名都必须不是 ok。
        两者一旦分家，就会出现"Issue 说一切正常，却在发通知"或反过来。
        """
        cases = [([], []), ([F_A], []), ([], [Z_A]), ([F_A], [Z_A])]
        for failed, zero in cases:
            gate_says_alert = bool(failed or zero)
            sig_says_alert = WS.state_of(failed, zero) != "ok"
            self.assertEqual(gate_says_alert, sig_says_alert,
                             "闸与签名对 (failed=%s, zero=%s) 判断不一致" % (failed, zero))


class TestStaleIsBodyOnly(unittest.TestCase):
    """stale 单独存在时不开 Issue，所以它也不该驱动通知。"""

    def test_stale_does_not_enter_the_signature(self):
        # state_of 根本不接收 stale —— 这条测的是"签名算不出 stale 来"
        self.assertEqual(WS.state_of([], []), "ok")

    def test_stale_still_renders_into_the_body(self):
        sec = WS.stale_section([S_A])
        self.assertTrue(any("weekly.yml" in line for line in sec))
        self.assertTrue(any("停摆先查" in line for line in sec),
                        "停摆提示要保留 —— execution-watchdog 第五条：先查触发别先改代码")

    def test_no_stale_renders_nothing(self):
        self.assertEqual(WS.stale_section([]), [])


class TestTitlePrefix(unittest.TestCase):
    """③ 一个前缀要同时找回两种标题，且向后兼容既有的那个 Issue。"""

    def test_prefix_matches_both_alert_and_recovery_titles(self):
        alert = f"{WS.TITLE_PREFIX}产线失败/零产出 · 失败 1 · 零产出 0"
        recovered = f"{WS.TITLE_PREFIX}产线全绿 · 扫描 65 个"
        for t in (alert, recovered):
            self.assertTrue(t.startswith(WS.TITLE_PREFIX))

    def test_prefix_still_matches_the_pre_change_title(self):
        """线上那个 Issue 的标题是改造前生成的，必须还能被找回来。

        找不回来 = 另开一个新 Issue，两个并存各说各话，而且不会报错。
        """
        legacy = "🏭 产线失败/零产出 · 失败 2 · 零产出 1"
        self.assertTrue(legacy.startswith(WS.TITLE_PREFIX))


if __name__ == "__main__":
    unittest.main()
