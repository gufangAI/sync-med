// render_cards.mjs —— 方剂图文卡批量渲染(内容产线·2026-09-02 痛点驱动版)
//
// 【为什么是这个版式】创始人 2026-09-01:「天天拿个几千年前的思维来看,00后90后会喜欢吗?」
// 于是去 B站 拉真实播放量数据(不是我拍脑袋):
//   痛点类「拯救你的痘痘肌」419万 · 「熬夜/脸色蜡黄人群」135万
//   纯方名类「小柴胡汤」仅 11万  → **差 40 倍**
// 结论:年轻人不搜方名、搜症状;不要知识、要"我该怎么办"。
// 故版式 = ①顶部点名人群("说的就是我") ②主标题是**用户的痛**(不是方名)
//        ③古方降为底部背书 ④真古籍书页做底(独家资产·别人抄不走)
//
// 【铁律】未经创始人审核不得发布,见 PUBLISH_GATE.md。出处一律「见于」不写「出自」。
import satori from 'satori';
import { Resvg } from '@resvg/resvg-js';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FONT = fs.readFileSync(path.join(HERE, 'NotoSansSC-Regular.ttf'));
const OUT = path.join(HERE, 'out');
const HOOKS = JSON.parse(fs.readFileSync(path.join(HERE, 'hooks.json'), 'utf-8'));

// 背景:真古籍书页(bg/*.jpg,由 fetch_bg.py 从 123 预抓)。无则退回纯色底。
let BGS = [];
try {
  BGS = fs.readdirSync(path.join(HERE, 'bg')).filter(f => /\.(jpg|jpeg|png)$/i.test(f))
    .map(f => 'data:image/jpeg;base64,' + fs.readFileSync(path.join(HERE, 'bg', f)).toString('base64'));
} catch { /* 无 bg → 纯色底 */ }

function cardTree(name, h, bg) {
  const layers = [];
  if (bg) {
    layers.push({ type: 'img', props: { src: bg, style: { position: 'absolute', top: 0, left: 0, width: '1080px', height: '1080px', objectFit: 'cover' } } });
    layers.push({ type: 'div', props: { style: { position: 'absolute', top: 0, left: 0, width: '1080px', height: '1080px',
      background: 'linear-gradient(160deg, rgba(18,13,7,.90) 0%, rgba(24,17,9,.86) 55%, rgba(14,10,5,.95) 100%)' } } });
  }
  layers.push({ type: 'div', props: { style: { position: 'relative', display: 'flex', flexDirection: 'column',
      width: '100%', height: '100%', padding: '70px 62px', justifyContent: 'space-between',
      ...(bg ? {} : { background: 'linear-gradient(160deg,#1a140c,#2e2416)' }) }, children: [
    { type: 'div', props: { style: { display: 'flex' }, children: [
      { type: 'div', props: { style: { fontSize: 26, color: '#1a140c', background: '#e8c072', padding: '8px 20px', borderRadius: 24, letterSpacing: 1 }, children: h.who } },
    ] } },
    { type: 'div', props: { style: { display: 'flex', flexDirection: 'column', gap: 26 }, children: [
      { type: 'div', props: { style: { display: 'flex', fontSize: 72, fontWeight: 700, lineHeight: 1.28, color: '#fff6e4', letterSpacing: 2, textShadow: '0 3px 16px rgba(0,0,0,.8)' }, children: h.hook } },
      { type: 'div', props: { style: { display: 'flex', fontSize: 30, lineHeight: 1.6, color: '#d9c9a8' }, children: h.note } },
    ] } },
    { type: 'div', props: { style: { display: 'flex', flexDirection: 'column', gap: 18 }, children: [
      { type: 'div', props: { style: { display: 'flex', alignItems: 'center', gap: 14,
          background: 'rgba(200,160,90,.14)', borderLeft: '4px solid #c9a45e', padding: '20px 24px', borderRadius: 3 }, children: [
        { type: 'div', props: { style: { fontSize: 25, color: '#e8c072', letterSpacing: 1 }, children: h.tag } },
        // 「见于」不写「出自」:sue_formulas.book 是"记录抄自哪本"≠"首出何书"
        { type: 'div', props: { style: { fontSize: 27, color: '#f2e8d5', letterSpacing: 1 }, children: `${name} · 见于《${h.book}》` } },
      ] } },
      { type: 'div', props: { style: { display: 'flex', justifyContent: 'space-between', fontSize: 21, color: '#8f7c58' }, children: [
        { type: 'div', props: { children: '古方 AI 星图 · gufangai.com' } },
        { type: 'div', props: { children: bg ? '底图：平台藏古籍原书影像' : '君臣佐使 · 文献溯源' } },
      ] } },
    ] } },
  ] } });
  return { type: 'div', props: { style: { display: 'flex', width: '100%', height: '100%', position: 'relative', fontFamily: 'Noto' }, children: layers } };
}

const names = Object.keys(HOOKS).filter(k => !k.startsWith('_'));
fs.mkdirSync(OUT, { recursive: true });
console.log(`痛点映射 ${names.length} 个方剂 · 背景图 ${BGS.length} 张`);
let n = 0;
for (const name of names) {
  try {
    const bg = BGS.length ? BGS[n % BGS.length] : null;
    const svg = await satori(cardTree(name, HOOKS[name], bg), { width: 1080, height: 1080, fonts: [{ name: 'Noto', data: FONT, weight: 400, style: 'normal' }] });
    const png = new Resvg(svg, { fitTo: { mode: 'width', value: 1080 } }).render().asPng();
    const safe = name.replace(/[^一-鿿A-Za-z0-9]/g, '');
    fs.writeFileSync(path.join(OUT, `card_${++n}_${safe}.png`), png);
    console.log(`  ✓ card_${n}_${safe}.png (${(png.length / 1024).toFixed(0)}KB)`);
  } catch (e) {
    console.log(`  ! 渲染失败 ${name}: ${String(e).slice(0, 110)}`);
  }
}
console.log(`共渲染 ${n} 张 → ${OUT}(未经审核不得发布·见 PUBLISH_GATE.md)`);
if (n === 0) process.exit(1);
