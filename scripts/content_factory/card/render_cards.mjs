// render_cards.mjs —— 方剂图文卡批量渲染(内容产线·2026-08-31)
//
// 立此因(创始人反复下令的"自动输出图文",终于落地):把 6 万方剂/1953 导读渲成
// 带出处的分享图文卡。架构选型经 feasibility 实测确定:
//   · satori(HTML→SVG) + @resvg/resvg-js(SVG→PNG),纯 JS,一张 ~200ms
//   · ❌ 不走 CF Worker live 端点:CJK 全字体 10-17MB,爆 Worker 体积限制
//   · ✅ 走批量(本脚本·GitHub Actions/Node,全字体无限制)——本地禁算力,批量只在云端
//   · 字体:NotoSansSC 静态实例(OFL 可分发;satori 吃不了 variable 字体,必须静态)
//
// 数据源:D1 sue_formulas(有 CF 密钥时真取);无密钥(fork 自测)→退回 sample_formulas.json。
// 输出:out/card_*.png。上传 R2/服务前台是后续(本轮先出图,进 Actions artifact 可下载核验)。
import satori from 'satori';
import { Resvg } from '@resvg/resvg-js';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FONT = fs.readFileSync(path.join(HERE, 'NotoSansSC-Regular.ttf'));
const OUT = path.join(HERE, 'out');
const LIMIT = parseInt(process.env.CARD_LIMIT || '6', 10);

// R2 上传(可选):有 S_EP/S_AK/S_SK/S_BUCKET(主仓)→卡片 PUT 到 R2 `_cards/formula/` 新前缀持久化。
// 零 R2 移动铁律:只 PUT 新对象、绝不移动/复制现有图、绝不 LIST。照 ocr.py 写 _ocr/ 同款(S3 兼容)。
// 无密钥(fork 自测)→ 跳过上传,只出本地 out/ + Actions artifact(不影响渲染验证)。
const R2_PREFIX = '_cards/formula/';
let _s3 = null, _bucket = null;
async function initR2() {
  const { S_EP, S_AK, S_SK, S_BUCKET } = process.env;
  if (!(S_EP && S_AK && S_SK && S_BUCKET)) return false;
  const { S3Client } = await import('@aws-sdk/client-s3');
  _s3 = new S3Client({ endpoint: S_EP, region: 'auto',
    credentials: { accessKeyId: S_AK, secretAccessKey: S_SK } });
  _bucket = S_BUCKET;
  return true;
}
async function putCard(key, png) {
  if (!_s3) return false;
  const { PutObjectCommand } = await import('@aws-sdk/client-s3');
  await _s3.send(new PutObjectCommand({ Bucket: _bucket, Key: key, Body: png, ContentType: 'image/png' }));
  return true;
}

function parseComp(c) {
  try { const a = JSON.parse(c); return Array.isArray(a) ? a.join('  ') : String(c); }
  catch { return String(c || ''); }
}

async function d1(sql) {
  const acc = process.env.CF_ACCOUNT_ID, db = process.env.D1_DATABASE_ID, tok = process.env.D1_API_TOKEN;
  if (!(acc && db && tok)) return null;
  const r = await fetch(`https://api.cloudflare.com/client/v4/accounts/${acc}/d1/database/${db}/query`, {
    method: 'POST', headers: { Authorization: 'Bearer ' + tok, 'Content-Type': 'application/json' },
    body: JSON.stringify({ sql }),
  });
  const j = await r.json();
  if (!j.success) throw new Error('D1: ' + JSON.stringify(j.errors).slice(0, 160));
  return j.result[0].results;
}

async function getFormulas() {
  try {
    const rows = await d1(
      "SELECT name,book,composition,indication FROM sue_formulas " +
      "WHERE is_formula=1 AND composition IS NOT NULL AND length(composition)>10 " +
      "AND indication IS NOT NULL AND length(indication)>6 AND book IS NOT NULL " +
      `AND length(name) BETWEEN 2 AND 8 AND ai_ok=1 LIMIT ${LIMIT}`);
    if (rows && rows.length) { console.log(`来源: D1 真方剂 ${rows.length} 条`); return rows; }
  } catch (e) { console.log('D1 取数失败,退样例: ' + String(e).slice(0, 120)); }
  const s = JSON.parse(fs.readFileSync(path.join(HERE, 'sample_formulas.json'), 'utf-8'));
  console.log(`来源: 样例(fork 无密钥) ${s.length} 条`);
  return s.slice(0, LIMIT);
}

function cardTree(f) {
  const comp = parseComp(f.composition);
  const ind = String(f.indication || '').slice(0, 80);
  const gold = '#e8c88a', cream = '#f0e6d2', faint = '#8f7a52';
  return { type: 'div', props: { style: { display: 'flex', flexDirection: 'column', width: '100%', height: '100%',
      background: 'linear-gradient(135deg,#2a2118,#463621)', color: cream, padding: '64px 56px',
      fontFamily: 'Noto', justifyContent: 'space-between' }, children: [
    { type: 'div', props: { style: { display: 'flex', flexDirection: 'column' }, children: [
      { type: 'div', props: { style: { fontSize: 60, fontWeight: 700, letterSpacing: 3, color: gold }, children: f.name } },
      { type: 'div', props: { style: { fontSize: 25, marginTop: 14, color: '#b89b6a' }, children: '出自《' + f.book + '》' } },
    ] } },
    { type: 'div', props: { style: { display: 'flex', flexDirection: 'column', gap: 22 }, children: [
      { type: 'div', props: { style: { display: 'flex', fontSize: 32, lineHeight: 1.5, color: cream }, children: '【组成】' + comp } },
      { type: 'div', props: { style: { display: 'flex', fontSize: 30, lineHeight: 1.6, color: '#d8c9a8' }, children: '【主治】' + ind } },
    ] } },
    { type: 'div', props: { style: { display: 'flex', justifyContent: 'space-between', fontSize: 23, color: faint }, children: [
      { type: 'div', props: { children: '古方 AI 星图 · guyaofang.cn' } },
      { type: 'div', props: { children: '君臣佐使 · 文献溯源' } },
    ] } },
  ] } };
}

const formulas = await getFormulas();
fs.mkdirSync(OUT, { recursive: true });
const r2on = await initR2();
console.log(r2on ? `R2 上传: 开(卡片将 PUT 到 ${R2_PREFIX})` : 'R2 上传: 关(无密钥·只出本地+artifact)');
let n = 0, uploaded = 0;
for (const f of formulas) {
  try {
    const svg = await satori(cardTree(f), { width: 1080, height: 1080, fonts: [{ name: 'Noto', data: FONT, weight: 400, style: 'normal' }] });
    const png = new Resvg(svg, { fitTo: { mode: 'width', value: 1080 } }).render().asPng();
    const safe = String(f.name).replace(/[^一-鿿A-Za-z0-9]/g, '');
    fs.writeFileSync(path.join(OUT, `card_${++n}_${safe}.png`), png);
    let up = '';
    if (r2on) {
      try { await putCard(`${R2_PREFIX}${safe}.png`, png); uploaded++; up = ' ↑R2'; }
      catch (e) { up = ` !R2失败:${String(e).slice(0, 50)}`; }
    }
    console.log(`  ✓ card_${n}_${safe}.png (${(png.length / 1024).toFixed(0)}KB)${up}`);
  } catch (e) {
    console.log(`  ! 渲染失败 ${f.name}: ${String(e).slice(0, 100)}`);
  }
}
console.log(`共渲染 ${n} 张方剂卡 → ${OUT}${r2on ? ` · 已传R2 ${uploaded} 张(${R2_PREFIX})` : ''}`);
if (n === 0) process.exit(1);
