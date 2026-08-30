# -*- coding: utf-8 -*-
"""指标受控注册表的 CI 闸 —— 把「未注册的数字不得上决策桌」变成机器执行的事。

═══════════════════════════════════════════════════════════════════════════
本文件守什么

`scripts/metrics_registry.yaml`（参谋总长交付件，抄件未改逻辑）顶部写着：

    未在此登记的数字，不得进入任何裁决路径。
    新增指标必须由人登记；改进者、CC、评测器均无权自行新增。

一条只写在 YAML 注释里的纪律，和「只写进日志的产线」是同一种死法 —— 没人执行。
所以这里把它挂进 CI。

**刻意不做的事**：不在 CI 里跑 `check_metric.py --validate` 的全量校验。
因为 `frozen` 段有三项待人拍板（judge_supplier / judge_model_version / scorer_version），
挂上去就是一条**永远红**的检查 —— 而永远红的守卫等于没有守卫，还会训练人忽略它。
那三项属决策，不属代码。本测试只守**现在就该成立**的部分，并把待填项如实列出来。
"""
import io
import os
import re
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
REGISTRY = os.path.join(SCRIPTS, "metrics_registry.yaml")
sys.path.insert(0, SCRIPTS)

REQUIRED = ("name", "role", "numerator", "denominator",
            "freeze_requires", "comparable_within", "owner")


def load():
    import yaml
    with io.open(REGISTRY, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


class TestRegistryShape(unittest.TestCase):

    def test_registry_exists_and_parses(self):
        self.assertTrue(os.path.exists(REGISTRY), "注册表不存在：%s" % REGISTRY)
        reg = load()
        self.assertIsInstance(reg, dict)
        self.assertTrue(reg.get("season_id"), "缺 season_id：跨赛季分数无法作废")

    def test_every_metric_carries_full_口径(self):
        """★ 门1-1.1 的验收断言原文：「metrics_registry 新增指标全部带口径」。

        口径 = 分子/分母/冻结条件/可比范围/负责人。缺一项，这个数字就没法被别人复算，
        也就没法判断两次跑分能不能比 —— 那正是 banned 清单里四条翻车的共同根因。
        """
        for m in (load().get("metrics") or []):
            tag = m.get("name", "<无名>")
            for f in REQUIRED:
                with self.subTest(metric=tag, field=f):
                    self.assertTrue(m.get(f), "指标 %s 缺字段 %s" % (tag, f))

    def test_primary_metric_states_promotion_rule(self):
        """主判据必须写明晋升规则，否则阈值又变回拍脑袋的常数。"""
        for m in (load().get("metrics") or []):
            if m.get("role") == "PRIMARY":
                self.assertTrue(m.get("promotion_rule"),
                                "主判据 %s 未写 promotion_rule" % m.get("name"))

    def test_no_duplicate_metric_names(self):
        names = [m.get("name") for m in (load().get("metrics") or [])]
        self.assertEqual(len(names), len(set(names)), "指标重名：%s" % names)


class TestBannedMetricsStayDead(unittest.TestCase):
    """★ banned 清单不是纪念碑，是活闸：那四个名字不许再出现在任何脚本里。"""

    def test_banned_names_absent_from_scripts(self):
        banned = [b.get("name") for b in (load().get("banned") or []) if b.get("name")]
        self.assertTrue(banned, "banned 清单为空 —— 历史翻车记录不该丢")
        offenders = []
        for root, dirs, files in os.walk(SCRIPTS):
            dirs[:] = [d for d in dirs if d not in ("__pycache__", ".venv")]
            for fn in files:
                if not fn.endswith((".py", ".js", ".mjs")):
                    continue
                p = os.path.join(root, fn)
                try:
                    src = io.open(p, encoding="utf-8", errors="ignore").read()
                except OSError:
                    continue
                for b in banned:
                    # 注册表与本测试自己会提到这些名字，不算违规
                    if os.path.abspath(p) in (os.path.abspath(REGISTRY),):
                        continue
                    if re.search(r"\b%s\b" % re.escape(b), src):
                        offenders.append((os.path.relpath(p, REPO), b))
        self.assertEqual(offenders, [],
                         "被永久禁用的指标又出现在脚本里：%s\n"
                         "它们是历史翻车的名字，见 metrics_registry.yaml 的 banned 段。"
                         % offenders)


class TestGateActuallyBites(unittest.TestCase):
    """★ 守卫自身的活体检查 —— 一条永远绿的闸等于没有闸。"""

    def test_unregistered_metric_is_rejected(self):
        from check_metric import assert_registered, RegistryError
        with self.assertRaises(RegistryError,
                               msg="未注册的指标竟然通过了闸 —— 闸失效"):
            assert_registered("__definitely_not_registered__", __import__("pathlib").Path(REGISTRY))

    def test_banned_metric_is_rejected(self):
        from check_metric import assert_registered, RegistryError
        banned = [b.get("name") for b in (load().get("banned") or []) if b.get("name")]
        if not banned:
            self.skipTest("banned 清单为空")
        with self.assertRaises(RegistryError,
                               msg="禁用清单里的指标竟然通过了闸 —— 闸失效"):
            assert_registered(banned[0], __import__("pathlib").Path(REGISTRY))


class TestFrozenGapsAreVisible(unittest.TestCase):
    """待人拍板的冻结项：不判失败（那是决策不是代码），但必须**看得见**。"""

    def test_report_unfilled_frozen_fields(self):
        frozen = (load().get("frozen") or {})
        unfilled = [k for k, v in frozen.items()
                    if isinstance(v, str) and v.startswith("<待填")]
        print("\n[注册表待填冻结项] %s"
              % (unfilled if unfilled else "无 —— 三冻结已齐，可开全量校验闸"))
        # 刻意不 assert：这三项是决策(选哪家当考官/锁哪个版本)，不在 CC 权限内。
        # 挂成硬失败会造出一条永远红的检查，而永远红的守卫会训练人忽略它。
        self.assertIsInstance(unfilled, list)


if __name__ == "__main__":
    unittest.main()
