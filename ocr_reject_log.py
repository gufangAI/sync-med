# -*- coding: utf-8 -*-
# 判退页台账:把「哪几页被质量闸判退」落成能读回来的记录,而不是只留一个计数。
#
# 为什么必须有它(2026-07-28 血证):
#   质量闸的 repeat_ngram / max_run / line_dup 三条判据原先没有长度下限,
#   在短页上比例的分母太小,正常的方剂组成页靠巧合就撞阈值被当垃圾判退
#   (桂枝湯组成 26 字 -> max_run=0.46;线上实证 _ocr/bcgmj21/page_0007.txt)。
#   判据已在 1824f50 修好,但【已经被误杀的那些页找不回来】——三条产线判退时
#   只 cnt["rej"] += 1,run log 里只剩一个数字,R2 的 _ocr_rejected/ 下是一堆
#   没有索引的对象。要捞回只能靠考古:翻 run log 找当时的 TOTAL/PILOT,
#   按脚本的分片算法重建候选集,再逐个 HEAD 探。判据每修一次就得考古一次。
#
#   这正是平台铁律第 8 条换了身衣服:「凡是只写进日志的产线一律视为没人看」
#   (pan-register 在日志里连喊 12 天 not-in-D1=3965,零人响应)。
#   判退页不是一条日志,是一条【待办】——它必须留下能被程序读回的名单。
#
# 成本纪律(zero-r2-move):每 shard 一次 GET + 一次 PUT,零 LIST。
#   为什么要那一次 GET:ocr_ndl 每 6 小时一轮 cron、每轮 RUN_CAP=300 页,
#   直接覆盖写等于每轮把上一轮的判退名单抹掉,一周后台账里只剩最后 6 小时,
#   那还是「没人看得见」。GET 回来合并再写,台账才是累积的。
#
# 记什么:key + 判据原因 + 各项 ratio + 时间 + run_id。
#   记 ratio 而不只是 label,是为了下次判据再调时【不用再考古】——
#   直接读台账就能算出"这次放松会捞回哪些页",一次 GET 抵一次全量 HEAD 扫描。
import json
import os
import time

# 台账封顶条数。判退本该是稀有事件(2026-07-24 那两次 run:800 页里 87 页),
# 但闸门一旦回归(比如再引入一条没有长度下限的判据)会瞬间爆量。封顶保留最新的,
# 并把丢弃数写进台账头 —— 截断必须是看得见的,不能静默吞掉。
MAX_ENTRIES = 5000

RUN_ID = os.environ.get("GITHUB_RUN_ID", "")


def ledger_key(engine, shard):
    """台账落点。engine 用与各线已有汇总台账相同的词根(ocr / ocrxf / ocr_ndl),
    这样 _ledger/ 下 rejected_* 和它对应的汇总 json 一眼能对上。"""
    return f"_ledger/rejected_{engine}_{shard}.json"


def record(entries, key, qa, stored):
    """把一条判退追加进本轮内存清单。

    qa = ocr_quality.analyze() 的返回。调用方必须用 analyze 而不是 verdict ——
    verdict 只给一个 label,拿不到 reasons 和 ratio,记下来的台账就又变成
    「只有计数」的那个东西了。

    stored=False 表示连 _ocr_rejected/ 都没写进去(PUT 抛异常被吞了)。
    这种页在 R2 里一个字节都不存在,台账是它唯一的存在证明 —— 尤其要记。
    """
    entries.append({
        "key": key,
        "reasons": qa.get("reasons") or [],
        "len": qa.get("len"),
        # 六项 ratio 全留,下次调阈值可直接在台账上重算判级,不必回 R2 取原文
        "repeat_ngram": qa.get("repeat_ngram"),
        "repeat_n": qa.get("repeat_n"),
        "max_run": qa.get("max_run"),
        "run_period": qa.get("run_period"),
        "line_dup": qa.get("line_dup"),
        "garbage_ratio": qa.get("garbage_ratio"),
        "single_char": qa.get("single_char"),
        "cjk_ratio": qa.get("cjk_ratio"),
        # 背靠背连出次数 / 元话术(2026-07-28 加)也必须落台账,理由和上面六项一模一样:
        # abs_repeat 的门槛 8 是按【实测到的最小恶性值】定的,3~7 那一段现在没有样本;
        # 不把它记下来,将来想把门槛往下挪就又只能靠翻 run log + 逐个 HEAD 的考古法。
        # 同日晚些又把 abs_repeat 由「短页专用」扩到全长度(比例判据在长页上误杀方剂
        # 组成页,靠它纠),所以这个数在长页判退里同样有值,不再只有短页那一档有意义。
        "abs_repeat": qa.get("abs_repeat"),
        "abs_repeat_unit": qa.get("abs_repeat_unit"),
        "model_meta": qa.get("model_meta"),
        "score": qa.get("score"),
        "stored": bool(stored),
        "ts": int(time.time()),
        "run_id": RUN_ID,
    })


def _load_old(s3, bucket, key):
    """读回已有台账。返回 (items, ok)。

    ok=False 表示「读失败,且不确定台账是不是本来就不存在」—— 这种情况调用方
    必须放弃写入,否则会拿本轮这几条把历史整份覆盖掉。保住已做的 > 写新的。
    """
    try:
        raw = s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")
    except Exception as e:
        # botocore 的 NoSuchKey/404 = 台账首次创建,是正常状态,不是故障。
        # 其它错误(限流/网络/权限)则不能当"空台账"处理。
        code = ""
        resp = getattr(e, "response", None)
        if isinstance(resp, dict):
            code = str((resp.get("Error") or {}).get("Code") or "")
        if code in ("NoSuchKey", "404", "NotFound"):
            return [], True
        print(f"WARN 判退台账 {key} 读取失败({str(e)[:100]})-> 本轮不写,避免覆盖历史", flush=True)
        return [], False
    try:
        doc = json.loads(raw)
    except Exception as e:
        print(f"WARN 判退台账 {key} 解析失败({str(e)[:100]})-> 本轮不写,避免覆盖历史", flush=True)
        return [], False
    if isinstance(doc, dict):
        return list(doc.get("items") or []), True
    if isinstance(doc, list):
        return list(doc), True
    print(f"WARN 判退台账 {key} 形状异常 -> 本轮不写,避免覆盖历史", flush=True)
    return [], False


def flush(s3, bucket, engine, shard, entries):
    """把本轮判退清单合并进 R2 台账。每 shard 一次 GET + 一次 PUT,零 LIST。

    返回 (总条数, 本轮新增条数, 因封顶丢弃条数);写不成返回 (-1, len(entries), 0)。
    永不抛异常 —— OCR 本身已经跑完了,台账写不成不该让整个 shard 判失败。
    """
    key = ledger_key(engine, shard)
    if not entries:
        # 本轮没有判退:不做任何 R2 调用。空写一次也是一次 Class A。
        return (0, 0, 0)
    old, ok = _load_old(s3, bucket, key)
    if not ok:
        print(f"=== 判退台账 {key} 本轮跳过写入,本轮 {len(entries)} 条只留在 run log ===", flush=True)
        for e in entries:
            print(f"    REJECTED {e['key']} :: {'/'.join(e['reasons'])}", flush=True)
        return (-1, len(entries), 0)

    # 按 key 合并,同一页以最新一条为准(同一张图喂同一个引擎结果一样,
    # 留最新的那条就够;留两条只会让"判退了多少页"这个数字变得没法读)。
    merged = {}
    for e in old:
        k = e.get("key")
        if k:
            merged[k] = e
    new_n = 0
    for e in entries:
        if e["key"] not in merged:
            new_n += 1
        merged[e["key"]] = e
    items = sorted(merged.values(), key=lambda x: (int(x.get("ts") or 0), x.get("key") or ""))
    dropped = 0
    if len(items) > MAX_ENTRIES:
        dropped = len(items) - MAX_ENTRIES
        items = items[-MAX_ENTRIES:]          # 留最新的
    doc = {
        "engine": engine,
        "shard": shard,
        "updated_at": int(time.time()),
        "last_run_id": RUN_ID,
        "count": len(items),
        "added_this_run": new_n,
        "dropped_by_cap": dropped,
        "cap": MAX_ENTRIES,
        "items": items,
    }
    try:
        s3.put_object(Bucket=bucket, Key=key,
                      Body=json.dumps(doc, ensure_ascii=False).encode("utf-8"),
                      ContentType="application/json; charset=utf-8")
    except Exception as e:
        print(f"WARN 判退台账 {key} 写入失败({str(e)[:120]})-> 本轮 {len(entries)} 条只剩 run log", flush=True)
        return (-1, len(entries), 0)
    # 台账写成了也要在 run log 里报一声总数:光有对象没人知道去看它,
    # 就又回到「只写进日志 = 没人看」的老路上。
    print(f"=== 判退台账 {key}: 本轮 +{new_n},累计 {len(items)} 条"
          f"{f'(封顶丢弃 {dropped})' if dropped else ''} ===", flush=True)
    return (len(items), new_n, dropped)
