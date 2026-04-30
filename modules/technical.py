# -*- coding: utf-8 -*-
"""技术面筛选模块 - MA/KDJ/RSI/量比/技术评分"""

from .http_client import session
from concurrent.futures import ThreadPoolExecutor, as_completed


def get_stock_kline(code, days=60):
    """获取单只股票历史K线数据（含成交额）

    使用腾讯K线API（前复权），返回完整K线数据供预选使用。

    参数:
        code: 股票代码（6位数字）
        days: 回溯天数（默认60天）

    返回:
        dict: {
            'dates': 日期列表,
            'opens': 开盘价列表,
            'closes': 收盘价列表,
            'highs': 最高价列表,
            'lows': 最低价列表,
            'volumes': 成交量列表（股）,
            'amounts': 成交额列表（元）,
            'last_close': 昨日收盘价,
            'last_amount': 昨日成交额（元）,
            'last_volume': 昨日成交量（股）,
        }
        或 None（获取失败）
    """
    try:
        # 构造腾讯代码
        symbol = f"sh{code}" if code.startswith('6') else f"sz{code}"

        url = 'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get'
        params = {
            'param': f'{symbol},day,,,{days},qfq',  # 日K线，前复权，注意参数格式
        }
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://gu.qq.com/',
        }

        r = session.get(url, params=params, headers=headers, timeout=10)
        d = r.json()

        if d.get('code') != 0:
            return None

        # 解析K线数据 - 使用 qfqday 或 day 字段
        data = d.get('data', {})
        if not data:
            return None

        stock_key = list(data.keys())[0]
        stock_data = data[stock_key]

        # 优先使用前复权数据 qfqday，否则用原始数据 day
        kline_list = stock_data.get('qfqday') or stock_data.get('day') or []
        if not kline_list or len(kline_list) < 20:
            return None

        # 腾讯返回格式: [日期, 开盘, 收盘, 最高, 最低, 成交量]
        # 注意：没有成交额字段，需要用 收盘价*成交量 估算
        dates = []
        opens = []
        closes = []
        highs = []
        lows = []
        volumes = []
        amounts = []

        for item in kline_list:
            try:
                if len(item) >= 6:
                    dates.append(item[0])
                    open_val = float(item[1]) if item[1] else 0
                    close_val = float(item[2]) if item[2] else 0
                    high_val = float(item[3]) if item[3] else 0
                    low_val = float(item[4]) if item[4] else 0
                    vol_val = float(item[5]) if item[5] else 0

                    opens.append(open_val)
                    closes.append(close_val)
                    highs.append(high_val)
                    lows.append(low_val)
                    volumes.append(vol_val)

                    # 成交额估算 = 成交量 * 收盘价
                    amount_est = vol_val * close_val
                    amounts.append(amount_est)
            except (ValueError, TypeError):
                continue

        if len(closes) < 20:
            return None

        return {
            'dates': dates,
            'opens': opens,
            'closes': closes,
            'highs': highs,
            'lows': lows,
            'volumes': volumes,
            'amounts': amounts,
            'last_close': closes[-1] if closes else 0,
            'last_amount': amounts[-1] if amounts else 0,
            'last_volume': volumes[-1] if volumes else 0,
        }

    except Exception as e:
        print(f"  获取{code}K线失败: {e}")
        return None


def get_klines_batch(stock_codes, days=60, max_workers=10):
    """批量获取多只股票的K线数据（并发）

    参数:
        stock_codes: 股票代码列表
        days: 回溯天数
        max_workers: 最大并发数

    返回:
        dict: {code: kline_data}
    """
    kline_data = {}

    def fetch_one(code):
        return code, get_stock_kline(code, days)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(fetch_one, code): code for code in stock_codes}
        for future in as_completed(futures):
            try:
                code, data = future.result()
                if data:
                    kline_data[code] = data
            except Exception:
                pass

    return kline_data


# TODO: calc_ma

def calculate_technical_indicators(code, days=30):
    """计算技术指标：MA均线、KDJ、RSI、量比

    参数:
        code: 股票代码
        days: 回溯天数（默认30天）

    返回:
        dict: {
            'ma5': MA5均线价格,
            'ma10': MA10均线价格,
            'ma20': MA20均线价格,
            'price_above_ma5': 是否站上MA5,
            'price_above_ma20': 是否站上MA20,
            'kdj_k': K值,
            'kdj_d': D值,
            'kdj_j': J值,
            'kdj_cross': 是否金叉(K上穿D),
            'rsi': RSI值,
            'volume_ratio': 量比（今日成交量/5日均量）
        }
    """
    try:
        from datetime import datetime, timedelta

        # 获取K线数据
        end_date = datetime.now().strftime('%Y-%m-%d')
        start_date = (datetime.now() - timedelta(days=days+10)).strftime('%Y-%m-%d')

        # 使用新浪K线API
        sina_code = code
        if code.startswith('6'):
            sina_code = f"sh{code}"
        elif code.startswith('0') or code.startswith('3'):
            sina_code = f"sz{code}"

        url = "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"
        params = {
            "symbol": sina_code,
            "scale": "240",  # 日K
            "ma": "no",
            "datalen": str(days + 5),  # 多获取几天确保计算准确
        }
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"}

        r = session.get(url, params=params, headers=headers, timeout=10)
        data = r.json()

        if not data or not isinstance(data, list) or len(data) < 20:
            return None

        # 按日期排序
        klines = []
        for item in data:
            if isinstance(item, dict):
                klines.append({
                    "date": item.get("day", ""),
                    "open": float(item.get("open", 0)),
                    "close": float(item.get("close", 0)),
                    "high": float(item.get("high", 0)),
                    "low": float(item.get("low", 0)),
                    "volume": float(item.get("volume", 0)),
                })
        klines.sort(key=lambda x: x["date"])

        if len(klines) < 20:
            return None

        closes = [k["close"] for k in klines]
        highs = [k["high"] for k in klines]
        lows = [k["low"] for k in klines]
        volumes = [k["volume"] for k in klines]
        current_price = closes[-1]

        # === 计算MA均线 ===
        def calc_ma(prices, period):
            if len(prices) < period:
                return 0
            return sum(prices[-period:]) / period

        ma5 = calc_ma(closes, 5)
        ma10 = calc_ma(closes, 10)
        ma20 = calc_ma(closes, 20)

        price_above_ma5 = current_price > ma5
        price_above_ma20 = current_price > ma20

        # === 计算KDJ ===
        # KDJ需要计算RSV(n日最低价与n日最高价的相对位置)
        n = 9  # KDJ默认周期
        if len(closes) >= n:
            # 计算RSV
            low_n = min(lows[-n:])
            high_n = max(highs[-n:])
            rsv = (current_price - low_n) / (high_n - low_n) * 100 if high_n != low_n else 50

            # 需要历史K/D值来平滑计算，简化处理：假设前一天K=D=50
            # 实际计算: K = 2/3 * 前K + 1/3 * RSV, D = 2/3 * 前D + 1/3 * K
            prev_k = 50
            prev_d = 50

            # 递推计算KDJ
            k_values = []
            d_values = []
            for i in range(n, len(closes)):
                low_i = min(lows[i-n:i])
                high_i = max(highs[i-n:i])
                close_i = closes[i]
                rsv_i = (close_i - low_i) / (high_i - low_i) * 100 if high_i != low_i else 50

                if i == n:
                    k_i = 50
                    d_i = 50
                else:
                    k_i = 2/3 * k_values[-1] + 1/3 * rsv_i
                    d_i = 2/3 * d_values[-1] + 1/3 * k_i

                k_values.append(k_i)
                d_values.append(d_i)

            kdj_k = k_values[-1] if k_values else 50
            kdj_d = d_values[-1] if d_values else 50
            kdj_j = 3 * kdj_k - 2 * kdj_d

            # 判断金叉：今天K>D且昨天K<D
            kdj_cross = False
            if len(k_values) >= 2:
                kdj_cross = k_values[-1] > d_values[-1] and k_values[-2] <= d_values[-2]
        else:
            kdj_k = kdj_d = kdj_j = 50
            kdj_cross = False

        # === 计算RSI ===
        # RSI = 100 - 100/(1+RS), RS = 平均涨幅/平均跌幅
        rsi_period = 14
        if len(closes) >= rsi_period + 1:
            changes = [closes[i+1] - closes[i] for i in range(len(closes)-1)]
            gains = [c if c > 0 else 0 for c in changes[-rsi_period:]]
            losses = [abs(c) if c < 0 else 0 for c in changes[-rsi_period:]]

            avg_gain = sum(gains) / rsi_period
            avg_loss = sum(losses) / rsi_period

            if avg_loss == 0:
                rsi = 100
            else:
                rs = avg_gain / avg_loss
                rsi = 100 - 100 / (1 + rs)
        else:
            rsi = 50

        # === 计算量比 ===
        # 量比 = 今日成交量 / 5日均量
        if len(volumes) >= 6 and volumes[-1] > 0:
            avg_volume_5 = sum(volumes[-6:-1]) / 5  # 前5天平均
            volume_ratio = volumes[-1] / avg_volume_5 if avg_volume_5 > 0 else 1
        else:
            volume_ratio = 1

        # 计算动量（20日/60日收益率）
        momentum_20 = 0
        momentum_60 = 0
        if len(closes) >= 20:
            momentum_20 = (closes[-1] - closes[-20]) / closes[-20] * 100
        if len(closes) >= 60:
            momentum_60 = (closes[-1] - closes[-60]) / closes[-60] * 100

        return {
            'ma5': round(ma5, 2),
            'ma10': round(ma10, 2),
            'ma20': round(ma20, 2),
            'price_above_ma5': price_above_ma5,
            'price_above_ma20': price_above_ma20,
            'kdj_k': round(kdj_k, 1),
            'kdj_d': round(kdj_d, 1),
            'kdj_j': round(kdj_j, 1),
            'kdj_cross': kdj_cross,
            'rsi': round(rsi, 1),
            'volume_ratio': round(volume_ratio, 2),
            'momentum_20': round(momentum_20, 2),
            'momentum_60': round(momentum_60, 2),
            'current_price': round(current_price, 2),
        }

    except Exception as e:
        print(f"  计算{code}技术指标失败: {e}")
        return None


def evaluate_technical_score(code, tech_data):
    """根据技术指标计算评分

    返回: (技术面评分, 技术面原因列表)
    """
    if not tech_data:
        return 0, []

    score = 0
    reasons = []

    # 1. 均线加分（最高5分）
    if tech_data.get('price_above_ma5'):
        score += 2
        reasons.append("站上MA5")
    if tech_data.get('price_above_ma20'):
        score += 3
        reasons.append("站上MA20 ✓")

    # 2. KDJ金叉加分（最高3分）
    if tech_data.get('kdj_cross'):
        score += 3
        reasons.append("KDJ金叉")
    elif tech_data.get('kdj_k', 50) > tech_data.get('kdj_d', 50):
        score += 1  # K在D上方也算弱金叉

    # 3. RSI区间加分（最高2分）
    rsi = tech_data.get('rsi', 50)
    if 30 <= rsi <= 70:
        score += 2
        reasons.append(f"RSI {rsi:.0f} 正常区间")
    elif rsi < 30:
        reasons.append(f"RSI {rsi:.0f} 超卖可能反弹")
    elif rsi > 70:
        reasons.append(f"RSI {rsi:.0f} 超买注意风险")

    # 4. 量比加分（最高2分）
    volume_ratio = tech_data.get('volume_ratio', 1)
    if volume_ratio >= 1.5 and volume_ratio <= 3:
        score += 2
        reasons.append(f"量比 {volume_ratio:.1f} 放量")
    elif volume_ratio > 1:
        score += 1

    # 技术面最高12分
    return min(score, 12), reasons[:3]
