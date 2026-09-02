#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抓取调度层 —— 队列 / 去重 / 断点续跑 / 幂等。

═══ 吸收来源与许可(必须原样保留)═══
本文件的技术来自以下开源项目的**源码阅读**,均为可商用宽松许可:

  · scrapy/scrapy            BSD-3-Clause    可抄代码
      - scrapy/dupefilters.py  RFPDupeFilter:`requests.seen` 长度前缀只追加日志
        → 落在本文件 class SeenLog(格式照抄,并修了它的一个真缺陷,见该类注释)
      - scrapy/utils/request.py fingerprint():规范化字段 dict → json.dumps(sort_keys=True)
        → sha1,而不是字符串拼接
        → 落在本文件 fingerprint()
      - scrapy/pqueues.py      ScrapyPriorityQueue:每个优先级一个独立子队列
        → 本文件**没有照抄**,改写成 SQLite 的 `ORDER BY prio, seq`,理由见 Scheduler 注释
      - queuelib/queue.py      FifoSQLiteQueue:每次 push/pop 各自一个事务的持久队列
        → 落在本文件 Scheduler 的 SQLite 存储
      - queuelib/queue.py      FifoDiskQueue:**反面教材,故意不抄**,理由见文末「为什么不用 JOBDIR」

  · apify/crawlee-python      Apache-2.0      可抄代码
      - _sql/_request_queue_client.py  租约(time_blocked_until + client_key)取代锁
        → 落在本文件 Scheduler.fetch() 的 CAS 认领
      - _sql/_request_queue_client.py  幂等入队 = 主键冲突忽略(INSERT ... ON CONFLICT DO NOTHING)
        → 落在本文件 Scheduler.push_many() 的 INSERT OR IGNORE
      - _file_system/_request_queue_client.py  pending → in_progress → handled 三态
        → 落在本文件 state 列 + 租约过期自动回收
      - _utils/requests.py     normalize_url:去 utm_ / query 排序 / 去尾斜杠
        → 落在本文件 normalize_url(),但**改了两处**,见该函数注释

  · apify/crawlee (TS)        Apache-2.0      可抄代码
      - packages/core/src/storages/request_list.ts  游标一致性护栏:对不上就 throw 不静默续跑
        → 落在本文件 Scheduler._guard_state() 的三道校验

  · firecrawl                 AGPL-3.0        **传染性许可 —— 本文件只借鉴架构思路,
                                                零代码搬运,一行都没有**

═══ 这根柱子解决什么 ═══
GitHub Actions 的 job 最长 6 小时,到点是 **SIGKILL,没有优雅退出**(这一条决定了
本文件几乎所有设计:任何"靠 close() 落盘"的方案在我们这儿都是错的)。
我们又没有 Redis、没有常驻服务,状态只能是一个文件。所以:

  ① 去重      —— 指纹化,同一页不抓两遍;指纹跨 run、跨 Python 版本必须稳定
  ② 优先级    —— 鹰眼榜单(每天必须出数)排在 RAG 语料(慢慢磨)前面
  ③ 断点续跑  —— 状态就是 .sqlite3 一个文件,走 actions/cache 或 upload-artifact 传给下一轮
  ④ 幂等      —— 同一个 workflow 重跑一百遍,结果一样;这是平台加 cron 前的硬门槛

红线:纯 stdlib(sqlite3 / urllib / hashlib / json),零 pip、零常驻进程、零本地算力。
"""
import io
import os
import re
import sys
import json
import time
import signal
import sqlite3
import hashlib
import urllib.parse

# Windows 控制台默认 GBK,中文输出会被静默改写成 '?'。
# **用 reconfigure 而不是 sys.stdout = TextIOWrapper(sys.stdout.buffer, ...)** ——
# 后者是实测踩到的坑(2026-09-02):下面我们要 import arsenal_mine,而它模块级也做了
# 一次同样的包装;两次包装叠在同一个 BufferedWriter 上,中间那个 TextIOWrapper
# 没人引用被 GC,__del__ 顺手把底层 buffer 关了 → 后续 print 全部报
# "ValueError: I/O operation on closed file"。reconfigure 是原地改,不产生新对象,躲开这个雷。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                     # noqa: BLE001
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))

# 复用鹰眼挖掘引擎已有的配额闸门异常,**不新造一份**
# (平台铁律:同一份逻辑只许有一份实现;历史上 CJK 正则出现过五份互相打架)。
# arsenal_mine._get() 里 _calls >= BUDGET 就抛这个;调度层接住它 = 干净停机并保住租约。
try:
    sys.path.insert(0, os.path.join(ROOT, "scripts", "intel_radar"))
    from arsenal_mine import BudgetExhausted          # noqa: F401  真身在那边
except Exception:                                     # noqa: BLE001
    class BudgetExhausted(Exception):
        """兜底定义:只在 arsenal_mine 不可导入时生效(比如单独跑本文件的自检)。
        真身永远是 scripts/intel_radar/arsenal_mine.BudgetExhausted。"""


# GitHub Actions 的 job 硬上限。官方限制 6 小时,到点直接杀,不发 SIGTERM 也不跑 finally。
# 用它当 run(deadline_seconds=) 的参考值时**必须留余量**:留给收尾写盘、上传 artifact。
ACTIONS_JOB_LIMIT_SECONDS = 6 * 3600

# 四条真实产线的默认优先级(数字小 = 先跑)。分级的意义在于:配额/时间中途耗尽时,
# 损失的一定是最不着急的那条,而不是"每天必须出数"的那条。
LANE_PRIO = {
    "eagle": 10,    # 鹰眼情报:GitHub API / trending / awesome 榜单,每天要出数
    "guji": 30,     # 古籍站点:海外馆藏,量大但可以跨天
    "social": 60,   # 社媒情报:强反爬,慢
    "rag": 90,      # RAG 语料:正文入库,慢慢磨
}


# ═══════════════════════════════════════════════════════════════════════
# 一、URL 规范化 —— 去重键的地基
# ═══════════════════════════════════════════════════════════════════════

# 追踪参数名单。**只列实际见过的**,不凭想象扩充:
#   utm_*        —— 通用 UTM,榜单站/自媒体转来的链接几乎都挂
#   spm / spm_id_from / vd_source —— 阿里系与 B 站分享链接
#   share_source / share_medium / share_plat / share_tag / unique_k —— B 站 App 分享
#   xhsshare / apptime —— 小红书 App 分享
#   fbclid / gclid / msclkid / yclid / igshid —— 各家广告平台点击 ID
#   ref_src / ref_url —— Twitter 嵌入
#   mc_cid / mc_eid —— Mailchimp
# 故意**没有**收进来的两个,理由值得写下来防后人手贱:
#   from      —— 太通用,真实接口里 ?from=2020-01-01 这种是业务参数,剥了就抓错页
#   timestamp —— 同上,某些馆藏接口拿它当分页/版本号
_DROP_EXACT = {
    "fbclid", "gclid", "msclkid", "yclid", "igshid", "ttclid",
    "spm", "spm_id_from", "vd_source",
    "share_source", "share_medium", "share_plat", "share_tag", "share_from",
    "unique_k", "xhsshare", "apptime", "appuid",
    "ref_src", "ref_url", "mc_cid", "mc_eid", "_hsenc", "_hsmi",
}
_DROP_PREFIX = ("utm_",)

# path/query 里的非 ASCII 要百分号编码后再入键,否则同一页写成 %E4%B8%AD 和写成中文字
# 会被判成两页。safe 里放了 '%' —— 这是为了不把**已经编码好**的 %E4 二次编码成 %25E4。
# 代价:路径里真出现一个裸 '%' 时不会被编码。实测的各馆藏/维基链接里没有这种情况,
# 真遇到再说,总比二次编码把每一条已编码 URL 都算成新页强。
_QUOTE_SAFE = "/:@!$&'()*+,;=~-._%"

# 键的长度闸。SQLite 的 TEXT 主键索引对超长串很浪费,而 data: / 带巨型 token 的 URL
# 真实存在(小红书的 xsec_token 就上百字符)。超过就砍断留可读前缀 + 全串 sha1 尾巴,
# 保证既不失去唯一性、又还能一眼看出是哪个站。480 不是实测阈值,是**保守取值**:
# 我们手上所有真实样本的 scheme+host+path 都远短于 480,砍不到能认出页面的那部分。
_KEY_READABLE_MAX = 480


def normalize_url(url, drop_params=None, keep_fragment=False):
    """把 URL 规范化成**去重键**。

    吸收自 crawlee-python `_utils/requests.py: normalize_url`(Apache-2.0),
    但对着我们的实际情况改了两处,这两处不改会出错:

      ① 它依赖第三方 yarl,我们纯 stdlib 用 urlsplit + parse_qsl + urlencode 复写。
      ② **它结尾做了整串 .lower(),这是个坑,我们不抄。**
         GitHub 的 owner/repo 大小写不敏感所以它没被咬到,但一般站点的 path 是
         大小写敏感的 —— `/Docs/A.html` 和 `/docs/a.html` 会被误判成同一页,
         表现是"某些页面永远抓不到",而且不报错。
         我们只 lower scheme 和 host(这两个按 RFC 3986 本来就不区分大小写)。

    另一条必须记住的分界:**这个函数产出的是去重键,不是要去抓的地址。**
    Scheduler 里 url 列存原样、key 列存这个。小红书的 xsec_token 一类参数
    剥掉是对的(它是会变的一次性凭据,不剥就同一页反复抓),但拿剥完的 URL
    去请求会 403 —— 所以抓取永远用原始 url。
    """
    drop = _DROP_EXACT if drop_params is None else set(drop_params)
    try:
        sp = urllib.parse.urlsplit(url.strip())
    except Exception:                                 # noqa: BLE001
        return url.strip()

    scheme = (sp.scheme or "http").lower()
    host = (sp.hostname or "").lower()
    # 默认端口要去掉,否则 https://a.com 和 https://a.com:443 是两个键
    port = sp.port
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host = "%s:%d" % (host, port)

    path = urllib.parse.quote(sp.path or "", safe=_QUOTE_SAFE)
    # 尾斜杠去掉;根路径 "/" 直接归零,这样 https://a.com 和 https://a.com/ 同键
    path = path.rstrip("/")

    # query 排序 —— 这是跨 run 稳定的关键:同一个页面被两个来源以不同参数顺序引用时,
    # 不排序就是两条。keep_blank_values 保住 `?a=&b=1` 里的 a,那可能是有意义的开关。
    pairs = []
    for k, v in urllib.parse.parse_qsl(sp.query, keep_blank_values=True):
        kl = k.lower()
        if kl in drop or any(kl.startswith(p) for p in _DROP_PREFIX):
            continue
        pairs.append((k, v))
    pairs.sort()
    query = urllib.parse.urlencode(pairs, quote_via=urllib.parse.quote)

    out = "%s://%s%s" % (scheme, host, path)
    if query:
        out += "?" + query
    # 片段(#anchor)默认丢弃:它是浏览器端锚点,服务端拿到的是同一个页面。
    # keep_fragment 留给单页应用那种真靠 hash 路由的站。
    if keep_fragment and sp.fragment:
        out += "#" + sp.fragment
    return out


# fingerprint 里参与计算的请求头白名单。抄自 crawlee(Apache-2.0)的同一份名单。
# **不能把全部 header 都算进去**:cookie / user-agent / 各种 trace id 每次都不一样,
# 算进去等于去重彻底失效,同一页每次都是"新页"。
_HDR_WHITELIST = ("accept", "accept-language", "authorization", "content-type")


def fingerprint(url, method="GET", body=b"", headers=None, verbatim=False):
    """算去重指纹。返回一个字符串,**GET 请求下就是可读的规范化 URL 本身**。

    这里做了一个和 scrapy 相反的选择,理由值得写清楚:
      scrapy 一律 sha1,拿到的是 40 位十六进制;crawlee 默认直接用规范化 URL。
      我们跟 crawlee:状态文件用 `sqlite3 xxx.db "select key from queue limit 20"`
      打开就能看见排队的是哪些页面,查"这条为什么没抓 / 为什么抓了两遍"不用写解码脚本。
      平台历史上最难查的一类事故就是"日报全绿但某个源从此不再被抓",
      可读的键是把这类盲区变成一眼可见的最便宜手段。

    只有在**方法不是 GET、或带 body、或带白名单里的 header** 时才退回哈希:
      key = 可读 URL + "#" + sha1(规范化字段 dict 的 json)[:16]

    哈希那一步照抄 scrapy `utils/request.py: fingerprint`(BSD-3-Clause)的两个要点:
      ① 用 json.dumps 而不是 f-string 拼接 —— URL 里本身就带 ':' 和 '|',
         拼接会有分隔符歧义,两条不同请求可能撞成同一个指纹;
      ② sort_keys=True —— 保证跨 Python 版本、跨 dict 插入顺序结果稳定。
         昨天和今天算出来必须是同一串,否则"续跑"就等于"全部重来"。
      ③ body 走 .hex() —— JSON 不支持 bytes,这是 scrapy 注释里写明的原因。

    verbatim=True 是逃生口(scrapy 叫 verbatim_url):query 顺序真有意义的接口用它,
    跳过规范化,原样入键。
    """
    key = url.strip() if verbatim else normalize_url(url)

    hdrs = {}
    for k, v in (headers or {}).items():
        if k.lower() in _HDR_WHITELIST:
            hdrs[k.lower()] = v
    m = (method or "GET").upper()
    if m != "GET" or body or hdrs:
        if isinstance(body, str):
            body = body.encode("utf-8")
        data = {"method": m, "url": key, "body": (body or b"").hex(), "headers": hdrs}
        digest = hashlib.sha1(json.dumps(data, sort_keys=True).encode("utf-8")).hexdigest()
        key = key + "#" + digest[:16]

    if len(key) > _KEY_READABLE_MAX:
        key = key[:_KEY_READABLE_MAX] + "#" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return key


# 键逻辑的自证探针。见 Scheduler._guard_state():每次开库都拿它算一遍,
# 和上次存进库里的值比。有人改了 normalize_url / fingerprint 而没意识到后果时,
# 这里会当场炸,而不是"旧队列的去重从此静默失效、所有页面被当成新页重抓一遍"。
_KEYING_PROBE = "HTTPS://Example.COM:443/a/b/?utm_source=x&b=2&a=1#frag"
KEYING_VERSION = 1


# ═══════════════════════════════════════════════════════════════════════
# 二、SeenLog —— scrapy requests.seen 格式的崩溃安全集合
# ═══════════════════════════════════════════════════════════════════════

class SeenLog(object):
    """只追加、自带长度的去重台账。格式照抄 scrapy `dupefilters.py`(BSD-3-Clause)。

    格式:每条 = 2 字节大端长度 + 那么多字节的 UTF-8 键。永不改写已有字节。
    读的时候逐条读 2 字节长度再读那么多字节,一旦发现读到的比声明的短,
    就认定这是**被强杀截断的半条**,直接停止读取并丢弃它 —— 前面所有完整记录都还在。
    不需要 fsync、不需要事务、不需要临时文件重命名,崩溃安全性来自格式本身。

    **这个类不是 Scheduler 的一部分,Scheduler 的去重只有 SQLite 一份实现。**
    它服务的是另一类场景:纯集合去重、不需要队列。比如 arsenal_mine 想记住
    "这个仓这轮已经挖过了",开一个 SQLite 太重。两者共用同一个 fingerprint(),
    也就是说**会漂移的那部分逻辑(键怎么算)只有一份**。

    对 scrapy 原版做了一处修正:**每写一条就 flush()。**
    原版靠 Python 文件对象的缓冲,而缓冲在用户态 —— 进程被 SIGKILL 时那一段
    直接随进程消失,能丢掉几百条。flush() 之后数据已经进内核页缓存,
    SIGKILL 杀不掉它(只有断电才丢)。我们的场景恰恰就是 SIGKILL,所以必须补这一下。
    代价是每条一次 write(2) 系统调用,实测 10 万条量级完全无感。
    """

    def __init__(self, path):
        d = os.path.dirname(os.path.abspath(path))
        if d and not os.path.isdir(d):
            os.makedirs(d)
        # 'a+b' = 追加读写。追加模式下写入**永远落在文件末尾**,不受 seek 影响,
        # 所以下面 seek(0) 去读全量是安全的。
        self._f = open(path, "a+b")
        self._f.seek(0)
        # truncated 必须**在读之前**初始化:_read_all 是生成器,读到半条时会把它置 True,
        # 写在下面就会被无条件覆盖回 False。(2026-09-02 自检当场抓到的自造 bug,
        # 留这行注释是因为这种"初始化写在消费之后"的错在生成器里特别不显眼。)
        self.truncated = False        # 上次是不是被强杀过(读到半条)
        self._set = set(self._read_all())

    def _read_all(self):
        while True:
            head = self._f.read(2)
            if len(head) < 2:
                break
            size = int.from_bytes(head, "big")
            raw = self._f.read(size)
            if len(raw) < size:
                # 源码注释原话是"被不干净的关机截断"。这里额外记一笔:
                # 这不是错误,是**上一轮被 6 小时上限杀掉**的正常痕迹。
                self.truncated = True
                break
            yield raw.decode("utf-8", "replace")

    def add(self, key):
        """加入。返回 True = 之前没见过(是新的);False = 见过了。"""
        if key in self._set:
            return False
        self._set.add(key)
        raw = key.encode("utf-8")
        if len(raw) > 65535:          # 2 字节长度头的上限,超了就存哈希
            raw = hashlib.sha1(raw).hexdigest().encode("ascii")
        self._f.write(len(raw).to_bytes(2, "big") + raw)
        self._f.flush()               # ← 这一行就是上面说的那处修正
        return True

    def seen(self, key):
        return key in self._set

    def __len__(self):
        return len(self._set)

    def __contains__(self, key):
        return key in self._set

    def close(self):
        try:
            self._f.flush()
            self._f.close()
        except Exception:             # noqa: BLE001
            pass


# ═══════════════════════════════════════════════════════════════════════
# 三、Scheduler —— 优先级队列 + 租约 + 断点续跑
# ═══════════════════════════════════════════════════════════════════════

class StateInconsistent(RuntimeError):
    """状态文件和当前代码/配置对不上。**故意抛异常而不是 warn。**

    抄的是 crawlee(TS)`request_list.ts: restoreState()` 的态度:那里三道校验
    全是 throw 不是 warn。理由:游标/键规则一旦和数据错位,表现是
    "某些源从此再也不被抓" —— 不报错、不缺文件、日报全绿,能瞒好几周
    (平台已有血证:pan-register 的缺口数字在 run log 里连喊 12 天零人响应)。
    让它在第一时间变成一条可见的失败,比让它变成一个安静的盲区便宜太多。
    """


SCHEMA_VERSION = 1

_DDL = """
CREATE TABLE IF NOT EXISTS queue (
  key         TEXT PRIMARY KEY,   -- 去重指纹,同时也是可读的规范化 URL
  url         TEXT NOT NULL,      -- 原始 URL,抓取用这个(见 normalize_url 注释)
  lane        TEXT NOT NULL DEFAULT 'default',
  prio        INTEGER NOT NULL DEFAULT 100,
  seq         INTEGER NOT NULL,   -- 同优先级内的 FIFO 序号;负数 = 插队
  meta        TEXT,               -- 随行 JSON,爱存啥存啥
  state       INTEGER NOT NULL DEFAULT 0,   -- 0 待办 / 1 已完成 / 2 已弃(重试超限)
  attempts    INTEGER NOT NULL DEFAULT 0,
  lease_until REAL,               -- 租约到期时间戳;NULL = 没人占
  client_key  TEXT,               -- 谁占的
  added_at    REAL NOT NULL,
  handled_at  REAL,
  last_error  TEXT
);
CREATE INDEX IF NOT EXISTS ix_queue_pick  ON queue(state, prio, seq);
CREATE INDEX IF NOT EXISTS ix_queue_lease ON queue(state, lease_until);
CREATE TABLE IF NOT EXISTS sched_meta (k TEXT PRIMARY KEY, v TEXT);
"""


class Request(object):
    """一条待办。故意做成薄壳,不带行为 —— 行为都在 Scheduler 上,
    免得日后有人往这里加抓取逻辑,变成第二份抓取实现。"""
    __slots__ = ("key", "url", "lane", "prio", "seq", "meta", "attempts")

    def __init__(self, key, url, lane, prio, seq, meta, attempts):
        self.key, self.url, self.lane = key, url, lane
        self.prio, self.seq, self.attempts = prio, seq, attempts
        self.meta = meta or {}

    def __repr__(self):
        return "<Request %s prio=%d try=%d %s>" % (self.lane, self.prio, self.attempts, self.url[:70])


class Scheduler(object):
    """一个 .sqlite3 文件就是全部状态。零常驻服务、零 pip。

    ── 为什么是 SQLite 而不是 scrapy 那套分优先级子目录 ──
    scrapy `pqueues.py` 用 N 个 FIFO 拼出优先级队列,磁盘上每个优先级一个子目录,
    再把"哪几个优先级还非空"写进 active.json。那套的致命处是 active.json
    **只在 close() 里写**:被 SIGKILL 就作废,表现是既丢件又重放,而且丢和重都不报错。
    SQLite 版本根本不需要那个文件 —— `ORDER BY prio ASC, seq ASC LIMIT 1` 一条 SQL
    顶掉整个 curprio 维护逻辑,状态就在表里,天然崩溃安全。
    同优先级内严格 FIFO(堆做不到,同优先级顺序随机,续跑时不可复现)。

    ── 为什么不开 WAL ──
    WAL 会把已提交但未 checkpoint 的数据留在 `-wal` 边车文件里。我们的状态要靠
    actions/cache 或 upload-artifact 在两次 run 之间传递,**通常只传主 .db**,
    边车一丢就等于丢掉最近一批提交。用默认的回滚日志模式,每次 commit 之后
    主文件本身就是完整一致的,单文件拷走即可。这是拿一点写入性能换可搬运性,
    对我们(几万条量级、批量提交)划算。
    """

    def __init__(self, db_path, client_key=None, lease_seconds=900,
                 max_attempts=3, config_fingerprint=None, allow_config_change=False):
        """
        lease_seconds: 租约时长。crawlee 用 300 秒,**对我们偏短**:
            抓一个大 awesome 仓要拉十几个 md 文件,单条处理可能超过 5 分钟,
            租约短于处理时间会导致同一条被两个 shard 同时干。
            这里默认 900 秒(15 分钟),仍然是**保守估计不是实测值** ——
            接产线时应当按该产线单条的实测 P95 耗时定,并在这里写下那个数字。
        max_attempts: 同一条被认领几次之后放弃。注意计数是在**认领时**加的,
            不是在失败时加 —— 因为进程被 SIGKILL 时根本没有"失败"这个事件,
            只在失败时计数的话,一条会让进程崩溃的毒药 URL 会永远循环下去。
        config_fingerprint: 种子/配置的指纹。变了就拒绝静默续跑,见 _guard_state()。
        """
        d = os.path.dirname(os.path.abspath(db_path))
        if d and not os.path.isdir(d):
            os.makedirs(d)
        self.path = db_path
        self.client_key = client_key or hashlib.sha1(
            ("%s-%s-%s" % (os.getpid(), time.time(), os.urandom(8).hex())).encode()).hexdigest()[:32]
        self.lease_seconds = float(lease_seconds)
        self.max_attempts = int(max_attempts)
        self.db = sqlite3.connect(db_path, timeout=30.0)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_DDL)
        self.db.commit()
        self._guard_state(config_fingerprint, allow_config_change)

    # ── 状态一致性护栏 ────────────────────────────────────────────────
    def _guard_state(self, config_fp, allow_change):
        """开库时的三道校验。对不上就抛,绝不静默续跑。

        抄自 crawlee(TS)`request_list.ts: restoreState()` 的三道 throw,
        换成适合我们存储的三条:

          ① schema 版本:表结构变过,老库直接拒绝,免得按新代码读出错位的语义
          ② 键规则探针:拿 _KEYING_PROBE 现算一遍指纹,和上次存的比。
             有人改了 normalize_url 却没想到后果时,这里当场炸 —— 否则后果是
             "旧队列里所有 key 都对不上,全站被当成新页重抓一遍",而且没有任何报错
          ③ 配置指纹:种子表 / 榜单来源变了,由调用方决定是接着跑还是换新库
        """
        cur = self.db.execute("SELECT k,v FROM sched_meta")
        got = {r["k"]: r["v"] for r in cur.fetchall()}

        sv = got.get("schema_version")
        if sv is not None and int(sv) != SCHEMA_VERSION:
            raise StateInconsistent(
                "状态文件 schema 版本 %s,当前代码要求 %d —— 拒绝续跑。"
                "换一个新的 db 文件,或者写迁移脚本。" % (sv, SCHEMA_VERSION))

        probe_now = "%d:%s" % (KEYING_VERSION, fingerprint(_KEYING_PROBE))
        probe_old = got.get("keying_probe")
        if probe_old is not None and probe_old != probe_now:
            raise StateInconsistent(
                "去重键规则变了(库里 %r,现在 %r)。这意味着旧库里每一条 key 都对不上,"
                "接着跑会把所有页面当成新页重抓一遍且不报错 —— 所以在这里停。"
                "确认要换规则就 KEYING_VERSION+1 并换新 db 文件。" % (probe_old, probe_now))

        cfg_old = got.get("config_fp")
        if config_fp is not None and cfg_old is not None and cfg_old != config_fp and not allow_change:
            raise StateInconsistent(
                "配置指纹变了(库里 %s,现在 %s)。种子/来源换过之后接着跑,"
                "很可能有源从此再也轮不到 —— 确认无妨就传 allow_config_change=True。"
                % (cfg_old[:16], config_fp[:16]))

        rows = [("schema_version", str(SCHEMA_VERSION)), ("keying_probe", probe_now)]
        if config_fp is not None:
            rows.append(("config_fp", config_fp))
        if "created_at" not in got:
            rows.append(("created_at", str(time.time())))
        self.db.executemany("INSERT OR REPLACE INTO sched_meta(k,v) VALUES(?,?)", rows)
        self.db.commit()

    # ── 序号 ──────────────────────────────────────────────────────────
    def _next_seq(self, n=1):
        """取 n 个连续序号。序号只保证单调递增,不保证连续无洞(中途回滚会留洞),
        这没关系 —— 它只用来在同优先级内定 FIFO 顺序。"""
        cur = self.db.execute("SELECT v FROM sched_meta WHERE k='seq'")
        row = cur.fetchone()
        start = int(row["v"]) if row else 1
        self.db.execute("INSERT OR REPLACE INTO sched_meta(k,v) VALUES('seq',?)", (str(start + n),))
        return start

    # ── 入队 ──────────────────────────────────────────────────────────
    def push(self, url, lane="default", prio=None, meta=None, front=False,
             method="GET", body=b"", headers=None, verbatim=False):
        """入队一条。返回 True = 真的新加了;False = 早就有了(幂等)。"""
        return self.push_many([{
            "url": url, "lane": lane, "prio": prio, "meta": meta, "front": front,
            "method": method, "body": body, "headers": headers, "verbatim": verbatim,
        }]) == 1

    def push_many(self, items):
        """批量入队。**返回值 = 这一轮真正新发现的条数**。

        幂等靠的是主键冲突忽略(`INSERT OR IGNORE`),而不是在应用层再维护一个
        "见过的集合" —— 抄自 crawlee `_sql/_request_queue_client.py`。
        这么做把"去重"和"幂等"合并成同一个机制,少一份状态就少一类不一致。

        返回的新增数是个**该往上报的哨兵数字**:同一个 workflow 重跑,第二次应当返回 0;
        如果它天天非零且数量相当,说明键算错了(每次都当成新页),
        这种事故躺在 run log 里没人看得出来,报到日报/Issue 里一眼就发现。
        """
        rows, now = [], time.time()
        seq0 = self._next_seq(len(items))
        for i, it in enumerate(items):
            url = it["url"]
            key = fingerprint(url, it.get("method") or "GET", it.get("body") or b"",
                              it.get("headers"), it.get("verbatim") or False)
            lane = it.get("lane") or "default"
            prio = it.get("prio")
            if prio is None:
                prio = LANE_PRIO.get(lane, 100)
            seq = seq0 + i
            if it.get("front"):
                # 插队:用负序号。同一优先级里负数排在正数前面,一条 ORDER BY 就搞定,
                # 不需要额外的"队首队列"。
                seq = -seq
            rows.append((key, url, lane, int(prio), seq,
                         json.dumps(it.get("meta") or {}, ensure_ascii=False), now))

        before = self.db.total_changes
        self.db.executemany(
            "INSERT OR IGNORE INTO queue(key,url,lane,prio,seq,meta,added_at) "
            "VALUES(?,?,?,?,?,?,?)", rows)
        self.db.commit()
        # executemany 之后 cursor.rowcount 在各版本上语义不统一,用连接级的
        # total_changes 差值才靠谱(实测 Python 3.12.6 / SQLite 3.45.3)。
        return self.db.total_changes - before

    # ── 出队(租约认领)────────────────────────────────────────────────
    def fetch(self, n=1):
        """认领最多 n 条。返回 [Request]。

        租约取代锁,抄自 crawlee `_sql/_request_queue_client.py`(Apache-2.0)。
        为什么是租约不是"开机时把 in_progress 清空":后者假设同时只有一个客户端,
        而租约不需要任何假设 —— runner 被 SIGKILL、没人清理,租约到期后那一条
        自己就可被别人领走。直接好处两个:
          ① 6 小时超时不需要在 workflow 里写任何 cleanup step
          ② 以后开 Actions 矩阵并行(4 个 shard 同抓一个队列),这套代码不用改

        认领的原子性靠 compare-and-swap:SELECT 只是**选候选**,
        真正的判据是 UPDATE 的 WHERE 子句(把同一组谓词再写一遍)。
        crawlee 那边用 `.returning()` 拿回抢到的 id;我们用 cursor.rowcount 判断
        —— 单行 UPDATE 的 rowcount 语义在所有 sqlite3 版本上都是明确的,
        这样连 RETURNING 需要 SQLite ≥ 3.35 这个版本门槛都省了
        (本机实测 3.45.3 支持,但 runner 上不必赌)。
        """
        now = time.time()
        cur = self.db.execute(
            "SELECT key,url,lane,prio,seq,meta,attempts FROM queue "
            "WHERE state=0 AND attempts < ? "
            "  AND (lease_until IS NULL OR lease_until < ?) "
            "ORDER BY prio ASC, seq ASC LIMIT ?",
            (self.max_attempts, now, max(1, n) * 3))     # 多选些候选,抢不到还有备胎
        cands = cur.fetchall()

        got = []
        for r in cands:
            if len(got) >= n:
                break
            c = self.db.execute(
                "UPDATE queue SET lease_until=?, client_key=?, attempts=attempts+1 "
                "WHERE key=? AND state=0 AND attempts < ? "
                "  AND (lease_until IS NULL OR lease_until < ?)",
                (now + self.lease_seconds, self.client_key, r["key"], self.max_attempts, now))
            if c.rowcount == 1:
                got.append(Request(r["key"], r["url"], r["lane"], r["prio"], r["seq"],
                                   json.loads(r["meta"] or "{}"), r["attempts"] + 1))
        self.db.commit()
        return got

    def fetch_one(self):
        g = self.fetch(1)
        return g[0] if g else None

    # ── 收尾三态 ──────────────────────────────────────────────────────
    def done(self, req):
        """标记完成。清租约。**下游写入必须自己也幂等**(D1 用 INSERT OR IGNORE /
        UPSERT,别用裸 INSERT)—— 因为本调度的语义是"至少一次":
        处理完了但还没来得及 done 就被杀,下次会重跑这一条。"""
        self.db.execute(
            "UPDATE queue SET state=1, handled_at=?, lease_until=NULL, client_key=NULL, "
            "last_error=NULL WHERE key=?", (time.time(), req.key))
        self.db.commit()

    def fail(self, req, err="", retry=True):
        """失败。retry=True 就放回待办(租约立刻释放,让别人/下轮能拿);
        retry=False 直接判死。attempts 已经在认领时加过了,这里不再加。"""
        if retry and req.attempts < self.max_attempts:
            self.db.execute(
                "UPDATE queue SET lease_until=NULL, client_key=NULL, last_error=? WHERE key=?",
                (str(err)[:400], req.key))
        else:
            self.db.execute(
                "UPDATE queue SET state=2, lease_until=NULL, client_key=NULL, "
                "last_error=?, handled_at=? WHERE key=?",
                (str(err)[:400], time.time(), req.key))
        self.db.commit()

    def reclaim(self, req):
        """主动交还(不算失败、不加计数)。到点收工、配额耗尽时用它 ——
        这条会立刻回到待办队首附近,下一轮 run 优先拿到。"""
        self.db.execute(
            "UPDATE queue SET lease_until=NULL, client_key=NULL, "
            "attempts=CASE WHEN attempts>0 THEN attempts-1 ELSE 0 END WHERE key=?", (req.key,))
        self.db.commit()

    # ── 观测 ──────────────────────────────────────────────────────────
    def stats(self):
        """给日报/哨兵用的数字。**这些数要往 Issue / 日报上报,别只写进 run log**
        (平台铁律:只写进日志的产线一律视为没人看 —— pan-register 的缺口数字
         在 run log 里连喊 12 天零人响应)。"""
        now = time.time()
        q = lambda sql, *a: self.db.execute(sql, a).fetchone()[0]      # noqa: E731
        return {
            "total": q("SELECT COUNT(*) FROM queue"),
            "pending": q("SELECT COUNT(*) FROM queue WHERE state=0 AND attempts<?", self.max_attempts),
            "leased": q("SELECT COUNT(*) FROM queue WHERE state=0 AND lease_until IS NOT NULL "
                        "AND lease_until >= ?", now),
            "done": q("SELECT COUNT(*) FROM queue WHERE state=1"),
            "dead": q("SELECT COUNT(*) FROM queue WHERE state=2"),
            # 毒药:还没被判死、但重试次数已经用光的。它不会再被认领,
            # 静默留在库里就是个黑洞,所以单独数出来报上去。
            "poisoned": q("SELECT COUNT(*) FROM queue WHERE state=0 AND attempts>=?", self.max_attempts),
            "lanes": {r["lane"]: r["n"] for r in self.db.execute(
                "SELECT lane, COUNT(*) n FROM queue WHERE state=0 GROUP BY lane").fetchall()},
        }

    def dump(self, limit=20, state=0):
        """人眼查库用。可读的 key 在这里体现价值:直接看得出排的是哪些页面。"""
        return [dict(r) for r in self.db.execute(
            "SELECT key,url,lane,prio,seq,state,attempts,last_error FROM queue "
            "WHERE state=? ORDER BY prio ASC, seq ASC LIMIT ?", (state, limit)).fetchall()]

    # ── 主循环 ────────────────────────────────────────────────────────
    def run(self, handler, deadline_seconds=None, max_items=None,
            sleep_between=0.0, on_progress=None, verbose=True):
        """跑到没活、到点、或者配额耗尽为止。返回一份计数。

        handler(req) 的约定:
          返回 None / True        → 算完成
          返回 False              → 算失败,还会重试
          返回 list               → 算完成,并把列表里的东西当新发现入队
                                    (元素是 str 或 {"url":..,"lane":..,"prio":..,"meta":..})
          抛 BudgetExhausted      → 交还当前这条,干净停机(不算失败)
          抛其它异常              → 算失败,按 max_attempts 决定是否还重试

        deadline_seconds 是**这一轮自己给自己设的上限**,要比 Actions 的 6 小时
        硬上限小,留出收尾写盘和上传 artifact 的时间。到点主动停,是为了让状态
        停在干净的地方;真被 SIGKILL 也不会坏账(租约会过期),但那样会白扔
        正在处理的那一条的进度。
        """
        t0 = time.time()
        deadline = (t0 + float(deadline_seconds)) if deadline_seconds else None
        n_done = n_fail = n_new = 0
        stop = {"flag": False, "why": ""}

        def _sig(signum, frame):                        # noqa: ARG001
            # Actions 取消 job 时先发 SIGINT/SIGTERM 再 SIGKILL,能接住就干净收工。
            # 接不住(直接 SIGKILL)也不会坏账 —— 那是租约存在的意义。
            stop["flag"] = True
            stop["why"] = "收到信号 %s" % signum

        old = {}
        for s in ("SIGTERM", "SIGINT"):
            sig = getattr(signal, s, None)
            if sig is None:
                continue
            try:
                old[sig] = signal.signal(sig, _sig)
            except (ValueError, OSError):               # 不在主线程就算了
                pass

        try:
            while True:
                if stop["flag"]:
                    break
                if max_items and (n_done + n_fail) >= max_items:
                    stop["why"] = "达到 max_items=%d" % max_items
                    break
                if deadline and time.time() >= deadline:
                    stop["why"] = "到达本轮时限 %.0fs" % (time.time() - t0)
                    break

                req = self.fetch_one()
                if req is None:
                    stop["why"] = stop["why"] or "队列空了"
                    break

                try:
                    out = handler(req)
                except BudgetExhausted as e:
                    self.reclaim(req)
                    stop["flag"] = True
                    stop["why"] = "配额耗尽:%s" % str(e)[:120]
                    break
                except Exception as e:                  # noqa: BLE001
                    self.fail(req, "%s: %s" % (type(e).__name__, str(e)[:200]))
                    n_fail += 1
                    if verbose:
                        print("  ✗ %s  %s" % (req.url[:60], str(e)[:70]))
                    continue

                if out is False:
                    self.fail(req, "handler 返回 False", retry=False)
                    n_fail += 1
                else:
                    if isinstance(out, (list, tuple)):
                        items = []
                        for x in out:
                            items.append({"url": x} if isinstance(x, str) else dict(x))
                        if items:
                            n_new += self.push_many(items)
                    self.done(req)
                    n_done += 1
                if on_progress:
                    on_progress(req, n_done, n_fail)
                if sleep_between:
                    time.sleep(sleep_between)
        finally:
            for sig, h in old.items():
                try:
                    signal.signal(sig, h)
                except Exception:                       # noqa: BLE001
                    pass

        st = self.stats()
        res = {"done": n_done, "failed": n_fail, "new_pushed": n_new,
               "elapsed": round(time.time() - t0, 1), "stopped_because": stop["why"] or "跑完",
               "pending_left": st["pending"], "poisoned": st["poisoned"], "dead": st["dead"]}
        if verbose:
            print("  ── 本轮:完成 %d · 失败 %d · 新入队 %d · 用时 %.1fs · 停因「%s」"
                  % (res["done"], res["failed"], res["new_pushed"], res["elapsed"], res["stopped_because"]))
            print("  ── 队列:待办 %d · 已完成 %d · 已弃 %d · 毒药 %d"
                  % (st["pending"], st["done"], st["dead"], st["poisoned"]))
            if st["poisoned"]:
                # 毒药数不为零必须显式喊出来,别让它安静躺着
                print("  ⚠ 有 %d 条重试用尽仍未完成,拿 dump(state=0) 看 last_error" % st["poisoned"])
        return res

    def close(self):
        try:
            self.db.commit()
            self.db.close()
        except Exception:                               # noqa: BLE001
            pass


def config_fingerprint(obj):
    """把种子表 / 来源配置算成一个指纹,喂给 Scheduler(config_fingerprint=)。
    用的还是 json.dumps(sort_keys=True) 那一套,理由同 fingerprint()。"""
    return hashlib.sha1(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


# ═══════════════════════════════════════════════════════════════════════
# 为什么不用 scrapy 的 JOBDIR(读源码读出来的,写下来防后人再走一遍)
# ═══════════════════════════════════════════════════════════════════════
# queuelib `FifoDiskQueue` 把数据写进 q00000/q00001 分块文件(这部分确实落盘),
# 但 head/tail/size 三个游标只活在内存 dict 里,唯一写盘的地方是 close() 里的
# _saveinfo()。上层 scrapy Scheduler 的 _write_dqs_state() 写 active.json 同样只在
# close() 里调。于是进程被 SIGKILL 时:
#     · 本轮 push 进去的数据在 chunk 文件里躺着,但 head 不知道 → 读不出来(丢件)
#     · 本轮已 pop 并处理完的,tail 没前进 → 下次全部重新弹出(重放)
# 而且丢和重都不报错,查都没处查。scrapy 文档里 JOBDIR 的前提就是 Ctrl-C 优雅退出,
# 而 GitHub Actions 6 小时到点是 SIGKILL,没有优雅退出这回事。
# 所以照抄 JOBDIR 会得到一个"表面上有断点续跑、实际每次超时静默丢一批 + 重跑一批"的东西。
# 同一个仓里的 FifoSQLiteQueue 才是对的那份,本文件抄的是它。
# ═══════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════
# 自检:真跑,不是断言演习
# ═══════════════════════════════════════════════════════════════════════

def _demo_handler(req):
    """演示用的 handler:真发 HTTP、真提中文正文。

    **正文提取的生产实现在 scripts/intel_radar/arsenal_enrich.py: gh_readme()**
    (那边已经用 trafilatura 做兜底)。这里只是自检要证明"调度层真能驱动真活",
    不是第二份提取实现 —— 接产线时 handler 应当直接调那边的函数。
    """
    import urllib.request
    r = urllib.request.Request(req.url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml"})
    html = urllib.request.urlopen(r, timeout=40).read().decode("utf-8", "replace")
    text = ""
    try:
        import trafilatura
        text = trafilatura.extract(html, include_comments=False, favor_precision=True) or ""
    except Exception:                                   # noqa: BLE001
        text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    print("    ✓ %-8s %6d 字节 → 正文 %5d 字(中文 %d):%s"
          % (req.lane, len(html), len(text), cjk, text[:60]))
    return None


def _selftest():
    scratch = os.environ.get("SCHEDULE_SELFTEST_DIR") or os.path.join(HERE, "_selftest")
    if not os.path.isdir(scratch):
        os.makedirs(scratch)
    db = os.path.join(scratch, "queue.sqlite3")
    for p in (db, db + "-journal"):
        if os.path.isfile(p):
            os.remove(p)

    print("═" * 74)
    print("schedule.py 自检 · db=%s" % db)
    print("═" * 74)

    cfg_fp = config_fingerprint({"seeds": ["gov.cn", "zh.wikipedia.org"], "v": 1})

    # ── 1. 规范化与指纹 ──
    print("\n① 规范化 / 指纹(去重键的地基)")
    samples = [
        "https://www.gov.cn/",
        "https://WWW.GOV.CN:443/?utm_source=weixin&utm_medium=x",   # 应与上一条同键
        "https://zh.wikipedia.org/wiki/%E4%B8%AD%E8%8D%AF",
        "https://zh.wikipedia.org/wiki/%E4%B8%AD%E8%8D%AF#/media/File:x.jpg",  # 同键
        "https://www.bilibili.com/video/BV1x?spm_id_from=333.999&vd_source=abc&p=2",
    ]
    for s in samples:
        print("    %-72s → %s" % (s[:72], fingerprint(s)))
    assert fingerprint(samples[0]) == fingerprint(samples[1]), "utm/大小写/默认端口没归一"
    assert fingerprint(samples[2]) == fingerprint(samples[3]), "锚点没剥"
    assert fingerprint(samples[4]).endswith("?p=2"), "B站追踪参数没剥干净"
    # 大小写敏感的 path 不许被合并(这是我们和 crawlee 的分歧点,必须验)
    assert fingerprint("https://a.com/Docs/A.html") != fingerprint("https://a.com/docs/a.html"), \
        "path 被 lower 了,会误判成同一页"
    print("    断言通过:utm/端口/大小写主机/锚点归一,path 大小写保持敏感")

    # ── 2. 幂等入队 ──
    print("\n② 幂等入队(同一批推两遍,第二遍必须是 0 条新增)")
    sch = Scheduler(db, lease_seconds=900, max_attempts=3, config_fingerprint=cfg_fp)
    batch = [
        {"url": "https://zh.wikipedia.org/wiki/%E4%B8%AD%E8%8D%AF", "lane": "rag"},
        {"url": "https://gufangai.com/", "lane": "eagle"},
        # 与上一条同一个页面,只是带了 utm + 大写主机 + 尾斜杠 —— 必须被判成重复
        {"url": "https://GUFANGAI.com/?utm_source=weixin", "lane": "eagle"},
        # 故意的坏地址:演示"重试到用尽 → 判死",顺便证明失败不会卡住整轮
        {"url": "https://no-such-host.invalid/x", "lane": "social"},
    ]
    a = sch.push_many(batch)
    b = sch.push_many(batch)
    print("    第一遍新增 %d 条(4 条里有 1 条是同页不同参)· 第二遍新增 %d 条" % (a, b))
    assert a == 3 and b == 0, "幂等没做到:a=%s b=%s" % (a, b)

    # ── 3. 优先级 ──
    print("\n③ 优先级(eagle=10 必须排在 social=60 / rag=90 前面)")
    head = sch.dump(limit=4)
    for r in head:
        print("    prio=%-4d seq=%-4d %-6s %s" % (r["prio"], r["seq"], r["lane"], r["url"][:56]))
    assert head[0]["lane"] == "eagle", "优先级没生效"

    # ── 4. 真跑:发真请求、提真中文正文 ──
    print("\n④ 真跑一轮(真发 HTTP + trafilatura 提中文正文)")
    res = sch.run(_demo_handler, deadline_seconds=120, sleep_between=0.5)

    # ── 5. 模拟 SIGKILL:认领后不收尾,看下一轮能不能靠租约过期把它捞回来 ──
    print("\n⑤ 模拟被 SIGKILL(认领了但没 done,状态文件里留着一份未到期租约)")
    sch.lease_seconds = 1.0           # 只为自检把租约压到 1 秒;生产默认 900
    sch.push("https://zh.wikipedia.org/wiki/%E4%B8%AD%E5%8C%BB", lane="rag")
    got = sch.fetch_one()
    print("    认领:%s(租约 %.0fs,attempts=%d)" % (got.url, sch.lease_seconds, got.attempts))
    st = sch.stats()
    print("    此刻:待办 %d · 被占 %d" % (st["pending"], st["leased"]))
    assert st["leased"] == 1, "租约没记上"
    sch.close()                       # ← 故意不 done、不 reclaim,等同于进程被 SIGKILL

    print("    ── 等租约自然过期(不做任何 cleanup,这正是它的意义)──")
    time.sleep(1.4)
    sch2 = Scheduler(db, lease_seconds=900, max_attempts=3, config_fingerprint=cfg_fp)
    back = sch2.fetch(10)
    assert got.key in [r.key for r in back], "租约过期后没能被重新领走"
    print("    回收成功:租约过期的那条重新出现在可认领集合里(本轮共认领 %d 条)" % len(back))
    # 把这一轮领到的都做掉,给下一步的幂等验证一个干净起点
    for r in back:
        sch2.done(r)

    # ── 6. 断点续跑 + 幂等重跑 ──
    print("\n⑥ 重跑(幂等:已完成的一条都不许再做)")
    before = sch2.stats()["pending"]
    calls = {"n": 0}

    def _count_handler(req):
        calls["n"] += 1
        return None
    sch2.run(_count_handler, deadline_seconds=30, verbose=False)
    print("    重跑前待办 %d 条 → handler 被调用 %d 次(已完成的 %d 条一次没碰)"
          % (before, calls["n"], sch2.stats()["done"]))
    assert calls["n"] == before, "重跑动了已完成的条目,幂等破了"

    # ── 7. 状态一致性护栏 ──
    print("\n⑦ 护栏(键规则/配置变了必须炸,不许静默续跑)")
    sch2.db.execute("INSERT OR REPLACE INTO sched_meta(k,v) VALUES('keying_probe','1:偷偷改过')")
    sch2.db.commit()
    sch2.close()
    try:
        Scheduler(db, config_fingerprint=cfg_fp)
        raise AssertionError("护栏没拦住 —— 这正是最危险的情况")
    except StateInconsistent as e:
        print("    如期拦下:%s" % str(e)[:88])
    # 复原,顺便验第二道护栏(配置指纹)
    fix = sqlite3.connect(db)
    fix.execute("INSERT OR REPLACE INTO sched_meta(k,v) VALUES('keying_probe',?)",
                ("%d:%s" % (KEYING_VERSION, fingerprint(_KEYING_PROBE)),))
    fix.commit(); fix.close()
    try:
        Scheduler(db, config_fingerprint=config_fingerprint({"seeds": ["换了"], "v": 2}))
        raise AssertionError("配置指纹护栏没拦住")
    except StateInconsistent as e:
        print("    如期拦下:%s" % str(e)[:88])

    # ── 8. SeenLog(scrapy requests.seen 格式)──
    print("\n⑧ SeenLog:只追加台账 + 半条截断的崩溃恢复")
    sp = os.path.join(scratch, "seen.log")
    if os.path.isfile(sp):
        os.remove(sp)
    sl = SeenLog(sp)
    print("    新增:%s %s %s" % (sl.add(fingerprint("https://a.com/1")),
                                 sl.add(fingerprint("https://a.com/2")),
                                 sl.add(fingerprint("https://A.com/1/?utm_x=1"))))
    n_before = len(sl)
    sl.close()
    with open(sp, "ab") as f:                 # 人为造一条被强杀截断的半条记录
        f.write((40).to_bytes(2, "big") + b"half-written")
    sl2 = SeenLog(sp)
    print("    文件 %d 字节 · 重开后完整记录 %d 条(截断标记 %s)"
          % (os.path.getsize(sp), len(sl2), sl2.truncated))
    assert len(sl2) == n_before == 2 and sl2.truncated, "截断恢复不对"
    sl2.close()

    # ── 收尾 ──
    sch3 = Scheduler(db, config_fingerprint=config_fingerprint({"seeds": ["换了"], "v": 2}),
                     allow_config_change=True)
    print("\n⑨ 最终队列状态(这些数字是要往日报/Issue 上报的哨兵值)")
    print("    %s" % json.dumps(sch3.stats(), ensure_ascii=False))
    print("    状态文件 %d 字节 —— 全部断点续跑状态就这一个文件,可直接进 actions/cache"
          % os.path.getsize(db))
    sch3.close()
    print("\n══ 自检全部通过 ══")
    return 0


if __name__ == "__main__":
    sys.exit(_selftest())
