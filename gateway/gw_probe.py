# -*- coding: utf-8 -*-
"""
网关注册表探活 —— 在 GitHub Actions 跑，把每家的真实状态写回 providers.json。

为什么必须在 Actions 而不是 CF Workers：
runner 每次分配独立 IP，能测到 CF 出网打不通的那些免费源
（opencode 在 CF 侧固定 429、NVIDIA 曾 503），Workers 里永远测不出它们的真实状态。

只改 status / checked_at / latency_ms 三个字段，其余人工维护的字段一律不碰。
"""
import json, os, sys, time, urllib.request, urllib.error

ROOT = os.path.dirname(os.path.abspath(__file__))
REG  = os.path.join(ROOT, 'providers.json')
Q    = [{'role': 'user', 'content': '说"通"'}]


def call(p, key):
    body = {'model': p['model'], 'messages': Q, 'max_tokens': p.get('min_max_tokens') or 256}
    req = urllib.request.Request(
        p['base'].rstrip('/') + '/chat/completions',
        data=json.dumps(body).encode(),
        headers={'Content-Type': 'application/json',
                 **({'Authorization': f'Bearer {key}'} if key else {})})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            d = json.loads(r.read())
        msg = (d.get('choices') or [{}])[0].get('message', {})
        txt = (msg.get('content') or msg.get('reasoning_content') or '').strip()
        return ('ok' if txt else 'empty'), int((time.time() - t0) * 1000), ''
    except urllib.error.HTTPError as e:
        return f'http_{e.code}', int((time.time() - t0) * 1000), e.read()[:120].decode('utf-8', 'replace')
    except Exception as e:
        return 'error', int((time.time() - t0) * 1000), str(e)[:120]


def main():
    doc = json.load(open(REG, encoding='utf-8'))
    ps = doc['providers']
    only_enabled = os.environ.get('PROBE_ALL', '') != '1'
    n_ok = 0
    for p in ps:
        if only_enabled and not p.get('enabled'):
            continue
        if p.get('needs_extra_headers'):
            p['status'] = 'skip_needs_headers'   # 身份头逻辑在网关里，探活脚本不复制一份
            continue
        key = os.environ.get(p.get('secret') or '', '')
        if p.get('auth') == 'apikey' and not key:
            p['status'] = 'no_key'
            continue
        st, ms, err = call(p, key)
        p['status'] = st
        p['latency_ms'] = ms
        p['checked_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
        if err:
            p['last_error'] = err
        elif 'last_error' in p:
            del p['last_error']
        if st == 'ok':
            n_ok += 1
        print(f"  {'OK ' if st=='ok' else 'BAD'} {p['id']:24} {st:14} {ms:>6}ms {err[:60]}", flush=True)

    doc['updated_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
    doc.setdefault('counts', {})['ok'] = n_ok
    json.dump(doc, open(REG, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print(f'\n  可用 {n_ok} 家')


if __name__ == '__main__':
    main()
