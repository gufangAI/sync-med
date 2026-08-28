# -*- coding: utf-8 -*-
"""写 Issue 的 workflow 必须显式声明 issues: write —— 否则它只在真出事那次炸。

═══════════════════════════════════════════════════════════════════════════
立此因（2026-08-28 实测）

`watch-roundtable.yml`（内容工厂哨兵）run 33114029499：

    POST /repos/gufangAI/sync-med/issues
    403  Resource not accessible by integration

它**检测对了**（「百家论道内容工厂产帖停滞(近3h新增 0 篇)」），
但写 Issue 被本仓 GITHUB_TOKEN 的默认只读权限拒掉，报错还把整个 job 拖成 failure。

这个失败形态特别阴：

  · 告警步骤通常挂在 `if: failure()` 或"检测到异常才写"的分支上，
    **平时根本不执行**，所以 CI 一片绿；
  · 只有真出事那一轮才会走到写 Issue，然后 403 ——
    **它精确地在你最需要它的那一刻失灵**；
  · run 历史看上去是绿红交替，很容易被当成"偶发抖动"划掉。

而且这不是没人发现过：PR #309 早在 2026-08-14 就修对了 watch-roundtable.yml，
**开了 14 天没合，期间哨兵一直在失败**。所以光修一次不够，要有东西守着。

═══════════════════════════════════════════════════════════════════════════
这条测试怎么判

  写 Issue 的迹象 =  ① 正文里有 `issues.create` / `issues.update` /
                       `issues.createComment` / `issues.addLabels`（inline github-script）
                    ② 或者它跑的某个 scripts/*.py 会 POST/PATCH /issues
                       （含 `from gh_issue import`）

  合规 = 正文里有 `issues: write`（允许后面跟注释）或 `permissions: write-all`

⚠️ 正则写法上踩过的坑，别再踩：第一版用 `^\\s*issues:\\s*write\\s*$` 逐行全匹配，
   把 `issues: write          # 修正环:改不了的开 Issue 让人改` 判成"没声明"，
   一次性误报了 evolve-controller / frontend_sentinel / lane-probe 三个
   **本来就合规**的 workflow。行尾注释在本仓非常普遍 —— 所以这里用 `\\b` 收尾，
   并且下面 TestTheDetectorItself 专门钉住"这三个必须被认成合规"。

用法: python -m pytest tests/test_workflow_permissions.py -q
"""
import io
import os
import re
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF_DIR = os.path.join(REPO, ".github", "workflows")
SCRIPTS_DIR = os.path.join(REPO, "scripts")

# 允许行尾注释 —— 见文件头那条踩坑记录
RE_PERM_ISSUES = re.compile(r"^\s*issues:\s*write\b", re.M)
RE_PERM_WRITE_ALL = re.compile(r"^\s*permissions:\s*write-all\b", re.M)
RE_INLINE_WRITE = re.compile(r"issues\.(create|update|createComment|addLabels)\b")


def _read(path):
    return io.open(path, encoding="utf-8", errors="ignore").read()


def issue_writing_scripts():
    """scripts/ 下会写 Issue 的 .py，返回 {仓内相对路径}。"""
    out = set()
    for root, dirs, files in os.walk(SCRIPTS_DIR):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".venv")]
        for fn in files:
            if not fn.endswith(".py"):
                continue
            p = os.path.join(root, fn)
            src = _read(p)
            writes = bool(re.search(r"from gh_issue import|import gh_issue", src))
            if not writes and "/issues" in src:
                writes = bool(re.search(
                    r"['\"]POST['\"]|['\"]PATCH['\"]|method=['\"](POST|PATCH)", src))
            if writes:
                out.add(os.path.relpath(p, REPO).replace(os.sep, "/"))
    return out


def scan():
    """→ [(workflow 文件名, 写 Issue 的理由列表, 是否已声明权限)]，只含会写 Issue 的。"""
    writers = issue_writing_scripts()
    rows = []
    for fn in sorted(os.listdir(WF_DIR)):
        if not fn.endswith((".yml", ".yaml")):
            continue
        y = _read(os.path.join(WF_DIR, fn))
        why = []
        if RE_INLINE_WRITE.search(y):
            why.append("inline github-script")
        why += sorted(rel.split("/")[-1] for rel in writers if rel in y)
        if not why:
            continue
        declared = bool(RE_PERM_ISSUES.search(y) or RE_PERM_WRITE_ALL.search(y))
        rows.append((fn, why, declared))
    return rows


class TestEveryIssueWriterDeclaresPermission(unittest.TestCase):

    def test_no_workflow_writes_issues_without_the_permission(self):
        missing = [(fn, why) for fn, why, ok in scan() if not ok]
        self.assertEqual(
            missing, [],
            "以下 workflow 会写 Issue 却没声明 `issues: write`：\n" +
            "\n".join("  %s  (via %s)" % (fn, ", ".join(why)) for fn, why in missing) +
            "\n\n本仓 GITHUB_TOKEN 默认只读，写 Issue 会拿到\n"
            "  403 Resource not accessible by integration\n"
            "而告警步骤通常挂在 if: failure() 上，平时不跑 —— 所以它**只在真出事那次炸**。\n"
            "在 workflow 顶层补：\n"
            "  permissions:\n"
            "    contents: read\n"
            "    issues: write")

    def test_scan_actually_found_something(self):
        """★ 探测器自身的活体检查。

        如果哪天正则写坏了、或者目录挪了位置，scan() 会返回空列表，
        上面那条测试就会**恒真通过** —— 一条永远绿的守卫等于没有守卫。
        """
        rows = scan()
        self.assertGreaterEqual(len(rows), 10,
                                "只探到 %d 个写 Issue 的 workflow，探测器八成坏了" % len(rows))


class TestTheDetectorItself(unittest.TestCase):
    """钉住第一版正则误报过的那三个 —— 它们本来就合规。"""

    def test_trailing_comment_after_write_still_counts_as_declared(self):
        by_name = {fn: ok for fn, _, ok in scan()}
        for fn in ("evolve-controller.yml", "frontend_sentinel.yml", "lane-probe.yml"):
            with self.subTest(workflow=fn):
                self.assertIn(fn, by_name, "%s 不在扫描结果里 —— 探测器变了？" % fn)
                self.assertTrue(by_name[fn],
                                "%s 明明声明了 issues: write（只是行尾带注释），"
                                "被判成没声明 = 又踩了同一个正则坑" % fn)

    def test_a_synthetic_missing_case_is_caught(self):
        """反向验证：故意造一个"写 Issue 但没权限"的文本，必须被判定为不合规。"""
        y = "name: x\non:\n  schedule:\n    - cron: '0 * * * *'\njobs:\n  a:\n" \
            "    steps:\n      - run: echo\n        # await github.rest.issues.create({})\n"
        self.assertTrue(RE_INLINE_WRITE.search(y), "该文本应被认成会写 Issue")
        self.assertFalse(RE_PERM_ISSUES.search(y) or RE_PERM_WRITE_ALL.search(y),
                         "该文本不该被认成已声明权限")


if __name__ == "__main__":
    unittest.main()
