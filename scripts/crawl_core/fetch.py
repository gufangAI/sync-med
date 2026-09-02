#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""取页层 —— 一个 fetch(url) 就稳定拿到页面。

这是"抓取核心"四根柱子里的第一根:重试 / 退避 / 代理轮换 / 指纹。
上面所有产线(鹰眼情报、古籍站、RAG 语料、将来的社媒)都从这里出网,
**取页逻辑只许有这一份实现**。

═══════════════════════════════════════════════════════════════════════
吸收来源与许可(逐条对应到本文件里的哪个函数/类)
═══════════════════════════════════════════════════════════════════════
① apify/crawlee —— Apache-2.0(允许抄代码,须注明来源)
   · packages/core/src/http.ts :: parseRetryAfterHeader
       → 本文件 parse_retry_after()。数字秒 + HTTP-date 双形态,0/负数/过期一律 None。
   · packages/core/src/storages/throttling_request_manager.ts :: #recordRateLimit
       → 本文件 DomainSlot.record_rate_limit()。429 是**域级状态机**,含突发抑制与退避衰减。
   · packages/basic-crawler/src/internals/basic-crawler.ts :: canRequestBeRetried /
     errorAbsolvesSession / requestFunctionErrorHandler
       → 本文件的四类异常与 _dispatch()。429 推域级退避、403 才退休会话。
   · packages/core/src/session_pool/session.ts
       → 本文件 Session / SessionPool。errorScore 半衰治愈、maxUsageCount 到点主动换身份。
   · packages/core/src/session_pool/fingerprint.ts + packages/impit-client/src/index.ts
       → 本文件 _PROFILES / _VERSIONS / Session.build_headers()。
         合法组合表 + 具体版本号 + **按会话钉死不逐请求重掷**。
   · packages/utils/src/internals/blocked.ts
       → 本文件 ROTATE_PROXY_MARKS(代理级错误串)与 looks_like_challenge()(挑战页特征)。
② scrapy/scrapy —— BSD-3-Clause(允许抄代码,须注明来源)
   · scrapy/downloadermiddlewares/retry.py :: get_retry_request
       → 本文件 RetryQueue.retry()。重试 = 降优先级重新入队,不是原地 sleep 再撞。
   · scrapy/settings/default_settings.py :: RETRY_HTTP_CODES 等默认值
       → 本文件 RETRY_STATUS / NON_RETRY_STATUS(522/524 是 Cloudflare 源站超时,原表里有,我原来漏了)。
   · scrapy/core/downloader/__init__.py :: Slot / download_delay
       → 本文件 DomainSlot 的抖动间隔(固定间隔本身就是机器特征)。
   · scrapy/extensions/throttle.py :: _adjust_delay
       → 本文件 DomainSlot.observe_latency()。含那条最值钱的守门:非 200 不许把间隔调小。
③ gocolly/colly —— Apache-2.0(允许抄代码,须注明来源)
   · http_backend.go :: LimitRule / Do 的 defer 次序
       → 本文件 DomainSlot 的 next_allowed 语义:间隔卡在"上次收完 → 下次发出"之间。
④ firecrawl —— AGPL-3.0(传染性许可)
   **本文件不含 firecrawl 的任何代码,也未借鉴其任何实现细节。** 一行都没抄。
   (红线:AGPL 项目只许读架构,本模块连架构也没用到它。)

═══════════════════════════════════════════════════════════════════════
它治的是我们自己的什么病(不是泛泛的"更健壮")
═══════════════════════════════════════════════════════════════════════
· scripts/intel_radar/arsenal_mine.py 的 _get() 里写的是
      int(e.headers.get('Retry-After') or 0)
  包在 try 里。GitHub / Cloudflare 发 HTTP-date 形态时 int() 抛异常被吞、wait 归 0,
  最后落到写死的 min(wait or 20, 90) 兜底等 20 秒。
  **服务端明明告诉了我们该等多久,我们从来没读到过。** parse_retry_after() 补的就是这个洞。
· 同一批 URL 打同一个域时,每个请求各自 sleep 各自计数,并发一高就同时挨 429 同时各退各的。
  改成域级一份状态 + 突发抑制之后,一次限流事件只推进一次指数。
· 「国内直连 github.com 超时、本机代理 1082、Actions 上不要代理」这件事,
  在这里是 Session 的一个属性,取页代码一行都不分叉(见 default_proxies)。

红线:纯 stdlib,不装浏览器,不跑模型,不碰 R2/D1。
可选增强 curl_cffi(TLS 指纹)未启用,原因见 HONEST_GAPS。
"""
from __future__ import annotations

import contextlib
import datetime
import email.utils
import gzip
import io
import json
import os
import random
import re
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field
from http.cookiejar import CookieJar

# 平台铁律:脚本自带 stdout 自救头。中文经 Windows 控制台的 GBK 边界会被静默改写,
# 这一行保证我们打印的是 UTF-8 字节,乱码只会乱在显示层、不会污染写进文件的数据。
#
# **必须用 reconfigure,不能用 sys.stdout = io.TextIOWrapper(sys.stdout.buffer, ...)** ——
# 这是同批柱子里 schedule.py 实测踩到、我直接接住的坑(2026-09-02):
# arsenal_mine 模块级已经包过一层,本模块被它 import 时再包一层,
# 中间那个 TextIOWrapper 没人引用被 GC,__del__ 顺手把底层 buffer 关掉,
# 之后所有 print 报 "ValueError: I/O operation on closed file"。
# reconfigure 是原地改,不产生新对象,躲开这个雷。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                             # noqa: BLE001
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


# ═══════════════════════════════════════════════════════════════════════
# 一、错误分类 —— 判据是「这次失败到底怪谁」
#     来源:crawlee basic-crawler.ts(canRequestBeRetried / errorAbsolvesSession)
# ═══════════════════════════════════════════════════════════════════════

class FetchError(Exception):
    """取页层错误的根。"""


class CriticalError(FetchError):
    """怪我们自己:配置错、代码错。重试一万次也没用 —— 直接向外抛,整个任务终止。"""


class NonRetryableError(FetchError):
    """怪这个 URL:404、410 这类。本请求判死,不再重试,但**不影响会话、不影响域**。"""


class SessionError(FetchError):
    """怪这套身份:403 封禁、代理连不上、超时。
    处置是「既退休会话又重试请求」—— 换一套身份(新指纹 + 下一个代理)重来还有戏。"""


class RequestThrottledError(FetchError):
    """怪这个域:429 / 限流。

    这是全篇最反直觉的一条分界:429 和 403 都是 4xx、都在通常的 blocked 表里,
    但一个该**等**、一个该**换身份**。crawlee 的注释写得很直白:
    a rate limit is a property of the domain —— 所以 429 不给会话记黑分、不触发换代理,
    混为一谈会让我们在被限流时白白烧掉代理池。
    """

    def __init__(self, msg, delay=None):
        super().__init__(msg)
        self.delay = delay          # 服务端指示的等待秒数(可能为 None)


class BrowserRequiredError(NonRetryableError):
    """这个站点纯 HTTP 拿不下来了(Cloudflare Turnstile / Incapsula 挑战页)。

    故意做成**不可重试**:Turnstile 就是拿来挡非浏览器的,换 UA 换代理都过不去。
    命中它是「该上浏览器方案了」的客观证据,不是「再多轮换几次」的信号。
    """


class BudgetExhausted(FetchError):
    """配额用尽。

    沿用 arsenal_mine.py 里已有的语义(故意用异常而不是静默返回空):
    静默返回空会让调用方以为"这条矿脉没东西",跟真的没东西分不开。
    """


# scrapy default_settings.py 的 RETRY_HTTP_CODES 原表。
# 522/524 是 Cloudflare 的源站超时 —— 我原来根本没往重试表里放。
RETRY_STATUS = frozenset({500, 502, 503, 504, 522, 524, 408, 429})
# 明确**不重试**的:URL 本身的问题,重试多少次都还是这个结果。
NON_RETRY_STATUS = frozenset({400, 404, 405, 410, 414, 451})
# 会话级(封禁):换身份重来还有戏。注意 429 **不在**这里,它归域级退避。
SESSION_STATUS = frozenset({401, 403, 407})


# ═══════════════════════════════════════════════════════════════════════
# 二、Retry-After 解析
#     来源:crawlee packages/core/src/http.ts :: parseRetryAfterHeader(Apache-2.0)
# ═══════════════════════════════════════════════════════════════════════

_DIGITS_ONLY = re.compile(r"^\d+$")


def parse_retry_after(raw, now=None):
    """把 Retry-After 头解析成"还要等几秒";拿不出有效延迟一律返回 None。

    为什么 `Retry-After: 0` 必须返回 None 而不是 0(crawlee 作者在注释里点明的):
    返回 0 会让调用方**白白把请求推迟一次、又立刻原样重发**,而这次仍被计成一次限流事件
    —— 等于既没退避又污染了计数。返回 None 让调用方走自己的指数退避,是对的。

    两种形态都要认(RFC 9110 允许):
      · delay-seconds:纯数字。负数/小数按规范都不算 delay-seconds,落到下一分支。
      · HTTP-date:GitHub、Cloudflare 都会发这种;我们原来的 int() 在这里抛异常被吞掉。
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if _DIGITS_ONLY.match(s):
        secs = int(s)
        return float(secs) if secs > 0 else None
    try:
        dt = email.utils.parsedate_to_datetime(s)
    except (TypeError, ValueError, OverflowError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        # HTTP-date 按规范就是 GMT;没带时区的一律当 UTC,别用本机时区解释,
        # 否则 Actions(UTC)和本机(东八区)会算出差 8 小时的等待时间。
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    delta = dt.timestamp() - (time.time() if now is None else now)
    return delta if delta > 0 else None


def retry_delay_from_headers(headers, now=None):
    """从响应头里挖出该等多久。Retry-After 优先,其次 GitHub 的 X-RateLimit-Reset。

    X-RateLimit-Reset 只在 Remaining=0 时才当作限流信号 —— 平时每个响应都带这个头,
    照单全收会把正常响应也当成"要等到下个小时"。
    (arsenal_mine 原来的写法没做这个判断,只是恰好因为它只在 403/429 分支里读才没出事。)
    """
    if not headers:
        return None
    d = parse_retry_after(_h(headers, "Retry-After"), now=now)
    if d is not None:
        return d
    remaining = _h(headers, "X-RateLimit-Remaining")
    reset = _h(headers, "X-RateLimit-Reset")
    if reset and (remaining is None or str(remaining).strip() in ("0", "")):
        try:
            delta = int(str(reset).strip()) - (time.time() if now is None else now)
            return delta if delta > 0 else None
        except (TypeError, ValueError):
            return None
    return None


class CIDict(dict):
    """大小写无关的响应头字典。

    **这不是洁癖,是我自测时真踩到的坑**(2026-09-02 实测):
      zh.wikipedia.org 发的是小写 `content-encoding: gzip`,
      而 api.github.com / ctext.org 发的是首字母大写 `Content-Encoding`。
    我原来写的是 dict.get("Content-Encoding") 精确匹配 —— 维基那条**静默**取不到,
    于是不解压,拿到 29668 字节的 gzip 二进制还当成 HTML,
    trafilatura 提出 0 字正文,表面上却是「HTTP 200 成功」。
    这种失败最阴险:状态码是绿的,数据是垃圾的。

    HTTP 头按 RFC 9110 本来就是大小写无关的,谁大写谁小写是服务端的自由。
    所以对外暴露的 headers 一律是这个类型,调用方怎么写大小写都取得到。
    """

    def __init__(self, src=None):
        super().__init__(src or {})
        self._low = {str(k).lower(): k for k in self.keys()}

    def get(self, key, default=None):
        k = self._low.get(str(key).lower())
        return dict.get(self, k, default) if k is not None else default

    def __getitem__(self, key):
        k = self._low.get(str(key).lower())
        if k is None:
            raise KeyError(key)
        return dict.__getitem__(self, k)

    def __contains__(self, key):
        return str(key).lower() in self._low


def _h(headers, name):
    """大小写无关地取一个头。urllib 的 HTTPMessage 本来就是大小写无关的,
    但普通 dict 不是 —— 自测和调用方常直接传 dict 进来,所以这里统一兜住。"""
    if headers is None:
        return None
    get = getattr(headers, "get", None)
    if get is None:
        return None
    v = get(name)
    if v is not None:
        return v
    low = name.lower()
    try:
        for k in headers.keys():
            if str(k).lower() == low:
                return headers[k]
    except Exception:                                             # noqa: BLE001
        return None
    return None


# ═══════════════════════════════════════════════════════════════════════
# 三、域级槽 —— 429 状态机 + 并发信号量 + 带抖动的礼貌间隔 + 自适应延迟
#     来源:crawlee throttling_request_manager.ts / scrapy downloader Slot
#           + scrapy extensions/throttle.py / colly http_backend.go
# ═══════════════════════════════════════════════════════════════════════

# 默认值全部标出实测或原表出处,禁止拍脑袋:
#   · DEFAULT_DELAY 1.0s:arsenal_mine 抓 trending 用的是 time.sleep(1.2)、
#     搜索接口用 0.6s,两者都跑通过没被封,取中位当默认。
#   · PER_DOMAIN_CONCURRENCY 4:scrapy 默认 CONCURRENT_REQUESTS_PER_DOMAIN=8,
#     我们减半 —— 我们不是通用爬虫,打的是 GitHub / 古籍馆这类要长期打交道的域,
#     宁可慢一半也别被人家拉黑。
#   · DEFAULT_TIMEOUT 40s:沿用 arsenal_mine._get 已在跑的 timeout=40。
#     (scrapy 默认 DOWNLOAD_TIMEOUT=180 对我们太长,一个卡住的请求会拖垮整批。)
DEFAULT_DELAY = 1.0
PER_DOMAIN_CONCURRENCY = 4
DEFAULT_TIMEOUT = 40
# crawlee 的退避基数与上限。maxDelay 封顶避免"服务端发了个离谱的 Retry-After 就睡到天荒地老"。
BACKOFF_BASE = 2.0
BACKOFF_MAX = 90.0
# AutoThrottle(scrapy extensions/throttle.py 的默认值)
AUTOTHROTTLE_TARGET_CONCURRENCY = 1.0
AUTOTHROTTLE_MAX_DELAY = 60.0


@dataclass
class DomainSlot:
    """一个域一份状态。**429 的处置是域的事,不是单个请求的事。**"""

    host: str
    delay: float = DEFAULT_DELAY
    concurrency: int = PER_DOMAIN_CONCURRENCY
    autothrottle: bool = False

    # ── 429 状态机(crawlee #recordRateLimit 的五个字段)──
    backoff_until: float = 0.0
    backoff_decays_at: float = 0.0
    consecutive_429: int = 0
    rate_limited_since: float = 0.0
    last_rate_limited_at: float = 0.0

    # ── 并发槽 + 礼貌间隔 ──
    # next_allowed 是"下一次**发出**请求最早的时刻",在上一次请求**收完**时才写。
    # colly 的写法是 defer 里先 sleep 再放槽,保证间隔卡在「上次收完 → 下次发出」之间;
    # 若卡在「两次发起之间」,慢响应时会退化成无间隔猛打。
    # 这里用时间戳表达同一语义,好处是等待期间不占着槽睡觉。
    next_allowed: float = 0.0
    _sem: threading.BoundedSemaphore = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # ── 统计(出问题时要能说出数字,不能只说"好像被限流了")──
    n_req: int = 0
    n_429: int = 0
    n_suppressed_429: int = 0

    def __post_init__(self):
        if self._sem is None:
            self._sem = threading.BoundedSemaphore(max(1, int(self.concurrency)))

    # ---- 429 -------------------------------------------------------
    def record_rate_limit(self, server_delay=None, now=None):
        """收到 429。返回 True 表示"这次被突发抑制了,没有推进指数"。

        三把锁,少一把都会出问题:
          ① 无条件记 last_rate_limited_at —— 被抑制掉的 429 也是「域在赶我们走」,
             停滞检测要看得见它,否则我们会以为域安静了。
          ② 突发抑制:限流触发时**已经在飞**的那批请求会全部返回 429,
             它们描述的是同一个限流事件。只让第一个推进指数,
             否则并发数本身就在驱动指数爆炸(并发 8 → 一瞬间指数跳 8 级)。
             判据只能读 backoff_until,不能读 next_allowed —— 后者每次派发后都指向未来,
             拿它判会把域发来的每一个 429(含带 Retry-After 的)全丢掉。
          ③ 退避衰减:域安静了整整一个额外退避窗口,就当作新一轮,
             不把旧指数带进无关的下一次。
        """
        now = time.time() if now is None else now
        with self._lock:
            self.n_429 += 1
            self.last_rate_limited_at = now                        # ①
            if now < self.backoff_until:                           # ②
                self.n_suppressed_429 += 1
                return True
            if now >= self.backoff_decays_at:                      # ③
                self.consecutive_429 = 0
                self.rate_limited_since = now
            self.consecutive_429 += 1
            if server_delay is not None and server_delay > 0:
                delay = float(server_delay)                        # 服务端说了等多久就等多久
            else:
                delay = BACKOFF_BASE * (2 ** (self.consecutive_429 - 1))
            if delay > BACKOFF_MAX:
                delay = BACKOFF_MAX
                print("  [throttle] %s 退避被封顶到 %.0fs(第 %d 次连续 429)"
                      % (self.host, BACKOFF_MAX, self.consecutive_429))
            self.backoff_until = now + delay
            self.backoff_decays_at = self.backoff_until + delay
            return False

    def wait_for_backoff(self, sleeper=None):
        """域在退避期就等到期。返回实际等了多少秒(0 = 没等)。"""
        with self._lock:
            wait = self.backoff_until - time.time()
        if wait > 0:
            (sleeper or time.sleep)(wait)
            return wait
        return 0.0

    # ---- 并发 + 间隔 ------------------------------------------------
    def jittered_delay(self):
        """random.uniform(0.5*delay, 1.5*delay) —— scrapy 的 RANDOMIZE_DOWNLOAD_DELAY 默认就是开的。
        固定间隔本身就是机器特征:整整齐齐每 1.000 秒来一发,日志里一眼就认出来。"""
        return random.uniform(0.5 * self.delay, 1.5 * self.delay)

    @contextlib.contextmanager
    def guard(self, sleeper=None):
        self._sem.acquire()
        try:
            with self._lock:
                wait = self.next_allowed - time.time()
            if wait > 0:
                (sleeper or time.sleep)(wait)
            yield
        finally:
            with self._lock:
                # 在**收完**的时刻记下下次最早发出时间(colly 的语义)
                self.next_allowed = time.time() + self.jittered_delay()
                self.n_req += 1
            self._sem.release()

    # ---- AutoThrottle ----------------------------------------------
    def observe_latency(self, latency, status):
        """用观测到的响应延迟反推该有多大间隔(scrapy _adjust_delay 的算术直译)。

        不需要预先知道任何站点的限速,让延迟自己说话:
        服务器要 latency 秒才应答,想同时保持 N 个在飞,就每 latency/N 秒发一个。

        最后那个守门条件是全篇最值钱的一行:错误页和重定向体积小、延迟低,
        照单收下就会把间隔越调越小,形成「越被拒越猛打」的正反馈 ——
        这种事出问题时极难查,而防它只要一个 if。
        """
        if not self.autothrottle or latency is None or latency <= 0:
            return
        target = latency / AUTOTHROTTLE_TARGET_CONCURRENCY
        new = (self.delay + target) / 2.0
        # 目标比当前大时直接取目标:**变慢要快、变快要慢**的不对称,对问题站点效果更好。
        new = max(target, new)
        if status != 200 and new <= self.delay:
            return
        self.delay = min(max(new, DEFAULT_DELAY), AUTOTHROTTLE_MAX_DELAY)


class DomainRegistry:
    """host -> DomainSlot。取页器全局共享一份,四条产线同时打 GitHub 时才拦得住。"""

    def __init__(self, delay=DEFAULT_DELAY, concurrency=PER_DOMAIN_CONCURRENCY,
                 autothrottle=False, per_host=None):
        self._slots = {}
        self._lock = threading.Lock()
        self.delay = delay
        self.concurrency = concurrency
        self.autothrottle = autothrottle
        # per_host: {"api.github.com": {"delay": 0.6, "concurrency": 2}}
        # 各域限速不同,一律用同一个数就是在拿最松的域的经验套最严的域。
        self.per_host = dict(per_host or {})

    def slot(self, url_or_host):
        host = url_or_host
        if "//" in url_or_host or url_or_host.startswith("http"):
            host = urllib.parse.urlsplit(url_or_host).hostname or url_or_host
        host = (host or "").lower()
        with self._lock:
            s = self._slots.get(host)
            if s is None:
                cfg = self.per_host.get(host, {})
                s = DomainSlot(host=host,
                               delay=float(cfg.get("delay", self.delay)),
                               concurrency=int(cfg.get("concurrency", self.concurrency)),
                               autothrottle=bool(cfg.get("autothrottle", self.autothrottle)))
                self._slots[host] = s
            return s

    def stats(self):
        with self._lock:
            return {h: {"req": s.n_req, "429": s.n_429,
                        "429_suppressed": s.n_suppressed_429,
                        "delay": round(s.delay, 2)}
                    for h, s in self._slots.items() if s.n_req or s.n_429}


# ═══════════════════════════════════════════════════════════════════════
# 四、指纹 —— 组合合法、版本具体、**按会话钉死**
#     来源:crawlee session_pool/fingerprint.ts + impit-client/src/index.ts
# ═══════════════════════════════════════════════════════════════════════

# 只列**现实中真实存在**的 (浏览器, 平台) 组合。
# edge+android / safari+windows / chrome+ios(iOS 上 Chrome 其实是 WebKit)这类
# 不存在的搭配一律不生成:**随机出一个现实中没有的组合,这个组合本身就是破绽。**
_PROFILES = (
    ("chrome", "windows"), ("chrome", "macos"), ("chrome", "linux"), ("chrome", "android"),
    ("firefox", "windows"), ("firefox", "macos"), ("firefox", "linux"), ("firefox", "android"),
    ("safari", "macos"), ("safari", "ios"),
    ("edge", "windows"), ("edge", "macos"),
)

# 版本必须**具体**。impit 作者的注释说得很清楚:plain 'chrome' 这种别名会退到
# 该库支持的最老版本,that is a fingerprint giveaway。
# 这里列的是 2025 下半年—2026 年在用的真实大版本号,过期了就往后补,别写通配。
_VERSIONS = {
    "chrome": ("128.0.0.0", "131.0.0.0", "133.0.0.0", "136.0.0.0", "139.0.0.0"),
    "edge": ("128.0.0.0", "131.0.0.0", "133.0.0.0", "136.0.0.0"),
    "firefox": ("128.0", "133.0", "136.0", "140.0"),
    "safari": ("17.6", "18.2", "18.4"),
}

_PLATFORM_UA = {
    ("chrome", "windows"): "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{v} Safari/537.36",
    ("chrome", "macos"): "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{v} Safari/537.36",
    ("chrome", "linux"): "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{v} Safari/537.36",
    ("chrome", "android"): "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{v} Mobile Safari/537.36",
    ("edge", "windows"): "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{v} Safari/537.36 Edg/{v}",
    ("edge", "macos"): "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{v} Safari/537.36 Edg/{v}",
    ("firefox", "windows"): "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:{v}) Gecko/20100101 Firefox/{v}",
    ("firefox", "macos"): "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:{v}) Gecko/20100101 Firefox/{v}",
    ("firefox", "linux"): "Mozilla/5.0 (X11; Linux x86_64; rv:{v}) Gecko/20100101 Firefox/{v}",
    ("firefox", "android"): "Mozilla/5.0 (Android 14; Mobile; rv:{v}) Gecko/{v} Firefox/{v}",
    ("safari", "macos"): "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/{v} Safari/605.1.15",
    ("safari", "ios"): "Mozilla/5.0 (iPhone; CPU iPhone OS 17_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/{v} Mobile/15E148 Safari/604.1",
}

_SEC_CH_PLATFORM = {"windows": '"Windows"', "macos": '"macOS"', "linux": '"Linux"',
                    "android": '"Android"', "ios": '"iOS"'}


# ═══════════════════════════════════════════════════════════════════════
# 五、会话 —— 一套指纹 + 一个代理 + 一个 cookie jar 打包
#     来源:crawlee session_pool/session.ts
# ═══════════════════════════════════════════════════════════════════════

# crawlee 的默认值,原样沿用(这几个数是他们跑出来的,我没有更好的实测依据去改):
SESSION_MAX_ERROR_SCORE = 3.0
SESSION_ERROR_DECREMENT = 0.5      # 一次成功只治好**半次**失败
SESSION_MAX_AGE_SECS = 3000
SESSION_MAX_USAGE = 50             # 一套身份用久了本身就是特征,没出错也要主动换掉


@dataclass
class Session:
    sid: str
    proxy: str | None
    browser: str
    platform: str
    version: str
    ua: str
    error_score: float = 0.0
    usage_count: int = 0
    expires_at: float = 0.0
    retired: bool = False
    _jar: CookieJar = field(default=None, repr=False)
    _opener: urllib.request.OpenerDirector = field(default=None, repr=False)

    # ---- 健康分 ----------------------------------------------------
    def mark_good(self):
        """成功。errorScore 扣 0.5 而不是清零 —— 偶发抖动能自愈,持续变坏必然出局。"""
        self.usage_count += 1
        self.error_score = max(0.0, self.error_score - SESSION_ERROR_DECREMENT)
        self._maybe_self_retire()

    def mark_bad(self):
        """外部瞬时故障(5xx、连接抖动)。只记一分,别一棒子打死。"""
        self.usage_count += 1
        self.error_score += 1.0
        self._maybe_self_retire()

    def retire(self):
        """**终态**。确信是会话本身的问题(403 封禁、代理坏了)时才用。
        直接把分加满 —— mark_good() 也救不回来,这是故意的。"""
        self.error_score = SESSION_MAX_ERROR_SCORE
        self.retired = True

    def is_usable(self, now=None):
        now = time.time() if now is None else now
        return (not self.retired
                and self.error_score < SESSION_MAX_ERROR_SCORE
                and now < self.expires_at
                and self.usage_count < SESSION_MAX_USAGE)

    def _maybe_self_retire(self):
        if not self.is_usable():
            self.retired = True

    # ---- 出网 ------------------------------------------------------
    def opener(self):
        """这个会话专属的 opener:代理绑定终身 + 自己的 cookie jar。

        显式传 ProxyHandler(哪怕是空的):不传的话 urllib 会去读环境变量 HTTP(S)_PROXY,
        于是"我以为在直连、其实走了代理"—— 这类看不见的分叉最难查。
        """
        if self._opener is None:
            self._jar = CookieJar()
            handlers = [
                urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy}
                                            if self.proxy else {}),
                urllib.request.HTTPCookieProcessor(self._jar),
            ]
            self._opener = urllib.request.build_opener(*handlers)
            self._opener.addheaders = []          # 头我们自己给全,别让 urllib 塞它的 Python-urllib UA
        return self._opener

    def build_headers(self, extra=None, accept=None):
        """按会话钉死的一套头。

        关键是**同一会话每次请求都冒充同一个具体版本**,不逐次重掷:
        逐请求随机 UA 会造成"同一个 cookie 会话在几秒内换了三种浏览器"这种
        现实中不可能出现的轨迹 —— 那比不轮换更可疑。
        """
        chromium = self.browser in ("chrome", "edge")
        h = {
            "User-Agent": self.ua,
            "Accept": accept or ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                                 "image/avif,image/webp,*/*;q=0.8"),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            # 真浏览器一定发 Accept-Encoding,不发反而是特征。
            # 只声明 gzip/deflate(stdlib 能解),**不声明 br** —— 声明了却解不开更糟。
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }
        if chromium:
            major = self.version.split(".")[0]
            brand = "Microsoft Edge" if self.browser == "edge" else "Google Chrome"
            h["sec-ch-ua"] = ('"Not_A Brand";v="8", "Chromium";v="%s", "%s";v="%s"'
                              % (major, brand, major))
            h["sec-ch-ua-mobile"] = "?1" if self.platform in ("android", "ios") else "?0"
            h["sec-ch-ua-platform"] = _SEC_CH_PLATFORM.get(self.platform, '"Windows"')
            h["Sec-Fetch-Site"] = "none"
            h["Sec-Fetch-Mode"] = "navigate"
            h["Sec-Fetch-User"] = "?1"
            h["Sec-Fetch-Dest"] = "document"
        if extra:
            h.update(extra)
        return h


class SessionPool:
    """会话池。**「换代理」这个动作在这套设计里根本不单独存在** ——
    它是「会话退休 → 新会话诞生 → 领到下一个代理」的副产物。

    (crawlee v4 把 tieredProxyUrls 那套分层代理删掉了,源码里只留报错提示 ——
     说明这复杂度他们自己也放弃了。我们更没必要上。)
    """

    def __init__(self, proxies=None, max_pool_size=6, seed=None):
        # proxies 里的 None 表示"直连"。空列表等价于 [None]。
        self.proxies = list(proxies) if proxies else [None]
        self.max_pool_size = max(1, int(max_pool_size))
        self._pool = []
        self._proxy_i = 0
        self._n = 0
        self._lock = threading.Lock()
        self._rand = random.Random(seed)

    def _new_session(self):
        # 代理轮询:crawlee ProxyConfiguration.newUrl() 就是 proxyUrls[i++ % len] 的朴素轮询。
        proxy = self.proxies[self._proxy_i % len(self.proxies)]
        self._proxy_i += 1
        self._n += 1
        browser, platform = self._rand.choice(_PROFILES)
        version = self._rand.choice(_VERSIONS[browser])
        ua = _PLATFORM_UA[(browser, platform)].format(v=version)
        return Session(sid="s%d" % self._n, proxy=proxy, browser=browser,
                       platform=platform, version=version, ua=ua,
                       expires_at=time.time() + SESSION_MAX_AGE_SECS)

    def get(self):
        with self._lock:
            self._pool = [s for s in self._pool if s.is_usable()]
            if len(self._pool) < self.max_pool_size:
                s = self._new_session()
                self._pool.append(s)
                return s
            return self._rand.choice(self._pool)

    def drop(self, sess):
        with self._lock:
            self._pool = [s for s in self._pool if s is not sess and s.is_usable()]

    def stats(self):
        with self._lock:
            return {"live": len(self._pool), "created": self._n,
                    "proxies": [p or "direct" for p in self.proxies]}


# ═══════════════════════════════════════════════════════════════════════
# 六、代理 —— 自动适应「本机」与「Actions」两种环境
# ═══════════════════════════════════════════════════════════════════════

LOCAL_PROXY = "http://127.0.0.1:1082"     # 实测:本机走这个,国内直连 github.com 会超时
_probe_cache = {}


def _tcp_alive(host, port, timeout=0.6):
    """探一下端口活着没。0.6s 足够判断本机回环端口,不会拖慢启动。"""
    key = (host, port)
    if key in _probe_cache:
        return _probe_cache[key]
    try:
        with socket.create_connection((host, port), timeout=timeout):
            _probe_cache[key] = True
    except OSError:
        _probe_cache[key] = False
    return _probe_cache[key]


def in_actions():
    """是不是在 GitHub Actions 上。Actions 的 runner 在境外,直连即可、且没有 1082。"""
    return (os.environ.get("GITHUB_ACTIONS") == "true"
            or os.environ.get("CI", "").lower() == "true")


def default_proxies():
    """决定这次出网用哪些代理。**取页代码一行都不用为环境分叉。**

    优先级:
      ① 显式环境变量 CRAWL_PROXIES(逗号分隔;"direct" 表示直连)—— 人说了算
      ② 在 Actions 上 → [None] 直连(境外 runner,加代理反而错)
      ③ 本机 → 环境里的 HTTPS_PROXY/HTTP_PROXY + 127.0.0.1:1082,**逐个探活**,
         都不通才退回直连(退回时会打一行,不静默 —— 静默直连在国内就是集体超时)
    """
    raw = os.environ.get("CRAWL_PROXIES", "").strip()
    if raw:
        out = []
        for p in raw.split(","):
            p = p.strip()
            if not p:
                continue
            out.append(None if p.lower() in ("direct", "none", "-") else p)
        return out or [None]
    if in_actions():
        return [None]
    cands = []
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        v = os.environ.get(k)
        if v and v not in cands:
            cands.append(v.strip())
    if LOCAL_PROXY not in cands:
        cands.append(LOCAL_PROXY)
    alive = []
    for c in cands:
        sp = urllib.parse.urlsplit(c if "//" in c else "http://" + c)
        if sp.hostname and _tcp_alive(sp.hostname, sp.port or 80):
            alive.append(c)
    if not alive:
        print("  [proxy] 没探到可用代理,退回直连 —— 国内直连 github.com 大概率超时")
        return [None]
    return alive


# ═══════════════════════════════════════════════════════════════════════
# 七、被拦识别 —— HTTP 200 也可能是挑战页
#     来源:crawlee packages/utils/src/internals/blocked.ts
# ═══════════════════════════════════════════════════════════════════════

# crawlee 用 cheerio 跑 CSS 选择器;我们是纯 stdlib,退化成字符串包含判断。
# 准确度不如选择器,但比不检测强得多 —— 不检测就会把挑战页当正文存进 RAG 库。
_CHALLENGE_MARKS = (
    b"challenges.cloudflare.com",          # Cloudflare Turnstile
    b"_Incapsula_Resource",                # Imperva / Incapsula
    b"/cdn-cgi/challenge-platform",        # Cloudflare 托管挑战
)
# Google 的 sorry 页:两个特征必须同时出现,单看 infoDiv0 太泛会误判。
_GOOGLE_SORRY = (b"infoDiv0", b"google.com/policies/terms")


def looks_like_challenge(body, content_type=""):
    """这页是不是反爬挑战页(而不是我们要的正文)。

    只在 HTML/XML 上判 —— JSON、图片里出现这些字节串多半是巧合(比如某个仓库的
    README 里正好在讲 Cloudflare),在那上面判会误伤。
    """
    if not body:
        return False
    ct = (content_type or "").lower()
    if ct and not ("html" in ct or "xml" in ct):
        return False
    head = body[:200000]                   # 挑战页都很小,只看头部足够且省内存
    if any(m in head for m in _CHALLENGE_MARKS):
        return True
    return all(m in head for m in _GOOGLE_SORRY)


# 代理级错误特征。crawlee 的 ROTATE_PROXY_ERRORS 是 JS 错误码串表,
# Python 侧的对等物主要靠**异常类型**,字符串只作补充(有些代理把原因塞在消息里)。
ROTATE_PROXY_MARKS = ("proxy", "tunnel connection failed", "connection reset",
                      "connection refused", "econnreset", "econnrefused",
                      "remote end closed connection")


def classify_transport_error(exc):
    """连接层异常 → 该怪谁。

    「超时该换代理」是本任务点名的要求:超时在我们这里几乎总是代理不通
    (实测:国内直连 github.com 就是超时,不是 404 也不是 429),
    所以归 SessionError —— 退休当前会话,下一次自然领到轮询里的下一个代理。
    """
    root = exc
    while getattr(root, "reason", None) is not None and isinstance(root.reason, BaseException):
        root = root.reason
    if isinstance(root, (socket.timeout, TimeoutError)):
        return SessionError("超时(疑似代理不通):%s" % str(exc)[:120])
    if isinstance(root, (ConnectionResetError, ConnectionRefusedError, ConnectionAbortedError)):
        return SessionError("连接被断/被拒(疑似代理坏了):%s" % str(exc)[:120])
    if isinstance(root, ssl.SSLError):
        # 注意实测教训:Windows 上 curl exit 35 那类是**证书吊销检查**在骗人,不是服务挂了。
        # SSL 错也先当会话问题处理(换代理常常就好),而不是判定"站点挂了"。
        return SessionError("SSL 错误:%s" % str(exc)[:120])
    msg = str(exc).lower()
    if any(m in msg for m in ROTATE_PROXY_MARKS):
        return SessionError("代理层错误:%s" % str(exc)[:120])
    if isinstance(root, socket.gaierror):
        # DNS 解析不了:如果在走代理,那是代理的事;直连时才是 URL 的事。
        return SessionError("DNS 解析失败:%s" % str(exc)[:120])
    return SessionError("未归类连接错误:%s" % str(exc)[:120])


# ═══════════════════════════════════════════════════════════════════════
# 八、取页器
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int
    headers: dict
    body: bytes
    elapsed: float
    attempts: int
    session_id: str
    proxy: str | None

    @property
    def text(self):
        """按响应头声明的编码解码;没声明就 UTF-8。

        errors='replace' 是故意的:宁可留几个替换符,也不要因为一个坏字节
        丢掉整页中文正文。(这是显示层的容忍,不是数据层的妥协 ——
        body 原始字节始终留着。)
        """
        ct = (_h(self.headers, "Content-Type") or "").lower()
        enc = "utf-8"
        m = re.search(r"charset=([\w\-]+)", ct)
        if m:
            enc = m.group(1)
        try:
            return self.body.decode(enc, "replace")
        except LookupError:
            return self.body.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.text)


class Fetcher:
    """一个 fetch(url) 就稳定拿到页面。

    每次尝试的完整链路:
      配额闸门 → 域级退避等待 → 取会话(带指纹+代理) → 域并发槽+礼貌间隔
      → 发请求 → 挑战页检测 → 错误分类 → 按类处置(等/换身份/判死/终止)
    """

    def __init__(self, proxies=None, delay=DEFAULT_DELAY,
                 concurrency=PER_DOMAIN_CONCURRENCY, autothrottle=False,
                 per_host=None, budget=0, max_retries=2, timeout=DEFAULT_TIMEOUT,
                 verbose=True, seed=None):
        self.pool = SessionPool(proxies if proxies is not None else default_proxies(), seed=seed)
        self.domains = DomainRegistry(delay=delay, concurrency=concurrency,
                                      autothrottle=autothrottle, per_host=per_host)
        # budget=0 表示不限。配额闸门语义与 arsenal_mine.BudgetExhausted 一致。
        self.budget = int(budget or 0)
        self.max_retries = int(max_retries)     # scrapy RETRY_TIMES 默认 2(即最多 3 次请求)
        self.timeout = timeout
        self.verbose = verbose
        self.calls = 0                          # 真实发出的 HTTP 请求数(含重试)
        self._lock = threading.Lock()

    # ---- 配额 ------------------------------------------------------
    def _spend(self):
        with self._lock:
            if self.budget and self.calls >= self.budget:
                raise BudgetExhausted("已用 %d 次请求,达到配额上限 %d" % (self.calls, self.budget))
            self.calls += 1

    # ---- 主入口 ----------------------------------------------------
    def fetch(self, url, method="GET", headers=None, data=None, accept=None,
              timeout=None, max_retries=None, retry_queue=None):
        """取一个页面。成功返回 FetchResult;彻底失败抛具体的异常子类。

        **不吞异常。** 调用方需要"失败就当没有"的语义时用 try_fetch(),
        但那必须是调用方显式选的 —— 静默吞掉会让"抓不到"和"没有"分不开,
        这正是我们在鹰眼上栽过的那种瞎。
        """
        timeout = self.timeout if timeout is None else timeout
        max_retries = self.max_retries if max_retries is None else max_retries
        slot = self.domains.slot(url)
        last_exc = None

        for attempt in range(max_retries + 1):
            self._spend()                                   # 配额闸门(会抛 BudgetExhausted)
            waited = slot.wait_for_backoff()                # 域在退避期就先等到期
            if waited and self.verbose:
                print("  [throttle] %s 域级退避,等了 %.1fs" % (slot.host, waited))
            sess = self.pool.get()
            try:
                with slot.guard():
                    t0 = time.time()
                    res = self._one_shot(url, sess, method, headers, data, accept, timeout)
                    latency = time.time() - t0
            except FetchError as e:
                res, latency, exc = None, None, e
            else:
                exc = None

            if res is not None:
                slot.observe_latency(latency, res.status)
                sess.mark_good()
                res.attempts = attempt + 1
                return res

            last_exc = exc
            # ── 按错因分头处置 ──────────────────────────────────
            if isinstance(exc, CriticalError):
                raise exc                                   # 怪我们自己,重试无意义,直接终止
            if isinstance(exc, NonRetryableError):
                raise exc                                   # 怪这个 URL(404 / 挑战页)
            if isinstance(exc, RequestThrottledError):
                # 怪这个域。推域级退避,**不给会话记黑分、不换代理**。
                suppressed = slot.record_rate_limit(exc.delay)
                if self.verbose:
                    print("  [429] %s %s(第 %d 次连续)%s"
                          % (slot.host, ("服务端指示 %.0fs" % exc.delay) if exc.delay else "无 Retry-After",
                             slot.consecutive_429, " · 突发抑制" if suppressed else ""))
            elif isinstance(exc, SessionError):
                # 怪这套身份。退休 → 下次 get() 会造一个新会话并领到下一个代理。
                sess.retire()
                self.pool.drop(sess)
                if self.verbose:
                    print("  [session] 退休 %s(proxy=%s):%s"
                          % (sess.sid, sess.proxy or "direct", str(exc)[:80]))
            else:
                sess.mark_bad()                             # 5xx 这类外部瞬时故障:只记一分

            if attempt >= max_retries:
                break
            if retry_queue is not None:
                # scrapy 的正面写法:不在这里 sleep 硬撞,把它降优先级丢回队尾,
                # 先去跑别的 URL,等队列轮回来时这个域已经凉了一会儿。
                retry_queue.retry(url, times=attempt + 1)
                break

        raise last_exc if last_exc else FetchError("取页失败但没有捕获到异常:%s" % url)

    def try_fetch(self, url, **kw):
        """要"失败就当没有"的语义时用这个 —— 调用方**显式**选择吞掉异常。"""
        try:
            return self.fetch(url, **kw)
        except BudgetExhausted:
            raise                                           # 配额永远向上抛,不许吞
        except FetchError:
            return None

    # ---- 单次请求 --------------------------------------------------
    def _one_shot(self, url, sess, method, headers, data, accept, timeout):
        req = urllib.request.Request(
            url, data=data, method=method,
            headers=sess.build_headers(extra=headers, accept=accept))
        try:
            resp = sess.opener().open(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            body = b""
            try:
                body = e.read() or b""
            except Exception:                               # noqa: BLE001
                pass
            raise self._classify_http(e.code, e.headers, body, url)
        except urllib.error.URLError as e:
            raise classify_transport_error(e)
        except (socket.timeout, TimeoutError, ConnectionError, ssl.SSLError) as e:
            raise classify_transport_error(e)
        except ValueError as e:
            # 不合法的 URL/schema:这是**我们**给错了参数,不是站点的问题。
            raise CriticalError("URL 不合法:%s(%s)" % (url, str(e)[:80]))

        with resp:
            raw = resp.read()
            # 必须是 CIDict:服务端大小写不统一,精确匹配会静默漏掉(见 CIDict 的实测注释)
            hdrs = CIDict(dict(resp.headers.items()))
            status = resp.getcode()
            final = resp.geturl()
        body = _decompress(raw, _h(hdrs, "Content-Encoding") or "")
        if looks_like_challenge(body, _h(hdrs, "Content-Type") or ""):
            # 200 也可能是被拦。反爬系统会拿 200 骗你,只看状态码会把挑战页当正文存进库。
            raise BrowserRequiredError(
                "%s 返回的是反爬挑战页(HTTP %d)—— 纯 HTTP 拿不下,这是该上浏览器的证据,"
                "不是继续轮换代理的信号" % (urllib.parse.urlsplit(url).hostname, status))
        return FetchResult(url=url, final_url=final, status=status, headers=hdrs,
                           body=body, elapsed=0.0, attempts=1,
                           session_id=sess.sid, proxy=sess.proxy)

    def _classify_http(self, code, headers, body, url):
        """状态码 → 四类错误之一。这张表是「不同错因不同处置」的骨架。"""
        if code == 429 or (code == 403 and retry_delay_from_headers(headers) is not None):
            # GitHub 的二级限流会用 **403** 带 Retry-After / X-RateLimit-Reset 发过来,
            # 那是限流不是封禁 —— 靠"有没有给退避指示"来分辨,别只看状态码。
            return RequestThrottledError("HTTP %d 限流:%s" % (code, url),
                                         delay=retry_delay_from_headers(headers))
        if code in SESSION_STATUS:
            return SessionError("HTTP %d 拒绝(这套身份被认出来了):%s" % (code, url))
        if code in NON_RETRY_STATUS:
            return NonRetryableError("HTTP %d:%s" % (code, url))
        if looks_like_challenge(body, _h(headers, "Content-Type") or ""):
            return BrowserRequiredError("HTTP %d 且是挑战页:%s" % (code, url))
        if code in RETRY_STATUS:
            return FetchError("HTTP %d(服务端瞬时故障):%s" % (code, url))
        return NonRetryableError("HTTP %d(未列入重试表):%s" % (code, url))

    def stats(self):
        return {"http_calls": self.calls, "sessions": self.pool.stats(),
                "domains": self.domains.stats()}


def _decompress(raw, encoding):
    """我们声明了 Accept-Encoding: gzip, deflate,就必须自己解 —— urllib 不会代劳。
    (声明了却解不开会拿到一堆二进制垃圾,还以为是"页面变了"。)"""
    enc = (encoding or "").lower().strip()
    if not raw:
        return raw
    try:
        if "gzip" in enc:
            return gzip.decompress(raw)
        if "deflate" in enc:
            try:
                return zlib.decompress(raw)
            except zlib.error:
                return zlib.decompress(raw, -zlib.MAX_WBITS)      # 裸 deflate,没有 zlib 头
    except (OSError, zlib.error):
        return raw                                                # 解不开就原样给出去,别丢数据
    return raw


# ═══════════════════════════════════════════════════════════════════════
# 九、重试队列 —— 重试 = 降优先级重新入队,不是原地 sleep 再撞一次
#     来源:scrapy/downloadermiddlewares/retry.py :: get_retry_request(BSD-3)
# ═══════════════════════════════════════════════════════════════════════

RETRY_TIMES = 2                    # scrapy 默认值:最多 3 次请求
RETRY_PRIORITY_ADJUST = -1         # 重试件压到队尾


class RetryQueue:
    """给批量产线(鹰眼那种一批 URL 跑批)用的队列。

    退避不靠 sleep 阻塞,靠**队列排序**:失败件降权丢回队尾,先去跑别的 URL,
    等队列自然轮回来时那个域已经凉了一会儿 —— 天然不占线程,也不会把某个域连撞三下。
    """

    def __init__(self, max_times=RETRY_TIMES):
        import heapq                                        # 只在用到时导入,保持模块头干净
        self._heapq = heapq
        self._h = []
        self._seq = 0
        self.max_times = max_times
        self.given_up = []

    def push(self, url, priority=0, meta=None, times=0):
        self._seq += 1
        # heapq 是小顶堆,而 scrapy 的 priority 越大越先出 —— 取负号对齐语义。
        self._heapq.heappush(self._h, (-priority, self._seq,
                                       {"url": url, "priority": priority,
                                        "meta": meta or {}, "times": times}))

    def pop(self):
        if not self._h:
            return None
        return self._heapq.heappop(self._h)[2]

    def retry(self, url, times=1, priority=0, meta=None):
        """次数用完就**记账放弃**,不是静默丢掉 —— 放弃了多少件必须能报出数字。"""
        if times > self.max_times:
            self.given_up.append({"url": url, "times": times})
            return False
        self.push(url, priority=priority + RETRY_PRIORITY_ADJUST, meta=meta, times=times)
        return True

    def __len__(self):
        return len(self._h)


# ═══════════════════════════════════════════════════════════════════════
# 十、模块级便捷入口
# ═══════════════════════════════════════════════════════════════════════

_default_fetcher = None
_default_lock = threading.Lock()


def get_fetcher(**kw):
    """全局共享的取页器。**必须共享** —— 域级节流和会话池只有共享才有意义,
    每个调用点各造一个 Fetcher 就等于没有节流(那正是我们现在的状态)。"""
    global _default_fetcher
    with _default_lock:
        if _default_fetcher is None:
            _default_fetcher = Fetcher(**kw)
        return _default_fetcher


def fetch(url, **kw):
    """取一个页面,返回 FetchResult。失败抛异常。"""
    return get_fetcher().fetch(url, **kw)


def get_text(url, **kw):
    """取正文文本。抓不到返回 None(显式选择了"失败就当没有")。"""
    r = get_fetcher().try_fetch(url, **kw)
    return r.text if r else None


def get_json(url, **kw):
    r = get_fetcher().try_fetch(url, accept="application/json", **kw)
    if not r:
        return None
    try:
        return r.json()
    except ValueError:
        return None


def calls_used():
    """全局已发出的真实 HTTP 请求数(含重试)。给上层配额对账用。"""
    return _default_fetcher.calls if _default_fetcher else 0


# ── GitHub API 便捷入口 ────────────────────────────────────────────────
# 与 scripts/intel_radar/arsenal_mine.py 的 _get() **返回契约完全一致**:
#   json 响应 → dict/list;raw 响应 → str;404 或彻底失败 → None。
# 这样那边可以一行委托过来,不必留第二份退避实现。
GITHUB_API = "https://api.github.com"


def github_token():
    for k in ("GH_TOKEN", "GITHUB_TOKEN", "ARSENAL_GH_TOKEN"):
        v = os.environ.get(k)
        if v:
            return v.strip()
    return ""


def github_api_get(url, accept="application/vnd.github+json", timeout=DEFAULT_TIMEOUT,
                   retries=2, fetcher=None):
    """一次 GitHub API 调用。

    退避完全交给 Fetcher:读服务端给的 Retry-After / X-RateLimit-Reset,
    走域级状态机 —— 不是对错误码硬重试(平台铁律)。
    """
    f = fetcher or get_fetcher()
    hdrs = {"Accept": accept}
    t = github_token()
    if t:
        hdrs["Authorization"] = "Bearer " + t
    try:
        r = f.fetch(url, headers=hdrs, accept=accept, timeout=timeout, max_retries=retries)
    except BudgetExhausted:
        raise                                               # 配额向上抛,与原实现一致
    except FetchError:
        return None                                         # 404 / 彻底失败 → None,与原实现一致
    if accept.endswith("raw"):
        return r.text
    try:
        return r.json()
    except ValueError:
        return None


# ═══════════════════════════════════════════════════════════════════════
# 自测:纯逻辑部分离线跑,取页部分**真抓真网页**
#   python fetch.py            → 全部
#   python fetch.py --offline  → 只跑不联网的部分
# ═══════════════════════════════════════════════════════════════════════

def _selftest_offline():
    """纯逻辑自测。不联网,Actions 上也能当单元测试跑。"""
    out = []

    def ck(name, got, want):
        ok = got == want
        out.append({"case": name, "ok": ok, "got": repr(got), "want": repr(want)})
        return ok

    # ── Retry-After 四种形态 ──
    ck("retry_after 纯数字 120", parse_retry_after("120"), 120.0)
    ck("retry_after 为 0 必须是 None", parse_retry_after("0"), None)
    ck("retry_after 负数不是 delay-seconds", parse_retry_after("-5"), None)
    ck("retry_after 垃圾串", parse_retry_after("soon"), None)
    ck("retry_after 空", parse_retry_after(""), None)
    # HTTP-date:这正是 arsenal_mine 的 int() 抛异常吞掉的那一种
    now = 1_800_000_000.0
    future = email.utils.formatdate(now + 42, usegmt=True)
    got = parse_retry_after(future, now=now)
    ck("retry_after HTTP-date(旧实现在这里归 0)", got is not None and 41 <= got <= 43, True)
    past = email.utils.formatdate(now - 100, usegmt=True)
    ck("retry_after 过期的 HTTP-date", parse_retry_after(past, now=now), None)
    # X-RateLimit-Reset 兜底 + Remaining 守门
    ck("reset 在 Remaining=0 时才算限流",
       retry_delay_from_headers({"X-RateLimit-Remaining": "0",
                                 "X-RateLimit-Reset": str(int(now + 30))}, now=now) is not None, True)
    ck("Remaining 还有剩就不当限流",
       retry_delay_from_headers({"X-RateLimit-Remaining": "4999",
                                 "X-RateLimit-Reset": str(int(now + 3000))}, now=now), None)

    # ── 域级 429 状态机:突发抑制 + 指数 + 衰减 ──
    slot = DomainSlot(host="t.example")
    t = 1000.0
    s1 = slot.record_rate_limit(None, now=t)
    ck("第一个 429 推进指数", (s1, slot.consecutive_429), (False, 1))
    s2 = slot.record_rate_limit(None, now=t + 0.1)
    ck("在飞的第二个 429 被突发抑制", (s2, slot.consecutive_429), (True, 1))
    ck("被抑制的 429 也要记 last_rate_limited_at", slot.last_rate_limited_at, t + 0.1)
    # 退避窗口内不算新一轮;窗口过了、但还没到 decays 才推进指数
    t2 = slot.backoff_until + 0.1
    slot.record_rate_limit(None, now=t2)
    ck("退避到期后的 429 推进指数到 2", slot.consecutive_429, 2)
    # 安静一整个额外退避窗口 → 指数清零
    t3 = slot.backoff_decays_at + 1
    slot.record_rate_limit(None, now=t3)
    ck("安静够久后指数衰减回 1", slot.consecutive_429, 1)
    # 服务端给了 Retry-After 就听它的
    slot2 = DomainSlot(host="t2.example")
    slot2.record_rate_limit(37.0, now=5000.0)
    ck("有 Retry-After 就用它", round(slot2.backoff_until - 5000.0, 1), 37.0)

    # ── 会话健康分 ──
    s = Session(sid="x", proxy=None, browser="chrome", platform="windows",
                version="139.0.0.0", ua="ua", expires_at=time.time() + 999)
    s.mark_bad(); s.mark_bad()
    ck("两次失败 errorScore=2", s.error_score, 2.0)
    s.mark_good()
    ck("一次成功只治好半次(2 - 0.5)", s.error_score, 1.5)
    ck("还可用", s.is_usable(), True)
    s.retire()
    ck("retire 是终态", s.is_usable(), False)
    s.mark_good()
    ck("retire 后 mark_good 也救不回来", s.is_usable(), False)
    s3 = Session(sid="y", proxy=None, browser="chrome", platform="windows",
                 version="139.0.0.0", ua="ua", expires_at=time.time() + 999)
    s3.usage_count = SESSION_MAX_USAGE
    ck("用满 50 次主动换身份(哪怕零错误)", s3.is_usable(), False)

    # ── 指纹组合合法性 ──
    bad = [(b, p) for (b, p) in _PROFILES
           if (b == "edge" and p == "android") or (b == "safari" and p == "windows")
           or (b == "safari" and p == "linux")]
    ck("组合表里没有现实中不存在的搭配", bad, [])
    ck("每个组合都有 UA 模板",
       [k for k in _PROFILES if k not in _PLATFORM_UA], [])
    pool = SessionPool(proxies=[None], seed=7)
    a = pool.get()
    ck("同一会话 UA 钉死(不逐请求重掷)",
       a.build_headers()["User-Agent"] == a.build_headers()["User-Agent"], True)
    ck("UA 带具体版本号,不是 'chrome' 别名", a.version in a.ua, True)

    # ── 错误分类 ──
    f = Fetcher(proxies=[None], verbose=False)
    ck("404 不重试", isinstance(f._classify_http(404, {}, b"", "u"), NonRetryableError), True)
    ck("429 归域级限流",
       isinstance(f._classify_http(429, {"Retry-After": "30"}, b"", "u"), RequestThrottledError), True)
    ck("429 读到了服务端指示的 30s",
       f._classify_http(429, {"Retry-After": "30"}, b"", "u").delay, 30.0)
    ck("403 归会话(换身份)",
       isinstance(f._classify_http(403, {}, b"", "u"), SessionError), True)
    ck("403 带 Retry-After 是二级限流,不是封禁",
       isinstance(f._classify_http(403, {"Retry-After": "60"}, b"", "u"), RequestThrottledError), True)
    ck("503 可重试且不判死",
       type(f._classify_http(503, {}, b"", "u")) is FetchError, True)
    ck("522 在重试表里(Cloudflare 源站超时)", 522 in RETRY_STATUS, True)
    ck("超时归会话 → 换代理",
       isinstance(classify_transport_error(urllib.error.URLError(socket.timeout("timed out"))),
                  SessionError), True)

    # ── 挑战页识别 ──
    ck("Turnstile 挑战页认得出",
       looks_like_challenge(b'<iframe src="https://challenges.cloudflare.com/x">',
                            "text/html"), True)
    ck("JSON 里提到 cloudflare 不误判",
       looks_like_challenge(b'{"a":"challenges.cloudflare.com"}', "application/json"), False)
    ck("正常 HTML 不误判",
       looks_like_challenge(b"<html><body>hello</body></html>", "text/html"), False)

    # ── 重试队列:降优先级回队尾 ──
    q = RetryQueue(max_times=2)
    q.push("A", priority=0); q.push("B", priority=0)
    # retry 的语义是「这件已经出队、跑失败了,现在把它降权丢回去」,
    # 所以要先 pop 出来 —— 不 pop 就等于队列里凭空多一份,是调用方用错了。
    first = q.pop()
    ck("先进先出(同优先级)", first["url"], "A")
    q.retry(first["url"], times=1)
    order = [q.pop()["url"] for _ in range(len(q))]
    ck("重试件被压到队尾(B 先于重试的 A)", order, ["B", "A"])
    ck("次数用完记账放弃而不是静默丢", (q.retry("A", times=3), len(q.given_up)), (False, 1))

    # ── 解压 + 响应头大小写(2026-09-02 自测真踩到的 bug,焊成回归用例)──
    ck("gzip 解得开", _decompress(gzip.compress("中文".encode()), "gzip").decode(), "中文")
    ck("解不开就原样返回不丢数据", _decompress(b"notgzip", "gzip"), b"notgzip")
    ci = CIDict({"content-encoding": "gzip", "Content-Type": "text/html; charset=utf-8"})
    ck("小写 content-encoding 也要取得到(维基就是发小写的)", _h(ci, "Content-Encoding"), "gzip")
    ck("大写 Content-Type 也要取得到", _h(ci, "content-type"), "text/html; charset=utf-8")
    ck("CIDict 自己的 get 也大小写无关", ci.get("CONTENT-ENCODING"), "gzip")
    fr = FetchResult(url="u", final_url="u", status=200,
                     headers=CIDict({"content-type": "text/html; charset=gbk"}),
                     body="中医".encode("gbk"), elapsed=0, attempts=1,
                     session_id="s", proxy=None)
    ck("按小写 content-type 里的 charset 正确解码", fr.text, "中医")

    return out


def _selftest_online():
    """真抓真网页。抓不到就如实报错,不许说"应该能通"。"""
    rows = []
    proxies = default_proxies()
    rows.append({"case": "代理决策", "detail": "in_actions=%s proxies=%s"
                 % (in_actions(), [p or "direct" for p in proxies])})
    f = Fetcher(proxies=proxies, delay=0.8, verbose=True,
                per_host={"api.github.com": {"delay": 0.6, "concurrency": 2}})

    # ① GitHub API(国内直连必超时,正好验代理这条链)
    try:
        j = github_api_get(GITHUB_API + "/repos/apify/crawlee", fetcher=f)
        rows.append({"case": "GitHub API", "ok": bool(j),
                     "detail": "%s ★%s %s" % (j.get("full_name"), j.get("stargazers_count"),
                                              (j.get("license") or {}).get("spdx_id"))
                     if j else "拿不到"})
    except Exception as e:                                    # noqa: BLE001
        rows.append({"case": "GitHub API", "ok": False, "detail": "%s: %s" % (type(e).__name__, str(e)[:120])})

    # ② 404 必须判死而不是重试三次
    t0 = time.time()
    try:
        f.fetch(GITHUB_API + "/repos/hosonzuo8848/this-repo-does-not-exist-x9", max_retries=2)
        rows.append({"case": "404 判死", "ok": False, "detail": "居然成功了?"})
    except NonRetryableError as e:
        rows.append({"case": "404 判死", "ok": True,
                     "detail": "%.2fs 内一次判死:%s" % (time.time() - t0, str(e)[:70])})
    except Exception as e:                                    # noqa: BLE001
        rows.append({"case": "404 判死", "ok": False, "detail": "%s: %s" % (type(e).__name__, str(e)[:90])})

    # ③ trending HTML(arsenal_mine 现在自己裸抓的那一条,委托过来验)
    try:
        r = f.fetch("https://github.com/trending?since=daily")
        items = re.findall(
            r'<h2[^>]*class="[^"]*lh-condensed[^"]*"[^>]*>\s*<a[^>]+href="/([^/"]+/[^/"?#]+)"', r.text)
        rows.append({"case": "trending HTML", "ok": len(items) > 0,
                     "detail": "HTTP %d · %d 字节 · 解析出 %d 个项目 · 头 3:%s"
                               % (r.status, len(r.body), len(items), items[:3])})
    except Exception as e:                                    # noqa: BLE001
        rows.append({"case": "trending HTML", "ok": False, "detail": "%s: %s" % (type(e).__name__, str(e)[:120])})

    # ④ 中文正文:取页 + trafilatura 提正文(复用 arsenal_enrich 已在用的那把)
    #    两个源都要过 —— ctext 发大写 Content-Encoding、维基发小写 content-encoding,
    #    两个都验才盖得住那个大小写 bug(只验一个就是我第一次没发现它的原因)。
    for name, zh_url in (
        ("中文正文·ctext 古籍", "https://ctext.org/huangdi-neijing/zh"),
        ("中文正文·维基(小写头)", "https://zh.wikipedia.org/wiki/%E9%BB%84%E5%B8%9D%E5%86%85%E7%BB%8F"),
    ):
        try:
            r = f.fetch(zh_url)
            try:
                import trafilatura
                txt = trafilatura.extract(r.text, include_comments=False,
                                          favor_precision=True) or ""
            except ImportError:
                txt = "(未装 trafilatura)"
            zh = sum(1 for c in txt if "一" <= c <= "鿿")
            rows.append({"case": name, "ok": zh > 50,
                         "detail": "HTTP %d · %d 字节 · 正文 %d 字(汉字 %d)· 开头:%s"
                                   % (r.status, len(r.body), len(txt), zh,
                                      txt[:50].replace("\n", " "))})
        except Exception as e:                                # noqa: BLE001
            rows.append({"case": name, "ok": False,
                         "detail": "%s: %s" % (type(e).__name__, str(e)[:120])})

    rows.append({"case": "取页器统计", "detail": json.dumps(f.stats(), ensure_ascii=False)})
    return rows


def main():
    offline = "--offline" in sys.argv
    res = {"offline": [], "online": []}
    res["offline"] = _selftest_offline()
    n_ok = sum(1 for r in res["offline"] if r["ok"])
    print("离线自测:%d/%d 通过" % (n_ok, len(res["offline"])))
    for r in res["offline"]:
        if not r["ok"]:
            print("  失败 %s: got=%s want=%s" % (r["case"], r["got"], r["want"]))
    if not offline:
        print("\n联网自测(真抓真网页):")
        res["online"] = _selftest_online()
        for r in res["online"]:
            print("  [%s] %s | %s" % ("OK" if r.get("ok", True) else "FAIL",
                                      r["case"], r.get("detail", "")))
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_selftest_result.json")
    with io.open(out, "w", encoding="utf-8") as fp:
        json.dump(res, fp, ensure_ascii=False, indent=1)
    print("\n结果落盘:%s" % out)
    return 0 if n_ok == len(res["offline"]) else 1


if __name__ == "__main__":
    sys.exit(main())
