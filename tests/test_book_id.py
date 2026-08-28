# -*- coding: utf-8 -*-
"""book_id 命名规范只许有一套，且这一套的行为被钉死在这里。

创始人 2026-08-28：「一定要规范命名规则，只有统一的一套」

这条测试守两件事：
  ① **全仓只许有一处 `def to_book_id`** —— 谁再抄一份，这里当场红。
     这不是洁癖：清点脚本算出的「能对上 D1 的数」和真实注册行为一旦用了
     两套规则，缺口数就成了假的，而没有人会发现。
  ② 现有行为逐例钉死 —— 放宽规则是**危险动作**：映射不上只是"这本没入库"，
     映射错了是"这本的图挂到了另一本书上"。改了就得在这里显式改，
     顺带被迫说明为什么。
"""
import os
import re
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import book_id  # noqa: E402


class TestOnlyOneDefinition(unittest.TestCase):
    """★ 本文件的命门：规则只许有一份实现。"""

    def test_no_second_to_book_id_anywhere(self):
        found = []
        for root, dirs, files in os.walk(REPO):
            dirs[:] = [d for d in dirs
                       if d not in (".git", "node_modules", "__pycache__", ".venv")]
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                p = os.path.join(root, fn)
                rel = os.path.relpath(p, REPO).replace(os.sep, "/")
                try:
                    src = open(p, encoding="utf-8", errors="ignore").read()
                except OSError:
                    continue
                if re.search(r"^def to_book_id", src, re.M):
                    found.append(rel)
        self.assertEqual(
            sorted(found), ["scripts/book_id.py"],
            "book_id 规则出现了第二份实现：%s\n"
            "全仓只许有一处 —— 从 scripts/book_id.py import，别抄。\n"
            "（book_id.py 是零依赖模块，import 它不会读环境变量、不会 sys.exit，"
            "所以'为了避免 import 副作用而复制'这个理由不成立。）" % found)

    def test_the_two_scanners_import_it(self):
        """两个真正在用它的脚本必须是 import 来的，不是自带的。"""
        for rel in ("scripts/pan_register.py", "scripts/pan_inventory.py"):
            with self.subTest(file=rel):
                src = open(os.path.join(REPO, rel), encoding="utf-8").read()
                self.assertIn("from book_id import", src,
                              "%s 没有从共享模块取 book_id 规则" % rel)


class TestNamingConvention(unittest.TestCase):
    """三条规则逐例钉死。改了必须在这里显式改。"""

    def test_chinese_catalog_prefix_maps_to_pinyin(self):
        cases = {
            "別024-0001-01 某书": "bie024-0001-01",
            "别024-0001-01":     "bie024-0001-01",
            "子305-0012 方书":    "zi305-0012",
            "史120-0003-02":     "shi120-0003-02",
            "集088-0011":        "ji088-0011",
            "經045-0002":        "jing045-0002",
            "经045-0002":        "jing045-0002",
        }
        for name, want in cases.items():
            with self.subTest(name=name):
                self.assertEqual(book_id.to_book_id(name), want)
                self.assertTrue(book_id.is_recognized(name))

    def test_bare_three_digit_gets_zi_prefix(self):
        """内阁子部番号历史上省了'子'，补回来才能对上 D1。"""
        self.assertEqual(book_id.to_book_id("301-0027-01"), "zi301-0027-01")
        self.assertTrue(book_id.is_recognized("301-0027-01"))

    def test_two_digit_prefix_is_NOT_recognized(self):
        """★ 这一条是 not-in-D1 那批的形态，别不小心把它'修好'。

        实测样本 `01-0022680`（123 上真实存在的目录名）：两位数字开头，
        规则①要中文前缀、规则②要三位数字，两条都不命中 → 原样返回 → D1 里没有。
        2026-08-27 那轮 pan-register 报的 not-in-D1=14,273，大概率主要是这一类。

        **不要为了让它'能匹配'就放宽规则**：`01-0022680` 到底该对应哪本书，
        目前没有证据。先用 pan-inventory 数出这类名字有多少、长什么样，
        再决定是改名还是扩规则。猜一个映射比不映射更危险。
        """
        self.assertEqual(book_id.to_book_id("01-0022680"), "01-0022680")
        self.assertFalse(book_id.is_recognized("01-0022680"),
                         "两位数字前缀不该被当成合规范命名 —— "
                         "清点脚本靠 is_recognized 区分'命名不合规'与'真的是新书'")

    def test_first_token_before_space_is_the_id(self):
        """约定：目录名形如 '<book_id> <书名>'，取第一个空格前的整段。"""
        self.assertEqual(book_id.to_book_id("zi042-0008-04 醫方集略"), "zi042-0008-04")

    def test_unknown_shapes_fall_through_unchanged(self):
        for name in ("道藏五千三百五卷", "GufangP", ""):
            with self.subTest(name=name):
                self.assertFalse(book_id.is_recognized(name))

    def test_module_imports_clean_with_no_env_at_all(self):
        """零依赖是刻意的 —— 它得能在**任何**环境下被 import。

        第一版这条测试是 grep 源码里有没有 "sys.exit"，当场误报了：
        book_id.py 的注释里正好在解释"为什么不这么做"。
        **该测行为，不该测文本** —— 今晚在 grep 上栽了六次，这是第七次的预防。
        改成:清空环境变量、在子进程里真 import 一次，看它活不活得下来。
        """
        import subprocess
        code = ("import sys;sys.path.insert(0, r'%s');"
                "import book_id;"
                "print(book_id.to_book_id('301-0027-01'))"
                % os.path.join(REPO, "scripts").replace("\\", "/"))
        r = subprocess.run([sys.executable, "-c", code],
                           capture_output=True, text=True, env={
                               "PATH": os.environ.get("PATH", ""),
                               "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
                           }, timeout=60)
        self.assertEqual(r.returncode, 0,
                         "空环境下 import book_id 就挂了 —— 它必须零依赖。 stderr: "
                         + (r.stderr or "")[:400])
        self.assertEqual(r.stdout.strip(), "zi301-0027-01")


if __name__ == "__main__":
    unittest.main()
