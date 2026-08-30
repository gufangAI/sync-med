#!/usr/bin/env python3
"""
指标受控注册表 · 机器闸
=======================

用途：让「未注册的数字不得上决策桌」这条纪律由脚本执行，而不是靠人记得。

用法
----
    # 校验注册表本身是否完整（冻结字段是否已填、结构是否合法）
    python check_metric.py --validate

    # 评测脚本在使用任一指标前调用；未注册 / 未冻结 → 退出码 1
    python check_metric.py --use pairwise_win_rate

    # 一次校验多个
    python check_metric.py --use pairwise_win_rate citation_backcheck

    # 在评测脚本里以库的方式调用
    from check_metric import assert_registered
    assert_registered("pairwise_win_rate")

退出码
------
    0  通过
    1  指标未注册 / 命中禁用清单 / 冻结条件未满足 / 注册表不完整
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 抄件适配（2026-08-30 · 平台CTO）：本文件原样来自参谋总长交付件，逻辑一行未改，
# 只补这一行 —— 它的输出里有 ✓/✗ 等非 ASCII 字符，在 Windows 默认 GBK 终端下会
# 整脚本崩掉（UnicodeEncodeError: 'gbk' codec can't encode character '✗'），
# 导致本机**根本跑不了这道机器闸**。生产在 Actions(Ubuntu/UTF-8) 不受影响，
# 但本地验证被它挡死。写法照抄本仓既有的十余处，不另造。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover - 老版本 Python 没有 reconfigure
    pass

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.stderr.write("需要 pyyaml：pip install pyyaml\n")
    sys.exit(1)

REGISTRY_PATH = Path(__file__).with_name("metrics_registry.yaml")

REQUIRED_METRIC_FIELDS = (
    "name",
    "role",
    "numerator",
    "denominator",
    "freeze_requires",
    "comparable_within",
    "owner",
)

PLACEHOLDER_PREFIX = "<待填"


class RegistryError(Exception):
    """注册表本身有问题，或指标未通过闸。"""


def load_registry(path: Path = REGISTRY_PATH) -> dict:
    if not path.exists():
        raise RegistryError(f"注册表不存在：{path}")
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise RegistryError("注册表格式非法：顶层必须是映射")
    return data


def validate_registry(reg: dict) -> list[str]:
    """返回问题清单；空表示合格。"""
    problems: list[str] = []

    frozen = reg.get("frozen") or {}
    if not frozen:
        problems.append("缺少 frozen 段：三冻结未声明")
    for key, value in frozen.items():
        if isinstance(value, str) and value.startswith(PLACEHOLDER_PREFIX):
            problems.append(f"冻结项未填写：frozen.{key}")

    if not reg.get("season_id"):
        problems.append("缺少 season_id：跨赛季分数无法作废")

    metrics = reg.get("metrics") or []
    if not metrics:
        problems.append("metrics 为空：没有任何指标被登记")

    seen: set[str] = set()
    for idx, m in enumerate(metrics):
        tag = m.get("name", f"#{idx}")
        for field in REQUIRED_METRIC_FIELDS:
            if not m.get(field):
                problems.append(f"指标 {tag} 缺字段：{field}")
        if m.get("name") in seen:
            problems.append(f"指标重名：{m.get('name')}")
        seen.add(m.get("name"))
        # 主判据必须写明晋升规则，防止阈值又变成拍脑袋的常数
        if m.get("role") == "PRIMARY" and not m.get("promotion_rule"):
            problems.append(f"主判据 {tag} 未写 promotion_rule：晋升阈值无依据")

    return problems


def assert_registered(metric_name: str, path: Path = REGISTRY_PATH) -> dict:
    """指标未注册 / 被禁用 / 冻结条件未满足 → 抛 RegistryError。"""
    reg = load_registry(path)

    banned = {b.get("name") for b in (reg.get("banned") or [])}
    if metric_name in banned:
        detail = next(
            (b for b in reg["banned"] if b.get("name") == metric_name), {}
        )
        raise RegistryError(
            f"指标 [{metric_name}] 在禁用清单中。\n"
            f"  历史：{detail.get('what_happened', '（无记录）')}\n"
            f"  后果：{detail.get('consequence', '（无记录）')}"
        )

    metrics = {m.get("name"): m for m in (reg.get("metrics") or [])}
    if metric_name not in metrics:
        raise RegistryError(
            f"指标 [{metric_name}] 未注册。\n"
            f"  未注册的数字不得进入裁决路径。\n"
            f"  先在 {path.name} 登记（名称/分子/分母/冻结条件/可比范围/负责人），再跑。"
        )

    m = metrics[metric_name]
    frozen = reg.get("frozen") or {}
    for need in m.get("freeze_requires") or []:
        value = frozen.get(need)
        if not value or (isinstance(value, str) and value.startswith(PLACEHOLDER_PREFIX)):
            raise RegistryError(
                f"指标 [{metric_name}] 要求冻结项 [{need}]，但该项未填写。\n"
                f"  冻结条件不全 = 这一轮的分数不可比，禁止用于裁决。"
            )
    return m


def main() -> int:
    ap = argparse.ArgumentParser(description="指标受控注册表机器闸")
    ap.add_argument("--validate", action="store_true", help="校验注册表本身")
    ap.add_argument("--use", nargs="+", metavar="METRIC", help="声明本次使用的指标")
    ap.add_argument("--registry", type=Path, default=REGISTRY_PATH)
    args = ap.parse_args()

    if not args.validate and not args.use:
        ap.print_help()
        return 1

    try:
        if args.validate:
            problems = validate_registry(load_registry(args.registry))
            if problems:
                print("✗ 注册表不合格：")
                for p in problems:
                    print(f"   - {p}")
                return 1
            print(f"✓ 注册表合格（赛季 {load_registry(args.registry).get('season_id')}）")

        for name in args.use or []:
            m = assert_registered(name, args.registry)
            print(f"✓ [{name}] 已注册 · 角色={m.get('role')} · 可比范围={m.get('comparable_within')}")

    except RegistryError as exc:
        print(f"✗ {exc}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
