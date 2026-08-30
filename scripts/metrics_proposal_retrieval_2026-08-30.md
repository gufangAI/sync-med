# 指标登记提案 · 检索召回两项（待人登记，CC 无权自行新增）

> 平台CTO · 2026-08-30 · 提交给创始人 / 参谋总长
> 依据 `scripts/metrics_registry.yaml` 顶部规则原文：
> 「**新增指标必须由人登记；改进者、CC、评测器均无权自行新增。**」
>
> 所以本文件**只提案，不落表**。同意后请把下面两段整体粘进 `metrics_registry.yaml`
> 的 `metrics:` 列表，并把 owner 换成实际登记人。

---

## 一 · 为什么提这两个（自陈违规在先）

门0-0.2 的验收断言里有「召回基线复测 ≥ 0.698」。该项此前**没有测试集**，
我于 2026-08-30 依《放权阶梯v1》档3「测试数据生成」自建了金标集并跑出数字：

```
hit@1 0.7551 · hit@3 1.0000 · hit@10 1.0000
recall@1 0.4933 · recall@3 0.8800 · recall@10 1.0000
```

把这份 registry 抄件落地后，用它的机器闸一验，**当场判我违规**：

```
$ python check_metric.py --use retrieval_hit_at_k
✗ 指标 [retrieval_hit_at_k] 未注册。
  未注册的数字不得进入裁决路径。
```

**门0 验收表就是裁决路径。** 所以在这两项被登记之前，上面那组数字按规矩
**不具备上决策桌的资格** —— 这一条我认，不绕。本提案就是来补这道手续的。

---

## 二 · 提案条目（可直接粘贴）

```yaml
  - name: retrieval_hit_at_k
    role: GATE               # 检索资格闸：索引坏没坏，过/不过
    numerator: "在检索结果前 k 条中至少命中一个 gold chunk 的查询数"
    denominator: "金标集查询总数"
    unit: ratio
    deterministic: true      # 不经任何模型：ground truth 由 SQL LIKE 精确子串判定
    freeze_requires: [question_set, scorer_version]
    comparable_within: question_set
    owner: "<待填：登记人>"

  - name: retrieval_recall_at_k
    role: SCORE              # 连续分：找全了多少，不是二值
    numerator: "在检索结果前 k 条中被检出的 gold chunk 数"
    denominator: "金标集中 gold chunk 总数"
    unit: ratio
    deterministic: true
    freeze_requires: [question_set, scorer_version]
    comparable_within: question_set
    owner: "<待填：登记人>"
```

---

## 三 · 口径必须随数字一起走的三条（登记时请一并记入）

1. **k 必须随数字报出，且不同 k 不可比。** 实测 k≥3 即饱和（hit@3 = hit@10 = 1.0000），
   **只有 k=1 有区分度**。当"换引擎硬闸"用必须锁 `k=1`，否则这把尺量不出退化。
2. **deterministic: true 是这两项最大的价值。** ground truth 由 SQL `LIKE` 精确子串判定，
   **与被测的 FTS 完全无关**，也不经任何考官模型 —— 所以它不受 `judge_supplier` /
   `judge_model_version` 两个冻结项影响，`frozen` 那三项没填也不妨碍它可比。
   （对比 `pairwise_win_rate` 是 `deterministic: false`，必须等冻考官。）
3. **question_set 版本要钉。** 当前金标集 = 49 题 / 来自 60 部不同的书 /
   gold 数 1–5（平均 1.5），文件 `tests/fixtures/recall_gold.jsonl`（在 guyaofang-web 仓）。
   建议登记时给它一个冻结号（如 `recall_gold_v1_49`），换题即换号，与 `regset_v1_frozen_40` 同制。

### 已知短板（登记前请知悉，别把它当强尺子）

这套题是**已知条目检索**：查询是从原文抽的 8 字短语，逐字存在于目标段中，
bigram 索引近乎确定性命中。**它能证明"索引没坏"，不能证明"检索质量高"。**
要量"概念检索/改写查询"的能力，需要另建难题集 —— 那是另一个指标，不要拿这两项冒充。

---

## 四 · 顺带报三个冻结项未填（机器闸点名）

```
$ python check_metric.py --validate
✗ 注册表不合格：
   - 冻结项未填写：frozen.judge_supplier
   - 冻结项未填写：frozen.judge_model_version
   - 冻结项未填写：frozen.scorer_version
```

`frozen.question_set` 已填（`regset_v1_frozen_40`），其余三项空着。
**这三项是决策**（选哪家当考官、锁哪个版本、判分函数版本号），按放权阶梯不在 CC 权限内。
在它们填上之前，`pairwise_win_rate` 等依赖冻考官的指标**全部不可用于裁决** ——
这不是脚本坏了，是它按设计在拦。

上面提案的两项 `deterministic: true`，**不依赖冻考官**，登记后即可用。
