/**
 * lanes.js \u2014 \u6a21\u578b lane \u6e05\u5355\u7684\u3010\u552f\u4e00\u771f\u6e90\u3011\u8bbf\u95ee\u5c42
 *
 * \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
 * \u4e3a\u4ec0\u4e48\u6709\u8fd9\u4e2a\u6587\u4ef6
 *
 * 2026-07-27 \u4e4b\u524d\uff0c\u540c\u4e00\u4efd\u6a21\u578b/\u5382\u5546\u6e05\u5355\u5728\u4e09\u4e2a\u5730\u65b9\u5404\u5b58\u4e00\u4efd\uff1a
 *   \u00b7 call_model.js        \u2014\u2014 8 \u6761\u5b9e\u6d4b lane\uff08base / model / env\uff09
 *   \u00b7 provider_registry.js \u2014\u2014 17 \u5bb6\u5382\u5546\u7684 baseUrl + \u6a21\u578b\u5217\u8868
 *   \u00b7 school_model_map.json\u2014\u2014 16 \u5b66\u6d3e\u5404\u81ea\u7684 primary / fallbacks \u6a21\u578b\u540d
 *
 * \u5f53\u665a SiliconFlow \u8fd4\u56de HTTP 402\uff08\u8d26\u6237\u4f59\u989d\u4e0d\u8db3\uff09\uff0c\u53ea\u6709 call_model.js
 * \u90a3\u4e00\u4efd\u77e5\u60c5\uff0c\u53e6\u5916\u4e24\u5904\u4ecd\u7136\u5f53\u5b83\u662f\u6d3b\u7684\u3002\u4e00\u7aef\u6539\u4e86\u3001\u53e6\u4e00\u7aef\u4e0d\u77e5\u9053 \u2014\u2014 \u8fd9\u6b63\u662f
 * \u672c\u9879\u76ee\u53cd\u590d\u72af\u7684\u75c5\u3002
 *
 * \u73b0\u5728\uff1a\u6570\u636e\u53ea\u5b58\u5728 lanes.json\uff0c\u5176\u4f59\u4e09\u5904\u4e00\u5f8b\u4ece\u8fd9\u91cc\u6d3e\u751f\u3002
 *   lanes.json \u2500\u2500\u252c\u2500\u2192 call_model.js         \uff08\u771f\u5b9e\u8c03\u7528\uff09
 *                \u251c\u2500\u2192 provider_registry.js  \uff08\u5382\u5546\u6ce8\u518c\u8868\u7684 baseUrl / \u6a21\u578b\u4e8b\u5b9e\uff09
 *                \u2514\u2500\u2192 school_router.js      \uff0816 \u5b66\u6d3e\u8def\u7531\u94fe\uff0c\u6309 tags \u9009\uff0c\u4e0d\u5199\u6a21\u578b\u540d\uff09
 *
 * \u6539\u4e00\u5904 = \u4e09\u5904\u540c\u6b65\u3002\u60f3\u52a0/\u505c\u4e00\u6761 lane\uff0c\u53ea\u52a8 lanes.json\u3002
 * \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
 *
 * \u3010\u7ea2\u7ebf\u3011\u53ea\u5217\u514d\u8d39\u6c60\u3002\u6309\u91cf\u8ba1\u8d39\u6e90\uff08GPT-4o / Claude Opus / o3 \u7b49\uff09\u4e00\u5f8b\u4e0d\u5165\u8868\uff1b
 * \u9700\u8981\u4ed8\u8d39\u80fd\u529b\u65f6\u5448\u8bc1\u636e\u7ed9\u521b\u59cb\u4eba\u51b3\u5b9a\uff0c\u4ee3\u7801\u91cc\u4e0d\u7559\u540e\u95e8\u3002
 *
 * \u3010\u73af\u5883\u5751 1 \u00b7 \u4ee3\u7406\u3011Node \u539f\u751f fetch **\u4e0d\u8bfb** HTTPS_PROXY \u73af\u5883\u53d8\u91cf\u3002
 *   \u5883\u5916 lane\uff08nvidia / cerebras / agnes\uff09\u5fc5\u987b\u8fd9\u6837\u542f\u52a8\uff1a
 *       node --use-env-proxy scripts/probe_lanes.js
 *   \u5426\u5219\u8fd9\u4e9b lane \u4f1a\u5168\u90e8\u8d85\u65f6\uff0c\u800c\u65e5\u5fd7\u53ea\u663e\u793a"\u8d85\u65f6"\uff0c\u770b\u4e0d\u51fa\u662f\u4ee3\u7406\u6ca1\u751f\u6548\u3002
 *   \u56fd\u5185 lane\uff08zhipu / siliconflow / sensenova\uff09\u901a\u5e38\u5728 NO_PROXY \u540d\u5355\u5185\uff0c\u76f4\u8fde\u5373\u53ef\u3002
 *
 * \u3010\u73af\u5883\u5751 2 \u00b7 UA\u3011Cerebras \u7ad9\u5728 Cloudflare \u540e\u9762\uff1a\u9ed8\u8ba4 node/python UA \u4f1a\u5403
 *   403\uff08error 1010\uff09\u3002\u6240\u6709\u8bf7\u6c42\u5fc5\u987b\u5e26\u6d4f\u89c8\u5668 UA \u2014\u2014 \u89c1\u4e0b\u9762 BROWSER_UA\u3002
 *
 * \u96f6\u4f9d\u8d56\u3001ESM\u3002\u8bfb JSON \u8d70 readFileSync + JSON.parse\uff08\u4e0e school_router.js
 * \u65e2\u6709\u5199\u6cd5\u4e00\u81f4\uff09\uff0c\u4e0d\u7528 import assertion\uff0c\u907f\u514d Node \u7248\u672c\u95f4\u7684\u5b9e\u9a8c\u6027\u544a\u8b66\u3002
 */

import { readFileSync, writeFileSync, existsSync } from 'fs';
import { join, dirname } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));

/** \u771f\u6e90\u6587\u4ef6\u8def\u5f84 \u2014\u2014 \u5168\u9879\u76ee\u552f\u4e00\u7684\u6a21\u578b\u6e05\u5355\u843d\u76d8\u5904 */
export const LANES_PATH = join(__dirname, 'lanes.json');

/**
 * Cerebras \u5728 Cloudflare \u540e\u9762\uff0c\u9ed8\u8ba4 UA \u4f1a\u5403 403 (error 1010)\u3002
 * \u6240\u6709 lane \u7edf\u4e00\u5e26\u8fd9\u4e2a UA\uff0c\u7701\u5f97\u533a\u5206\u3002
 */
export const BROWSER_UA =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ' +
  '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36';

/** status \u5408\u6cd5\u53d6\u503c\u3002probe_lanes.js \u53ea\u4f1a\u5199\u5165\u8fd9\u51e0\u4e2a\u503c\u4e4b\u4e00\u3002 */
export const LANE_STATUS = Object.freeze({
  ACTIVE: 'active',
  NO_CREDIT: 'no_credit',
  NO_KEY: 'no_key',
  RETIRED: 'retired',
  ERROR: 'error',
});

const VALID_STATUS = new Set(Object.values(LANE_STATUS));

// \u2500\u2500 \u52a0\u8f7d \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
let _doc = null;

function loadDoc(force = false) {
  if (_doc && !force) return _doc;
  if (!existsSync(LANES_PATH)) {
    throw new Error(`[lanes] \u771f\u6e90\u6587\u4ef6\u4e0d\u5b58\u5728: ${LANES_PATH}`);
  }
  const doc = JSON.parse(readFileSync(LANES_PATH, 'utf-8'));
  if (!Array.isArray(doc.lanes)) {
    throw new Error('[lanes] lanes.json \u7f3a lanes \u6570\u7ec4');
  }
  // \u6821\u9a8c\uff1a\u72b6\u6001\u503c\u5fc5\u987b\u5408\u6cd5\uff0c\u5426\u5219\u5b81\u53ef\u70b8\u4e5f\u4e0d\u9759\u9ed8\u5e26\u75c5\u8dd1
  for (const l of doc.lanes) {
    if (!VALID_STATUS.has(l.status)) {
      throw new Error(`[lanes] lane ${l.id} \u7684 status \u975e\u6cd5: ${l.status}`);
    }
  }
  doc.lanes.sort((a, b) => (a.rank ?? 999) - (b.rank ?? 999));
  _doc = doc;
  return _doc;
}

/** \u5f3a\u5236\u91cd\u8bfb\u771f\u6e90\uff08probe \u5199\u56de\u540e\u7528\uff09 */
export function reload() {
  return loadDoc(true);
}

/** \u5168\u90e8 lane\uff08\u542b\u5df2\u505c\u7528\u7684\uff09\uff0c\u6309 rank \u6392\u5e8f */
export function allLanes() {
  return loadDoc().lanes;
}

/** \u5382\u5546\u5143\u4fe1\u606f { slug: { label, registryKey, region } } */
export function vendorMeta() {
  return loadDoc().vendors || {};
}

/** \u6309 id \u53d6\u5355\u6761 lane */
export function getLane(id) {
  return allLanes().find(l => l.id === id) || null;
}

/** \u67d0\u5382\u5546\u7684\u5168\u90e8 lane */
export function lanesByVendor(vendor) {
  return allLanes().filter(l => l.vendor === vendor);
}

// \u2500\u2500 \u51ed\u636e \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
/**
 * \u53d6 key\u3002\u652f\u6301\u9017\u53f7\u5206\u9694\u7684\u591a\u628a key\uff0c\u6309 rotate \u8f6e\u6362 \u2014\u2014 \u591a\u628a key \u53ea\u7528\u7b2c\u4e00\u628a
 * \u662f\u767d\u767d\u6d6a\u8d39\u989d\u5ea6\u3002
 *
 * envKey \u4e4b\u5916\u8fd8\u8bd5 envAliases\uff1a\u540c\u4e00\u5bb6\u5382\u5546\u5728\u672c\u673a / \u751f\u4ea7\u7f51\u5173 / \u8001\u811a\u672c\u91cc
 * \u7528\u8fc7\u4e09\u5957\u4e0d\u540c\u53d8\u91cf\u540d\uff08NVIDIA_API_KEY vs NVIDIA_NIM_API_KEY vs NVIDIA_KEYS\uff09\uff0c
 * \u8fd9\u91cc\u4e00\u5e76\u517c\u5bb9\uff0c\u514d\u5f97"\u6709\u51ed\u636e\u5374\u8bfb\u4e0d\u5230"\u8fd9\u79cd\u5047\u6545\u969c\u3002
 */
export function resolveKey(lane, rotate = 0) {
  if (!lane) return '';
  const names = [lane.envKey, ...(lane.envAliases || [])].filter(Boolean);
  const env = (typeof process !== 'undefined' && process.env) ? process.env : {};
  for (const name of names) {
    const raw = env[name] || '';
    const keys = raw.split(',').map(s => s.trim()).filter(Boolean);
    if (keys.length) return keys[rotate % keys.length];
  }
  return '';
}

/** \u8be5 lane \u73af\u5883\u91cc\u662f\u5426\u6709\u51ed\u636e */
export function hasCredential(id) {
  return !!resolveKey(getLane(id));
}

/**
 * \u771f\u6b63\u53ef\u7528\u7684 lane\uff1a\u72b6\u6001\u662f active \u4e14\u672c\u673a\u6709\u51ed\u636e\u3002
 * no_credit / no_key / retired / error \u4e00\u5f8b\u4e0d\u8fdb\u8c03\u7528\u94fe \u2014\u2014 \u94fe\u91cc\u6df7\u8fdb\u4e00\u4e2a\u7528\u4e0d\u4e86\u7684
 * provider\uff0c\u6545\u969c\u65f6\u4f1a\u88ab\u9759\u9ed8\u8df3\u8fc7\u3001\u628a\u5ef6\u8fdf\u644a\u5728\u771f\u5b9e\u8bf7\u6c42\u4e0a\uff0c\u800c\u65e5\u5fd7\u53ea\u663e\u793a"\u5df2\u5207\u6362"\uff0c
 * \u6392\u969c\u65f6\u770b\u4e0d\u51fa\u5b83\u4ece\u6765\u5c31\u6ca1\u80fd\u7528\u8fc7\u3002\u5b81\u53ef\u94fe\u77ed\u800c\u771f\u3002
 */
export function usableLanes() {
  return allLanes().filter(l => l.status === LANE_STATUS.ACTIVE && resolveKey(l));
}

/** \u6309\u80fd\u529b\u6807\u7b7e\u6311 lane\uff0c\u8fd4\u56de\u6392\u597d\u5e8f\u7684 lane \u6570\u7ec4\uff08\u4e0d\u505a\u51ed\u636e\u8fc7\u6ee4\uff0c\u4ea4\u7ed9\u8c03\u7528\u65b9\uff09 */
export function lanesByTags(tags = [], { includeUnusable = false } = {}) {
  const wanted = tags.filter(Boolean);
  const pool = allLanes().filter(l => {
    if (l.status === LANE_STATUS.RETIRED) return false;
    if (includeUnusable) return true;
    return l.status === LANE_STATUS.ACTIVE;
  });

  // \u6253\u5206\uff1a\u8d8a\u9760\u524d\u7684\u6807\u7b7e\u6743\u91cd\u8d8a\u9ad8\uff1b\u540c\u5206\u6309 rank\uff08\u8d5b\u9a6c\u540d\u6b21\uff09\u515c\u5e95
  const score = lane => {
    let s = 0;
    wanted.forEach((t, i) => {
      if ((lane.tags || []).includes(t)) s += (wanted.length - i) * 10;
    });
    return s;
  };

  return pool
    .map(l => ({ lane: l, s: score(l) }))
    .sort((a, b) => (b.s - a.s) || ((a.lane.rank ?? 999) - (b.lane.rank ?? 999)))
    .map(x => x.lane);
}

// \u2500\u2500 \u5199\u56de\uff08\u63a2\u6d3b\u7528\uff09 \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
/**
 * \u628a\u63a2\u6d3b\u7ed3\u679c\u5199\u56de\u771f\u6e90\u3002\u53ea\u5141\u8bb8\u6539 status / lastChecked / lastError / metrics \u2014
 * base / model / envKey \u8fd9\u7c7b\u4eba\u5de5\u4e8b\u5b9e\u4e0d\u8bb8\u88ab\u811a\u672c\u8986\u76d6\u3002
 *
 * @param {Array<{id, status, lastChecked, lastError?, metrics?}>} results
 * @returns {number} \u5b9e\u9645\u6539\u52a8\u7684 lane \u6761\u6570
 */
export function updateLaneStatus(results) {
  const doc = loadDoc(true);
  let changed = 0;

  for (const r of results) {
    const lane = doc.lanes.find(l => l.id === r.id);
    if (!lane) continue;
    if (!VALID_STATUS.has(r.status)) {
      throw new Error(`[lanes] \u62d2\u7edd\u5199\u5165\u975e\u6cd5 status: ${r.id} \u2192 ${r.status}`);
    }
    // retired \u662f\u4eba\u5de5\u51b3\u5b9a\uff0c\u63a2\u6d3b\u4e0d\u8bb8\u628a\u5b83\u6539\u6d3b
    if (lane.status === LANE_STATUS.RETIRED) continue;

    const before = `${lane.status}|${lane.lastChecked}|${lane.lastError}`;
    lane.status = r.status;
    lane.lastChecked = r.lastChecked;
    lane.lastError = r.lastError ?? null;
    if (r.metrics && Object.keys(r.metrics).length) {
      lane.metrics = { ...(lane.metrics || {}), ...r.metrics };
    }
    if (`${lane.status}|${lane.lastChecked}|${lane.lastError}` !== before) changed++;
  }

  writeFileSync(LANES_PATH, JSON.stringify(doc, null, 2) + '\n', 'utf-8');
  _doc = doc;
  return changed;
}

// \u2500\u2500 \u81ea\u68c0 \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
/** \u6e05\u5355\u6982\u89c8\uff0chealthcheck / \u62a5\u544a\u7528 */
export function laneSummary() {
  const lanes = allLanes();
  const byStatus = {};
  for (const l of lanes) byStatus[l.status] = (byStatus[l.status] || 0) + 1;
  return {
    total: lanes.length,
    vendors: new Set(lanes.map(l => l.vendor)).size,
    byStatus,
    usable: usableLanes().length,
    withCredential: lanes.filter(l => resolveKey(l)).length,
    source: LANES_PATH,
  };
}

/**
 * \u4ee3\u7406\u81ea\u68c0\uff1a\u5883\u5916 lane \u9700\u8981 --use-env-proxy\uff0c\u5426\u5219\u4f1a\u5168\u7ebf\u8d85\u65f6\u3002
 * \u8fd4\u56de { needed, enabled, proxy } \u2014\u2014 \u7531\u8c03\u7528\u65b9\u51b3\u5b9a\u662f\u8b66\u544a\u8fd8\u662f\u62d2\u8dd1\u3002
 */
export function proxyStatus() {
  const env = (typeof process !== 'undefined' && process.env) ? process.env : {};
  const proxy = env.HTTPS_PROXY || env.https_proxy || env.ALL_PROXY || '';
  const argv = (typeof process !== 'undefined' && process.execArgv) ? process.execArgv : [];
  const nodeOpts = env.NODE_OPTIONS || '';
  const enabled = argv.includes('--use-env-proxy') || nodeOpts.includes('--use-env-proxy');
  const meta = vendorMeta();
  const needed = allLanes().some(l => meta[l.vendor]?.region === 'overseas');
  return { needed, enabled, proxy };
}
