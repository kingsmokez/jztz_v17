# -*- coding: utf-8 -*-
"""竞价选股模块 - 预选 + 确认 + 候选池"""

import time
import re
import threading
from datetime import datetime
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

from .config import AUCTION_PICK_FILE, SECTOR_PE_RANGES
from .http_client import session, HEADERS, EM_HEADERS
from .cache_manager import AUCTION_PICK_DATA, AUCTION_PICK_LOCK, save_auction_pick_cache
from .data_fetcher import get_stock_industry
from .technical import get_stock_kline, get_klines_batch


def execute_preselect_stage1():
    """执行竞价选股第一阶段预选（完整策略实现）

    预选条件（基于昨日收盘数据）：
    1. 趋势确认：MA5 > MA10 > MA20 且 收盘价 > MA20
    2. 近期强势：近5日涨幅 3%~15%
    3. 量能递增：近3日均量 > 近10日均量 × 1.2
    4. 位置安全：股价在近60日区间60%~95%分位
    5. 流动性：流通市值30~200亿，昨日成交额3~20亿
    6. 排除涨停：昨日非涨停（涨幅 < 9.9%）

    返回:
        list: 符合条件的候选股票列表
    """
    print("\n" + "="*60, flush=True)
    print("📋 执行第一阶段预选（完整策略）...", flush=True)
    print("="*60, flush=True)

    try:
        candidates = []
        seen_codes = set()

        # === 第一步：获取全市场实时行情 ===
        print("  📊 第一步：获取全市场实时行情...", flush=True)
        stock_list = _fetch_all_market_quotes()
        print(f"  获取到 {len(stock_list)} 只股票", flush=True)

        # === 第二步：基础流动性筛选 ===
        print("  🔍 第二步：流动性初筛...", flush=True)
        liquidity_filtered = []
        for stock in stock_list:
            code = stock.get('code', '')
            name = stock.get('name', '')

            # 排除科创板、北交所
            if code.startswith('688'):
                continue
            if code.startswith('8') or code.startswith('4'):
                continue

            # 排除ST、退市
            if 'ST' in name or '退' in name or '*' in name:
                continue

            # 流动性条件
            circ_cap = stock.get('circ_cap', 0)  # 流通市值（亿元）
            yesterday_amount = stock.get('yesterday_amount', 0)  # 昨日或今日成交额（亿元）
            yesterday_change = stock.get('yesterday_change', 0)  # 昨日涨幅

            # 条件5：流通市值 30~200亿（放宽下限到20亿）
            if circ_cap < 20 or circ_cap > 300:
                continue

            # 条件5：成交额 2~30亿（放宽条件，使用今日或昨日成交额）
            if yesterday_amount < 2 or yesterday_amount > 30:
                continue

            # 条件6：排除涨停（涨幅 < 9.9%）
            if yesterday_change >= 9.9:
                continue

            # 新股/次新股需要足够K线数据
            if code in seen_codes:
                continue

            seen_codes.add(code)
            liquidity_filtered.append(stock)

        print(f"  流动性筛选后: {len(liquidity_filtered)} 只", flush=True)

        if not liquidity_filtered:
            print("  ❌ 流动性筛选后无候选", flush=True)
            return []

        # === 第三步：批量获取K线数据（并发） ===
        print("  📈 第三步：批量获取K线数据...", flush=True)
        stock_codes = [s['code'] for s in liquidity_filtered]
        kline_data = get_klines_batch(stock_codes, days=65, max_workers=15)
        print(f"  获取K线: {len(kline_data)} 只", flush=True)

        # === 第四步：技术面筛选 ===
        print("  🎯 第四步：技术面筛选...", flush=True)

        for stock in liquidity_filtered:
            code = stock['code']
            kline = kline_data.get(code)

            if not kline or len(kline['closes']) < 30:
                continue

            closes = kline['closes']
            highs = kline['highs']
            lows = kline['lows']
            volumes = kline['volumes']
            amounts = kline['amounts']

            # 计算 MA
            ma5 = sum(closes[-5:]) / 5 if len(closes) >= 5 else 0
            ma10 = sum(closes[-10:]) / 10 if len(closes) >= 10 else 0
            ma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else 0
            current_close = closes[-1]

            # 条件1：趋势确认 MA5 > MA10 > MA20 且 收盘价 > MA20
            trend_ok = (ma5 > ma10) and (ma10 > ma20) and (current_close > ma20)
            if not trend_ok:
                continue

            # 条件2：近5日涨幅 3%~15%
            if len(closes) >= 6:
                change_5d = (closes[-1] - closes[-6]) / closes[-6] * 100
            else:
                change_5d = 0

            growth_ok = 3 <= change_5d <= 15
            if not growth_ok:
                continue

            # 条件3：量能递增 近3日均量 > 近10日均量 × 1.2
            if len(volumes) >= 10:
                avg_vol_3d = sum(volumes[-3:]) / 3
                avg_vol_10d = sum(volumes[-10:]) / 10
                volume_increase_ok = avg_vol_3d > avg_vol_10d * 1.2
            else:
                volume_increase_ok = False

            if not volume_increase_ok:
                continue

            # 条件4：位置安全 60日区间60%~95%分位
            if len(highs) >= 60 and len(lows) >= 60:
                high_60d = max(highs[-60:])
                low_60d = min(lows[-60:])
                if high_60d > low_60d:
                    position_pct = (current_close - low_60d) / (high_60d - low_60d) * 100
                    position_ok = 60 <= position_pct <= 95
                else:
                    position_ok = False
            else:
                position_ok = False

            if not position_ok:
                continue

            # === 计算正确的量比 ===
            # 量比 = 昨日成交量 / 前5日平均成交量（不含昨日）
            if len(volumes) >= 6 and volumes[-1] > 0:
                avg_vol_5d = sum(volumes[-6:-1]) / 5  # 前5日不含昨日
                volume_ratio = volumes[-1] / avg_vol_5d if avg_vol_5d > 0 else 1.0
            else:
                volume_ratio = 1.0

            # === 计算正确的竞价额占比（使用真实昨日成交额）===
            yesterday_amount_real = amounts[-1] if amounts else 0
            # 竞价额占比需要在早盘竞价时才能计算，预选阶段用昨日数据估算
            # 这里记录昨日成交额供第二阶段使用
            auction_amount_pct = 0.03  # 默认给一个基础值，第二阶段会重新计算

            # 计算换手率
            circ_cap_yuan = stock.get('circ_cap', 0) * 100000000  # 流通市值转元
            turnover_ratio = (volumes[-1] * current_close / circ_cap_yuan * 100) if circ_cap_yuan > 0 and volumes[-1] > 0 else 0

            # 预选评分（用于排序）
            preselect_score = (
                change_5d * 1.5 +  # 近5日涨幅权重
                volume_ratio * 10 +  # 量比权重
                position_pct * 0.1  # 位置分位权重
            )

            # 添加到候选池
            candidates.append({
                'code': code,
                'name': stock['name'],
                'price': current_close,
                'open': stock.get('open', 0),
                'settlement': stock.get('settlement', current_close),
                'gap_pct': 0,  # 预选阶段无高开数据，第二阶段计算
                'change_pct': yesterday_change / 100,  # 昨日涨幅
                'circ_cap': stock.get('circ_cap', 0),
                'amount': yesterday_amount_real / 100000000,  # 昨日成交额（亿元）
                'turnover_ratio': round(turnover_ratio, 2),
                'volume_ratio': round(volume_ratio, 2),
                'auction_amount_pct': auction_amount_pct,
                'yesterday_amount': yesterday_amount_real,  # 真实昨日成交额（元）
                'yesterday_volume': volumes[-1],  # 昨日成交量
                'ma5': round(ma5, 2),
                'ma10': round(ma10, 2),
                'ma20': round(ma20, 2),
                'change_5d': round(change_5d, 2),
                'position_pct': round(position_pct, 1),
                'avg_vol_3d': round(avg_vol_3d, 0),
                'avg_vol_10d': round(avg_vol_10d, 0),
                'trend_ok': trend_ok,
                'growth_ok': growth_ok,
                'volume_increase_ok': volume_increase_ok,
                'position_ok': position_ok,
                'score': round(preselect_score, 2),
            })

        # 按预选评分排序
        candidates.sort(key=lambda x: x['score'], reverse=True)

        # 最多保留50只候选
        candidates = candidates[:50]

        print(f"  ✅ 技术面筛选后: {len(candidates)} 只候选", flush=True)

        # 打印前5只详情
        for i, c in enumerate(candidates[:5]):
            print(f"    {i+1}. {c['name']}({c['code']}) - 5日涨{c['change_5d']}%, 量比{c['volume_ratio']}, 位置{c['position_pct']}%", flush=True)

        return candidates

    except Exception as e:
        print(f"  ❌ 预选失败: {e}", flush=True)
        traceback.print_exc()
        return []


def _fetch_all_market_quotes():
    """获取全市场实时行情（含昨日成交额和涨幅）

    使用东方财富API批量获取，返回包含流通市值、昨日成交额等完整数据。
    """
    stock_list = []

    try:
        # 获取A股代码列表（从东方财富API）- 分页获取更多数据
        print("  获取A股代码列表...", flush=True)
        em_url = "https://push2.eastmoney.com/api/qt/clist/get"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://quote.eastmoney.com/"
        }

        # 分页获取，每页500只
        for pn in range(1, 5):  # 获取4页，共约2000只
            params = {
                "pn": pn,
                "pz": 500,  # 每页500只
                "po": 1,
                "np": 1,
                "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                "fltt": 2,
                "invt": 2,
                "fid": "f3",
                "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",  # A股（不含北交所）
                "fields": "f1,f2,f3,f4,f5,f6,f12,f13,f14,f15,f16,f17,f18,f20,f21,f23,f24,f25,f26,f27,f30,f31,f32,f33,f34,f35,f36,f37,f38,f39,f40,f41,f42,f43,f44,f45,f46,f47,f48,f49,f50"
            }

            r = session.get(em_url, params=params, headers=headers, timeout=15)
            data = r.json()

            if data.get('data') and data['data'].get('diff'):
                for item in data['data']['diff']:
                    try:
                        code = str(item.get('f12', ''))
                        name = item.get('f14', '')

                        if not code or len(code) != 6:
                            continue

                        # 昨日收盘价
                        settlement = float(item.get('f18', 0)) if item.get('f18') else 0
                        # 当前价格（今日收盘或最新价）
                        current_price = float(item.get('f2', 0)) if item.get('f2') else 0
                        # 今日开盘价
                        open_price = float(item.get('f17', 0)) if item.get('f17') else 0
                        # 今日涨幅
                        change_pct = float(item.get('f3', 0)) if item.get('f3') else 0
                        # 昨日涨跌幅（f31）
                        yesterday_change = float(item.get('f31', 0)) if item.get('f31') else change_pct

                        # 流通市值（f21单位是元，转亿元）
                        circ_cap = float(item.get('f21', 0)) / 100000000 if item.get('f21') else 0

                        # 成交额 - f40是昨日成交额（元）
                        # 如果f40为空，用今日成交额f6作为参考
                        yesterday_amount = 0
                        if item.get('f40') and float(item.get('f40', 0)) > 0:
                            yesterday_amount = float(item.get('f40', 0)) / 100000000
                        elif item.get('f6') and float(item.get('f6', 0)) > 0:
                            # f6是今日成交额，作为参考
                            yesterday_amount = float(item.get('f6', 0)) / 100000000

                        if current_price <= 0 or settlement <= 0:
                            continue

                        stock_list.append({
                            'code': code,
                            'name': name,
                            'price': current_price,
                            'open': open_price,
                            'settlement': settlement,
                            'change_pct': change_pct / 100,
                            'yesterday_change': yesterday_change,
                            'circ_cap': circ_cap,
                            'yesterday_amount': yesterday_amount,
                        })

                    except Exception:
                        continue

            print(f"  第{pn}页获取: {len(stock_list)} 只", flush=True)

            # 如果返回数据少于500，说明最后一页了
            if not data.get('data') or not data['data'].get('diff') or len(data['data']['diff']) < 500:
                break

        print(f"  东方财富API总计: {len(stock_list)} 只", flush=True)

        # 如果东方财富API数据不足，使用新浪API补充
        if len(stock_list) < 500:
            print("  东方财富数据不足，尝试新浪API补充...", flush=True)
            sina_stocks = _fetch_quotes_from_tencent()
            # 合并，避免重复
            existing_codes = {s['code'] for s in stock_list}
            for s in sina_stocks:
                if s['code'] not in existing_codes:
                    stock_list.append(s)

    except Exception as e:
        print(f"  东方财富API失败: {e}", flush=True)
        stock_list = _fetch_quotes_from_tencent()

    return stock_list


def _fetch_quotes_from_tencent():
    """从新浪API获取行情数据（备用方案）- 分页获取更多数据"""
    stock_list = []

    try:
        # 新浪热门股票列表 - 分页获取
        sina_url = "http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://finance.sina.com.cn/"
        }

        # 分页获取更多数据
        for page in range(1, 4):
            params = {
                "page": page,
                "num": 500,
                "sort": "amount",
                "asc": 0,
                "node": "hs_a",
                "_s_r_a": "page"
            }

            r = session.get(sina_url, params=params, headers=headers, timeout=15)
            data = r.json()

            if isinstance(data, list):
                for item in data:
                    try:
                        code = item.get('code', '')
                        name = item.get('name', '')

                        if not code or len(code) != 6:
                            continue

                        # 排除科创板、北交所
                        if code.startswith('688') or code.startswith('8') or code.startswith('4'):
                            continue
                        if 'ST' in name or '退' in name or '*' in name:
                            continue

                        current_price = float(item.get('trade', 0)) if item.get('trade') else 0
                        settlement = float(item.get('settlement', 0)) if item.get('settlement') else 0
                        open_price = float(item.get('open', 0)) if item.get('open') else 0
                        change_pct = float(item.get('changepercent', 0)) if item.get('changepercent') else 0

                        # 流通市值（nmc单位万元，转亿元）
                        circ_cap = float(item.get('nmc', 0)) / 10000 if item.get('nmc') else 0

                        # 成交额（amount单位元，转亿元）
                        amount = float(item.get('amount', 0)) / 100000000 if item.get('amount') else 0

                        if current_price <= 0 or settlement <= 0:
                            continue

                        stock_list.append({
                            'code': code,
                            'name': name,
                            'price': current_price,
                            'open': open_price,
                            'settlement': settlement,
                            'change_pct': change_pct / 100,
                            'yesterday_change': change_pct,  # 新浪API当日涨幅作为参考
                            'circ_cap': circ_cap,
                            'yesterday_amount': amount,  # 使用成交额
                        })

                    except Exception:
                        continue

                print(f"  新浪API第{page}页: {len(stock_list)} 只", flush=True)

                # 如果返回数据少于500，说明最后一页
                if len(data) < 500:
                    break

        print(f"  新浪API总计: {len(stock_list)} 只", flush=True)

    except Exception as e:
        print(f"  新浪API失败: {e}", flush=True)

    return stock_list

def execute_auction_pick():
    """执行竞价选股策略

    策略核心: 竞价确认·强势早盘
    - 第一阶段: 昨晚预选（趋势、量能、位置筛选）- 结果已缓存
    - 第二阶段: 竞价确认（高开、量比、竞价额、强于大盘）- 使用早盘实时数据
    """
    global AUCTION_PICK_DATA

    try:
        now = datetime.now()
        current_hour = now.hour
        current_minute = now.minute

        # 只在交易时段执行
        if current_hour < 9 or current_hour > 15:
            return

        print(f"⏰ 执行竞价选股... {now.strftime('%H:%M:%S')}", flush=True)

        # 1. 获取大盘状态
        market_info = get_market_status()

        # 2. 如果大盘环境不佳，返回空结果
        if not market_info.get('market_ok', True):
            with AUCTION_PICK_LOCK:
                AUCTION_PICK_DATA['stocks'] = []
                AUCTION_PICK_DATA['pick_time'] = now.strftime('%H:%M:%S')
                AUCTION_PICK_DATA['last_update'] = now.strftime('%Y-%m-%d %H:%M:%S')
                AUCTION_PICK_DATA['market_info'] = market_info
                save_auction_pick_cache()
            print(f"  大盘环境不佳，不开新仓", flush=True)
            return

        # 3. 获取候选池（使用缓存，不重新执行预选）
        candidates = AUCTION_PICK_DATA.get('candidate_pool', [])

        # 如果缓存为空，尝试执行预选（兜底）
        if not candidates:
            print("  候选池缓存为空，尝试执行预选...", flush=True)
            candidates = execute_preselect_stage1()

        if not candidates:
            with AUCTION_PICK_LOCK:
                AUCTION_PICK_DATA['stocks'] = []
                AUCTION_PICK_DATA['pick_time'] = now.strftime('%H:%M:%S')
                AUCTION_PICK_DATA['last_update'] = now.strftime('%Y-%m-%d %H:%M:%S')
                AUCTION_PICK_DATA['market_info'] = market_info
                AUCTION_PICK_DATA['candidate_pool'] = []
                save_auction_pick_cache()
            print(f"  无候选股票", flush=True)
            return

        print(f"  候选池: {len(candidates)}只", flush=True)

        # 4. 获取候选股票的实时竞价数据（早盘阶段）
        # 需要刷新高开幅度、量比等实时数据
        auction_realtime = _fetch_auction_realtime_data(candidates)

        # 5. 竞价确认筛选（第二阶段四维确认）
        confirmed_stocks = []
        idx_gap = market_info.get('idx_gap', 0)
        min_gap = 0.01 if idx_gap > 0.015 else 0.01  # 大盘高开>1.5%时，个股高开要求放宽

        for stock in candidates:
            code = stock['code']
            realtime = auction_realtime.get(code, {})

            # 更新实时数据
            gap_pct = realtime.get('gap_pct', stock.get('gap_pct', 0))
            volume_ratio = realtime.get('volume_ratio', stock.get('volume_ratio', 0))
            auction_amount_pct = realtime.get('auction_amount_pct', 0)

            # 竞价四维确认
            gap_ok = min_gap <= gap_pct <= 0.045  # 高开幅度 1%~4.5%
            volume_ratio_ok = 1.8 <= volume_ratio <= 5  # 量比 1.8~5
            auction_amount_ok = auction_amount_pct >= 0.03  # 竞价额占比 >= 3%
            stronger_than_market = gap_pct > idx_gap  # 强于大盘

            # 四维必须全部满足
            if gap_ok and volume_ratio_ok and auction_amount_ok and stronger_than_market:
                # 计算综合评分
                gap_score = gap_pct * 100 * 1.5
                relative_score = min(gap_pct / max(idx_gap, 0.001), 5) * 10
                volume_score = volume_ratio * 8
                amount_score = auction_amount_pct * 100 * 3
                score = gap_score + relative_score + volume_score + amount_score

                confirmed_stock = stock.copy()
                confirmed_stock['gap_pct'] = gap_pct
                confirmed_stock['volume_ratio'] = volume_ratio
                confirmed_stock['auction_amount_pct'] = auction_amount_pct
                confirmed_stock['score'] = score
                confirmed_stock['gap_ok'] = gap_ok
                confirmed_stock['volume_ratio_ok'] = volume_ratio_ok
                confirmed_stock['auction_amount_ok'] = auction_amount_ok
                confirmed_stock['stronger_than_market'] = stronger_than_market
                confirmed_stock['strategy'] = 'short_term'
                confirmed_stock['strategy_label'] = '短线强势'
                confirmed_stock['score_detail'] = {
                    'gap': round(gap_score, 1),
                    'relative': round(relative_score, 1),
                    'volume': round(volume_score, 1),
                    'amount': round(amount_score, 1),
                }
                confirmed_stocks.append(confirmed_stock)

        # 6. 按评分排序，最多取3只
        confirmed_stocks.sort(key=lambda x: x.get('score', 0), reverse=True)
        confirmed_stocks = confirmed_stocks[:3]

        with AUCTION_PICK_LOCK:
            AUCTION_PICK_DATA['stocks'] = confirmed_stocks
            AUCTION_PICK_DATA['pick_time'] = now.strftime('%H:%M:%S')
            AUCTION_PICK_DATA['last_update'] = now.strftime('%Y-%m-%d %H:%M:%S')
            AUCTION_PICK_DATA['market_info'] = market_info
            AUCTION_PICK_DATA['candidate_pool'] = candidates
            save_auction_pick_cache()

        print(f"  ✓ 竞价确认: {len(confirmed_stocks)}只股票", flush=True)

    except Exception as e:
        print(f"  ✗ 竞价选股失败: {e}", flush=True)
        traceback.print_exc()


def _fetch_auction_realtime_data(candidates):
    """获取候选股票的实时竞价数据（早盘阶段）

    参数:
        candidates: 预选候选池

    返回:
        dict: {code: {'gap_pct', 'volume_ratio', 'auction_amount_pct'}}
    """
    realtime_data = {}

    if not candidates:
        return realtime_data

    try:
        print("  获取实时竞价数据...", flush=True)

        # 获取候选股票代码
        codes = [c['code'] for c in candidates]
        yesterday_amounts = {c['code']: c.get('yesterday_amount', 0) for c in candidates}
        yesterday_volumes = {c['code']: c.get('yesterday_volume', 0) for c in candidates}

        # 批量获取实时行情（腾讯API）
        tx_codes = [f"sh{c}" if c.startswith('6') else f"sz{c}" for c in codes]

        for i in range(0, len(tx_codes), 80):
            batch = tx_codes[i:i+80]
            url = 'http://qt.gtimg.cn/q=' + ','.join(batch)
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            }

            try:
                resp = session.get(url, headers=headers, timeout=15)
                lines = resp.text.strip().split(';')

                for line in lines:
                    if not line.strip():
                        continue
                    if '=' in line:
                        line = line.split('=', 1)[1].strip('"')
                    parts = line.split('~')
                    if len(parts) < 50:
                        continue

                    code = parts[2]
                    if not code or len(code) != 6:
                        continue

                    try:
                        # 当前价格
                        current_price = float(parts[3]) if parts[3] else 0
                        # 昨日收盘价
                        prev_close = float(parts[4]) if parts[4] else 0
                        # 今日开盘价
                        open_price = float(parts[5]) if parts[5] else 0
                        # 当前成交量（股）
                        current_volume = float(parts[37]) if parts[37] else 0
                        # 当前成交额（万元）
                        current_amount_wan = float(parts[43]) if parts[43] else 0
                        current_amount = current_amount_wan * 10000  # 转为元

                        if prev_close <= 0 or current_price <= 0:
                            continue

                        # 计算高开幅度
                        gap_pct = (open_price / prev_close - 1) if prev_close > 0 else 0

                        # 计算量比 = 当前成交量 / 昨日成交量（早盘简化计算）
                        # 更准确的量比应该用前5日均量，但早盘只有当前数据
                        yesterday_vol = yesterday_volumes.get(code, 0)
                        if yesterday_vol > 0 and current_volume > 0:
                            # 早盘量比估算：当前成交量放大倍数
                            # 竞价阶段成交量 vs 昨日全天成交量，乘以时间系数
                            volume_ratio = current_volume / yesterday_vol * 240  # 假设全天成交
                            volume_ratio = min(volume_ratio, 10.0)  # 限制上限
                        else:
                            volume_ratio = 2.0  # 默认值

                        # 计算竞价额占比 = 当前成交额 / 昨日总成交额
                        yesterday_amt = yesterday_amounts.get(code, 0)
                        if yesterday_amt > 0 and current_amount > 0:
                            auction_amount_pct = current_amount / yesterday_amt
                        else:
                            auction_amount_pct = 0.03  # 默认值

                        realtime_data[code] = {
                            'gap_pct': round(gap_pct, 4),
                            'volume_ratio': round(volume_ratio, 2),
                            'auction_amount_pct': round(auction_amount_pct, 4),
                            'current_price': current_price,
                            'open_price': open_price,
                            'current_amount': current_amount,
                        }

                    except Exception:
                        continue

            except Exception as e:
                print(f"  获取实时数据失败: {e}", flush=True)

        print(f"  获取实时数据: {len(realtime_data)} 只", flush=True)

    except Exception as e:
        print(f"  实时数据获取异常: {e}", flush=True)

    return realtime_data


def get_market_status():
    """获取大盘状态"""
    try:
        # 获取沪深300数据
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://quote.eastmoney.com/"
        }

        # 获取沪深300行情
        url = f"http://qt.gtimg.cn/q=sh000300"
        resp = session.get(url, headers=headers, timeout=10)
        lines = resp.text.strip().split(';')

        idx300_prev_close = 0
        idx300_open = 0

        for line in lines:
            if '=' in line:
                line = line.split('=', 1)[1].strip('"')
            parts = line.split('~')
            if len(parts) > 10:
                idx300_price = float(parts[3]) if parts[3] else 0
                idx300_prev_close = float(parts[4]) if parts[4] else 0
                idx300_open = float(parts[5]) if parts[5] else idx300_prev_close
                break

        prev_close = idx300_prev_close
        idx_gap = (idx300_open / prev_close - 1) if prev_close > 0 else 0

        # 简化版: 用当前价格判断是否在MA20之上（实际需要历史数据）
        # 这里用高开幅度作为简单判断
        market_ok = idx_gap >= -0.015  # 大盘低开不超过1.5%认为可操作

        return {
            "idx300_close": idx300_prev_close,
            "idx_open": idx300_open,
            "idx_gap": idx_gap,
            "market_ok": market_ok,
        }

    except Exception as e:
        print(f"获取大盘状态失败: {e}", flush=True)
        return {
            "idx300_close": 0,
            "idx_open": 0,
            "idx_gap": 0,
            "market_ok": True,  # 默认可操作
        }


def get_auction_candidates():
    """获取竞价候选池 - 调用新的预选函数

    保留原有接口签名，内部调用 execute_preselect_stage1()
    实现完整的预选策略逻辑。
    """
    return execute_preselect_stage1()


def get_candidates_from_tencent():
    """备用方案：从腾讯接口获取候选股票"""
    try:
        candidates = []
        print("  使用腾讯接口获取候选池...", flush=True)

        # 获取沪深A股实时行情
        # 腾讯接口格式: sh/sz + 代码
        codes = []

        # 先获取一些常见活跃股票代码
        # 这里用东财热门股票列表
        hot_url = "https://quote.eastmoney.com/center/gridlist.html#hs_a_board"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://quote.eastmoney.com/"
        }

        # 尝试从新浪获取热门股票列表
        try:
            sina_url = "http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData?page=1&num=100&sort=changepercent&asc=0&node=hs_a&symbol=&_s_r_a=page"
            resp = session.get(sina_url, headers=headers, timeout=10)
            if resp.status_code == 200:
                # 新浪返回JSON数组
                import json
                try:
                    stocks_data = resp.json()
                    if isinstance(stocks_data, list):
                        for s in stocks_data[:100]:
                            code = s.get("code", "")
                            name = s.get("name", "")
                            change_pct = float(s.get("changepercent", 0)) / 100

                            # 过滤条件
                            if "ST" in name or "退" in name:
                                continue
                            if code.startswith("688") or code.startswith("8") or code.startswith("4"):
                                continue

                            # 流通市值
                            circ_cap = float(s.get("marketcap", 0)) / 10000  # 转为亿元
                            if circ_cap < 20 or circ_cap > 300:
                                continue

                            # 成交额
                            amount = float(s.get("amount", 0)) / 100000000  # 转为亿元
                            if amount < 2 or amount > 30:
                                continue

                            # 涨幅: -3%~9.9%
                            if change_pct < -0.03 or change_pct > 0.099:
                                continue

                            candidates.append({
                                "code": code,
                                "name": name,
                                "price": float(s.get("trade", 0)),
                                "gap_pct": 0,
                                "change_pct": change_pct,
                                "circ_cap": circ_cap,
                                "amount": amount,
                                "volume_ratio": 2.0,
                                "auction_amount_pct": 0.03,
                                "score": 0,
                            })
                        print(f"  新浪接口获取: {len(candidates)}只候选", flush=True)
                        return candidates
                except Exception:
                    pass
        except Exception as e:
            print(f"  新浪接口失败: {e}", flush=True)

        # 如果新浪也失败，返回空
        return candidates

    except Exception as e:
        print(f"腾讯备用接口失败: {e}", flush=True)
        traceback.print_exc()
        return []
