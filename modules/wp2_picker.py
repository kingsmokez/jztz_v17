# -*- coding: utf-8 -*-
"""尾盘选股模块 - WP2策略"""

import time
import re
import threading
from datetime import datetime
import traceback

from .config import WP2_PICK_FILE
from .http_client import session, HEADERS
from .cache_manager import WP2_PICK_DATA, WP2_PICK_LOCK, save_wp2_pick_cache

def _wp2_calc_ma(prices, period):
    if len(prices) < period:
        return None
    return sum(prices[-period:]) / period


def _wp2_calc_ema(prices, period):
    if len(prices) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for i in range(period, len(prices)):
        ema = prices[i] * k + ema * (1 - k)
    return ema


def _wp2_calc_rsi(prices, period=14):
    if len(prices) < period + 1:
        return None
    gs, ls = 0, 0
    for i in range(len(prices) - period, len(prices)):
        d = prices[i] - prices[i - 1]
        if d > 0:
            gs += d
        else:
            ls -= d
    avg_g = gs / period
    avg_l = ls / period if ls > 0 else 0.001
    return 100 - 100 / (1 + avg_g / avg_l)


def _wp2_calc_macd(prices):
    if len(prices) < 35:
        return None
    dif = _wp2_calc_ema(prices, 12) - _wp2_calc_ema(prices, 26)
    if dif is None:
        return None
    da = []
    for i in range(25, len(prices)):
        f = _wp2_calc_ema(prices[:i + 1], 12)
        s = _wp2_calc_ema(prices[:i + 1], 26)
        if f is not None and s is not None:
            da.append(f - s)
    dea = _wp2_calc_ema(da, 9) if len(da) >= 9 else None
    macd = 2 * (dif - dea) if dea is not None else None
    return {'dif': dif, 'dea': dea, 'macd': macd}


def _wp2_calc_score(stock):
    sc = 0
    vr = stock.get('vr', 0)
    ch = stock.get('ch', 0)
    rsi = stock.get('rsi', 0)
    cap = stock.get('cap', 0) / 1e8
    ma_status = stock.get('ma_status', '')
    macd_val = stock.get('macd', 0)
    if vr >= 2.5:
        sc += 28
    elif vr >= 2:
        sc += 22
    elif vr >= 1.5:
        sc += 15
    else:
        sc += 8
    if 3 <= ch <= 6:
        sc += 22
    elif ch >= 2:
        sc += 18
    elif ch >= 1:
        sc += 12
    else:
        sc += 5
    if 55 <= rsi <= 65:
        sc += 20
    elif 50 <= rsi <= 70:
        sc += 15
    else:
        sc += 8
    if 50 <= cap <= 200:
        sc += 12
    elif 30 <= cap <= 300:
        sc += 8
    else:
        sc += 5
    if ma_status == 'perfect':
        sc += 10
    elif ma_status == 'good':
        sc += 6
    if macd_val > 0:
        sc += 8
    return min(sc, 100)


def _wp2_get_sina_quote(codes):
    url = f"https://hq.sinajs.cn/list={','.join(codes)}"
    try:
        r = session.get(url, headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://finance.sina.com.cn/'}, timeout=15)
        stocks = {}
        pattern = r'var hq_str_(\w+)="(.*?)";'
        for match in re.finditer(pattern, r.text):
            code = match.group(1)
            content = match.group(2)
            if not content:
                continue
            fields = content.split(',')
            if len(fields) < 32:
                continue
            try:
                price = float(fields[3])
                pre_close = float(fields[2])
                open_price = float(fields[1])
                high = float(fields[4])
                low = float(fields[5])
                volume = int(fields[8])
                amount = float(fields[9])
                pct_change = round((price - pre_close) / pre_close * 100, 2) if pre_close > 0 else 0
                pure_code = code[2:]
                market = '1' if code.startswith('sh') else '0'
                try:
                    turnover_rate = float(fields[30]) if len(fields) > 30 and fields[30] else 0
                except ValueError:
                    turnover_rate = 0
                try:
                    pe_val = float(fields[31]) if len(fields) > 31 and fields[31] else 0
                except ValueError:
                    pe_val = 0
                avg_vol_5 = 0
                if len(fields) > 8 and volume > 0:
                    avg_vol_5 = volume / max(turnover_rate / 5, 0.1) if turnover_rate > 0 else 0
                vol_ratio = round(volume / avg_vol_5, 2) if avg_vol_5 > 0 else 0
                stocks[pure_code] = {
                    'f2': price, 'f3': pct_change, 'f4': round(price - pre_close, 2),
                    'f5': volume, 'f6': amount, 'f7': round((high - low) / pre_close * 100, 2) if pre_close > 0 else 0,
                    'f8': turnover_rate, 'f9': pe_val, 'f10': vol_ratio, 'f12': pure_code, 'f13': market, 'f14': fields[0],
                    'f15': high, 'f16': low, 'f17': open_price, 'f18': pre_close, 'f20': 0, 'f21': 0, 'f23': pe_val,
                }
            except (ValueError, IndexError):
                continue
        return stocks
    except Exception as e:
        print(f"  [新浪行情] 失败: {e}")
        return {}


def _wp2_get_tencent_market_cap(codes):
    url = f"https://qt.gtimg.cn/q={','.join(codes)}"
    try:
        r = session.get(url, headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://gu.qq.com/'}, timeout=15)
        caps = {}
        pattern = r'v_(\w+)="(.*?)";'
        for match in re.finditer(pattern, r.text):
            full_code = match.group(1)
            content = match.group(2)
            if not content:
                continue
            fields = content.split('~')
            pure_code = full_code[2:]
            try:
                circ_mv = float(fields[45]) if len(fields) > 45 and fields[45] else 0
                caps[pure_code] = circ_mv
            except (ValueError, IndexError):
                caps[pure_code] = 0
        return caps
    except Exception as e:
        print(f"  [腾讯市值] 失败: {e}")
        return {}


def _wp2_get_tencent_kline(symbol, count=60):
    try:
        r = session.get('https://web.ifzq.gtimg.cn/appstock/app/fqkline/get',
                        params={'param': f'{symbol},day,,,{count},qfq'},
                        timeout=10)
        d = r.json()
        if d.get('code') != 0:
            return None
        data = d.get('data', {})
        stock_key = list(data.keys())[0] if data else None
        if not stock_key:
            return None
        qfqday = data[stock_key].get('qfqday') or data[stock_key].get('day') or []
        klines = []
        for row in qfqday:
            if len(row) >= 6:
                klines.append(f"{row[0]},{row[1]},{row[2]},{row[3]},{row[4]},{row[5]}")
        return {'data': {'klines': klines}}
    except Exception as e:
        return None


def execute_wp2_pick(min_cap=30, max_cap=300, min_amt=3, vol_mul=1.5, break_n=20, body_r=0.6, rsi_lo=50, rsi_hi=75, max_out=4):
    """执行尾盘强势股选股"""
    global WP2_PICK_DATA
    with WP2_PICK_LOCK:
        WP2_PICK_DATA['running'] = True
        WP2_PICK_DATA['progress'] = '正在获取股票列表...' 
    filter_log = []

    with WP2_PICK_LOCK:
        WP2_PICK_DATA['running'] = True

    try:
        now = datetime.now()
        print(f"\n{'='*50}")
        print(f"🕐 执行尾盘强势股选股... {now.strftime('%H:%M:%S')}")
        print(f"{'='*50}")

        market_info = {}
        try:
            ir = session.get('https://push2.eastmoney.com/api/qt/stock/get',
                             params={'secid': '1.000300', 'fields': 'f43,f60,f170'},
                             headers=EM_HEADERS, timeout=10)
            if ir.status_code == 200:
                idata = ir.json().get('data', {})
                if idata:
                    market_info = {
                        'idx_price': idata.get('f43', 0),
                        'idx_open': idata.get('f60', 0),
                        'idx_change': idata.get('f170', 0),
                    }
        except Exception:
            pass

        print("  获取股票代码列表...")
        try:
            import akshare as ak
            code_df = ak.stock_info_a_code_name()
        except Exception as e:
            print(f"  akshare获取失败: {e}")
            with WP2_PICK_LOCK:
                WP2_PICK_DATA['stocks'] = []
                WP2_PICK_DATA['pick_time'] = now.strftime('%H:%M:%S')
                WP2_PICK_DATA['last_update'] = now.strftime('%Y-%m-%d %H:%M:%S')
                WP2_PICK_DATA['filter_stats'] = [{'n': 'akshare', 'b': 0, 'a': 0}]
                WP2_PICK_DATA['market_info'] = market_info
                WP2_PICK_DATA['running'] = False
                WP2_PICK_DATA['progress'] = ''
                save_wp2_pick_cache()
            return

        print(f"  共 {len(code_df)} 只股票")

        sina_codes = []
        for _, row in code_df.iterrows():
            code = row['code']
            if code.startswith('688') or code.startswith('8') or code.startswith('4'):
                continue
            sina_codes.append(f'sh{code}' if code.startswith('6') else f'sz{code}')

        all_data = {}
        batch_size = 500
        for i in range(0, len(sina_codes), batch_size):
            batch = sina_codes[i:i + batch_size]
            batch_data = _wp2_get_sina_quote(batch)
            all_data.update(batch_data)
            time.sleep(0.3)

        print(f"  获取市值数据...")
        cap_data = {}
        for i in range(0, len(sina_codes), batch_size):
            batch = sina_codes[i:i + batch_size]
            batch_caps = _wp2_get_tencent_market_cap(batch)
            cap_data.update(batch_caps)
            time.sleep(0.3)

        for code, info in all_data.items():
            cap_yi = cap_data.get(code, 0)
            info['f21'] = cap_yi * 1e8

        all_stocks = list(all_data.values())
        print(f"  获取行情数据 {len(all_stocks)} 只")

        # 第1层：基础过滤
        s1 = []
        for s in all_stocks:
            c = str(s.get('f12', ''))
            n = str(s.get('f14', ''))
            if c.startswith('688') or c.startswith('8') or c.startswith('4'):
                continue
            if 'ST' in n or '退' in n or '*' in n:
                continue
            cap = float(s.get('f21', 0))
            if not cap or cap < min_cap * 1e8 or cap > max_cap * 1e8:
                continue
            amt = float(s.get('f6', 0))
            if not amt or amt < min_amt * 1e8:
                continue
            if not s.get('f2') or s.get('f2') == '-':
                continue
            s1.append(s)
        filter_log.append({'n': '基础过滤', 'b': len(all_stocks), 'a': len(s1)})
        print(f"  第1层 基础过滤: {len(all_stocks)}→{len(s1)}")

        if not s1:
            with WP2_PICK_LOCK:
                WP2_PICK_DATA['stocks'] = []
                WP2_PICK_DATA['pick_time'] = now.strftime('%H:%M:%S')
                WP2_PICK_DATA['last_update'] = now.strftime('%Y-%m-%d %H:%M:%S')
                WP2_PICK_DATA['filter_stats'] = filter_log
                WP2_PICK_DATA['market_info'] = market_info
                WP2_PICK_DATA['running'] = False
                WP2_PICK_DATA['progress'] = ''
                save_wp2_pick_cache()
            return

        s1.sort(key=lambda x: float(x.get('f6', 0)), reverse=True)

        # 获取K线数据
        print(f"  获取 {len(s1)} 只K线...")
        kline_data = {}
        for i in range(0, len(s1), 50):
            batch = s1[i:i + 50]
            for s in batch:
                code = str(s.get('f12', ''))
                market = str(s.get('f13', '1'))
                symbol = f'sh{code}' if market == '1' else f'sz{code}'
                kd = _wp2_get_tencent_kline(symbol, 60)
                if kd:
                    kline_data[code] = kd
                time.sleep(0.05)

        # 第2-5层过滤
        p_ma, p_vol, p_rsi, p_mc = 0, 0, 0, 0
        results = []

        for s in s1:
            code = str(s.get('f12', ''))
            kd = kline_data.get(code)
            if not kd or not kd.get('data') or not kd.get('data', {}).get('klines') or len(kd['data']['klines']) < 25:
                continue

            kl = []
            for line in kd['data']['klines']:
                parts = line.split(',')
                if len(parts) >= 6:
                    kl.append({'o': float(parts[1]), 'c': float(parts[2]), 'h': float(parts[3]), 'lo': float(parts[4]), 'v': float(parts[5])})
            kl = [k for k in kl if k['c'] > 0]
            if len(kl) < 25:
                continue

            cl = [k['c'] for k in kl]
            hi = [k['h'] for k in kl]
            vl = [k['v'] for k in kl]
            t = len(kl) - 1

            m5 = _wp2_calc_ma(cl, 5)
            m10 = _wp2_calc_ma(cl, 10)
            m20 = _wp2_calc_ma(cl, 20)
            m60 = _wp2_calc_ma(cl, 60)

            if not m5 or not m10 or not m20 or not m60:
                continue
            if not (m5 > m10 and m10 > m20 and m20 > m60 and cl[t] > m20):
                continue
            p_ma += 1

            if not (vl[t] > (vl[t - 1] or 1) * vol_mul):
                continue

            hn = 0
            for j in range(t - 1, max(0, t - break_n) - 1, -1):
                hn = max(hn, hi[j])
            if not (cl[t] > hn):
                continue

            body = cl[t] - kl[t]['o']
            rng = hi[t] - kl[t]['lo']
            if not (rng > 0 and body / rng > body_r):
                continue
            p_vol += 1

            rsi = _wp2_calc_rsi(cl)
            if rsi is None or not (rsi_lo < rsi < rsi_hi):
                continue
            p_rsi += 1

            mc = _wp2_calc_macd(cl)
            if not mc or mc['dif'] is None or mc['dea'] is None or mc['macd'] is None:
                continue
            if not (mc['dif'] > mc['dea'] and mc['macd'] > 0):
                continue
            p_mc += 1

            ma_status = 'perfect' if (m5 > m10 > m20 > m60 and cl[t] > m5) else 'good' if (m5 > m10 and m10 > m20) else ''
            score_stock = {
                'vr': float(s.get('f10', 0)),
                'ch': float(s.get('f3', 0)),
                'rsi': rsi,
                'cap': float(s.get('f21', 0)),
                'ma_status': ma_status,
                'macd': mc['macd'],
            }
            results.append({
                'code': code,
                'name': s.get('f14', ''),
                'price': round(cl[t], 2),
                'ch': float(s.get('f3', 0)),
                'amt': float(s.get('f6', 0)),
                'cap': float(s.get('f21', 0)),
                'vr': float(s.get('f10', 0)),
                'to': float(s.get('f8', 0)),
                'ma': f"{m5:.1f}>{m10:.1f}",
                'rsi': round(rsi, 1),
                'macd': round(mc['macd'], 4),
                'score': _wp2_calc_score(score_stock),
                'strategy': 'short_term',
                'strategy_label': '短线强势',
                'ma_status': ma_status,
            })

        filter_log.append({'n': 'MA多头', 'b': len(s1), 'a': p_ma})
        filter_log.append({'n': '量价突破', 'b': p_ma, 'a': p_vol})
        filter_log.append({'n': 'RSI安全', 'b': p_vol, 'a': p_rsi})
        filter_log.append({'n': 'MACD确认', 'b': p_rsi, 'a': p_mc})

        results.sort(key=lambda x: x['score'], reverse=True)
        final = results[:max_out]

        print(f"  ✓ 尾盘选股完成: {len(final)} 只")

        with WP2_PICK_LOCK:
            WP2_PICK_DATA['date'] = now.strftime('%Y-%m-%d')
            WP2_PICK_DATA['stocks'] = final
            WP2_PICK_DATA['pick_time'] = now.strftime('%H:%M:%S')
            WP2_PICK_DATA['last_update'] = now.strftime('%Y-%m-%d %H:%M:%S')
            WP2_PICK_DATA['filter_stats'] = filter_log
            WP2_PICK_DATA['market_info'] = market_info
            WP2_PICK_DATA['running'] = False
            WP2_PICK_DATA['progress'] = ''
            save_wp2_pick_cache()

    except Exception as e:
        print(f"  ✗ 尾盘选股失败: {e}")
        with WP2_PICK_LOCK:
            WP2_PICK_DATA['running'] = False
            WP2_PICK_DATA['progress'] = ''
            save_wp2_pick_cache()
        traceback.print_exc()
