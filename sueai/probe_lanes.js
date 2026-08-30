/**
 * probe_lanes.js \u2014 \u514d\u8d39\u6c60 lane \u63a2\u6d3b
 *
 * \u9010\u6761 lane \u53d1\u4e00\u6b21\u6781\u5c0f\u8bf7\u6c42\uff08max_tokens: 5\uff09\uff0c\u628a\u771f\u5b9e\u7ed3\u679c\u5199\u56de\u552f\u4e00\u771f\u6e90
 * sueai/lanes.json \u7684 status / lastChecked / lastError\u3002
 *
 * \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
 * \u4e3a\u4ec0\u4e48\u8981\u6709\u5b83
 *
 * 2026-07-27\uff1aSiliconFlow \u8d26\u6237\u4f59\u989d\u8017\u5c3d\uff08HTTP 402\uff09\u3002\u6628\u591c\u8d5b\u9a6c\u8fd8\u597d\u597d\u7684\uff0c
 * \u662f\u4e0b\u4e00\u6b21\u771f\u5b9e\u8c03\u7528\u70b8\u4e86\u624d\u53d1\u73b0 \u2014\u2014 \u800c\u5f53\u65f6\u6e05\u5355\u5b58\u5728\u4e09\u4e2a\u6587\u4ef6\u91cc\uff0c\u53ea\u6709\u4e00\u5904\u88ab\u6539\uff0c
 * \u53e6\u5916\u4e24\u5904\u4ecd\u628a\u5b83\u5f53\u6d3b\u7684\u5f80\u5916\u53d1\u3002
 *
 * \u63a2\u6d3b\u7684\u610f\u4e49\u5c31\u662f\u628a"\u4e0b\u6b21\u8c03\u7528\u624d\u70b8"\u63d0\u524d\u6210"\u5b9a\u65f6\u53d1\u73b0"\u3002
 * \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
 *
 * \u3010\u600e\u4e48\u8dd1\u3011\u5883\u5916 lane\uff08nvidia / cerebras / agnes\uff09\u5fc5\u987b\u5e26 --use-env-proxy\uff0c
 * \u56e0\u4e3a Node \u539f\u751f fetch \u4e0d\u8bfb HTTPS_PROXY \u73af\u5883\u53d8\u91cf\uff0c\u4e0d\u5e26\u5c31\u5168\u7ebf\u8d85\u65f6\uff1a
 *
 *     node --use-env-proxy scripts/probe_lanes.js
 *
 * \u53c2\u6570\uff1a
 *     --dry-run        \u53ea\u63a2\u4e0d\u5199\u56de
 *     --only <id>      \u53ea\u63a2\u67d0\u4e00\u6761 lane\uff08\u53ef\u91cd\u590d\uff09
 *     --sleep <ms>     \u6bcf\u6761\u4e4b\u95f4\u7684\u95f4\u9694\uff0c\u9ed8\u8ba4 3000
 *     --timeout <ms>   \u5355\u6761\u8d85\u65f6\uff0c\u9ed8\u8ba4 30000
 *
 * \u3010\u9650\u6d41\u7eaa\u5f8b\u3011\u4e25\u683c\u4e32\u884c + \u6bcf\u6761\u4e4b\u95f4 sleep\u3002\u7edd\u4e0d\u5e76\u53d1\u8f70 \u2014\u2014 Cerebras \u9650\u6d41\u5f88\u7d27\uff0c
 * \u5e76\u53d1\u63a2\u6d3b\u672c\u8eab\u5c31\u4f1a\u628a lane \u63a2\u6302\uff0c\u90a3\u662f\u81ea\u5df1\u9020\u7684\u5047\u6545\u969c\u3002
 *
 * \u3010\u7ea2\u7ebf\u3011\u53ea\u63a2\u514d\u8d39\u6c60\u3002\u6309\u91cf\u8ba1\u8d39\u6e90\u4e00\u5f8b\u4e0d\u5728 lanes.json \u91cc\uff0c\u6545\u4e5f\u6c38\u8fdc\u4e0d\u4f1a\u88ab\u63a2\u5230\u3002
 *
 * \u96f6\u4f9d\u8d56\u3001ESM\u3002
 */

import { pathToFileURL } from 'url';
import {
  allLanes, getLane, resolveKey, updateLaneStatus,
  proxyStatus, vendorMeta, BROWSER_UA, LANE_STATUS,
} from './lanes.js';

// \u2500\u2500 \u53c2\u6570 \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
function parseArgs(argv) {
  const o = { dryRun: false, only: [], sleepMs: 3000, timeoutMs: 30000, retry429Ms: 15000 };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--dry-run') o.dryRun = true;
    else if (a === '--only') o.only.push(argv[++i]);
    else if (a === '--sleep') o.sleepMs = Number(argv[++i]);
    else if (a === '--timeout') o.timeoutMs = Number(argv[++i]);
    else if (a === '--retry429') o.retry429Ms = Number(argv[++i]);
  }
  return o;
}

const sleep = ms => new Promise(r => setTimeout(r, ms));
const today = () => new Date().toISOString().slice(0, 10);

// \u2500\u2500 \u5224\u5b9a \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
/**
 * \u628a\u4e00\u6b21\u54cd\u5e94\u7ffb\u8bd1\u6210 status\u3002\u5224\u636e\u5199\u6b7b\u5728\u8fd9\u91cc\uff0c\u522b\u6563\u5230\u8c03\u7528\u5904 \u2014\u2014 \u6563\u4e86\u5c31\u4f1a\u518d\u6f02\u79fb\u3002
 *
 *   HTTP 200 + \u6709\u5185\u5bb9              \u2192 active
 *   HTTP 402 / \u6587\u6848\u63d0\u5230\u4f59\u989d\u3001\u6b20\u8d39   \u2192 no_credit
 *   HTTP 401 / \u51ed\u636e\u88ab\u62d2             \u2192 no_key
 *   HTTP 429 + rpm/tpm/\u9891\u7387        \u2192 active\uff08\u51ed\u636e\u6709\u6548\uff0c\u53ea\u662f\u9650\u6d41\uff0c\u5c5e\u77ac\u65f6\uff09
 *   HTTP 429 + \u4f59\u989d/\u6b20\u8d39            \u2192 no_credit
 *   HTTP 403 + Cloudflare 1010      \u2192 error\uff08UA \u88ab\u62e6\uff0c\u4e0d\u662f\u8d26\u53f7\u95ee\u9898\uff09
 *   \u5176\u5b83 4xx/5xx\u3001\u8d85\u65f6\u3001\u7f51\u7edc\u9519\u8bef     \u2192 error
 *
 * \u30102026-07-27 \u4fee\u6b63\u3011\u9650\u6d41 \u2260 \u6ca1\u94b1\uff0c\u522b\u6df7\u3002\u9996\u8dd1\u65f6 sensenova \u8fd4\u56de
 *   `429 {"message":"rpm exhausted","type":"quota_exceeded_error"}`\uff0c
 * \u65e7\u5224\u636e\u91cc `quota.*exhaust` \u547d\u4e2d\uff0c\u628a\u4e00\u6761\u5065\u5eb7 lane \u8bef\u5224\u6210 no_credit \u2014\u2014 \u90a3\u4f1a
 * \u628a\u5b83\u4ece\u6240\u6709\u8c03\u7528\u94fe\u91cc\u6458\u6389\uff0c\u662f\u81ea\u5df1\u9020\u7684\u5047\u6545\u969c\u3002rpm/tpm \u662f\u6bcf\u5206\u949f\u914d\u989d\uff0c\u7761\u4e00\u4f1a\u5c31\u6062\u590d\uff1b
 * \u53ea\u6709\u771f\u7684\u8bf4\u4f59\u989d/\u6b20\u8d39\u624d\u7b97 no_credit\u3002\u5224\u9650\u6d41\u5fc5\u987b\u5148\u4e8e\u5224\u4f59\u989d\u3002
 */
const RATE_PAT = /\br?pm\b|\btpm\b|rate.?limit|too many requests|requests? per|\u9891\u7387|\u5e76\u53d1|qps/i;
const CREDIT_PAT = /\u4f59\u989d|\u6b20\u8d39|insufficient|balance|arrears|credit|quota.*(exhaust|exceed)/i;
const AUTH_PAT = /invalid.*(api.?key|token)|unauthorized|\u8ba4\u8bc1\u5931\u8d25|\u9274\u6743|api.?key.*(invalid|error)/i;

function classify(httpStatus, bodyText) {
  const body = bodyText || '';
  if (httpStatus === 402) return { status: LANE_STATUS.NO_CREDIT, reason: `HTTP 402 ${trim(body)}` };
  if (httpStatus === 401) return { status: LANE_STATUS.NO_KEY, reason: `HTTP 401 ${trim(body)}` };
  if (httpStatus === 429) {
    // \u987a\u5e8f\u8981\u7d27\uff1a\u5148\u8ba4\u9650\u6d41\uff0c\u518d\u8ba4\u4f59\u989d\u3002\u53cd\u8fc7\u6765\u4f1a\u628a "rpm exhausted" \u8bef\u5224\u6210\u6ca1\u94b1\u3002
    if (RATE_PAT.test(body)) {
      return { status: LANE_STATUS.ACTIVE, reason: `HTTP 429 \u9650\u6d41\uff08\u51ed\u636e\u6709\u6548\uff0c\u77ac\u65f6\uff09: ${trim(body)}` };
    }
    if (CREDIT_PAT.test(body)) return { status: LANE_STATUS.NO_CREDIT, reason: `HTTP 429 ${trim(body)}` };
    return { status: LANE_STATUS.ACTIVE, reason: `HTTP 429 \u9650\u6d41\uff08\u51ed\u636e\u6709\u6548\uff09` };
  }
  if (httpStatus === 403) {
    if (/1010|cloudflare/i.test(body)) {
      return { status: LANE_STATUS.ERROR, reason: 'HTTP 403 Cloudflare 1010 \u2014 UA \u88ab\u62e6\uff0c\u975e\u8d26\u53f7\u95ee\u9898' };
    }
    return { status: LANE_STATUS.NO_KEY, reason: `HTTP 403 ${trim(body)}` };
  }
  if (httpStatus >= 400) {
    if (CREDIT_PAT.test(body)) return { status: LANE_STATUS.NO_CREDIT, reason: `HTTP ${httpStatus} ${trim(body)}` };
    if (AUTH_PAT.test(body)) return { status: LANE_STATUS.NO_KEY, reason: `HTTP ${httpStatus} ${trim(body)}` };
    return { status: LANE_STATUS.ERROR, reason: `HTTP ${httpStatus} ${trim(body)}` };
  }
  return null;
}

const trim = s => String(s).replace(/\s+/g, ' ').slice(0, 140);

// \u2500\u2500 \u5355\u6761\u63a2\u6d3b \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
async function probeOne(lane, timeoutMs) {
  const key = resolveKey(lane);
  if (!key) {
    return {
      id: lane.id, status: LANE_STATUS.NO_KEY, lastChecked: today(),
      lastError: `\u73af\u5883\u91cc\u6ca1\u6709 ${lane.envKey}\uff08\u4e5f\u6ca1\u6709\u522b\u540d ${(lane.envAliases || []).join('/') || '\u65e0'}\uff09`,
      metrics: {},
    };
  }

  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  const t0 = Date.now();
  try {
    const r = await fetch(lane.base.replace(/\/$/, '') + '/chat/completions', {
      method: 'POST',
      headers: {
        'Authorization': 'Bearer ' + key,
        'Content-Type': 'application/json',
        'User-Agent': BROWSER_UA,     // Cerebras \u5728 CF \u540e\u9762\uff0c\u9ed8\u8ba4 UA \u5403 403 (1010)
      },
      body: JSON.stringify({
        model: lane.model,
        messages: [{ role: 'user', content: 'ping' }],
        max_tokens: 5,                // \u6781\u5c0f\u8bf7\u6c42\uff1a\u63a2\u6d3b\u4e0d\u70e7\u989d\u5ea6
        temperature: 0,
      }),
      signal: ctrl.signal,
    });

    const elapsed = Date.now() - t0;
    const text = await r.text().catch(() => '');
    const verdict = classify(r.status, text);
    if (verdict) {
      // active \u65f6 reason \u4e5f\u7559\u7740\uff1a\u50cf "429 \u9650\u6d41" \u8fd9\u79cd\u867d\u4e0d\u81f4\u547d\uff0c\u4f46\u6392\u969c\u65f6\u8981\u770b\u5f97\u89c1
      return {
        id: lane.id, status: verdict.status, lastChecked: today(),
        lastError: verdict.reason,
        metrics: { probeMs: elapsed },
      };
    }

    // 2xx\uff1a\u786e\u8ba4\u771f\u6709\u5185\u5bb9\u8fd4\u56de\uff0c\u522b\u628a"200 \u4f46\u7a7a body"\u5f53\u6210\u6d3b\u7684
    let content = '';
    // A lane is alive if it returned any content at all. Providers put it in
    // one of four places; checking only the first marked working providers
    // dead (agnes / sensenova answered 200, were failed as "no content"):
    //   message.content            standard
    //   message.reasoning_content  reasoning models leave content empty
    //   choices[0].text            legacy completions shape
    //   delta.content              some servers reply in streaming shape
    let shape = '';
    try {
      const c0 = JSON.parse(text)?.choices?.[0] ?? {};
      for (const [k, v] of [
        ['message.content', c0.message?.content],
        ['message.reasoning_content', c0.message?.reasoning_content],
        ['text', c0.text],
        ['delta.content', c0.delta?.content],
      ]) {
        if (typeof v === 'string' && v.trim()) { content = v; shape = k; break; }
      }
    } catch { /* non-JSON body */ }
    if (!content) {
      return {
        id: lane.id, status: LANE_STATUS.ERROR, lastChecked: today(),
        lastError: `HTTP ${r.status} \u4f46\u65e0 content: ${trim(text)}`,
        metrics: { probeMs: elapsed },
      };
    }
    return {
      id: lane.id, status: LANE_STATUS.ACTIVE, lastChecked: today(),
      lastError: null, metrics: { probeMs: elapsed },
    };
  } catch (e) {
    const elapsed = Date.now() - t0;
    const aborted = e.name === 'AbortError';
    return {
      id: lane.id, status: LANE_STATUS.ERROR, lastChecked: today(),
      lastError: aborted
        ? `\u8d85\u65f6 ${timeoutMs}ms\uff08\u5883\u5916 lane \u8bf7\u786e\u8ba4\u5df2\u52a0 --use-env-proxy\uff09`
        : trim(e.message || String(e)),
      metrics: { probeMs: elapsed },
    };
  } finally {
    clearTimeout(timer);
  }
}

// \u2500\u2500 \u4e3b\u6d41\u7a0b \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
async function main() {
  const opt = parseArgs(process.argv.slice(2));
  const meta = vendorMeta();

  let targets = allLanes().filter(l => l.status !== LANE_STATUS.RETIRED);
  if (opt.only.length) targets = opt.only.map(getLane).filter(Boolean);

  const px = proxyStatus();
  console.log('\u514d\u8d39\u6c60 lane \u63a2\u6d3b');
  console.log('='.repeat(74));
  console.log(`\u771f\u6e90     : sueai/lanes.json`);
  console.log(`\u5f85\u63a2     : ${targets.length} \u6761${opt.dryRun ? '\uff08--dry-run\uff0c\u4e0d\u5199\u56de\uff09' : ''}`);
  console.log(`\u8282\u6d41     : \u4e32\u884c\uff0c\u6bcf\u6761\u95f4\u9694 ${opt.sleepMs}ms\uff0c\u5355\u6761\u8d85\u65f6 ${opt.timeoutMs}ms`);
  if (px.needed) {
    console.log(`\u4ee3\u7406     : HTTPS_PROXY=${px.proxy || '(\u672a\u8bbe)'} | --use-env-proxy ${px.enabled ? '\u2713 \u5df2\u542f\u7528' : '\u2717 \u672a\u542f\u7528'}`);
    if (!px.enabled) {
      console.log('           \u26a0 \u5883\u5916 lane\uff08nvidia/cerebras/agnes\uff09\u5927\u6982\u7387\u5168\u90e8\u8d85\u65f6\u3002');
      console.log('           \u26a0 \u6b63\u786e\u8dd1\u6cd5\uff1anode --use-env-proxy scripts/probe_lanes.js');
    }
  }
  console.log('='.repeat(74));

  const results = [];
  for (let i = 0; i < targets.length; i++) {
    const lane = targets[i];
    const region = meta[lane.vendor]?.region || '?';
    process.stdout.write(`[${i + 1}/${targets.length}] ${lane.id.padEnd(24)} ${region.padEnd(9)} ... `);
    let res = await probeOne(lane, opt.timeoutMs);

    /**
     * 429 \u5fc5\u987b\u590d\u6838\u4e00\u6b21\u624d\u8bb8\u4e0b\u7ed3\u8bba\u3002
     *
     * \u30102026-07-27 \u5b9e\u8bc1\u3011SenseNova \u540c\u4e00\u65f6\u6bb5\u8fd4\u56de\u8fc7\u4e24\u79cd\u6587\u6848\uff1a
     *   "rpm exhausted"\uff08\u770b\u7740\u50cf\u77ac\u65f6\u9650\u6d41\uff09\u548c "token plan limit exhausted"\uff08\u989d\u5ea6\u8017\u5c3d\uff09\u3002
     * \u53ea\u53d6\u4e00\u6b21\u6837\u672c\uff0c\u540c\u4e00\u6761 lane \u4f1a\u5728 active / no_credit \u4e4b\u95f4\u6765\u56de\u8df3 \u2014\u2014 \u72b6\u6001\u4e00\u6296\uff0c
     * 16 \u4e2a\u5b66\u6d3e\u7684\u8c03\u7528\u94fe\u5c31\u8ddf\u7740\u6296\u3002
     *
     * \u9694\u4e00\u6bb5\u518d\u6253\u4e00\u6b21\uff1a\u8fd8\u80fd\u901a = \u771f\u7684\u53ea\u662f\u77ac\u65f6\u9650\u6d41\uff08active\uff09\uff1b\u4ecd\u65e7 429 = \u4e0d\u662f\u77ac\u65f6\uff0c
     * \u6309\u989d\u5ea6\u8017\u5c3d\u5904\u7406\uff08no_credit\uff09\uff0c\u5e76\u628a\u4e24\u6b21\u89c2\u6d4b\u90fd\u8bb0\u8fdb lastError\u3002
     * \u8fd9\u5c31\u662f\u300c\u4f1a\u5413\u5230\u4eba\u7684\u7ed3\u8bba\u5fc5\u987b\u4e24\u6761\u72ec\u7acb\u89c2\u6d4b\u652f\u6491\u300d\u843d\u5230\u5de5\u5177\u91cc\u3002
     */
    if (/HTTP 429/.test(res.lastError || '') && opt.retry429Ms > 0) {
      process.stdout.write(`429 \u590d\u6838(\u7b49${Math.round(opt.retry429Ms / 1000)}s)... `);
      await sleep(opt.retry429Ms);
      const again = await probeOne(lane, opt.timeoutMs);
      if (/HTTP 429/.test(again.lastError || '')) {
        res = {
          ...again,
          status: LANE_STATUS.NO_CREDIT,
          lastError: `429 \u6301\u7eed\uff08\u95f4\u9694 ${Math.round(opt.retry429Ms / 1000)}s \u4e24\u6b21\u5747\u9650\u6d41\uff09: ${again.lastError}`,
        };
      } else {
        res = again;   // \u590d\u6838\u901a\u8fc7\uff0c\u4ee5\u7b2c\u4e8c\u6b21\u4e3a\u51c6
      }
    }
    results.push(res);

    const mark = res.status === LANE_STATUS.ACTIVE ? '\u2713' : '\u2717';
    const ms = res.metrics?.probeMs != null ? `${res.metrics.probeMs}ms` : '';
    console.log(`${mark} ${res.status.padEnd(10)} ${ms.padEnd(8)} ${res.lastError || ''}`);

    // \u4e32\u884c + sleep\uff0c\u7edd\u4e0d\u5e76\u53d1\u8f70
    if (i < targets.length - 1) await sleep(opt.sleepMs);
  }

  // \u2500\u2500 \u6c47\u603b \u2500\u2500
  console.log('='.repeat(74));
  const by = {};
  for (const r of results) (by[r.status] ||= []).push(r.id);
  for (const s of [LANE_STATUS.ACTIVE, LANE_STATUS.NO_CREDIT, LANE_STATUS.NO_KEY, LANE_STATUS.ERROR]) {
    if (by[s]) console.log(`${s.padEnd(10)} ${by[s].length} \u6761: ${by[s].join(', ')}`);
  }

  if (opt.dryRun) {
    console.log('\n--dry-run\uff1a\u672a\u5199\u56de lanes.json');
  } else {
    const changed = updateLaneStatus(results);
    console.log(`\n\u5df2\u5199\u56de lanes.json\uff1a${changed} \u6761\u72b6\u6001\u6709\u53d8\u66f4\uff08\u5171 ${results.length} \u6761\u63a2\u6d3b\uff09`);
  }

  // \u5173\u952e\u7f3a\u53e3\u4e0d\u80fd\u53ea\u8eba\u5728 run log \u91cc \u2014\u2014 \u7528\u9000\u51fa\u7801\u628a\u5b83\u9876\u4e0a\u53bb\uff0c\u597d\u8ba9 CI / cron \u80fd\u770b\u89c1
  const broken = (by[LANE_STATUS.NO_CREDIT]?.length || 0) + (by[LANE_STATUS.ERROR]?.length || 0);
  if (broken) {
    console.log(`\n\u26a0 ${broken} \u6761 lane \u9700\u8981\u4eba\u770b\uff08no_credit / error\uff09`);
    process.exitCode = 2;
  }
}

// \u2500\u2500 \u5165\u53e3\u4fdd\u62a4 \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
/**
 * \u53ea\u6709\u88ab\u5f53\u6210\u811a\u672c\u76f4\u63a5\u6267\u884c\u65f6\u624d\u771f\u7684\u63a2\u6d3b\u3002
 *
 * \u30102026-07-27 \u8840\u8bc1\u3011\u672c\u6587\u4ef6\u65e9\u5148\u5728\u9876\u5c42\u76f4\u63a5 `main()`\uff0c\u7ed3\u679c\u4e00\u53e5
 * `await import('scripts/probe_lanes.js')`\uff08\u672c\u610f\u53ea\u662f\u60f3\u9a8c\u8bc1\u8bed\u6cd5\uff09\u5c31\u771f\u7684\u53d1\u4e86
 * 8 \u6b21\u8bf7\u6c42\u3001\u5e76\u628a\u7ed3\u679c\u5199\u56de\u4e86 lanes.json \u2014\u2014 \u65e0\u51ed\u636e\u73af\u5883\u4e0b\u628a 7 \u6761\u5065\u5eb7 lane \u5199\u6210
 * no_key\uff0c\u6c61\u67d3\u4e86\u552f\u4e00\u771f\u6e90\u3002
 *
 * \u63a2\u6d3b\u4f1a\u53d1\u771f\u5b9e\u8bf7\u6c42\u3001\u4f1a\u6539\u771f\u6e90\uff0c\u5c5e\u4e8e\u6709\u526f\u4f5c\u7528\u7684\u52a8\u4f5c\uff0c\u7edd\u4e0d\u80fd\u88ab\u4e00\u6b21 import \u89e6\u53d1\u3002
 */
const isDirectRun = (() => {
  const entry = process.argv[1];
  if (!entry) return false;
  try {
    return import.meta.url === pathToFileURL(entry).href;
  } catch {
    return false;
  }
})();

if (isDirectRun) {
  main().catch(e => {
    console.error('\u63a2\u6d3b\u811a\u672c\u81ea\u8eab\u51fa\u9519:', e);
    process.exitCode = 1;
  });
}

// \u4f9b\u6d4b\u8bd5\u5f15\u7528\u5224\u636e\uff0c\u4e0d\u89e6\u53d1\u4efb\u4f55\u7f51\u7edc\u8bf7\u6c42
export { classify, probeOne, main };
