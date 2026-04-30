# -*- coding: utf-8 -*-
"""多因子评分系统 - 价值/质量/成长/动量/情绪 + 五维评估 + 买卖点

v17.1 优化:
- 新增策略识别: 根据股票特征自动判断短线/长线
- 动态权重: 短线高情绪/动量，长线高价值/质量
- 热点因子衰减: 避免追高
- 统一评分体系: 消除重复加分
"""

from .config import (
    SECTOR_PE_RANGES, STOCK_SECTOR_MAP,
    HOT_KEYWORD_TO_SECTOR, SECTOR_KEYWORDS,
    LIQUOR_NAMES, BANK_CODES,
)
from .data_fetcher import get_stock_industry, fetch_sina_sectors
from .technical import evaluate_technical_score
from .http_client import session, HEADERS

# ========== 策略权重配置 ==========
STRATEGY_WEIGHTS = {
    'long_term': {
        'value': 0.45,
        'quality': 0.25,
        'growth': 0.15,
        'momentum': 0.05,
        'sentiment': 0.10,
    },
    'swing': {
        'value': 0.18,
        'quality': 0.12,
        'growth': 0.15,
        'momentum': 0.28,
        'sentiment': 0.27,
    },
    'short_term': {
        'value': 0.15,
        'quality': 0.10,
        'growth': 0.15,
        'momentum': 0.30,
        'sentiment': 0.30,
    },
}

STRATEGY_LABELS = {
    'long_term': '长线价值',
    'swing': '中短线波段',
    'short_term': '短线强势',
}


def detect_strategy(stock, tech_data=None):
    """根据股票特征自动判断适合的策略类型 (v18.0: 平衡分布)

    返回: 'long_term' / 'swing' / 'short_term'

    目标分布:
    - 长线: 20-30%
    - 中短线(swing): 45-55%
    - 短线: 15-25%
    """
    roe = stock.get('roe', 0)
    pe = stock.get('pe', 0)
    market_cap = stock.get('market_cap', 0)
    turnover = stock.get('turnover_rate', 0)
    change_pct = stock.get('change_pct', 0)

    # 计算各类得分
    long_score = 0
    short_score = 0
    swing_score = 0
    
    # 市值评分
    if market_cap >= 1000:
        long_score += 4
    elif market_cap >= 500:
        long_score += 3
    elif market_cap >= 300:
        long_score += 1
        swing_score += 1
    elif market_cap >= 100:
        swing_score += 2
    elif market_cap >= 50:
        swing_score += 1
        short_score += 1
    else:
        short_score += 2
    
    # ROE评分
    if roe >= 20:
        long_score += 3
    elif roe >= 15:
        long_score += 2
    elif roe >= 10:
        long_score += 1
        swing_score += 1
    elif roe >= 5:
        swing_score += 1
    
    # PE评分
    if 0 < pe <= 20:
        long_score += 3
    elif 0 < pe <= 30:
        long_score += 2
    elif 0 < pe <= 50:
        swing_score += 1
    elif pe > 100 or pe <= 0:
        short_score += 1
    
    # 换手率评分
    if turnover >= 15:
        short_score += 4
    elif turnover >= 10:
        short_score += 3
    elif turnover >= 7:
        short_score += 2
        swing_score += 1
    elif turnover >= 4:
        swing_score += 2
    elif turnover >= 2:
        swing_score += 1
    else:
        long_score += 1
    
    # 技术指标
    if tech_data:
        m20 = tech_data.get('momentum_20', 0)
        if m20 > 20:
            short_score += 2
            swing_score += 1
        elif m20 > 10:
            swing_score += 2
            short_score += 1
        elif m20 > 5:
            swing_score += 1
        elif m20 > 0:
            long_score += 1
        
        price = tech_data.get('current_price', 0)
        ma5 = tech_data.get('ma5', 0)
        ma10 = tech_data.get('ma10', 0)
        ma20 = tech_data.get('ma20', 0)
        ma60 = tech_data.get('ma60', 0)
        
        # 趋势评分
        if price > 0 and ma20 > 0:
            if price > ma5 > ma10 > ma20 > ma60 and ma60 > 0:
                swing_score += 3
            elif price > ma5 > ma10 > ma20:
                swing_score += 2
            elif price > ma5 > ma10:
                swing_score += 1
        
        # RSI
        rsi = tech_data.get('rsi', 50)
        if 55 <= rsi <= 75:
            swing_score += 1
        elif rsi > 75:
            short_score += 1
        
        # 量能
        vr = tech_data.get('volume_ratio', 1)
        if vr >= 2.0:
            swing_score += 2
            short_score += 1
        elif vr >= 1.5:
            swing_score += 1
    
    # 涨幅评分
    if change_pct > 5:
        short_score += 2
    elif 2 < change_pct <= 5:
        swing_score += 2
    elif 0 < change_pct <= 2:
        swing_score += 1
        long_score += 1

    # === 决策逻辑 ===
    # 使用得分比较，取最高者
    if long_score >= short_score and long_score >= swing_score and long_score >= 5:
        return 'long_term'
    elif short_score >= long_score and short_score >= swing_score and short_score >= 5:
        return 'short_term'
    elif swing_score >= long_score and swing_score >= short_score:
        return 'swing'
    elif long_score >= short_score:
        return 'long_term'
    else:
        return 'short_term' 
def mf_score_value(stock):
    """价值因子（0-100分）— 2026-04-13修复：PE评分按板块动态调整"""
    score = 0
    pe = stock.get('pe', 0)
    pb = stock.get('pb', 0)
    profit_growth = stock.get('profit_growth', 0)
    market_cap = stock.get('market_cap', 0)
    
    # === 按行业动态调整PE评分标准 ===
    code = stock.get('code', '')
    
    # 优先从stock中获取已缓存的行业信息
    # 只在已有缓存时调用API（避免对全市场股票逐个请求东方财富）
    industry_info = {
        'pe_fair_max': stock.get('pe_fair_max', 0),
        'pe_fair_low': stock.get('pe_fair_low', 0),
        'industry': stock.get('industry', '未知'),
        'sector_type': stock.get('sector_type', 'default'),
    }
    if not industry_info['pe_fair_max']:
        # 只在INDUSTRY_CACHE中已有缓存时获取，不主动发起新请求
        from .config import INDUSTRY_CACHE
        if code in INDUSTRY_CACHE:
            industry_info = INDUSTRY_CACHE[code]
        else:
            industry_info = {'pe_fair_max': 30, 'pe_fair_low': 15, 'industry': '未知', 'sector_type': 'default'}
    pe_fair_max = industry_info.get('pe_fair_max', 30)
    pe_fair_low = industry_info.get('pe_fair_low', 15)

    stock['industry'] = industry_info.get('industry', '未知')
    stock['sector_type'] = industry_info.get('sector_type', 'default')
    stock['pe_fair_max'] = pe_fair_max
    stock['pe_fair_low'] = pe_fair_low
    
    # === PE评分（按板块动态调整）===
    # PE越低越好，但不同板块合理区间不同
    if pe > 0:
        if pe <= pe_fair_low:
            # 低于合理下限：非常便宜，满分
            score += 35
        elif pe <= pe_fair_max:
            # 在合理区间内：线性递减
            ratio = (pe - pe_fair_low) / (pe_fair_max - pe_fair_low)
            score += round(35 * (1 - ratio * 0.7), 1)  # 35→10.5
        elif pe <= pe_fair_max * 1.5:
            # 超出合理区间但不太离谱
            score += 5
        else:
            # 明显高估
            score += 2
    if pe > 0 and profit_growth > 0:
        peg = pe / profit_growth
        if peg < 0.5: score += 25
        elif peg < 1: score += 22
        elif peg < 1.5: score += 18
        elif peg < 2: score += 12
        else: score += 5
    if pb > 0:
        if pb < 1.5: score += 20
        elif pb < 3: score += 16
        elif pb < 5: score += 12
        elif pb < 8: score += 6
        else: score += 2
    else: score += 10
    if market_cap > 0:
        if 100 <= market_cap <= 500: score += 20
        elif 50 <= market_cap < 100 or 500 < market_cap <= 1000: score += 16
        elif 1000 < market_cap <= 2000: score += 12
        else: score += 8
    return min(score, 100)


def mf_score_quality(stock):
    """质量因子（0-100分）"""
    score = 0
    roe = stock.get('roe', 0)
    gm = stock.get('gross_margin', 0)
    nm = stock.get('net_margin', 0)
    dr = stock.get('debt_ratio', 0)
    if roe >= 20: score += 35
    elif roe >= 15: score += 28
    elif roe >= 12: score += 22
    elif roe >= 8: score += 15
    elif roe > 0: score += 8
    if gm >= 50: score += 25
    elif gm >= 40: score += 22
    elif gm >= 30: score += 18
    elif gm >= 20: score += 12
    elif gm > 0: score += 6
    if nm >= 20: score += 20
    elif nm >= 15: score += 16
    elif nm >= 10: score += 12
    elif nm >= 5: score += 8
    elif nm > 0: score += 4
    if dr > 0:
        if dr <= 30: score += 20
        elif dr <= 50: score += 16
        elif dr <= 60: score += 12
        elif dr <= 70: score += 6
    else: score += 10
    return min(score, 100)


def mf_score_growth(stock):
    """成长因子（0-100分）"""
    score = 0
    rg = stock.get('rev_growth', 0)
    pg = stock.get('profit_growth', 0)
    roe = stock.get('roe', 0)
    
    has_rev = rg != 0
    has_profit = pg != 0
    
    if has_rev and has_profit:
        # 两项都有，正常评分
        if rg >= 30: score += 40
        elif rg >= 20: score += 34
        elif rg >= 15: score += 28
        elif rg >= 10: score += 20
        elif rg > 0: score += 10
        if pg >= 30: score += 40
        elif pg >= 20: score += 34
        elif pg >= 15: score += 28
        elif pg >= 10: score += 20
        elif pg > 0: score += 10
        if pg > rg and pg > 0:
            accel = pg - rg
            if accel >= 10: score += 20
            elif accel >= 5: score += 15
            else: score += 10
        else: score += 5
    elif has_rev or has_profit:
        # 只有单一数据，给部分分数
        growth_val = rg if has_rev else pg
        if growth_val >= 30: score += 50
        elif growth_val >= 20: score += 45
        elif growth_val >= 15: score += 40
        elif growth_val >= 10: score += 35
        elif growth_val > 0: score += 30
        else: score += 20  # 负增长
        # 缺失项用 ROE 推断
        if roe >= 20: score += 20
        elif roe >= 15: score += 16
        elif roe > 0: score += 12
        else: score += 5
    else:
        # 两项都缺失，用 ROE 推断成长
        if roe >= 25: score += 60  # 高ROE通常意味着稳定增长
        elif roe >= 20: score += 55
        elif roe >= 15: score += 50
        elif roe >= 10: score += 45
        elif roe > 0: score += 40
        else: score += 25  # 亏损，给最低但不是0
    
    return min(score, 100)


def mf_score_momentum(tech_data):
    """动量因子（0-100分）"""
    score = 0
    if not tech_data: return 50
    m20 = tech_data.get('momentum_20', 0)
    m60 = tech_data.get('momentum_60', 0)
    ma5 = tech_data.get('ma5', 0)
    ma10 = tech_data.get('ma10', 0)
    ma20 = tech_data.get('ma20', 0)
    price = tech_data.get('current_price', 0)
    if 5 <= m20 <= 20: score += 35
    elif 0 <= m20 < 5: score += 28
    elif 20 < m20 <= 40: score += 25
    elif -5 <= m20 < 0: score += 15
    else: score += 5
    if 10 <= m60 <= 40: score += 25
    elif 0 <= m60 < 10: score += 20
    elif 40 < m60 <= 60: score += 15
    elif -10 <= m60 < 0: score += 10
    else: score += 5
    if price > ma5 > ma10 > ma20 and ma20 > 0: score += 25
    elif price > ma5 > ma10 and ma10 > 0: score += 20
    elif price > ma20 and ma20 > 0: score += 15
    elif price < ma5 < ma10 < ma20 and ma20 > 0: score += 0
    else: score += 10
    rsi = tech_data.get('rsi', 50)
    if 50 <= rsi <= 70: score += 15
    elif 40 <= rsi < 50: score += 10
    elif rsi > 70: score += 5
    else: score += 3
    return min(score, 100)


def mf_score_sentiment(stock, tech_data):
    """情绪因子（0-100分）"""
    score = 0
    if not tech_data: return 50
    turnover = stock.get('turnover_rate', 0)
    vr = tech_data.get('volume_ratio', 1)
    if 3 <= turnover <= 8: score += 50
    elif 1.5 <= turnover < 3: score += 40
    elif 8 < turnover <= 15: score += 30
    elif 0.5 <= turnover < 1.5: score += 25
    elif turnover > 15: score += 10
    else: score += 15
    if 1.2 <= vr <= 2.5: score += 30
    elif 0.8 <= vr < 1.2: score += 20
    elif 2.5 < vr <= 5: score += 15
    else: score += 10
    ma20 = tech_data.get('ma20', 0)
    price = tech_data.get('current_price', 0)
    if price > 0 and ma20 > 0:
        dist = (price - ma20) / ma20 * 100
        if 0 <= dist <= 10: score += 20
        elif 10 < dist <= 20: score += 15
        elif -5 <= dist < 0: score += 18
        else: score += 8
    return min(score, 100)


def multi_factor_evaluate(stock, tech_data=None):
    """
    多因子综合评分 v5.2 (动态权重方案)
    根据股票特征自动识别策略类型，应用不同权重:
    - 长线: 价值45% + 质量25% + 成长15% + 动量5% + 情绪10%
    - 中短线: 价值30% + 质量15% + 成长15% + 动量20% + 情绪20%
    - 短线: 价值15% + 质量10% + 成长15% + 动量30% + 情绪30%
    """
    v = mf_score_value(stock)
    q = mf_score_quality(stock)
    g = mf_score_growth(stock)
    m = mf_score_momentum(tech_data) if tech_data else 50
    s = mf_score_sentiment(stock, tech_data) if tech_data else 50

    strategy = detect_strategy(stock, tech_data)
    weights = STRATEGY_WEIGHTS[strategy]

    total = (v * weights['value'] + q * weights['quality'] +
             g * weights['growth'] + m * weights['momentum'] +
             s * weights['sentiment'])

    reasons = []
    strategy_label = STRATEGY_LABELS[strategy]
    reasons.append(f"[{strategy_label}]")
    if v >= 75:
        reasons.append(f"估值优秀(V{v:.0f})")
    elif v >= 60:
        reasons.append(f"估值合理(V{v:.0f})")
    if q >= 75:
        reasons.append(f"质量优秀(Q{q:.0f})")
    elif q >= 60:
        reasons.append(f"质量良好(Q{q:.0f})")
    if g >= 75:
        reasons.append(f"高成长(G{g:.0f})")
    elif g >= 60:
        reasons.append(f"成长良好(G{g:.0f})")
    if m >= 70:
        reasons.append(f"动量强劲(M{m:.0f})")
    elif m >= 55:
        reasons.append(f"动量中性(M{m:.0f})")

    if total >= 78:
        rec = "强烈推荐"
    elif total >= 68:
        rec = "推荐"
    elif total >= 58:
        rec = "关注"
    else:
        rec = "观望"

    return {
        'v5_total': round(total, 2),
        'v5_factors': {
            'value': round(v, 2), 'quality': round(q, 2),
            'growth': round(g, 2), 'momentum': round(m, 2), 'sentiment': round(s, 2),
        },
        'v5_reasons': reasons,
        'v5_recommendation': rec,
        'strategy': strategy,
        'strategy_label': strategy_label,
        'weights': weights,
    }


def evaluate_stock(stock, tech_data=None, priority_sectors=None):
    """五维价值投资评估 - 支持全市场股票

    参数:
        stock: 股票数据字典
        tech_data: 技术指标数据（可选，由外部计算后传入）
        priority_sectors: 当日优先板块列表（可选）
    """
    score = 0
    dimensions = {"profitability": 0, "growth": 0, "health": 0, "valuation": 0, "cashflow": 0}
    tech_score = 0  # 技术面评分单独计算，不加入dimensions
    reasons = []

    # 排除白酒和银行
    liquor_names = LIQUOR_NAMES
    bank_codes = BANK_CODES
    name = stock.get("name", "")
    code = stock.get("code", "")
    # 过滤北交所/B股/A股重复
    if code.startswith('8') or code.startswith('4') or code.startswith('920'):
        return None
    if code.startswith('900') or code.startswith('200') or code.startswith('A2'):
        return None
    if any(n in name for n in liquor_names) or code in bank_codes:
        return None

    # === 换手率基础筛选（新增）===
    turnover_rate = stock.get("turnover_rate", 0)
    if turnover_rate < 0.3 and turnover_rate > 0:
        # 换手率低于0.3%的极不活跃股票，直接排除
        # 注意：turnover_rate=0可能是数据缺失，不排除
        # 0.3%-0.5%的大盘蓝筹股保留，但后续换手率因子不给分
        return None

    roe = stock.get("roe", 0)
    gross_margin = stock.get("gross_margin", 0)
    net_margin = stock.get("net_margin", 0)
    rev_growth = stock.get("rev_growth", 0)
    profit_growth = stock.get("profit_growth", 0)
    pe = stock.get("pe", 0)
    pb = stock.get("pb", 0)
    debt_ratio = stock.get("debt_ratio", 0)
    market_cap = stock.get("market_cap", 0)

    # 数据完整度判断
    has_profitability = roe > 0 or gross_margin > 0 or net_margin > 0
    has_growth = rev_growth != 0 or profit_growth != 0
    has_valuation = pe > 0 or pb > 0

    # 盈利能力 (最高35分) - 连续评分而非阶梯式，增加区分度
    if roe < 0:
        dimensions["profitability"] = 0
        reasons.append(f"ROE {roe:.1f}% 亏损 ⚠️")
    elif roe >= 18:  # 优化：20% -> 18%
        # ROE 20%-40%映射到 25-35分（连续），每增加1%ROE多1分
        dimensions["profitability"] = min(25 + (roe - 20) * 1, 35)
        reasons.append(f"ROE {roe:.1f}% 优秀")
    elif roe >= 15:
        dimensions["profitability"] = 15 + (roe - 15) * 2  # 15-25分
        reasons.append(f"ROE {roe:.1f}% 良好")
    elif roe > 0:
        dimensions["profitability"] = roe * 1  # 0-15分
        reasons.append(f"ROE {roe:.1f}%")
    else:
        if profit_growth > 20:
            dimensions["profitability"] = 12
            reasons.append("净利润高增长，盈利能力推测良好")
        elif profit_growth > 0:
            dimensions["profitability"] = 8
        elif gross_margin > 30:
            dimensions["profitability"] = 6
            reasons.append("毛利率较高，盈利能力推测尚可")
        elif market_cap >= 200:
            dimensions["profitability"] = 4
            reasons.append("大盘股，盈利能力待确认")
        else:
            dimensions["profitability"] = 0

    if gross_margin >= 40:
        dimensions["profitability"] = min(dimensions["profitability"] + 8, 35)
        reasons.append(f"毛利率 {gross_margin:.1f}% ✓")
    elif gross_margin > 0:
        dimensions["profitability"] = min(dimensions["profitability"] + 3, 35)

    if net_margin >= 15:
        dimensions["profitability"] = min(dimensions["profitability"] + 5, 35)
        reasons.append(f"净利率 {net_margin:.1f}% ✓")
    score += dimensions["profitability"]

    # 成长性 (25分) - ROE为负时成长性打折
    has_rev = rev_growth != 0
    has_profit = profit_growth != 0
    
    if roe < 0:
        # 亏损企业，成长性最多5分（即使有增速也可能是扭亏为盈）
        if profit_growth > 20 and rev_growth > 0:
            dimensions["growth"] = 5
            reasons.append("亏损企业但有改善迹象")
        else:
            dimensions["growth"] = 0
        score += dimensions["growth"]
    elif has_rev and has_profit:
        # 两项都有，正常评分
        avg_growth = (rev_growth + profit_growth) / 2
        if avg_growth >= 20:
            dimensions["growth"] = min(20 + (avg_growth - 20) * 0.5, 25)
            reasons.append(f"成长性 {avg_growth:.1f}% 优秀")
        elif avg_growth >= 15:
            dimensions["growth"] = 15 + (avg_growth - 15) * 1
            reasons.append(f"成长性 {avg_growth:.1f}% 良好")
        elif avg_growth >= 10:
            dimensions["growth"] = 10 + (avg_growth - 10) * 1
        elif avg_growth > 0:
            dimensions["growth"] = avg_growth * 1
        else:
            dimensions["growth"] = max(avg_growth * 0.5, 5)  # 负增长给最低5分
        score += dimensions["growth"]
    elif has_rev or has_profit:
        # 只有单一数据
        growth_val = rev_growth if has_rev else profit_growth
        if growth_val >= 20:
            dimensions["growth"] = 18
            reasons.append(f"{'营收' if has_rev else '利润'}增长 {growth_val:.1f}% 优秀（缺另一项）")
        elif growth_val >= 10:
            dimensions["growth"] = 14
            reasons.append(f"{'营收' if has_rev else '利润'}增长 {growth_val:.1f}% 一般")
        elif growth_val > 0:
            dimensions["growth"] = 10
        else:
            dimensions["growth"] = 6
        # ROE 补充评分
        if roe >= 20:
            dimensions["growth"] += 3
        elif roe >= 15:
            dimensions["growth"] += 2
        dimensions["growth"] = min(dimensions["growth"], 25)
        score += dimensions["growth"]
    else:
        # 两项都缺失，用 ROE 推断
        if roe >= 20:
            dimensions["growth"] = 15
            reasons.append(f"增长数据缺失，ROE {roe:.1f}%推断成长性中等")
        elif roe >= 15:
            dimensions["growth"] = 12
            reasons.append("增长数据缺失，ROE中等推断")
        elif roe > 0:
            dimensions["growth"] = 8
            reasons.append("增长数据缺失，ROE偏低")
        else:
            dimensions["growth"] = 5
            reasons.append("增长数据和ROE均缺失")
        score += dimensions["growth"]

    # 财务健康 (20分)
    if debt_ratio > 0 and debt_ratio < 1000:  # 过滤异常值
        if debt_ratio <= 50:
            dimensions["health"] = 20
            reasons.append(f"资产负债率 {debt_ratio:.1f}% ✓健康")
        elif debt_ratio <= 70:
            dimensions["health"] = 12
        else:
            dimensions["health"] = 5
    else:
        dimensions["health"] = 0  # 优化：无数据不给分  # 无数据给中等分
    score += dimensions["health"]

    # 估值 (20分) - 连续评分
    # 注意：PE为负说明亏损（TTM），不应给估值分
    if pe > 0 and pe < 1000:
        if pe <= 12:  # 优化：15 -> 12
            dimensions["valuation"] = min(15 + (15 - pe) * 0.33, 20)  # PE越低分越高
            reasons.append(f"PE {pe:.1f} 低估 ✓")
        elif pe <= 20:  # 优化：25 -> 20
            dimensions["valuation"] = 15 - (pe - 15) * 0.5  # 15→10分
            reasons.append(f"PE {pe:.1f} 合理")
        elif pe <= 35:
            dimensions["valuation"] = 10 - (pe - 25) * 0.5  # 10→5分
        elif pe <= 50:
            dimensions["valuation"] = 5 - (pe - 35) * 0.33  # 5→0分
            dimensions["valuation"] = max(dimensions["valuation"], 0)
        else:
            dimensions["valuation"] = 0
            if pe > 100:
                reasons.append(f"PE {pe:.1f} 高估 ⚠️")
    elif pe <= 0:
        dimensions["valuation"] = 0
    else:
        dimensions["valuation"] = 8

    if 0 < pb <= 3:
        dimensions["valuation"] = min(dimensions["valuation"] + 5, 20)
    elif 3 < pb <= 5:
        dimensions["valuation"] = min(dimensions["valuation"] + 2, 20)
    score += dimensions["valuation"]

    # 现金流质量 (加分项，上限5分)
    # 改进方案：基础分 + 毛利率加分 + 负债率加分
    # 基础分由盈利质量(PE+ROE)决定，毛利率高/负债率低可额外加分
    market_cap_yi = market_cap  # 已经是亿元单位，直接使用

    cashflow_base = 0
    cashflow_reason = ""

    # 基础分：盈利质量（PE+ROE推导）
    if pe > 0 and roe > 0:
        if roe >= 20:
            # ROE优秀
            if pe <= 20:
                cashflow_base = 3
                cashflow_reason = f"ROE {roe:.1f}%优秀 + PE低 现金流充裕"
            elif pe <= 35:
                cashflow_base = 2
                cashflow_reason = f"ROE {roe:.1f}%优秀 盈利质量良好"
            else:
                cashflow_base = 1
                cashflow_reason = f"ROE {roe:.1f}%优秀 但PE偏高"
        elif roe >= 10:
            # ROE中等
            if pe <= 25:
                cashflow_base = 2
                cashflow_reason = f"ROE {roe:.1f}% + PE合理 盈利稳定"
            elif pe <= 40:
                cashflow_base = 1
                cashflow_reason = f"ROE {roe:.1f}% 盈利尚可"
            else:
                cashflow_base = 1
                cashflow_reason = f"ROE {roe:.1f}% 但估值偏高"
        else:
            # ROE较低但盈利
            cashflow_base = 1
            cashflow_reason = f"盈利中 ROE {roe:.1f}%待提升"
    elif roe <= 0 or pe <= 0:
        cashflow_base = 0
        cashflow_reason = "亏损企业 现金流堪忧"

    # 加分项：高毛利率（现金流通常更好）
    if gross_margin >= 40:
        cashflow_base += 1
        cashflow_reason += " | 毛利率高"

    # 加分项：低负债率（现金流压力小）
    if 0 < debt_ratio <= 50:
        cashflow_base += 1
        cashflow_reason += " | 负债率低"

    # 限制最高5分
    dimensions["cashflow"] = min(cashflow_base, 5)
    score += dimensions["cashflow"]

    if dimensions["cashflow"] > 0:
        reasons.append(f"{cashflow_reason} ✓")
    elif pe <= 0 or roe <= 0:
        reasons.append("亏损企业 现金流堪忧 ⚠️")

    # ===== 行情因子 (加分项，让每天结果有变化) =====
    # 涨跌幅因子：偏好适度涨跌，避免追高和暴跌
    change_pct = stock.get("change_pct", 0)
    market_bonus = 0

    # 涨跌幅加分逻辑
    if -5 <= change_pct <= 3:
        # 适度涨跌：跌5%到涨3%之间，加分
        if change_pct < 0:
            # 小跌可能是机会
            market_bonus += abs(change_pct) * 0.5  # 跌越多加分越多（抄底机会）
            reasons.append(f"回调 {change_pct:.1f}% 可能是机会")
        else:
            # 小涨也在合理范围
            market_bonus += 1
    elif 3 < change_pct <= 7:
        # 涨幅较大，小幅加分
        market_bonus += 0.5
        reasons.append(f"上涨 {change_pct:.1f}%")
    elif change_pct > 7:
        # 涨幅过大，不加行情分（避免追高）
        reasons.append(f"涨幅 {change_pct:.1f}% 较大 注意追高风险")
    elif change_pct < -7:
        # 跌幅过大，可能有问题
        market_bonus -= 1
        reasons.append(f"大跌 {change_pct:.1f}% 注意风险")

    # ===== 换手率因子（增强版）=====
    # 新评分规则：关注活跃度，不活跃股票已在开头过滤
    turnover_bonus = 0
    if 0.5 <= turnover_rate < 1:
        # 低活跃，不给分
        turnover_bonus = 0
    elif 1 <= turnover_rate < 3:
        # 正常活跃
        turnover_bonus = 2
    elif 3 <= turnover_rate < 10:
        # 高度活跃，最佳区间
        turnover_bonus = 4
        reasons.append(f"换手率 {turnover_rate:.1f}% 活跃 ✓")
    elif 10 <= turnover_rate < 20:
        # 超活跃，可能过热
        turnover_bonus = 3
        reasons.append(f"换手率 {turnover_rate:.1f}% 较活跃")
    elif turnover_rate >= 20:
        # 极度活跃，可能有异常
        turnover_bonus = 0
        reasons.append(f"换手率 {turnover_rate:.1f}% 异常活跃 注意")

    market_bonus += turnover_bonus

    # ===== 多因子v5评分（先计算v5，用于后续判断和买卖点计算）=====
    # 将技术数据转换为v5格式
    v5_tech = None
    if tech_data:
        v5_tech = {
            'momentum_20': tech_data.get('momentum_20', 0),
            'momentum_60': tech_data.get('momentum_60', 0),
            'ma5': tech_data.get('ma5', 0),
            'ma10': tech_data.get('ma10', 0),
            'ma20': tech_data.get('ma20', 0),
            'current_price': tech_data.get('ma5', stock.get('price', 0)),
            'rsi_14': tech_data.get('rsi', 50),
            'volume_ratio': tech_data.get('volume_ratio', 1),
        }
    v5_result = multi_factor_evaluate(stock, v5_tech)
    v5_total = v5_result['v5_total']

    # ===== 技术面评分（增强版）=====
    if tech_data:
        tech_score, tech_reasons = evaluate_technical_score(code, tech_data)
        if tech_score > 0:
            # 对swing股票，技术面评分权重提升50%
            if v5_result.get('strategy') == 'swing':
                tech_score = int(tech_score * 1.5)
            score += tech_score
            reasons.extend(tech_reasons)

        # 额外检测：量能逐步放大 + 突破形态
        volume_ratio = tech_data.get('volume_ratio', 1)
        if volume_ratio >= 1.5:
            vr_bonus = min((volume_ratio - 1.5) * 3, 6)
            score += vr_bonus
            if vr_bonus >= 3:
                reasons.append(f"量能放大{volume_ratio:.1f}倍")

        # 近期突破检测
        price = tech_data.get('current_price', 0)
        ma20 = tech_data.get('ma20', 0)
        ma60 = tech_data.get('ma60', 0)
        if price > 0 and ma20 > 0 and ma60 > 0:
            if price > ma20 * 1.03 and ma20 > ma60 * 1.02:
                score += 4
                reasons.append("均线多头排列+突破")
            elif price > ma20 * 1.02:
                score += 2
                reasons.append("站上MA20")

    # ===== 板块轮动加分（增强版）=====
    if priority_sectors:
        stock_sectors = STOCK_SECTOR_MAP.get(code, [])
        name_hints_sector = {
            "半导体": ["半导体", "芯片", "微", "华创"],
            "新能源": ["新能", "光伏", "锂电", "储能", "电源"],
            "医药": ["医", "药", "生物", "康"],
            "科技": ["科技", "电子", "信息", "软", "通"],
            "AI": ["人工智能", "AI", "算力", "大模型"],
            "机器人": ["机器人", "减速器", "伺服"],
            "固态电池": ["固态电池", "钠电池"],
        }
        for hint, keywords in name_hints_sector.items():
            if any(h in name for h in keywords):
                stock_sectors.append(hint)

        sector_bonus = 0
        best_sector = None
        best_change = 0
        for sector_name, sector_change, bonus in priority_sectors:
            for ss in stock_sectors:
                if ss in sector_name or sector_name in ss:
                    if sector_change > best_change:
                        best_change = sector_change
                        best_sector = sector_name
                    # 涨幅越大加分越多，但过热时衰减
                    if sector_change >= 8:
                        sector_bonus = max(sector_bonus, min(bonus, 3))
                    elif sector_change >= 5:
                        sector_bonus = max(sector_bonus, min(bonus + 1, 5))
                    elif sector_change >= 3:
                        sector_bonus = max(sector_bonus, min(bonus + 2, 6))
                    else:
                        sector_bonus = max(sector_bonus, bonus)
                    break

        if best_sector:
            reasons.append(f"【{best_sector}】板块+{best_change:.1f}%")
        score += sector_bonus

    # 加入行情加分（上限提高到5分，包含换手率）
    market_bonus = max(0, min(market_bonus, 5))
    score += market_bonus

    # 市值信息（不参与评分，仅展示）
    if market_cap_yi > 0:
        reasons.append(f"市值 {market_cap_yi:.0f}亿")

    # 买卖点（2026-04-24优化：传入技术指标计算动态买入价）
    buy_sell = calculate_buy_sell(stock, v5_total, tech_data)

    # 四舍五入所有维度分数，确保显示一致
    rounded_dimensions = {k: round(v) for k, v in dimensions.items()}

    # 添加换手率和技术指标信息
    tech_info = {}
    if tech_data:
        tech_info = {
            "ma5": tech_data.get('ma5', 0),
            "ma20": tech_data.get('ma20', 0),
            "kdj_k": tech_data.get('kdj_k', 0),
            "kdj_d": tech_data.get('kdj_d', 0),
            "rsi": tech_data.get('rsi', 0),
            "volume_ratio": tech_data.get('volume_ratio', 1),
        }

    return {
        "code": code,
        "name": name,
        "price": stock.get("price", 0),
        "change_pct": stock.get("change_pct", 0),
        "turnover_rate": turnover_rate,
        "pe": pe,
        "pb": pb,
        "roe": roe,
        "gross_margin": gross_margin,
        "net_margin": net_margin,
        "debt_ratio": debt_ratio,
        "rev_growth": rev_growth,
        "profit_growth": profit_growth,
        "market_cap": market_cap_yi,
        "industry": stock.get("industry", "未知"),
        "sector_type": stock.get("sector_type", "default"),
        "score": round(score, 1),
        "dimensions": rounded_dimensions,
        "reasons": reasons,
        "buy_sell": buy_sell,
        "tech_info": tech_info,
        "v5_score": v5_result['v5_total'],
        "v5_factors": v5_result['v5_factors'],
        "v5_reasons": v5_result['v5_reasons'],
        "v5_recommendation": v5_result['v5_recommendation'],
        "strategy": v5_result.get('strategy', 'swing'),
        "strategy_label": v5_result.get('strategy_label', '中短线波段'),
        "weights": v5_result.get('weights', STRATEGY_WEIGHTS['swing']),
    }


def calculate_buy_sell(stock, v5_score, tech_data=None):
    """计算买卖点 + 五星评级 - 基于技术形态、趋势和资金的动态买入价
    
    2026-04-24优化：
    - 基于均线支撑计算买入价（MA5/MA10/MA20动态支撑）
    - 考虑趋势强度（KDJ位置、RSI状态）
    - 考虑成交量（量比判断资金活跃度）
    - 不同策略类型（长线/中短线/短线）采用不同买入逻辑
    """
    price = stock.get("price", 0)
    pe = stock.get("pe", 0)
    roe = stock.get("roe", 0)
    gross_margin = stock.get("gross_margin", 0)
    rev_growth = stock.get("rev_growth", 0)
    profit_growth = stock.get("profit_growth", 0)
    strategy = stock.get("strategy", "swing")
    
    if price <= 0 or pe <= 0:
        return None

    # === 动态计算合理PE ===
    avg_growth = (rev_growth + profit_growth) / 2
    growth_premium = min(avg_growth * 0.3, 15)
    fair_pe = roe * 1.5 + growth_premium
    fair_pe = max(12, min(60, fair_pe))

    # === 基于技术形态计算动态买入价 ===
    buy_point = price
    buy_reason = ""
    
    if tech_data:
        ma5 = tech_data.get('ma5', 0)
        ma10 = tech_data.get('ma10', 0)
        ma20 = tech_data.get('ma20', 0)
        kdj_k = tech_data.get('kdj_k', 50)
        kdj_d = tech_data.get('kdj_d', 50)
        rsi = tech_data.get('rsi', 50)
        volume_ratio = tech_data.get('volume_ratio', 1)
        momentum_20 = tech_data.get('momentum_20', 0)
        price_above_ma5 = tech_data.get('price_above_ma5', False)
        price_above_ma20 = tech_data.get('price_above_ma20', False)
        kdj_cross = tech_data.get('kdj_cross', False)
        
        # 1. 均线支撑系统
        if price_above_ma5 and ma5 > 0:
            support_level = ma5
            support_name = "MA5"
        elif price_above_ma20 and ma20 > 0:
            if ma10 > 0 and price > ma10:
                support_level = ma10
                support_name = "MA10"
            else:
                support_level = ma20
                support_name = "MA20"
        elif ma20 > 0:
            support_level = ma20 * 0.98
            support_name = "MA20下方"
        else:
            support_level = price * 0.95
            support_name = "近期低点"
        
        # 2. 趋势强度调整
        trend_adjust = 0
        if kdj_cross:
            trend_adjust -= 0.02  # 金叉，买入价可以更高（更积极）
        elif kdj_k > 80:
            trend_adjust -= 0.05  # KDJ超买，等深回调
        elif kdj_k < 20:
            trend_adjust += 0.03  # KDJ超卖，可以更高价买入
            
        if rsi > 70:
            trend_adjust -= 0.03  # RSI超买
        elif rsi < 30:
            trend_adjust += 0.02  # RSI超卖
            
        # 3. 成交量/资金调整
        volume_adjust = 0
        if volume_ratio >= 2:
            volume_adjust -= 0.02  # 巨量，可能冲高回落
        elif volume_ratio >= 1.5:
            volume_adjust -= 0.01  # 放量，稍微保守
        elif volume_ratio >= 0.8:
            volume_adjust += 0.01  # 正常量能
        else:
            volume_adjust += 0.02  # 缩量，等放量确认
            
        # 4. 策略类型调整
        strategy_adjust = 0
        if strategy == 'short_term':
            # 短线：更激进，贴近支撑位
            strategy_adjust += 0.02
        elif strategy == 'long_term':
            # 长线：更保守，等更好价格
            strategy_adjust -= 0.02
            
        # 5. 综合计算买入价
        total_adjust = trend_adjust + volume_adjust + strategy_adjust
        total_adjust = max(-0.08, min(0.05, total_adjust))  # 限制调整幅度
        
        buy_point = support_level * (1 + total_adjust)
        
        # 确保买入价不超过当前价（不追高）
        buy_point = min(buy_point, price * 0.99)
        
        # 确保买入价不低于当前价的85%（避免过度悲观）
        buy_point = max(buy_point, price * 0.85)
        
        buy_reason = f"基于{support_name}支撑"
        if kdj_cross:
            buy_reason += "+KDJ金叉"
        elif kdj_k < 20:
            buy_reason += "+KDJ超卖"
        if volume_ratio >= 1.5:
            buy_reason += "+放量"
        elif volume_ratio < 0.8:
            buy_reason += "+缩量观望"
    else:
        # 无技术数据时，使用估值-based fallback
        if pe < fair_pe:
            buy_point = price * (0.92 + (pe/fair_pe) * 0.05)
        else:
            buy_point = price * 0.88
        buy_reason = "基于估值"
    
    buy_point = round(buy_point, 2)

    # === 卖出价计算 ===
    if pe < fair_pe:
        upside = min(max((fair_pe - pe) / pe, 0.2), 0.8)
    else:
        upside = 0.15
    sell_point = round(price * (1 + upside), 2)

    # === 推荐等级 ===
    if v5_score >= 82:
        rec = "强烈推荐"
        star_rating = 5 if v5_score >= 86 and roe >= 18 and gross_margin >= 28 else 4
    elif v5_score >= 68:
        rec = "推荐买入"
        star_rating = 4 if v5_score >= 75 else 3
    elif v5_score >= 55:
        rec = "可逢低关注"
        star_rating = 3 if v5_score >= 62 else 2
    else:
        rec = "轻度关注"
        star_rating = 1
        
    # 估值过高降级
    if pe > fair_pe * 1.5 and star_rating > 2:
        star_rating -= 1
        rec = "等待更好买点"

    return {
        "current": price,
        "buy": buy_point,
        "sell": sell_point,
        "upside": round((sell_point - price) / price * 100, 1),
        "downside": round((price - buy_point) / price * 100, 1),
        "recommendation": rec,
        "star_rating": star_rating,
        "buy_reason": buy_reason,
    }


def get_hot_sectors_and_news():
    """获取当日热门板块和新闻热点关键词

    返回:
        hot_sectors: 涨幅前列的板块及其涨幅
        hot_keywords: 新闻中频繁出现的热点关键词
    """
    hot_sectors = {}  # {板块名: 涨幅}
    hot_keywords = set()  # 热点关键词集合

    try:
        # 1. 获取板块行情
        print("  获取板块行情分析热点...")
        industry_sectors = fetch_sina_sectors('industry')
        concept_sectors = fetch_sina_sectors('class')

        all_sectors = industry_sectors + concept_sectors

        # 按涨幅排序，取前10热门板块
        sorted_sectors = sorted(all_sectors, key=lambda x: x.get('change_pct', 0), reverse=True)

        for s in sorted_sectors[:15]:  # Top 15 热门板块
            name = s.get('name', '')
            change = s.get('change_pct', 0)
            if change > 0:  # 只记录上涨板块
                hot_sectors[name] = change
                # 同时记录相关关键词
                for sector_name, keywords in SECTOR_KEYWORDS.items():
                    if sector_name in name or name in sector_name:
                        hot_keywords.update(keywords)

        print(f"    热门板块: {list(hot_sectors.keys())[:5]}")

        # 2. 获取新闻热点
        try:
            r = session.get("https://feed.mix.sina.com.cn/api/roll/get",
                           params={"pageid": "153", "lid": "2509", "k": "", "r": "0.5", "page": 1},
                           headers=HEADERS, timeout=10)
            d = r.json()
            if d.get('result') and d['result'].get('data'):
                news_titles = [item.get('title', '') for item in d['result']['data'][:30]]
                news_text = ' '.join(news_titles)

                # 统计热点关键词出现次数
                keyword_count = {}
                for keyword, sectors in HOT_KEYWORD_TO_SECTOR.items():
                    count = news_text.count(keyword)
                    if count > 0:
                        keyword_count[keyword] = count
                        hot_keywords.add(keyword)
                        # 把关键词对应的板块也加入热门
                        for sector in sectors:
                            if sector not in hot_sectors:
                                hot_sectors[sector] = 0.5  # 新闻热度加分

                # 按出现次数排序，取最热的10个关键词
                top_keywords = sorted(keyword_count.items(), key=lambda x: x[1], reverse=True)[:10]
                if top_keywords:
                    print(f"    新闻热点: {[k[0] for k in top_keywords[:5]]}")

        except Exception as e:
            print(f"    新闻获取失败: {e}")

    except Exception as e:
        print(f"    板块数据获取失败: {e}")

    return hot_sectors, hot_keywords


def calculate_hot_factor(stock_code, stock_name, hot_sectors, hot_keywords):
    """计算股票的热点因子 (v17.1: 衰减机制，避免追高)

    返回: (热点加分, 热点原因列表)

    衰减逻辑:
    - 板块涨幅越大，衰减越强: 涨幅>5%时加分减半，涨幅>8%时仅给1/3
    - 新闻热度加分上限降低: 3->2
    - 总上限从20降至12
    """
    bonus = 0
    reasons = []

    stock_sectors = STOCK_SECTOR_MAP.get(stock_code, [])

    name_hints = {
        "半导体": ["半导体", "芯片", "微", "创", "华创"],
        "新能源": ["新能", "光伏", "锂电", "储能", "电源", "宁德", "比亚迪"],
        "医药": ["医", "药", "生物", "康", "健"],
        "科技": ["科技", "电子", "信息", "软", "通"],
    }
    for hint, keywords in name_hints.items():
        if any(h in stock_name for h in keywords):
            stock_sectors.append(hint)

    for stock_sector in stock_sectors:
        for hot_sector, change in hot_sectors.items():
            if stock_sector in hot_sector or hot_sector in stock_sector:
                if change >= 8:
                    bonus += 3
                    reasons.append(f"过热【{hot_sector}】+{change:.1f}% 注意追高")
                elif change >= 5:
                    bonus += 5
                    reasons.append(f"热门【{hot_sector}】+{change:.1f}%")
                elif change >= 3:
                    bonus += 8
                    reasons.append(f"活跃【{hot_sector}】+{change:.1f}%")
                elif change >= 2:
                    bonus += 6
                    reasons.append(f"关注【{hot_sector}】+{change:.1f}%")
                elif change >= 1:
                    bonus += 3
                else:
                    bonus += 1
                break

    sector_keywords = ["固态电池", "钠电池", "半导体", "芯片", "光伏", "储能", "锂电", "新能源",
                       "人工智能", "AI", "数字经济", "机器人", "医药", "医疗", "创新药",
                       "消费电子", "汽车", "特斯拉", "华为", "苹果"]

    for keyword in sector_keywords:
        if keyword in stock_name:
            for hot_sector, change in hot_sectors.items():
                if keyword in hot_sector and change > 0:
                    if change >= 8:
                        bonus += 1
                    elif change >= 5:
                        bonus += 2
                    else:
                        bonus += 4
                        reasons.append(f"热点{keyword}")
                    break

    for keyword in hot_keywords:
        if keyword in stock_name:
            bonus += 2
            reasons.append(f"新闻【{keyword}】")

    bonus = min(bonus, 12)

    return bonus, reasons[:3]
