# -*- coding: utf-8 -*-
"""sueai/lanes.js 与 scripts/lanes.py 必须对同一个问题给同一个答案。

为什么需要这一条：

    lanes.json 是模型 lane 的唯一真源，但它有**两个访问层**，因为有两个运行时：
        sueai/lanes.js    Node 侧（probe_lanes.js / school_router.js / call_model.js）
        scripts/lanes.py  Actions 的 Python 批处理侧（direct_fleet.py）

    两个访问层本身是合理的 —— 语言不同，没法共用。**但它们对"这条 lane 能不能用"
    必须给出同一个答案**，否则真源虽然只有一份，判据却有两份，仓里反复犯的正是这个病。

    lanes.js 的文件头记着它自己诞生的原因：2026-07-27 之前同一份清单在三处各存一份，
    SiliconFlow 返回 HTTP 402 那晚只有一处知情。**多一个语言就多一次同样的机会。**

    这条测试第一次写出来时就抓到了一处真分叉：Python 侧原本用「死状态黑名单」，
    还自己编了一个 lanes.js 里不存在的 'disabled'。今天答案一样，
    **新增第六个状态那天答案相反** —— 黑名单会放行它，白名单会拦住它。

跑法：本文件只读两份源码，不发网络、不碰 D1。需要 node 在 PATH 上；
没有 node 时跳过（跳过时明说，不假装通过）。
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import lanes as PY  # noqa: E402


def _js_lane_status():
    """从 lanes.js 里真取 LANE_STATUS —— 不是照抄一份到测试里。

    照抄的话，这条测试就变成了**第三份**清单，正是它要防的东西。
    """
    node = shutil.which("node")
    if not node:
        return None
    src = ("import('./sueai/lanes.js')"
           ".then(m => console.log(JSON.stringify(Object.values(m.LANE_STATUS))))")
    r = subprocess.run([node, "-e", src], cwd=REPO,
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError("读 lanes.js 失败: " + (r.stderr or "")[:300])
    return json.loads(r.stdout.strip())


class TestLaneStatusParity(unittest.TestCase):

    def test_enum_matches_lanes_js_exactly(self):
        js = _js_lane_status()
        if js is None:
            self.skipTest("PATH 上没有 node —— 跳过，不当作通过")
        self.assertEqual(
            sorted(js), sorted(PY.LANE_STATUS),
            "lanes.py 的 LANE_STATUS 与 lanes.js 不一致。"
            "两边都要改，否则同一条 lane 会被判成两种结果。")

    def test_only_active_is_usable(self):
        """白名单语义：镜像 lanes.js 的 usableLanes()（status === ACTIVE）。

        别改成「不在死名单里就算能用」—— 那在加第六个状态那天会和 JS 侧相反。
        """
        self.assertTrue(PY.is_usable("active"))
        for st in ("no_credit", "no_key", "retired", "error"):
            with self.subTest(status=st):
                self.assertFalse(PY.is_usable(st))

    def test_unknown_status_is_neither_alive_nor_dead(self):
        """lanes.js 见到枚举外的 status 会 throw。

        Python 侧不能 throw（真源读不到时调用方仍须能跑），所以它必须
        **既不当成活的、也不当成死的**：告警 + 保留。判成活的会往死链上打，
        判成死的会误杀一条可能好的 lane —— 两种都比"说不知道"差。
        """
        self.assertFalse(PY.is_usable("throttled"))
        self.assertFalse(PY.is_dead("throttled"))
        self.assertFalse(PY.is_known("throttled"))

    def test_unknown_status_is_kept_even_when_enforcing(self):
        doc = {"lanes": [{"id": "x-new", "status": "throttled"}]}
        fd, tmp = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            with io.open(tmp, "w", encoding="utf-8") as f:
                f.write(json.dumps(doc))
            kept = PY.filter_dead([{"id": "x-new"}], enforce=True, path=tmp,
                                  label="parity-test")
            self.assertEqual(len(kept), 1,
                             "未知状态被丢掉了 —— 未知不等于死")
        finally:
            os.unlink(tmp)

    def test_lane_absent_from_source_is_kept(self):
        """真源里根本没有这条 lane：同样是未知，不是死。"""
        doc = {"lanes": [{"id": "other", "status": "active"}]}
        fd, tmp = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            with io.open(tmp, "w", encoding="utf-8") as f:
                f.write(json.dumps(doc))
            kept = PY.filter_dead([{"id": "not-listed"}], enforce=True, path=tmp,
                                  label="parity-test")
            self.assertEqual(len(kept), 1)
        finally:
            os.unlink(tmp)

    def test_unreadable_source_changes_nothing(self):
        """读不到真源时必须 fail-open —— 批处理不能因为配置文件缺失就全线停摆。"""
        mine = [{"id": "a"}, {"id": "b"}]
        kept = PY.filter_dead(mine, enforce=True,
                              path=os.path.join(REPO, "no-such-file.json"),
                              label="parity-test")
        self.assertEqual(len(kept), 2)


if __name__ == "__main__":
    unittest.main()
