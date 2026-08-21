# -*- coding: utf-8 -*-
"""
基金观察 · 微信估值快报推送脚本（v3：北京时间 + 盈亏合计 + 排版优化）
==========================================
推送内容：持仓/行业 名称 + 今日涨跌% + 预估盈亏 + 规模 + 今日合计 + 总盈亏

使用：
  1. 微信扫码注册 https://www.pushplus.plus/ 获取 token
  2. 设置环境变量 PUSHPLUS_TOKEN=<token> 或 --token 参数
  3. 运行：python push_wechat.py
     常驻调度：python push_wechat.py --token xxxx --schedule

调度：
  - GitHub Actions（.github/workflows/hourly.yml）：交易时段每小时，注意是 UTC，
    脚本内部已统一用北京时间显示
  - 本地 Windows 计划任务：安装定时推送.bat
"""
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

# 北京时间（脚本运行在 GitHub Actions 时是 UTC，必须显式转北京）
BEIJING = timezone(timedelta(hours=8))

# ============================================================
#  配置：持仓（amount=当前持有金额，cost=买入成本）
#        etf=场内对应ETF(盘中估算用)，None=用持仓加权
# ============================================================
HOLDINGS = [
    {'code': '501058', 'name': '新能源车C', 'etf': '515030', 'amount': 10383, 'cost': 10000},
    {'code': '023639', 'name': '电网设备C', 'etf': '159320', 'amount': 9286,  'cost': 10000},
    {'code': '004997', 'name': '高端制造A', 'etf': None,    'amount': 5708,  'cost': 10000},
    {'code': '020973', 'name': '机器人C',   'etf': '159530', 'amount': 4901,  'cost': 5000},
    {'code': '007300', 'name': '半导体A',   'etf': '512480', 'amount': 4441,  'cost': 5000},
    {'code': '027676', 'name': '科创50C',   'etf': '588000', 'amount': 907,   'cost': 1000},
    {'code': '004042', 'name': '华夏鼎茂债', 'etf': None,    'amount': 150000, 'cost': 150000},
    {'code': '003039', 'name': '广发集富债', 'etf': None,    'amount': 50000,  'cost': 50000},
]

SECTORS = [
    {'code': '161725', 'name': '白酒',     'etf': '512690'},
    {'code': '008020', 'name': '人工智能', 'etf': '159819'},
    {'code': '004432', 'name': '有色金属', 'etf': '512400'},
    {'code': '008279', 'name': '煤炭',     'etf': '515220'},
    {'code': '011102', 'name': '光伏',     'etf': '515790'},
    {'code': '006756', 'name': '生物医药', 'etf': '512010'},
    {'code': '001594', 'name': '银行',     'etf': '512800'},
    {'code': '000596', 'name': '军工',     'etf': '512660'},
    {'code': '012348', 'name': '恒生科技', 'etf': '513180'},
    {'code': '012728', 'name': '游戏',     'etf': '159869'},
]

# 纯债基金：不估算涨跌，只显示规模
BOND_CODES = {'004042', '003039'}

PUSHPLUS_URL = 'https://www.pushplus.plus/send'
UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
SEP = '━━━━━━━━━━━━━━'


def secid_of(code):
    c = code[0]
    return ('1.' if c in '569' else '0.') + code


def http_get(url, headers=None, timeout=12):
    req = urllib.request.Request(url, headers=headers or UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode('utf-8', 'ignore')


def fetch_quotes(secids):
    """批量行情：先试腾讯（更稳定），失败再试 push2"""
    out = fetch_quotes_tencent(secids)
    if out:
        return out
    return fetch_quotes_push2(secids)


def fetch_quotes_tencent(secids):
    """腾讯行情（qt.gtimg.cn），涨跌幅在字段[32]"""
    out = {}
    codes = []
    for s in secids:
        market, code = s.split('.')
        prefix = 'sh' if market == '1' else 'sz'
        codes.append(prefix + code)
    for i in range(0, len(codes), 50):
        batch = codes[i:i + 50]
        url = 'http://qt.gtimg.cn/q=' + ','.join(batch)
        try:
            text = http_get(url, headers={'Referer': 'https://gu.qq.com/'}, timeout=8)
            for line in text.strip().split(';'):
                line = line.strip()
                if '="' not in line:
                    continue
                key, val = line.split('=', 1)
                val = val.strip().strip('"')
                parts = val.split('~')
                if len(parts) < 33:
                    continue
                code6 = parts[2]
                name = parts[1]
                try:
                    chg = float(parts[32])
                except (ValueError, IndexError):
                    chg = None
                prefix = key.replace('v_', '')[:2]
                market = '1' if prefix == 'sh' else '0'
                out[market + '.' + code6] = {'ret': chg, 'name': name}
        except Exception:
            pass
        time.sleep(0.3)
    return out


def fetch_quotes_push2(secids):
    """push2 ulist 批量行情（备选源）"""
    out = {}
    secids = [s for s in secids if s]
    if not secids:
        return out
    for i in range(0, len(secids), 30):
        batch = secids[i:i + 30]
        cb = 'cb' + str(int(time.time() * 1000)) + str(i)
        url = ('https://push2.eastmoney.com/api/qt/ulist.np/get?secids={}'
               '&fields=f12,f13,f14,f2,f3&cb={}').format(','.join(batch), cb)
        try:
            text = http_get(url)
            data = json.loads(text[text.find('(') + 1:text.rfind(')')])
            if data and data.get('data') and data['data'].get('diff'):
                for d in data['data']['diff']:
                    secid = ('1.' if d.get('f13') == 1 else '0.') + str(d.get('f12'))
                    ret = d.get('f3')
                    out[secid] = {
                        'ret': float(ret) / 100 if ret is not None and ret != '-' else None,
                        'name': d.get('f14', '')
                    }
        except Exception:
            pass
        time.sleep(0.5)
    return out


def fetch_holding(code):
    """主动基金前10大持仓"""
    url = 'https://fundf10.eastmoney.com/FundArchivesDatas.aspx?type=jjcc&code={}&topline=10'.format(code)
    try:
        raw = http_get(url, headers={
            'User-Agent': 'Mozilla/5.0',
            'Referer': 'https://fundf10.eastmoney.com/ccmx_{}.html'.format(code)
        })
    except Exception:
        return []
    m = re.search(r'content:\s*"((?:[^"\\]|\\.)*)"', raw, re.S)
    if not m:
        return []
    try:
        content = json.loads('"' + m.group(1) + '"')
    except Exception:
        return []
    holdings = []
    for tr in re.findall(r'<tr>(.*?)</tr>', content, re.S):
        tds = re.findall(r'<td[^>]*>(.*?)</td>', tr, re.S)
        if len(tds) < 9:
            continue
        stock_code = re.sub(r'<[^>]+>', '', tds[1]).strip()
        weight = re.sub(r'<[^>]+>', '', tds[6]).replace('%', '').strip()
        try:
            w = float(weight)
        except ValueError:
            continue
        if stock_code and len(stock_code) == 6 and w > 0.5:
            holdings.append({'code': stock_code, 'weight': w})
    return holdings


def fetch_fund_scale(code):
    """基金规模（亿元）"""
    url = 'https://fund.eastmoney.com/pingzhongdata/{}.js'.format(code)
    try:
        text = http_get(url, timeout=15)
        m = re.search(r'Data_fluctuationScale\s*=\s*(\{.*?\});', text, re.S)
        if m:
            o = json.loads(m.group(1))
            if o.get('series') and o['series']:
                y = o['series'][-1].get('y')
                if y is not None:
                    return float(y)
    except Exception:
        pass
    return None


def fmt_pct(ret, nd=2):
    if ret is None:
        return '   --  '
    sign = '+' if ret >= 0 else '-'
    return '{}{:.{}f}%'.format(sign, abs(ret), nd)


def fmt_money(v):
    if v is None:
        return '   --  '
    return '{}{:.0f}元'.format('+' if v >= 0 else '', v)


def build_message():
    now = datetime.now(BEIJING)
    lines = ['📊 基金观察 · {}'.format(now.strftime('%H:%M'))]
    lines.append(SEP)

    # ---------- 1. 批量收集行情 ----------
    etf_secids = [secid_of(f['etf']) for f in HOLDINGS + SECTORS if f.get('etf')]
    quotes = fetch_quotes(list(set(etf_secids)))

    hold_cache = {}
    extra_secids = []
    for f in HOLDINGS + SECTORS:
        if not f.get('etf') and f['code'] not in BOND_CODES:
            holds = fetch_holding(f['code'])
            if holds:
                hold_cache[f['code']] = holds
                extra_secids += [secid_of(h['code']) for h in holds]
    if extra_secids:
        quotes.update(fetch_quotes(list(set(extra_secids))))

    # ---------- 2. 规模 ----------
    scales = {}
    for f in HOLDINGS + SECTORS:
        s = fetch_fund_scale(f['code'])
        if s:
            scales[f['code']] = s
        time.sleep(0.15)

    # ---------- 3. 估算涨跌 ----------
    def est(fund):
        if fund['code'] in BOND_CODES:
            return None
        if fund.get('etf'):
            q = quotes.get(secid_of(fund['etf']))
            if q and q['ret'] is not None:
                return q['ret']
        if fund['code'] in hold_cache:
            sw = sr = 0.0
            for h in hold_cache[fund['code']]:
                q = quotes.get(secid_of(h['code']))
                if q and q['ret'] is not None:
                    sw += h['weight']
                    sr += h['weight'] * q['ret']
            if sw > 0:
                return sr / sw
        return None

    # ---------- 4. 组装：持仓 ----------
    lines.append('【持仓 · 今日预估】')
    sum_today = 0.0
    sum_total = 0.0
    for f in HOLDINGS:
        ret = est(f)
        scale = scales.get(f['code'])
        scale_txt = '{}亿'.format(int(round(scale))) if scale else '--'
        amt = f.get('amount')
        cost = f.get('cost')

        today = None
        if ret is not None and amt:
            today = amt - amt / (1 + ret / 100)
            sum_today += today
        if amt and cost:
            sum_total += (amt - cost)

        if ret is None:
            lines.append('   {}  {}'.format(f['name'], scale_txt))
        else:
            arrow = '🔴' if ret >= 0 else '🟢'
            lines.append('  {}{:<7} {}  {}  {}'.format(arrow, f['name'], fmt_pct(ret), fmt_money(today), scale_txt))

    # ---------- 5. 合计 ----------
    lines.append(SEP)
    lines.append('💰 今日合计：{}'.format(fmt_money(sum_today)))
    lines.append('📈 持仓总盈亏：{}'.format(fmt_money(sum_total)))
    lines.append(SEP)

    # ---------- 6. 行业观察 ----------
    lines.append('【行业观察】')
    for s in SECTORS:
        ret = est(s)
        scale = scales.get(s['code'])
        scale_txt = '{}亿'.format(int(round(scale))) if scale else '--'
        if ret is None:
            lines.append('  {}  {}'.format(s['name'], scale_txt))
        else:
            arrow = '🔴' if ret >= 0 else '🟢'
            lines.append('  {}{:<6} {}  {}'.format(arrow, s['name'], fmt_pct(ret, 1), scale_txt))

    lines.append(SEP)
    lines.append('⚠️ 盘中估算 · 仅供参考 · 以官方净值为准')
    return '\n'.join(lines)


def push_wechat(token, title, content):
    body = json.dumps({'token': token, 'title': title, 'content': content, 'template': 'txt'}).encode('utf-8')
    req = urllib.request.Request(PUSHPLUS_URL, data=body,
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.loads(resp.read().decode('utf-8'))
    return result.get('code') == 200


def schedule_loop(token):
    """常驻模式：周一至周五 9:30/10:30/11:30/13:30/14:30/15:30"""
    times = [(9, 30), (10, 30), (11, 30), (13, 30), (14, 30), (15, 30)]
    last_push = None
    print('⏰ 常驻调度已启动：周一至周五 9:30/10:30/11:30/13:30/14:30/15:30 推送')
    while True:
        now = datetime.now(BEIJING)
        if now.weekday() < 5:
            hm = (now.hour, now.minute)
            if hm in times and (now.date(), hm) != last_push:
                last_push = (now.date(), hm)
                try:
                    content = build_message()
                    title = '📊 基金估值 {}:{}'.format(now.hour, str(now.minute).zfill(2))
                    ok = push_wechat(token, title, content)
                    print('✅ {} 已推送'.format(now.strftime('%H:%M')) if ok else '❌ 推送失败')
                except Exception as e:
                    print('⚠️ 推送异常: {}'.format(e))
        time.sleep(60)


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    token = os.environ.get('PUSHPLUS_TOKEN', '')
    args = sys.argv[1:]
    if '--token' in args:
        i = args.index('--token')
        if i + 1 < len(args):
            token = args[i + 1]
    if not token:
        print('请设置 PUSHPLUS_TOKEN 环境变量或使用 --token 参数')
        print('注册：https://www.pushplus.plus/')
        sys.exit(1)
    if '--schedule' in args:
        schedule_loop(token)
        return
    try:
        content = build_message()
    except Exception as e:
        content = '📊 基金观察快报\n抓取失败：{}\n请检查网络'.format(e)
    title = '📊 基金估值 {}'.format(datetime.now(BEIJING).strftime('%H:%M'))
    ok = push_wechat(token, title, content)
    if ok:
        print('✅ 已发送到微信')
    else:
        print('❌ 发送失败，请检查 token')
        sys.exit(1)


if __name__ == '__main__':
    main()
