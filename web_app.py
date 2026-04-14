# -*- coding: utf-8 -*-
"""
Value Investment King - Smart Stock Selection System v17-INDUSTRY-PE-UPDATE
Flask Backend + Frontend + News + Capital Flow + Stock Detail
v17 Improvement: Industry PE thresholds updated based on 189-stock full backtest
  - Semiconductor: 78->100 (median 84.6)
  - Software/AI: 52->120 (median 86.8)
  - Medical Devices: 46->80 (median 70.5)
  - New Energy: 36->50 (median 33-42)
  - Electronics: 65->85 (median 49)
  - Finance: 15->20 (median 13.6)
  Result: High-growth stocks now get fair PE scores
v16 Improvement: Round 4 optimal - Top 5, 90-day holding
  Backtest: +26.09% return, 80% win rate, 15 trades
  Params: V=0.36 Q=0.11 G=0.08 M=0.12 S=0.33, M20_hi=0.05
v15: Multi-factor scoring (Value 30% + Quality 25% + Growth 20% + Momentum 15% + Sentiment 10%)
v14改进: 每日推荐栏目化，定时自动选股（9:26/14:30），保留早晚结果

DEBT_RATIO_FIX_V3: Added debt_ratio to all data flows
"""
import requests
import json
import time
import sys
import io
import os
import hashlib
import threading
import atexit
from datetime import datetime, timedelta
from flask import Flask, render_template, jsonify, request, send_from_directory
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from concurrent.futures import ThreadPoolExecutor, as_completed

# 禁用SSL警告（离线模式）
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# === 行业PE合理区间配置（基于189只股票全样本回测）===
# 数据来源：2026-04-14 全股票回测分析，覆盖24个行业
# v17更新：根据实际PE分布调整阈值，避免高成长股被误判
SECTOR_PE_RANGES = {
    # 半导体/芯片：189只样本中位数84.6，P25=40.6，P75=151.6
    'semiconductor': {
        'industry_names': ['半导体', '芯片', '集成电路'],
        'keywords': ['半导体', '芯片', '集成电路', 'GPU', '算力'],
        'pe_fair_max': 100,  # v17: 原78提至100，覆盖中位数84.6
        'pe_fair_low': 28,   # v17: 原31降至28
    },
    # 生物制品/医药/医疗器械：中位数27-70，P75=96.5
    'bio_pharma': {
        'industry_names': ['生物制品', '医药', '医疗服务', '医疗器械', '中药', '医疗行业', '医药制造'],
        'keywords': ['生物制品', '医药', '医疗', '制药', '疫苗', 'CXO', '中药', '器械'],
        'pe_fair_max': 80,   # v17: 原46提至80，覆盖医疗器械中位数70.5
        'pe_fair_low': 13,
    },
    # 新能源/电池/光伏/风电：中位数33-42，P75=71.7
    'new_energy': {
        'industry_names': ['电池', '光伏', '储能', '锂电', '新能源', '光伏设备', '风电设备'],
        'keywords': ['电池', '光伏', '储能', '锂电', '新能源', '固态', '钠电', '充电桩', '风电'],
        'pe_fair_max': 50,   # v17: 原36提至50，覆盖电池中位数33
        'pe_fair_low': 22,   # v17: 原18提至22
    },
    # 电子元件/消费电子：中位数39-49，P75=74.2
    'electronics': {
        'industry_names': ['电子元件', '消费电子', '电子'],
        'keywords': ['电子元件', '消费电子', '光通信', 'PCB', '电路板', '苹果产业链'],
        'pe_fair_max': 85,   # v17: 原65提至85，覆盖中位数49.1
        'pe_fair_low': 25,   # v17: 原18提至25
    },
    # 软件/信息服务/AI：中位数86.8，P75=131.9（高成长行业）
    'software_it': {
        'industry_names': ['软件', '信息服务', '通信', '数字经济', '软件服务'],
        'keywords': ['软件', '信息', '科技', '数字', '云计算', '大数据', 'AI', '人工智能'],
        'pe_fair_max': 120,  # v17: 原52提至120，覆盖中位数86.8
        'pe_fair_low': 39,   # v17: 原27提至39
    },
    # 汽车制造/零部件：中位数15.9-26.2，P75=48.6
    'automotive': {
        'industry_names': ['汽车制造', '汽车零部件', '汽车整车'],
        'keywords': ['汽车制造', '汽车零部件', '汽车', '新能源汽车'],
        'pe_fair_max': 50,   # v17: 原39提至50，覆盖零部件P75=48.6
        'pe_fair_low': 10,   # v17: 原5提至10
    },
    # 电气设备/机械：中位数23.3，P75=96.2
    'electrical_machinery': {
        'industry_names': ['电气设备', '机械', '专用设备'],
        'keywords': ['电气设备', '机械', '重工', '电力设备', '专用设备'],
        'pe_fair_max': 35,   # v17: 原32提至35
        'pe_fair_low': 16,   # v17: 原10提至16
    },
    # 金融/地产/公用/券商：中位数6-14，低PE行业
    'finance_utility': {
        'industry_names': ['银行', '保险', '证券', '房地产', '公用事业', '券商信托', '电力行业', '港口水运'],
        'keywords': ['银行', '保险', '证券', '地产', '房地产', '公用', '电力', '水务', '高速', '港口', '券商'],
        'pe_fair_max': 20,   # v17: 原15提至20，覆盖券商P75=14
        'pe_fair_low': 8,    # v17: 原5提至8
    },
    # 周期/化工/有色：中位数18-22，P25=17
    'cyclical': {
        'industry_names': ['化工', '有色金属', '钢铁', '建材', '煤炭', '石油', '化工行业', '化学原料'],
        'keywords': ['化工', '有色', '钢铁', '建材', '煤炭', '石油', '水泥', '玻璃', '矿业', '化学'],
        'pe_fair_max': 30,
        'pe_fair_low': 12,   # v17: 原10提至12
    },
    # 消费/食品饮料：参考医药，中位数约25，合理区间15-40
    # Consumer/Food/Beverage/Liquor: median 17-20, P75=36.5
    'consumer': {
        'industry_names': ['食品饮料', '消费', '旅游', '免税', '零售', '白酒', '家电', '消费电子'],
        'keywords': ['消费', '食品', '饮料', '酒', '旅游', '免税', '零售', '家电'],
        'pe_fair_max': 45,
        'pe_fair_low': 14,
    },
}

# ========== 每日推荐数据存储 ==========
# 存储结构: {"morning": {...}, "afternoon": {...}, "last_update": "..."}
DAILY_PICK_DATA = {
    "morning": None,      # 早盘选股结果
    "afternoon": None,    # 午盘选股结果
    "last_update": None,  # 最后更新时间
}
DAILY_PICK_LOCK = threading.Lock()
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DAILY_PICK_FILE = os.path.join(_BASE_DIR, 'daily_pick_cache.json')

# 创建全局session，禁用SSL验证和代理 —— 关键！所有HTTPS请求必须通过此session
session = requests.Session()
session.verify = False
session.trust_env = False  # 禁用环境代理（解决代理服务未运行导致的连接失败）
retry_strategy = Retry(total=2, backoff_factor=1)
adapter = HTTPAdapter(max_retries=retry_strategy)
session.mount("https://", adapter)
session.mount("http://", adapter)

# ============================================================
# 多因子评分系统 v5.0（新增）
# 价值(30%) + 质量(25%) + 成长(20%) + 动量(15%) + 情绪(10%)
# ============================================================

def mf_score_value(stock):
    """价值因子（0-100分）— 2026-04-13修复：PE评分按板块动态调整"""
    score = 0
    pe = stock.get('pe', 0)
    pb = stock.get('pb', 0)
    profit_growth = stock.get('profit_growth', 0)
    market_cap = stock.get('market_cap', 0)
    
    # === 按行业动态调整PE评分标准 ===
    code = stock.get('code', '')
    
    # 从东方财富API获取行业信息
    industry_info = get_stock_industry(code)
    pe_fair_max = industry_info.get('pe_fair_max', 30)
    pe_fair_low = industry_info.get('pe_fair_low', 15)
    
    # 将行业信息写入stock（供后续API返回）
    stock['industry'] = industry_info.get('industry', '未知')
    stock['sector_type'] = industry_info.get('sector_type', 'default')
    
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
    多因子综合评分 v5.1 (Round 4 最优方案)
    价值(36%) + 质量(11%) + 成长(8%) + 动量(12%) + 情绪(33%)
    回测: 收益率+26.09% | 胜率80.0% | 15笔交易 | 持有90天
    M20_hi=0.05 (早期动量信号阈值)
    """
    v = mf_score_value(stock)
    q = mf_score_quality(stock)
    g = mf_score_growth(stock)
    m = mf_score_momentum(tech_data) if tech_data else 50
    s = mf_score_sentiment(stock, tech_data) if tech_data else 50
    # Round 4 最优权重
    total = v * 0.36 + q * 0.11 + g * 0.08 + m * 0.12 + s * 0.33
    reasons = []
    if v >= 75: reasons.append(f"估值优秀(V{v:.0f})")
    elif v >= 60: reasons.append(f"估值合理(V{v:.0f})")
    if q >= 75: reasons.append(f"质量优秀(Q{q:.0f})")
    elif q >= 60: reasons.append(f"质量良好(Q{q:.0f})")
    if g >= 75: reasons.append(f"高成长(G{g:.0f})")
    elif g >= 60: reasons.append(f"成长良好(G{g:.0f})")
    if m >= 70: reasons.append(f"动量强劲(M{m:.0f})")
    elif m >= 55: reasons.append(f"动量中性(M{m:.0f})")
    # Round 4 推荐阈值调整（选5只更严格）
    if total >= 78: rec = "强烈推荐"
    elif total >= 68: rec = "推荐"
    elif total >= 58: rec = "关注"
    else: rec = "观望"
    return {
        'v5_total': round(total, 2),
        'v5_factors': {
            'value': round(v, 2), 'quality': round(q, 2),
            'growth': round(g, 2), 'momentum': round(m, 2), 'sentiment': round(s, 2),
        },
        'v5_reasons': reasons,
        'v5_recommendation': rec,
    }

# 通用请求头
HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
EM_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Referer': 'https://quote.eastmoney.com/'
}
DC_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
    'Referer': 'https://data.eastmoney.com/'
}

# Windows控制台编码
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', write_through=True)

app = Flask(__name__, static_folder='static', template_folder='templates')

# ========== 每日推荐缓存管理 ==========

def load_daily_pick_cache():
    """从文件加载每日推荐缓存"""
    global DAILY_PICK_DATA
    try:
        if os.path.exists(DAILY_PICK_FILE):
            with open(DAILY_PICK_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                # 检查是否是今天的数据
                if data.get('date') == datetime.now().strftime('%Y-%m-%d'):
                    DAILY_PICK_DATA = data
                    print(f"✓ 加载今日选股缓存: 早上 {bool(data.get('morning'))}, 下午 {bool(data.get('afternoon'))}")
                    return
                else:
                    print("⚠️ 缓存日期不是今天，将重新选股")
    except Exception as e:
        print(f"加载缓存失败: {e}")
    # 重置为今天的空数据
    DAILY_PICK_DATA = {
        "date": datetime.now().strftime('%Y-%m-%d'),
        "morning": None,
        "afternoon": None,
        "last_update": None,
    }

def save_daily_pick_cache():
    """保存每日推荐缓存到文件"""
    try:
        with open(DAILY_PICK_FILE, 'w', encoding='utf-8') as f:
            json.dump(DAILY_PICK_DATA, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"保存缓存失败: {e}")

def execute_daily_pick(session_type):
    """执行选股并存储结果

    session_type: 'morning' 或 'afternoon'
    """
    global DAILY_PICK_DATA
    print(f"\n{'='*50}")
    print(f"🕐 开始执行{('早盘' if session_type == 'morning' else '午盘')}选股...")
    print(f"{'='*50}")

    try:
        results = run_picker()
        if results:
            total = results[0].get('_total_scanned', 0) if results else 0
            for r in results:
                r.pop('_total_scanned', None)

            # 过滤新股
            filtered = [r for r in results if not r.get('name','').startswith('N') and '退' not in r.get('name','') and r.get('change_pct',0) <= 100]

            # 早盘和午盘不同的排序策略
            if session_type == 'morning':
                # 早盘选股：侧重基本面和估值
                # 评分权重：基本面分 + 估值分，不看当日行情
                def morning_score(r):
                    base = r.get('score', 0)
                    pe = r.get('pe', 0)
                    roe = r.get('roe', 0)

                    # 估值加分：低PE加分
                    valuation_bonus = 0
                    if 0 < pe <= 15:
                        valuation_bonus = 5
                    elif 15 < pe <= 25:
                        valuation_bonus = 3
                    elif 25 < pe <= 35:
                        valuation_bonus = 1

                    # 高ROE加分
                    roe_bonus = 0
                    if roe >= 25:
                        roe_bonus = 3
                    elif roe >= 20:
                        roe_bonus = 2

                    return base + valuation_bonus + roe_bonus

                # 排除涨幅过大的（避免追高）
                morning_candidates = [r for r in filtered if r.get('change_pct', 0) <= 5]
                if len(morning_candidates) < 10:
                    morning_candidates = filtered

                top10 = sorted(morning_candidates, key=morning_score, reverse=True)[:10]
                # Round 4 最优方案: 选5只，集中火力
                top10 = top10[:5]
                pick_strategy = "早盘策略（Round 4）：选Top 5集中持仓，收益率+26.09%"
            else:
                # 午盘选股：侧重当日行情表现
                # 关注涨幅适中、换手活跃的股票
                def afternoon_score(r):
                    base_score = r.get('score', 0)
                    change_pct = r.get('change_pct', 0)
                    turnover = r.get('turnover_rate', 0)

                    bonus = 0

                    # 当日涨幅加分：偏好涨幅1-4%（已验证走势但未大涨）
                    if 1 <= change_pct <= 4:
                        bonus += 5  # 最佳涨幅区间
                    elif 0 <= change_pct < 1:
                        bonus += 3  # 微涨
                    elif -2 <= change_pct < 0:
                        bonus += 4  # 小跌可能是机会
                    elif 4 < change_pct <= 6:
                        bonus += 2  # 涨幅稍大
                    elif change_pct > 6:
                        bonus += 0  # 涨幅过大，不加分

                    # 换手率加分：偏好活跃但不疯狂
                    if 3 <= turnover <= 10:
                        bonus += 4  # 最活跃区间
                    elif 1.5 <= turnover < 3:
                        bonus += 3
                    elif 10 < turnover <= 15:
                        bonus += 2
                    elif turnover > 15:
                        bonus += 1  # 过于活跃，谨慎

                    return base_score + bonus

                # 排除大跌股票（风险较大）
                afternoon_candidates = [r for r in filtered if r.get('change_pct', 0) > -5]
                if len(afternoon_candidates) < 10:
                    afternoon_candidates = filtered

                # 午盘按综合分排序
                scored_candidates = [(r, afternoon_score(r)) for r in afternoon_candidates]
                scored_candidates.sort(key=lambda x: x[1], reverse=True)
                top10 = [r for r, s in scored_candidates[:10]]
                # Round 4 最优方案: 选5只
                top10 = top10[:5]
                pick_strategy = "午盘策略（Round 4）：选Top 5集中持仓，收益率+26.09%"

            with DAILY_PICK_LOCK:
                DAILY_PICK_DATA[session_type] = {
                    "results": top10,
                    "total_scanned": total,
                    "pick_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "session_type": "早盘选股" if session_type == 'morning' else "午盘选股",
                    "strategy": pick_strategy,
                }
                DAILY_PICK_DATA['last_update'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                DAILY_PICK_DATA['date'] = datetime.now().strftime('%Y-%m-%d')
                save_daily_pick_cache()

            print(f"✓ {('早盘' if session_type == 'morning' else '午盘')}选股完成: {len(top10)} 只股票")
            print(f"  策略: {pick_strategy}")
        else:
            print(f"✗ 选股失败，无结果")
    except Exception as e:
        print(f"✗ 选股执行失败: {e}")

def schedule_daily_pick():
    """定时任务：每天9:27和14:30执行选股"""
    global DAILY_PICK_DATA
    last_executed = {"morning": None, "afternoon": None}  # 记录已执行的时间点

    while True:
        now = datetime.now()
        today = now.strftime('%Y-%m-%d')
        current_time = now.strftime("%H:%M")

        # 检查是否需要重置（新的一天）
        with DAILY_PICK_LOCK:
            if DAILY_PICK_DATA.get('date') != today:
                DAILY_PICK_DATA = {
                    "date": today,
                    "morning": None,
                    "afternoon": None,
                    "last_update": None,
                }
                last_executed = {"morning": None, "afternoon": None}  # 重置执行记录
                print(f"📅 新的一天: {today}")

        # 早盘选股: 9:27（每天固定时间强制刷新）
        if current_time == "09:27" and last_executed["morning"] != today:
            last_executed["morning"] = today
            threading.Thread(target=execute_daily_pick, args=('morning',), daemon=True).start()
            print("⏰ 早盘选股任务已触发 (9:27)")

        # 午盘选股: 14:30（每天固定时间强制刷新）
        elif current_time == "14:30" and last_executed["afternoon"] != today:
            last_executed["afternoon"] = today
            threading.Thread(target=execute_daily_pick, args=('afternoon',), daemon=True).start()
            print("⏰ 午盘选股任务已触发 (14:30)")

        # 每分钟检查一次
        time.sleep(60)

def start_scheduler():
    """启动定时任务线程"""
    scheduler_thread = threading.Thread(target=schedule_daily_pick, daemon=True)
    scheduler_thread.start()
    print("✓ 定时选股任务已启动 (9:27 早盘, 14:30 午盘)")
    return scheduler_thread

# ========== 数据模块（复用smart_stock_picker逻辑）==========

def get_realtime_quotes():
    """获取实时行情数据 - 全市场扫描
    
    2026-04-02: push2/push2his clist/get 全被封禁。
    新方案：datacenter-web 获取财务筛选股票 + 腾讯qt.gtimg.cn批量获取实时行情
    """
    all_stocks = []
    dc_headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
        'Referer': 'https://data.eastmoney.com/'
    }

    try:
        # === 第1步：从datacenter-web获取有财务数据的股票列表 ===
        print("  从财务数据中心获取股票列表...")
        candidate_stocks = {}  # code -> {name, roe, gross_margin, rev_growth, profit_growth}
        
        # 2026-04-02修复: 必须用REPORT_DATE_NAME筛选年报, 否则API返回季报年化ROE(100-200%)
        latest_report = None
        current_year = datetime.now().year
        for yr in range(current_year, current_year - 2, -1):
            try:
                test_params = {
                    'reportName': 'RPT_F10_FINANCE_MAINFINADATA',
                    'columns': 'REPORT_DATE_NAME',
                    'filter': '(REPORT_DATE_NAME="' + str(yr) + '年报")',
                    'pageNumber': 1, 'pageSize': 1,
                    'source': 'WEB', 'client': 'WEB',
                }
                tr = session.get('https://datacenter-web.eastmoney.com/api/data/v1/get',
                                  params=test_params, headers=DC_HEADERS, timeout=10)
                td = tr.json()
                if td.get('success') and td.get('result') and td['result'].get('count', 0) > 0:
                    latest_report = str(yr) + '年报'
                    print(f"  使用最新年报: {latest_report}")
                    break
            except:
                continue
        if not latest_report:
            latest_report = str(current_year - 1) + '年报'
            print(f"  默认使用年报: {latest_report}")

        # 从MAINFINADATA获取年报ROE+毛利率（按ROE排序取前3000）
        report_filter = '(REPORT_DATE_NAME="' + latest_report + '")'
        for sort_col, extra_filter, sort_dir in [
            ('ROEJQ', '(ROEJQ>5)(ROEJQ<80)', '-1'),
            ('ROEJQ', '(ROEJQ>10)(ROEJQ<80)', '-1'),
        ]:
            try:
                params = {
                    'reportName': 'RPT_F10_FINANCE_MAINFINADATA',
                    'columns': 'SECURITY_CODE,SECURITY_NAME_ABBR,ROEJQ,XSMLL,ZCFZL,XSJLL',  # ROE, 毛利率, 资产负债率, 净利率
                    'filter': report_filter + extra_filter,
                    'pageNumber': 1, 'pageSize': 3000,
                    'source': 'WEB', 'client': 'WEB',
                    'sortColumns': sort_col,
                    'sortTypes': sort_dir
                }
                resp = session.get('https://datacenter-web.eastmoney.com/api/data/v1/get',
                                    params=params, headers=DC_HEADERS, timeout=15)
                d = resp.json()
                if d.get('success') and d.get('result') and d['result'].get('data'):
                    for item in d['result']['data']:
                        code = item.get('SECURITY_CODE', '')
                        name = item.get('SECURITY_NAME_ABBR', '')
                        if not code or not name or len(code) != 6:
                            continue
                        if 'ST' in name or '*' in name:
                            continue
                        # 排除北交所(8/4开头)、B股(900/200开头)、A股重复(A2开头)
                        if code.startswith('8') or code.startswith('4') or code.startswith('920'):
                            continue
                        if code.startswith('900') or code.startswith('200'):
                            continue
                        if code.startswith('A2'):
                            continue
                        roe = item.get('ROEJQ', 0)
                        gm = item.get('XSMLL', 0)
                        zcfzl = item.get('ZCFZL', 0)  # 资产负债率
                        xsjll = item.get('XSJLL', 0)  # 净利率
                        if code not in candidate_stocks:
                            candidate_stocks[code] = {
                                'name': name, 'roe': 0, 'gross_margin': 0,
                                'rev_growth': 0, 'profit_growth': 0,
                                'debt_ratio': 0, 'net_margin': 0,
                            }
                        if roe is not None:
                            fval = float(roe)
                            if 1 <= fval <= 80 and fval > candidate_stocks[code]['roe']:
                                candidate_stocks[code]['roe'] = fval
                        if gm is not None:
                            fgm = float(gm)
                            if fgm > 0 and fgm > candidate_stocks[code]['gross_margin']:
                                candidate_stocks[code]['gross_margin'] = fgm
                        if zcfzl is not None:
                            fzcfzl = float(zcfzl)
                            if 0 <= fzcfzl <= 100 and fzcfzl > candidate_stocks[code]['debt_ratio']:
                                candidate_stocks[code]['debt_ratio'] = fzcfzl
                        if xsjll is not None:
                            fxsjll = float(xsjll)
                            if fxsjll > 0 and fxsjll > candidate_stocks[code]['net_margin']:
                                candidate_stocks[code]['net_margin'] = fxsjll
            except Exception as e:
                print(f"  获取{sort_col}列表失败: {e}")

        # 补充CPD数据（营收/净利增速）- 用DATAYEAR+DATEMMDD筛选年报
        # CPD不支持REPORT_DATE_NAME, 用(DATAYEAR=xxxx)(DATEMMDD="年报")代替
        current_year_val = current_year
        cpd_report_filter = '(DATAYEAR=' + str(current_year_val) + ')(DATEMMDD="年报")'
        # 验证最新年报是否存在
        try:
            test_p = {
                'reportName': 'RPT_LICO_FN_CPD',
                'columns': 'SECURITY_CODE',
                'filter': cpd_report_filter,
                'pageNumber': 1, 'pageSize': 1,
                'source': 'WEB', 'client': 'WEB',
            }
            tr = session.get('https://datacenter-web.eastmoney.com/api/data/v1/get',
                              params=test_p, headers=DC_HEADERS, timeout=10)
            td = tr.json()
            if not (td.get('success') and td.get('result') and td['result'].get('count', 0) > 0):
                current_year_val -= 1
                cpd_report_filter = '(DATAYEAR=' + str(current_year_val) + ')(DATEMMDD="年报")'
        except:
            current_year_val -= 1
            cpd_report_filter = '(DATAYEAR=' + str(current_year_val) + ')(DATEMMDD="年报")'
        print(f"  CPD年报筛选: {cpd_report_filter}")
        
        for cpd_filter, sort_col in [
            ('(SJLTZ>10)(SJLTZ<5000)', 'SJLTZ'),
            ('(YSTZ>10)(YSTZ<5000)', 'YSTZ'),
        ]:
            try:
                params = {
                    'reportName': 'RPT_LICO_FN_CPD',
                    'columns': 'SECURITY_CODE,SECURITY_NAME_ABBR,YSTZ,SJLTZ',
                    'filter': cpd_report_filter + cpd_filter,
                    'pageNumber': 1, 'pageSize': 3000,
                    'source': 'WEB', 'client': 'WEB',
                    'sortColumns': sort_col, 'sortTypes': '-1'
                }
                resp = session.get('https://datacenter-web.eastmoney.com/api/data/v1/get',
                                    params=params, headers=DC_HEADERS, timeout=15)
                d = resp.json()
                if d.get('success') and d.get('result') and d['result'].get('data'):
                    for item in d['result']['data']:
                        code = item.get('SECURITY_CODE', '')
                        name = item.get('SECURITY_NAME_ABBR', '')
                        if not code or len(code) != 6:
                            continue
                        if not name or 'ST' in name or '*' in name:
                            continue
                        if code.startswith('8') or code.startswith('4') or code.startswith('920'):
                            continue
                        if code.startswith('900') or code.startswith('200'):
                            continue
                        if code.startswith('A2'):
                            continue
                        ystz = item.get('YSTZ', 0)
                        sjltz = item.get('SJLTZ', 0)
                        if code not in candidate_stocks:
                            candidate_stocks[code] = {
                                'name': name, 'roe': 0, 'gross_margin': 0,
                                'rev_growth': 0, 'profit_growth': 0,
                                'debt_ratio': 0, 'net_margin': 0,
                            }
                        if ystz is not None and abs(float(ystz)) <= 1000:
                            candidate_stocks[code]['rev_growth'] = max(candidate_stocks[code]['rev_growth'], float(ystz))
                        if sjltz is not None and abs(float(sjltz)) <= 10000:
                            candidate_stocks[code]['profit_growth'] = max(candidate_stocks[code]['profit_growth'], float(sjltz))
            except Exception as e:
                print(f"  获取CPD {sort_col}失败: {e}")

        print(f"  财务筛选出 {len(candidate_stocks)} 只候选股票")

        # === 第2步：用腾讯API批量获取实时行情（并发加速）===
        # 腾讯API格式: sh600519, sz002594，每批80只
        # 重要: 腾讯API必须用HTTP，不能走HTTPS（SSL证书问题）
        codes = list(candidate_stocks.keys())
        batch_size = 80
        total_batches = (len(codes) + batch_size - 1) // batch_size
        price_data = {}
        
        print(f"  通过腾讯API获取实时行情（{total_batches}批，并发获取）...")
        
        # 定义单批次获取函数
        def fetch_batch(batch_idx):
            batch_codes = codes[batch_idx:batch_idx+batch_size]
            tx_codes = [f"sh{c}" if c.startswith('6') else f"sz{c}" for c in batch_codes]
            batch_result = {}
            try:
                url = 'http://qt.gtimg.cn/q=' + ','.join(tx_codes)
                resp = session.get(url, timeout=10)  # timeout从15降到10
                lines = resp.text.strip().split(';')
                for line in lines:
                    if not line.strip():
                        continue
                    parts = line.split('~')
                    if len(parts) < 50:
                        continue
                    code = parts[2]
                    if not code or len(code) != 6:
                        continue
                    try:
                        price = float(parts[3]) if parts[3] else 0
                    except:
                        price = 0
                    if price <= 0:
                        continue
                    try:
                        change_pct = float(parts[32]) if parts[32] else 0
                    except:
                        change_pct = 0
                    try:
                        pe = float(parts[39]) if parts[39] and parts[39] != '-' else 0
                        if pe > 10000 or pe < 0: pe = 0
                    except:
                        pe = 0
                    try:
                        # 腾讯API: parts[44]=总市值，单位是"亿元"（已验证）
                        total_cap_yi = float(parts[44]) if parts[44] else 0
                    except:
                        total_cap_yi = 0
                    try:
                        amount_wan = float(parts[43]) if parts[43] else 0
                    except:
                        amount_wan = 0
                    try:
                        high = float(parts[33]) if parts[33] else 0
                    except:
                        high = 0
                    try:
                        low = float(parts[34]) if parts[34] else 0
                    except:
                        low = 0
                    try:
                        open_p = float(parts[5]) if parts[5] else 0
                    except:
                        open_p = 0
                    try:
                        prev_close = float(parts[4]) if parts[4] else 0
                    except:
                        prev_close = 0
                    try:
                        volume_gu = float(parts[37]) if parts[37] else 0
                    except:
                        volume_gu = 0
                    try:
                        turnover_rate = float(parts[38]) if parts[38] else 0
                    except:
                        turnover_rate = 0

                    batch_result[code] = {
                        'name': parts[1], 'price': price, 'change_pct': change_pct,
                        'volume': volume_gu, 'amount': amount_wan * 10000,
                        'market_cap': total_cap_yi,  # 直接存储亿元单位
                        'pe': pe,
                        'high': high, 'low': low, 'open': open_p, 'prev_close': prev_close,
                        'turnover_rate': turnover_rate,
                    }
            except Exception as e:
                print(f"  腾讯API第{batch_idx//batch_size+1}批失败: {e}")
            return batch_result
        
        # 并发获取所有批次（最多10个并发）
        start_time = time.time()
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(fetch_batch, i) for i in range(0, len(codes), batch_size)]
            for future in as_completed(futures):
                batch_result = future.result()
                price_data.update(batch_result)
        
        elapsed = time.time() - start_time
        print(f"  腾讯API获取到 {len(price_data)} 只实时行情，耗时 {elapsed:.1f}s")

        # === 第3步：合并数据 ===
        for code, fin in candidate_stocks.items():
            if code not in price_data:
                continue
            pd = price_data[code]
            stock = {
                'code': code, 'name': pd['name'] or fin['name'],
                'price': pd['price'], 'change_pct': pd['change_pct'],
                'volume': pd['volume'], 'amount': pd['amount'],
                'high': pd['high'], 'low': pd['low'],
                'open': pd['open'], 'prev_close': pd['prev_close'],
                'pe': pd['pe'], 'pb': 0,
                'roe': fin['roe'], 'gross_margin': fin['gross_margin'],
                'net_margin': fin.get('net_margin', 0), 'debt_ratio': fin.get('debt_ratio', 0),
                'rev_growth': fin['rev_growth'], 'profit_growth': fin['profit_growth'],
                'market_cap': pd['market_cap'],
                'turnover_rate': pd.get('turnover_rate', 0),
            }
            all_stocks.append(stock)

        # === 第4步：PB数据简化（跳过慢速补充，用预设数据或估算）===
        # PB不是关键评分指标，优先用预设数据或从PE/ROE估算
        # 不再调用push2 stock/get逐只获取（太慢）
        preset_financials = get_preset_financials()
        for stock in all_stocks:
            code = stock['code']
            if stock.get('pb', 0) == 0:
                # 优先用预设数据
                if code in preset_financials and preset_financials[code].get('pb', 0) > 0:
                    stock['pb'] = preset_financials[code]['pb']
                # 备选：从PE和ROE估算（PB ≈ PE × ROE / 100）
                elif stock.get('pe', 0) > 0 and stock.get('roe', 0) > 0:
                    stock['pb'] = round(stock['pe'] * stock['roe'] / 100, 2)
        
        print(f"  PB数据已补充（预设+估算），完成扫描共 {len(all_stocks)} 只股票")
        return all_stocks

    except Exception as e:
        print(f"获取行情失败: {e}")
        return []

# === 行业信息缓存 ===
INDUSTRY_CACHE = {}  # {code: {'industry': '...', 'pe_median': 50, ...}}

def get_stock_industry(code):
    """获取股票所属行业（从东方财富API）
    
    返回: {'industry': '半导体', 'sector_type': 'semiconductor', ...}
    """
    # 检查缓存
    if code in INDUSTRY_CACHE:
        return INDUSTRY_CACHE[code]
    
    result = {'industry': '未知', 'sector_type': 'default', 'pe_fair_max': 30, 'pe_fair_low': 15}
    
    try:
        market = 'SH' if code.startswith('6') else 'SZ'
        url = f'https://emweb.eastmoney.com/PC_HSF10/CompanySurvey/CompanySurveyAjax?code={market}{code}'
        resp = session.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)  # timeout改为10秒
        data = resp.json()
        
        if 'jbzl' in data:
            jbzl = json.loads(data['jbzl']) if isinstance(data['jbzl'], str) else data['jbzl']
            industry = jbzl.get('sshy', '')
            name = jbzl.get('agjc', '')
            
            if industry:
                # 匹配行业分类
                sector_type = 'default'
                for sname, sconfig in SECTOR_PE_RANGES.items():
                    # 优先匹配 industry_names
                    if 'industry_names' in sconfig:
                        for ind_name in sconfig['industry_names']:
                            if ind_name in industry or industry in ind_name:
                                sector_type = sname
                                break
                    # 其次匹配 keywords
                    if sector_type == 'default' and 'keywords' in sconfig:
                        for kw in sconfig['keywords']:
                            if kw in name or kw in industry:
                                sector_type = sname
                                break
                    if sector_type != 'default':
                        break
                
                sector_config = SECTOR_PE_RANGES.get(sector_type, {'pe_fair_max': 30, 'pe_fair_low': 15})
                result = {
                    'industry': industry,
                    'sector_type': sector_type,
                    'pe_fair_max': sector_config.get('pe_fair_max', 30),
                    'pe_fair_low': sector_config.get('pe_fair_low', 15),
                }
                
                # 缓存结果
                INDUSTRY_CACHE[code] = result
                print(f"  [行业] {code} -> {industry} ({sector_type})", flush=True)  # 添加日志
    except Exception as e:
        print(f"  [行业错误] {code}: {e}", flush=True)  # 添加错误日志
    
    return result


def get_financial_data_fast(code):
    """快速获取财务数据（timeout=3秒，只请求关键字段）

    性能优化版：减少timeout，只请求最关键的字段
    """
    base_url = 'https://datacenter-web.eastmoney.com/api/data/v1/get'
    result = {'roe': 0, 'rev_growth': 0, 'profit_growth': 0, 'gross_margin': 0, 'debt_ratio': 0, 'net_margin': 0}

    # 1. MAINFINADATA: 拿ROE + 毛利率 + 资产负债率（最关键的指标）
    try:
        params = {
            'reportName': 'RPT_F10_FINANCE_MAINFINADATA',
            'columns': 'ROEJQ,XSMLL,ZCFZL,XSJLL',  # ROE, 毛利率, 资产负债率, 净利率
            'filter': '(SECURITY_CODE="' + code + '")',
            'pageNumber': 1, 'pageSize': 1,
            'source': 'WEB', 'client': 'WEB',
        }
        resp = session.get(base_url, params=params, headers=DC_HEADERS, timeout=3)
        d = resp.json()
        if d.get('success') and d.get('result') and d['result'].get('data'):
            item = d['result']['data'][0]
            roe_val = item.get('ROEJQ', 0)
            if roe_val is not None:
                result['roe'] = float(roe_val)
            xsml_val = item.get('XSMLL', 0)
            if xsml_val is not None:
                result['gross_margin'] = float(xsml_val)
            zcfzl_val = item.get('ZCFZL', 0)
            if zcfzl_val is not None:
                result['debt_ratio'] = float(zcfzl_val)
            xsjll_val = item.get('XSJLL', 0)
            if xsjll_val is not None:
                result['net_margin'] = float(xsjll_val)
    except:
        pass

    # 2. CPD: 拿营收同比 + 净利同比（无论ROE如何都请求，亏损股也可能有增长）
    try:
        params = {
            'reportName': 'RPT_LICO_FN_CPD',
            'columns': 'YSTZ,SJLTZ',  # 只请求关键字段
            'filter': '(SECURITY_CODE="' + code + '")',
            'pageNumber': 1, 'pageSize': 1,
            'source': 'WEB', 'client': 'WEB',
        }
        resp = session.get(base_url, params=params, headers=DC_HEADERS, timeout=3)
        d = resp.json()
        if d.get('success') and d.get('result') and d['result'].get('data'):
            item = d['result']['data'][0]
            ystz = item.get('YSTZ', 0)
            if ystz is not None:
                result['rev_growth'] = float(ystz)
            sjltz = item.get('SJLTZ', 0)
            if sjltz is not None:
                result['profit_growth'] = float(sjltz)
    except:
        pass

    # 至少有一个有效数据才返回
    if result['roe'] != 0 or result['gross_margin'] != 0 or result['debt_ratio'] != 0:
        return result
    return None

def get_financial_data(code):
    """从东方财富财务数据中心获取个股关键财务指标（最新报告期）
    
    修复说明(2026-04-01):
    - ROE使用 RPT_F10_FINANCE_MAINFINADATA (ROEJQ字段, 有报告期排序)
    - 营收同比/净利同比使用 RPT_LICO_FN_CPD (YSTZ/SJLTZ字段)
    - 毛利率两个API都有, 优先用MAINFINADATA
    - 两个API都必须请求pageSize=1取最新报告期, 确保数据最新
    """
    base_url = 'https://datacenter-web.eastmoney.com/api/data/v1/get'
    result = {'roe': 0, 'rev_growth': 0, 'profit_growth': 0, 'gross_margin': 0, 'debt_ratio': 0, 'net_margin': 0, 'pb': 0}

    # 1. MAINFINADATA: 拿ROE + 毛利率 + 资产负债率 + 净利率 (有报告期排序, 最新数据)
    try:
        params = {
            'reportName': 'RPT_F10_FINANCE_MAINFINADATA',
            'columns': 'REPORT_DATE_NAME,ROEJQ,XSMLL,ZCFZL,XSJLL',  # ROE, 毛利率, 资产负债率, 净利率
            'filter': '(SECURITY_CODE="' + code + '")',
            'pageNumber': 1, 'pageSize': 1,
            'source': 'WEB', 'client': 'WEB',
        }
        resp = session.get(base_url, params=params, headers=DC_HEADERS, timeout=5)
        d = resp.json()
        if d.get('success') and d.get('result') and d['result'].get('data'):
            item = d['result']['data'][0]
            roe_val = item.get('ROEJQ', 0)
            if roe_val is not None:
                result['roe'] = float(roe_val)
            xsml_val = item.get('XSMLL', 0)
            if xsml_val is not None:
                result['gross_margin'] = float(xsml_val)
            zcfzl_val = item.get('ZCFZL', 0)
            if zcfzl_val is not None:
                result['debt_ratio'] = float(zcfzl_val)
            xsjll_val = item.get('XSJLL', 0)
            if xsjll_val is not None:
                result['net_margin'] = float(xsjll_val)
    except:
        pass

    # 2. CPD: 拿营收同比 + 净利同比 + PB (MAINFINADATA无此字段)
    try:
        params = {
            'reportName': 'RPT_LICO_FN_CPD',
            'columns': 'DATAYEAR,DATEMMDD,WEIGHTAVG_ROE,YSTZ,SJLTZ,XSMLL',
            'filter': '(SECURITY_CODE="' + code + '")',
            'pageNumber': 1, 'pageSize': 1,
            'source': 'WEB', 'client': 'WEB',
        }
        resp = session.get(base_url, params=params, headers=DC_HEADERS, timeout=5)
        d = resp.json()
        if d.get('success') and d.get('result') and d['result'].get('data'):
            item = d['result']['data'][0]
            ystz = item.get('YSTZ', 0)
            if ystz is not None:
                result['rev_growth'] = float(ystz)
            sjltz = item.get('SJLTZ', 0)
            if sjltz is not None:
                result['profit_growth'] = float(sjltz)
            # 如果MAINFINADATA没拿到ROE, 用CPD的作为备选
            if result['roe'] == 0:
                cpd_roe = item.get('WEIGHTAVG_ROE', 0)
                if cpd_roe is not None:
                    result['roe'] = float(cpd_roe)
            # 如果MAINFINADATA没拿到毛利率, 用CPD的作为备选
            if result['gross_margin'] == 0:
                cpd_xsml = item.get('XSMLL', 0)
                if cpd_xsml is not None:
                    result['gross_margin'] = float(cpd_xsml)
    except:
        pass

    # 至少有一个有效数据才返回
    if result['roe'] != 0 or result['rev_growth'] != 0 or result['profit_growth'] != 0 or result['debt_ratio'] != 0:
        return result
    return None

def get_preset_financials():
    """预设高质量中小盘股票财务数据 - 从离线数据库加载"""
    # 尝试从离线数据库加载
    offline_path = os.path.join(_BASE_DIR, 'offline_stocks.json')
    if os.path.exists(offline_path):
        try:
            with open(offline_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                stocks = data.get('stocks', [])
                # 转换为字典格式 {code: {name, roe, ...}}
                result = {}
                for s in stocks:
                    if not s.get('excluded', False):  # 排除白酒和银行
                        code = s['code']
                        result[code] = {
                            'name': s['name'],
                            'price': s.get('price', 0),
                            'roe': s['roe'],
                            'gross_margin': s['gross_margin'],
                            'net_margin': s['net_margin'],
                            'debt_ratio': s.get('debt_ratio', 0),  # 资产负债率
                            'rev_growth': s['rev_growth'],
                            'profit_growth': s['profit_growth'],
                            'pe': s['pe'],
                            'pb': s['pb'],
                            'market_cap': s['market_cap'],
                            'change_pct': s.get('change_pct', 0)
                        }
                print(f"✓ 从离线数据库加载 {len(result)} 只股票")
                return result
        except Exception as e:
            print(f"× 离线数据库加载失败: {e}")
    
    # 备用：硬编码15只核心股票
    return {
        "300015": {"name": "爱尔眼科", "roe": 22.5, "gross_margin": 48.5, "net_margin": 18.2, "rev_growth": 25.5, "profit_growth": 32.5, "pe": 65.2, "pb": 12.5, "market_cap": 2500},
        "300760": {"name": "迈瑞医疗", "roe": 32.5, "gross_margin": 85.5, "net_margin": 35.2, "rev_growth": 22.5, "profit_growth": 28.5, "pe": 45.2, "pb": 15.2, "market_cap": 35000},
        "300122": {"name": "智飞生物", "roe": 35.2, "gross_margin": 45.2, "net_margin": 32.5, "rev_growth": 28.5, "profit_growth": 38.5, "pe": 18.5, "pb": 8.2, "market_cap": 1200},
        "002007": {"name": "华兰生物", "roe": 22.5, "gross_margin": 65.5, "net_margin": 35.2, "rev_growth": 18.5, "profit_growth": 22.5, "pe": 35.2, "pb": 5.8, "market_cap": 450},
        "300059": {"name": "东方财富", "roe": 18.5, "gross_margin": 65.2, "net_margin": 65.2, "rev_growth": 35.2, "profit_growth": 42.5, "pe": 45.8, "pb": 5.2, "market_cap": 2800},
        "002049": {"name": "紫光国微", "roe": 28.5, "gross_margin": 52.5, "net_margin": 35.2, "rev_growth": 35.8, "profit_growth": 42.5, "pe": 55.8, "pb": 12.5, "market_cap": 1200},
        "002236": {"name": "大华股份", "roe": 18.2, "gross_margin": 42.5, "net_margin": 15.8, "rev_growth": 15.2, "profit_growth": 18.5, "pe": 22.5, "pb": 3.2, "market_cap": 550},
        "300274": {"name": "阳光电源", "roe": 22.5, "gross_margin": 28.5, "net_margin": 15.8, "rev_growth": 45.2, "profit_growth": 55.8, "pe": 35.2, "pb": 8.5, "market_cap": 1800},
        "002812": {"name": "恩捷股份", "roe": 22.5, "gross_margin": 45.8, "net_margin": 32.5, "rev_growth": 55.2, "profit_growth": 65.8, "pe": 28.5, "pb": 6.8, "market_cap": 580},
        "300014": {"name": "亿纬锂能", "roe": 20.5, "gross_margin": 22.5, "net_margin": 18.5, "rev_growth": 65.8, "profit_growth": 75.2, "pe": 32.5, "pb": 7.2, "market_cap": 1500},
        "002027": {"name": "分众传媒", "roe": 25.8, "gross_margin": 65.8, "net_margin": 42.5, "rev_growth": 18.5, "profit_growth": 25.2, "pe": 28.5, "pb": 4.8, "market_cap": 1200},
        "002371": {"name": "北方华创", "roe": 25.8, "gross_margin": 35.2, "net_margin": 18.5, "rev_growth": 35.8, "profit_growth": 45.2, "pe": 65.5, "pb": 10.5, "market_cap": 1800},
        "300751": {"name": "迈为股份", "roe": 28.5, "gross_margin": 72.5, "net_margin": 35.8, "rev_growth": 55.2, "profit_growth": 65.8, "pe": 45.2, "pb": 12.5, "market_cap": 950},
        "002352": {"name": "顺丰控股", "roe": 12.5, "gross_margin": 18.5, "net_margin": 5.2, "rev_growth": 28.5, "profit_growth": 35.8, "pe": 35.8, "pb": 3.8, "market_cap": 1850},
        "603288": {"name": "海天味业", "roe": 32.5, "gross_margin": 38.5, "net_margin": 28.5, "rev_growth": 15.2, "profit_growth": 18.5, "pe": 45.8, "pb": 12.5, "market_cap": 2500},
    }

# ========== 技术面筛选模块 ==========

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

# ========== 板块轮动策略 ==========

def get_daily_priority_sectors():
    """获取当日优先板块

    返回: {
        'strategy': '追涨' 或 '反弹',
        'priority_sectors': [(板块名, 涨幅, 加分)],
        'reason': 策略说明
    }
    """
    import random

    try:
        # 获取行业和概念板块行情
        industry_sectors = fetch_sina_sectors('industry')
        concept_sectors = fetch_sina_sectors('class')

        all_sectors = industry_sectors + concept_sectors

        # 按涨幅排序
        sorted_sectors = sorted(all_sectors, key=lambda x: x.get('change_pct', 0), reverse=True)

        # 获取涨幅前5的热门板块
        hot_sectors = [(s['name'], s['change_pct']) for s in sorted_sectors[:5] if s['change_pct'] > 0]

        # 获取跌幅前5的可能反弹板块
        rebound_sectors = [(s['name'], s['change_pct']) for s in sorted_sectors[-5:] if s['change_pct'] < -1]

        # 随机选择策略（增加每日变化）
        strategy_choice = random.choice(['hot', 'rebound', 'mixed'])

        priority_sectors = []
        strategy = ""
        reason = ""

        if strategy_choice == 'hot' and hot_sectors:
            # 追涨策略：优先热门板块
            strategy = "追涨"
            reason = "今日热点板块领涨，跟踪强势板块"
            for name, change in hot_sectors[:3]:
                bonus = 5 if change >= 3 else 4 if change >= 2 else 3
                priority_sectors.append((name, change, bonus))

        elif strategy_choice == 'rebound' and rebound_sectors:
            # 反弹策略：优先跌幅较大板块
            strategy = "反弹"
            reason = "关注超跌板块反弹机会"
            for name, change in rebound_sectors[:3]:
                bonus = 3  # 反弹板块统一加3分
                priority_sectors.append((name, change, bonus))

        else:
            # 混合策略：热门+反弹各取一半
            strategy = "混合"
            reason = "热门板块+潜在反弹板块组合"
            for name, change in hot_sectors[:2]:
                bonus = 4 if change >= 2 else 3
                priority_sectors.append((name, change, bonus))
            for name, change in rebound_sectors[:1]:
                priority_sectors.append((name, change, 2))

        return {
            'strategy': strategy,
            'priority_sectors': priority_sectors,
            'reason': reason,
        }

    except Exception as e:
        print(f"  获取优先板块失败: {e}")
        return {
            'strategy': '无',
            'priority_sectors': [],
            'reason': '',
        }

# ========== 动态热门股票获取 ==========

def get_hot_stocks_from_sectors(top_n=20):
    """从热门板块获取成分股

    筛选条件:
        - 当日涨幅 0-7%（排除涨停）
        - 换手率 > 2%
        - PE > 0 且 < 100

    返回: 热门股票列表 [{code, name, change_pct, turnover_rate, pe, hot_sector}]
    """
    hot_stocks = []

    try:
        # 1. 获取涨幅前3的热门板块
        industry_sectors = fetch_sina_sectors('industry')
        concept_sectors = fetch_sina_sectors('class')

        all_sectors = industry_sectors + concept_sectors
        sorted_sectors = sorted(all_sectors, key=lambda x: x.get('change_pct', 0), reverse=True)

        top_sectors = [s for s in sorted_sectors[:5] if s['change_pct'] > 1]

        if not top_sectors:
            return []

        print(f"  从{len(top_sectors)}个热门板块获取成分股...")

        # 2. 获取每个热门板块的成分股
        for sector in top_sectors[:3]:  # 只取前3个板块
            sector_code = sector.get('code', '')
            sector_name = sector.get('name', '')
            sector_change = sector.get('change_pct', 0)

            # 新浪板块成分股API
            try:
                # 板块成分股URL格式: https://money.finance.sina.com.cn/q/view/newFLJK.php?param=class:gn_hwqc
                # 返回板块内股票列表
                component_url = f"https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeStockCount"

                # 改用东方财富板块成分股API
                # https://push2.eastmoney.com/api/qt/clist/get (可能被封)
                # 使用datacenter-web获取板块成分股

                dc_url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
                dc_params = {
                    'reportName': 'RPT_SECTOR_STOCKLIST',
                    'columns': 'SECURITY_CODE,SECURITY_NAME,NEW_PRICE,CHANGE_RATE,TURNOVER_RATE,PE_TTM',
                    'filter': f'(SECTOR_CODE="{sector_code}")',
                    'pageNumber': 1,
                    'pageSize': 30,
                    'source': 'WEB',
                    'client': 'WEB',
                }

                # 简化处理：从板块领涨股开始，结合STOCK_SECTOR_MAP反向查找
                leader_code = sector.get('leader_code', '')
                leader_name = sector.get('leader_name', '')
                leader_change = sector.get('leader_change', 0)

                if leader_code and leader_change > 0 and leader_change < 8:
                    # 添加领涨股
                    clean_code = leader_code.replace('sh', '').replace('sz', '')
                    hot_stocks.append({
                        'code': clean_code,
                        'name': leader_name,
                        'change_pct': leader_change,
                        'turnover_rate': 0,  # 需后续补充
                        'pe': 0,
                        'hot_sector': sector_name,
                        'sector_change': sector_change,
                    })

            except Exception as e:
                continue

        # 3. 从STOCK_SECTOR_MAP中查找热门板块相关股票
        for sector_info in top_sectors[:3]:
            sector_name = sector_info.get('name', '')
            sector_change = sector_info.get('change_pct', 0)

            # 遍历STOCK_SECTOR_MAP找板块成分股
            for code, sectors in STOCK_SECTOR_MAP.items():
                # 检查股票是否属于该板块
                if any(s in sector_name or sector_name in s for s in sectors):
                    # 需要获取实时行情验证筛选条件
                    # 这里先添加，后续在run_picker中统一获取行情
                    if code not in [s['code'] for s in hot_stocks]:
                        hot_stocks.append({
                            'code': code,
                            'name': '',  # 后续填充
                            'change_pct': 0,
                            'turnover_rate': 0,
                            'pe': 0,
                            'hot_sector': sector_name,
                            'sector_change': sector_change,
                        })

        # 去重并限制数量
        unique_stocks = []
        seen_codes = set()
        for s in hot_stocks:
            if s['code'] not in seen_codes and len(unique_stocks) < top_n:
                seen_codes.add(s['code'])
                unique_stocks.append(s)

        print(f"    获取 {len(unique_stocks)} 只热门候选股")
        return unique_stocks

    except Exception as e:
        print(f"  获取热门股票失败: {e}")
        return []

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
    liquor_names = ["贵州茅台", "五粮液", "洋河股份", "泸州老窖", "山西汾酒", "酒鬼酒", "水井坊", "古井贡酒", "古井贡酒", "迎驾贡酒", "今世缘", "舍得酒业", "老白干酒", "伊力特", "口子窖", "金徽酒", "皇台酒业", "岩石股份", "顺鑫农业"]
    bank_codes = ["601398", "601288", "600000", "600036", "601166", "600015", "600016", "601328", "600919", "600028", "601939", "601988", "601318", "600030"]
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
    if turnover_rate < 0.5 and turnover_rate > 0:
        # 换手率低于0.5%的死股，直接排除或大幅减分
        # 注意：turnover_rate=0可能是数据缺失，不排除
        return None  # 直接过滤不活跃股票

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

    # ===== 技术面评分（新增）=====
    if tech_data:
        tech_score, tech_reasons = evaluate_technical_score(code, tech_data)
        if tech_score > 0:
            score += tech_score  # 技术面评分直接加入总分，不作为维度
            reasons.extend(tech_reasons)

    # ===== 板块轮动加分（新增）=====
    if priority_sectors:
        # 获取股票所属板块
        stock_sectors = STOCK_SECTOR_MAP.get(code, [])
        # 根据股票名称推断板块
        name_hints_sector = {
            "半导体": ["半导体", "芯片", "微", "华创"],
            "新能源": ["新能", "光伏", "锂电", "储能", "电源"],
            "医药": ["医", "药", "生物", "康"],
            "科技": ["科技", "电子", "信息", "软", "通"],
        }
        for hint, keywords in name_hints_sector.items():
            if any(h in name for h in keywords):
                stock_sectors.append(hint)

        sector_bonus = 0
        for sector_name, sector_change, bonus in priority_sectors:
            for ss in stock_sectors:
                if ss in sector_name or sector_name in ss:
                    sector_bonus = max(sector_bonus, bonus)
                    reasons.append(f"【{sector_name}】板块加分")
                    break

        score += sector_bonus

    # 加入行情加分（上限提高到5分，包含换手率）
    market_bonus = max(0, min(market_bonus, 5))
    score += market_bonus

    # 市值信息（不参与评分，仅展示）
    if market_cap_yi > 0:
        reasons.append(f"市值 {market_cap_yi:.0f}亿")

    # ===== 多因子v5评分（先计算v5，用于买卖点计算）===== # 2026-04-13修复：v5评分必须在买卖点之前计算
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

    # 买卖点（2026-04-13修复：使用v5_score替代旧score）
    buy_sell = calculate_buy_sell(stock, v5_total)

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
        "turnover_rate": turnover_rate,  # 新增换手率字段
        "pe": pe,
        "pb": pb,
        "roe": roe,
        "gross_margin": gross_margin,
        "net_margin": net_margin,
        "debt_ratio": debt_ratio,
        "rev_growth": rev_growth,
        "profit_growth": profit_growth,
        "market_cap": market_cap_yi,
        "industry": stock.get("industry", "未知"),  # 新增行业字段
        "sector_type": stock.get("sector_type", "default"),  # 新增行业类型字段
        "score": round(score, 1),  # 综合评分保留1位小数
        "dimensions": rounded_dimensions,  # 维度分数四舍五入为整数
        "reasons": reasons,
        "buy_sell": buy_sell,
        "tech_info": tech_info,  # 新增技术指标信息
        # v5多因子评分（新增）
        "v5_score": v5_result['v5_total'],
        "v5_factors": v5_result['v5_factors'],
        "v5_reasons": v5_result['v5_reasons'],
        "v5_recommendation": v5_result['v5_recommendation'],
    }

def calculate_buy_sell(stock, v5_score):
    """计算买卖点 + 五星评级
    
    2026-04-13修复：统一使用v5_score作为评分标准，消除首页/详情页星级不一致
    2026-04-08修复：
    - 调整fair_pe公式，考虑成长性溢价（高成长股PE理应更高）
    - 取消硬性None返回，score>=50的股票全部给出建议
    - 放宽门槛，让更多优质股能展示出来
    """
    price = stock.get("price", 0)
    pe = stock.get("pe", 0)
    roe = stock.get("roe", 0)
    gross_margin = stock.get("gross_margin", 0)
    rev_growth = stock.get("rev_growth", 0)
    profit_growth = stock.get("profit_growth", 0)
    if price <= 0 or pe <= 0:
        return None

    # === 动态计算合理PE（考虑成长性溢价）===
    # 基础：fair_pe = ROE * 1.5（比之前的1.2更宽松）
    # 成长性溢价：营收/净利增速越高，合理PE越高
    avg_growth = (rev_growth + profit_growth) / 2
    growth_premium = min(avg_growth * 0.3, 15)  # 成长溢价最多+15倍
    
    fair_pe = roe * 1.5 + growth_premium
    # 设置合理范围：最低 12 倍，最高 60 倍（成长股可以给更高估值）
    fair_pe = max(12, min(60, fair_pe))

    # 五星评级逻辑（统一使用v5_score）
    star_rating = 1  # 默认至少1星（有评分就有星级）

    if pe < fair_pe:
        # 当前低于合理估值：推荐买入区间
        if v5_score >= 82:
            buy_point = round(price * 0.95, 2)  # 5%折扣
            upside = min(max((fair_pe - pe) / pe, 0.25), 0.8)
            sell_point = round(price * (1 + upside), 2)
            rec = "强烈推荐"
            star_rating = 5 if v5_score >= 86 and roe >= 18 and gross_margin >= 28 else 4
            if star_rating == 4 and price - buy_point <= price * 0.05:
                star_rating = 5
        elif v5_score >= 68:
            buy_point = round(price * 0.92, 2)
            upside = min(max((fair_pe - pe) / pe, 0.25), 0.7)
            sell_point = round(price * (1 + upside), 2)
            rec = "推荐买入"
            star_rating = 4 if v5_score >= 75 else 3
        elif v5_score >= 55:
            buy_point = round(price * 0.88, 2)
            upside = min(max((fair_pe - pe) / pe, 0.2), 0.5)
            sell_point = round(price * (1 + upside), 2)
            rec = "可逢低关注"
            star_rating = 3 if v5_score >= 62 else 2
        else:
            # v5_score < 55 但仍进入评估的，给基本建议
            buy_point = round(price * 0.85, 2)
            upside = 0.3
            sell_point = round(price * 1.3, 2)
            rec = "轻度关注"
            star_rating = 1
    else:
        # 当前高于合理估值：等待回调或谨慎持有
        if v5_score >= 75 and pe < fair_pe * 1.3:
            # 估值偏高但基本面优秀
            buy_point = round(price * 0.85, 2)
            upside = min(max((fair_pe - pe) / pe, 0.15), 0.5)
            sell_point = round(price * (1 + max(upside, 0.2)), 2)
            rec = "等待更好买点"
            star_rating = 3
        elif v5_score >= 58:
            buy_point = round(price * 0.82, 2)
            upside = 0.25
            sell_point = round(price * 1.25, 2)
            rec = "高估观望"
            star_rating = 2
        else:
            buy_point = round(price * 0.80, 2)
            sell_point = round(price * 1.18, 2)
            rec = "暂不推荐"
            star_rating = 1

    return {
        "current": price,
        "buy": buy_point,
        "sell": sell_point,
        "upside": round((sell_point - price) / price * 100, 1),
        "downside": round((price - buy_point) / price * 100, 1),
        "recommendation": rec,
        "star_rating": star_rating,
    }

# 股票所属板块映射（关键股票）
STOCK_SECTOR_MAP = {
    # 半导体/芯片
    "002371": ["半导体", "芯片", "人工智能"],
    "300661": ["半导体", "芯片"],
    "688981": ["半导体", "芯片"],
    "603501": ["半导体", "芯片"],
    "002049": ["半导体", "芯片"],
    "688332": ["半导体", "芯片"],
    "603929": ["半导体", "芯片"],
    "300308": ["光通信", "人工智能", "通信"],
    "300394": ["光通信", "人工智能", "通信"],
    # 新能源/光伏/储能/固态电池
    "300274": ["光伏", "储能", "新能源", "固态电池"],
    "601012": ["光伏", "新能源"],
    "002459": ["光伏", "储能"],
    "300014": ["锂电", "新能源", "固态电池"],
    "002594": ["新能源汽车", "新能源", "汽车"],
    "300750": ["锂电", "新能源", "储能", "固态电池"],
    # 医药
    "300015": ["医药", "医疗服务"],
    "300760": ["医疗器械", "医药"],
    "300122": ["医药", "生物制品"],
    "002007": ["医疗器械", "医药"],
    "603259": ["医药", "CXO"],
    "600211": ["医药", "中药"],
    "600329": ["医药", "中药"],
    "688336": ["医药", "生物制品"],
    # 消费电子
    "002475": ["消费电子", "苹果", "汽车"],
    "002241": ["消费电子", "苹果"],
    "600588": ["人工智能", "数字经济"],
    # 科技/AI
    "300059": ["人工智能", "数字经济"],
    "002230": ["人工智能", "数字经济"],
    "002405": ["人工智能", "数字经济"],
    "300033": ["数字经济", "证券"],
    # 新能源汽车/汽车零部件
    "002812": ["新能源汽车", "锂电", "钠电池"],
    "600841": ["汽车零部件", "汽车", "新能源"],
    # 锂矿/锂电
    "000792": ["锂矿", "锂电", "新能源"],
    "002466": ["锂矿", "锂电"],
    "002460": ["锂电", "新能源"],
    # 其他
    "002352": ["物流"],
    "603288": ["食品饮料", "消费"],
    "002039": ["电力", "新能源"],
    "600415": ["商贸", "互联金融"],
    "600660": ["汽车零部件", "汽车"],
    "002546": ["电力设备", "新能源"],
    "002895": ["化工", "磷化工"],
    "000612": ["有色金属", "铝"],
}

# 热门关键词到板块的映射（用于从新闻中识别热点）
HOT_KEYWORD_TO_SECTOR = {
    # 科技
    "AI": ["人工智能", "数字经济"],
    "ChatGPT": ["人工智能"],
    "大模型": ["人工智能"],
    "芯片": ["半导体", "芯片"],
    "GPU": ["半导体"],
    "算力": ["人工智能", "数字经济"],
    "光模块": ["光通信", "人工智能"],
    "半导体": ["半导体", "芯片"],
    # 新能源
    "光伏": ["光伏", "储能"],
    "储能": ["储能", "新能源"],
    "锂电池": ["锂电", "新能源"],
    "锂电": ["锂电", "新能源"],
    "固态电池": ["固态电池", "锂电"],
    "钠电池": ["钠电池", "锂电"],
    "新能源": ["新能源", "光伏"],
    "电动车": ["新能源汽车", "汽车"],
    "充电桩": ["新能源汽车"],
    # 医药
    "医药": ["医药", "医疗器械"],
    "创新药": ["医药", "生物制品"],
    "疫苗": ["生物制品", "医药"],
    # 消费
    "消费": ["消费", "食品饮料"],
    "白酒": ["白酒", "消费"],
    # 周期
    "锂矿": ["锂矿", "锂电"],
    "铝": ["有色金属"],
    "铜": ["有色金属"],
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
    """计算股票的热点因子

    返回: (热点加分, 热点原因列表)
    """
    bonus = 0
    reasons = []

    # 1. 板块热度加分
    stock_sectors = STOCK_SECTOR_MAP.get(stock_code, [])

    # 根据股票名称推断板块
    name_hints = {
        "半导体": ["半导体", "芯片", "微", "创", "华创"],
        "新能源": ["新能", "光伏", "锂电", "储能", "电源", "宁德", "比亚迪"],
        "医药": ["医", "药", "生物", "康", "健"],
        "科技": ["科技", "电子", "信息", "软", "通"],
    }
    for hint, keywords in name_hints.items():
        if any(h in stock_name for h in keywords):
            stock_sectors.append(hint)

    # 检查股票所属板块是否在热门板块中（模糊匹配）
    for stock_sector in stock_sectors:
        for hot_sector, change in hot_sectors.items():
            # 模糊匹配：板块名称包含关系
            if stock_sector in hot_sector or hot_sector in stock_sector:
                # 板块涨幅越大，加分越多
                if change >= 3:
                    bonus += 10
                    reasons.append(f"🔥【{hot_sector}】+{change:.1f}%")
                elif change >= 2:
                    bonus += 6
                    reasons.append(f"热门【{hot_sector}】+{change:.1f}%")
                elif change >= 1:
                    bonus += 3
                else:
                    bonus += 1
                break  # 避免重复加分

    # 2. 根据热门板块关键词匹配股票名称
    # 关键词：固态电池、钠电池、AI、光伏等
    sector_keywords = ["固态电池", "钠电池", "半导体", "芯片", "光伏", "储能", "锂电", "新能源",
                       "人工智能", "AI", "数字经济", "机器人", "医药", "医疗", "创新药",
                       "消费电子", "汽车", "特斯拉", "华为", "苹果"]

    for keyword in sector_keywords:
        if keyword in stock_name:
            # 检查该关键词对应板块是否热门
            for hot_sector, change in hot_sectors.items():
                if keyword in hot_sector and change > 0:
                    bonus += 5
                    reasons.append(f"🔥{keyword}")
                    break

    # 3. 新闻热点关键词匹配
    for keyword in hot_keywords:
        if keyword in stock_name:
            bonus += 3
            reasons.append(f"热点【{keyword}】")

    # 上限20分
    bonus = min(bonus, 20)

    return bonus, reasons[:3]  # 最多返回3个原因

def run_picker():
    """执行选股主流程 - 全市场扫描 + 热点因子 + 技术面 + 板块轮动"""
    import traceback
    print("\n" + "="*50, flush=True)
    print("📊 开始执行智能选股...", flush=True)
    print("="*50, flush=True)

    # === 1. 获取板块轮动策略（新增）===
    priority_sectors = []
    try:
        print("\n🔄 获取板块轮动策略...", flush=True)
        priority_info = get_daily_priority_sectors()
        priority_sectors = priority_info.get('priority_sectors', [])
        if priority_sectors:
            print(f"  策略: {priority_info.get('strategy', '')} - {priority_info.get('reason', '')}", flush=True)
            print(f"  优先板块: {[s[0] for s in priority_sectors[:3]]}", flush=True)
    except Exception as e:
        print(f"  ⚠️ 获取板块轮动策略失败: {e}", flush=True)

    # === 2. 获取动态热门股票（新增）===
    hot_stocks = []
    hot_stock_codes = set()
    try:
        print("\n🔥 获取热门板块成分股...", flush=True)
        hot_stocks = get_hot_stocks_from_sectors(top_n=30)
        hot_stock_codes = set(s['code'] for s in hot_stocks)
        print(f"  获取到 {len(hot_stocks)} 只热门股票", flush=True)
    except Exception as e:
        print(f"  ⚠️ 获取热门股票失败: {e}", flush=True)

    # === 3. 获取全市场实时行情 ===
    print("\n📡 获取全市场实时行情...", flush=True)
    all_stocks = get_realtime_quotes()

    # 获取热点板块和新闻
    hot_sectors, hot_keywords = {}, set()
    try:
        hot_sectors, hot_keywords = get_hot_sectors_and_news()
    except Exception as e:
        print(f"  ⚠️ 获取热点板块失败: {e}", flush=True)

    if not all_stocks or len(all_stocks) < 50:
        print("⚠️ 实时行情获取失败，启用离线模式", flush=True)
        print("📦 从离线数据库加载股票...", flush=True)
        # fallback: 使用预设库
        preset_data = get_preset_financials()
        stock_pool = []
        for code, fin in preset_data.items():
            entry = {
                "code": code, "name": fin["name"],
                "price": fin.get("price", 0),
                "roe": fin["roe"], "gross_margin": fin["gross_margin"],
                "net_margin": fin["net_margin"], "debt_ratio": fin.get("debt_ratio", 0),
                "rev_growth": fin["rev_growth"],
                "profit_growth": fin["profit_growth"], "pe": fin["pe"],
                "pb": fin["pb"], "market_cap": fin["market_cap"],
                "change_pct": fin.get("change_pct", 0),
                "turnover_rate": fin.get("turnover_rate", 0),
            }
            stock_pool.append(entry)
        print(f"✓ 离线模式：加载 {len(stock_pool)} 只股票", flush=True)
    else:
        print(f"✓ 获取 {len(all_stocks)} 只股票行情", flush=True)
        # 不再用离线库填充财务数据（离线库数据是假的/过时的）
        # 统一用东方财富实时数据
        stock_pool = []
        for stock in all_stocks:
            stock_pool.append(stock)

        # === 4. 合并热门股票到候选池（新增）===
        for hot_stock in hot_stocks:
            code = hot_stock['code']
            # 检查是否已在股票池中
            existing = next((s for s in stock_pool if s['code'] == code), None)
            if existing:
                # 标记为热门股票
                existing['_is_hot'] = True
                existing['_hot_sector'] = hot_stock.get('hot_sector', '')
            else:
                # 添加到股票池
                stock_pool.append({
                    'code': code,
                    'name': hot_stock.get('name', ''),
                    'change_pct': hot_stock.get('change_pct', 0),
                    'turnover_rate': hot_stock.get('turnover_rate', 0),
                    'pe': hot_stock.get('pe', 0),
                    '_is_hot': True,
                    '_hot_sector': hot_stock.get('hot_sector', ''),
                })

        # === 5. 基础过滤（新增换手率过滤）===
        print(f"\n🔍 基础过滤（换手率>0.5%，非ST，非新股）...", flush=True)
        filtered_pool = []
        for stock in stock_pool:
            turnover = stock.get('turnover_rate', 0)
            name = stock.get('name', '')
            # 换手率>0.5% 或 数据缺失(turnover=0)，排除ST、退市股、新股
            if (turnover >= 0.5 or turnover == 0):
                if 'ST' not in name and '退' not in name and not name.startswith('N') and '*' not in name:
                    filtered_pool.append(stock)

        print(f"  过滤后剩余: {len(filtered_pool)} 只", flush=True)
        stock_pool = filtered_pool

        # 对所有股票用东方财富实时财务数据校准（不再只校准"高潜力"）
        # 行情API的ROE/毛利率经常缺失或过期，必须用年报数据覆盖
        print(f"\n📊 并发校准全部 {len(stock_pool)} 只股票的财务数据...", flush=True)

        # 定义单只股票校准函数（优化timeout）
        # 注意：东方财富实时数据始终优先，不管离线库有没有值
        def calibrate_single_stock(stock):
            fin_data = get_financial_data_fast(stock["code"])
            if fin_data:
                # 东方财富数据始终优先（不管离线库有没有值）
                if fin_data.get("roe", 0) != 0:
                    stock["roe"] = fin_data["roe"]
                if fin_data.get("gross_margin", 0) != 0:
                    stock["gross_margin"] = fin_data["gross_margin"]
                if fin_data.get("rev_growth", 0) != 0:
                    stock["rev_growth"] = fin_data["rev_growth"]
                if fin_data.get("profit_growth", 0) != 0:
                    stock["profit_growth"] = fin_data["profit_growth"]
                if fin_data.get("debt_ratio", 0) != 0:
                    stock["debt_ratio"] = fin_data["debt_ratio"]
                if fin_data.get("net_margin", 0) != 0:
                    stock["net_margin"] = fin_data["net_margin"]
            return stock["code"]

        # 并发执行（30个线程，timeout=3秒）
        start_time = time.time()
        with ThreadPoolExecutor(max_workers=30) as executor:
            futures = [executor.submit(calibrate_single_stock, s) for s in stock_pool]
            completed = 0
            for future in as_completed(futures):
                completed += 1
                if completed % 50 == 0:
                    print(f"    已校准 {completed}/{len(stock_pool)}...", flush=True)

        elapsed = time.time() - start_time
        print(f"  ✓ 全部校准完成 {len(stock_pool)} 只，耗时 {elapsed:.1f}s", flush=True)

    # === 6. 计算技术指标（对所有进入评估的股票）===
    print("\n📈 计算技术指标...", flush=True)
    tech_cache = {}  # 缓存技术指标结果

    # 所有股票都需要技术指标（不再只算30只）
    tech_candidates = stock_pool[:]

    if tech_candidates:
        print(f"  计算 {len(tech_candidates)} 只股票的技术指标...", flush=True)

        def calc_tech_for_stock(stock):
            code = stock['code']
            tech = calculate_technical_indicators(code, days=30)
            return (code, tech)

        start_time = time.time()
        with ThreadPoolExecutor(max_workers=15) as executor:
            futures = [executor.submit(calc_tech_for_stock, s) for s in tech_candidates]
            completed = 0
            for future in as_completed(futures):
                try:
                    code, tech = future.result(timeout=20)
                    if tech:
                        tech_cache[code] = tech
                    completed += 1
                    if completed % 100 == 0:
                        print(f"    已计算 {completed}/{len(tech_candidates)}...", flush=True)
                except Exception as e:
                    pass

        elapsed = time.time() - start_time
        print(f"  ✓ 技术指标计算完成，缓存 {len(tech_cache)} 只，耗时 {elapsed:.1f}s", flush=True)

    # === 7. 对所有股票进行五维评估 ===
    print("\n🎯 开始五维评估...", flush=True)
    results = []
    hot_stock_results = []  # 热门股票单独统计
    total_scanned = len(stock_pool)

    for stock in stock_pool:
        code = stock.get("code", "")
        tech_data = tech_cache.get(code)  # 从缓存获取技术指标

        r = evaluate_stock(stock, tech_data=tech_data, priority_sectors=priority_sectors)
        if r and r["score"] >= 50:
            # 计算热点因子
            hot_bonus, hot_reasons = calculate_hot_factor(
                stock.get("code", ""),
                stock.get("name", ""),
                hot_sectors,
                hot_keywords
            )
            if hot_bonus > 0:
                r["hot_bonus"] = hot_bonus
                r["hot_reasons"] = hot_reasons
                r["score"] += hot_bonus

            # 标记热门股票
            if stock.get('_is_hot'):
                r['is_hot'] = True
                r['hot_sector'] = stock.get('_hot_sector', '')
                hot_stock_results.append(r)

            results.append(r)

    # === 8. 动态排序（支持v5评分排序）===
    for r in results:
        change = r.get("change_pct", 0)
        # 涨跌幅调整：涨幅适度加分，跌幅减分
        if change > 0:
            change_adj = min(change * 0.5, 3)  # 涨幅最多加3分
        else:
            change_adj = max(change * 0.3, -2)  # 跌幅最多减2分
        r["_final_score"] = r["score"] + change_adj
        # v5最终评分（不含涨跌幅调整，保持纯净）
        r["_v5_final"] = r.get("v5_score", 0)

    # 按v5多因子评分排序（Round 4最优方案）
    results.sort(key=lambda x: x.get("v5_score", 0), reverse=True)

    # === 9. 结果处理 ===
    # 限制热门股票占比不超过30%
    max_hot_count = min(int(len(results) * 0.3), len(hot_stock_results))

    # 清理内部字段 + 分数四舍五入保留1位小数
    for r in results:
        r.pop("_final_score", None)
        # 评分保留1位小数
        r["score"] = round(r["score"], 1)
        # 确保debt_ratio存在
        if "debt_ratio" not in r:
            r["debt_ratio"] = 0
        # 买卖点价格也保留2位小数
        if r.get("buy_sell"):
            r["buy_sell"]["buy"] = round(r["buy_sell"]["buy"], 2)
            r["buy_sell"]["sell"] = round(r["buy_sell"]["sell"], 2)
            r["buy_sell"]["current"] = round(r["buy_sell"]["current"], 2)

    # Round 4 最优方案: 按v5多因子评分排序
    results.sort(key=lambda x: x.get("v5_score", 0), reverse=True)

    # 精选Top 5作为核心推荐，同时保留完整列表供用户查看
    final_results = results[:50]  # 返回Top 50完整列表
    top5 = results[:5]  # 精选5只（推荐持仓）

    # 在第一个结果中附带精选Top 5信息
    if final_results:
        final_results[0]['_total_scanned'] = total_scanned
        # 只存代码和分数，避免循环引用
        final_results[0]['_top5_codes'] = [r['code'] for r in top5]
        final_results[0]['_top5_scores'] = [round(r.get('v5_score', 0), 2) for r in top5]

    print(f"\n{'='*50}", flush=True)
    print(f"✅ 选股完成！", flush=True)
    print(f"  扫描股票: {total_scanned} 只", flush=True)
    print(f"  符合条件: {len(results)} 只", flush=True)
    print(f"  返回结果: {len(final_results)} 只", flush=True)
    print(f"  热门股票: {len(hot_stock_results)} 只", flush=True)
    print(f"{'='*50}\n", flush=True)

    return final_results

# ========== 新闻热点模块 ==========

import re

# 板块关键词映射（全局复用）
SECTOR_KEYWORDS = {
    "半导体": ["芯片", "半导体", "集成电路", "AI芯片", "GPU", "CPU", "存储芯片", "封装", "光刻"],
    "人工智能": ["人工智能", "AI", "大模型", "ChatGPT", "生成式AI", "机器学习", "深度学习", "自动驾驶", "Sora"],
    "新能源汽车": ["新能源车", "电动车", "电动汽车", "混动", "充电桩", "电池", "锂电", "固态电池", "比亚迪", "特斯拉", "宁德时代"],
    "光伏": ["光伏", "太阳能", "硅片", "组件", "逆变器", "HJT", "TOPCon"],
    "医药生物": ["医药", "生物", "创新药", "疫苗", "CXO", "医疗器械", "中药", "仿制药", "PD-1", "医保"],
    "消费电子": ["消费电子", "手机", "华为", "苹果", "MR", "VR", "AR", "折叠屏", "智能穿戴"],
    "房地产": ["房地产", "楼市", "房价", "房企", "拿地", "保交楼", "城中村", "地产"],
    "银行": ["银行", "信贷", "贷款", "降准", "降息", "LPR", "利率", "央行"],
    "军工": ["军工", "国防", "航天", "航空", "导弹", "军备", "战斗机", "航母"],
    "白酒": ["白酒", "茅台", "五粮液", "酒"],
    "证券": ["证券", "券商", "资本市场", "IPO", "注册制", "北交所", "牛市", "熊市"],
    "数字经济": ["数字经济", "数据要素", "云计算", "大数据", "数据中心", "算力"],
    "机器人": ["机器人", "人形机器人", "工业机器人", "减速器", "伺服电机"],
    "游戏传媒": ["游戏", "传媒", "影视", "短剧", "直播", "网游"],
    "有色金属": ["有色", "黄金", "铜", "铝", "锂", "稀土", "钴", "镍"],
    "养殖": ["养殖", "猪", "鸡", "饲料", "农业"],
    "电力": ["电力", "电网", "储能", "特高压", "风电", "核电", "火电"],
    "化工": ["化工", "新材料", "塑料", "化纤"],
}

def fetch_sina_sectors(category):
    """从新浪财经获取板块实时行情数据
    
    category: 'class' (概念板块, ~175个) 或 'industry' (行业板块, ~84个)
    数据源: https://money.finance.sina.com.cn/q/view/newFLJK.php?param={category}
    
    返回字段: code, name, stock_count, avg_pe, change_pct, turnover,
              volume, amount, leader_code, leader_name, leader_price, leader_change
    """
    url = f'https://money.finance.sina.com.cn/q/view/newFLJK.php?param={category}'
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    
    try:
        r = session.get(url, headers=HEADERS, timeout=15)
        r.encoding = 'gb2312'
        text = r.text.strip()
        
        start = text.find('{')
        end = text.rfind('}')
        if start < 0 or end < 0:
            return []
        
        data = json.loads(text[start:end+1])
        sectors = []
        
        for key, val in data.items():
            parts = val.split(',')
            if len(parts) < 13:
                continue
            try:
                sectors.append({
                    "code": parts[0],                    # gn_hwqc / hangye_ZA01
                    "name": parts[1],                    # 板块名称
                    "stock_count": int(parts[2]),        # 成分股数量
                    "avg_pe": float(parts[3]),           # 平均PE
                    "change_pct": float(parts[4]),       # 涨跌幅(%)
                    "turnover": float(parts[5]),         # 换手率(%)
                    "volume": int(parts[6]),             # 成交量(手)
                    "amount": int(parts[7]),             # 成交额(元)
                    "leader_code": parts[8],             # 领涨股代码 如 sh603158
                    "leader_name": parts[12],            # 领涨股名称
                    "leader_price": float(parts[10]),    # 领涨股现价
                    "leader_change": float(parts[9]),    # 领涨股涨幅(%)
                })
            except (ValueError, IndexError):
                continue
        
        return sectors
    except Exception as e:
        print(f"  fetch_sina_sectors({category}) 失败: {e}")
        return []


def get_sector_news():
    """抓取财经新闻 + 板块实时行情 + 关联分析
    
    2026-04-02 v8: push2/push2his 全被封。
    数据源方案:
    - 行业板块(84个): 新浪 newFLJK.php?param=industry
    - 概念板块(175个): 新浪 newFLJK.php?param=class  
    - 新闻: 新浪 feed.mix.sina.com.cn
    - 不再依赖 sector_codes.json 和 push2 API
    
    2026-04-03 v9: 离线模式支持
    """
    news_list = []

    # ---- 1. 新浪API获取行业板块实时行情 ----
    print("  获取行业板块行情(新浪)...")
    sector_data = fetch_sina_sectors('industry')
    print(f"  行业板块: {len(sector_data)} 个")

    # ---- 2. 新浪API获取概念板块实时行情 ----
    print("  获取概念板块行情(新浪)...")
    concept_data = fetch_sina_sectors('class')
    print(f"  概念板块: {len(concept_data)} 个")

    # ---- 3. 离线模式检测 ----
    if len(sector_data) == 0 and len(concept_data) == 0:
        print("⚠️ 板块数据获取失败，启用离线模式")
        # 返回模拟板块数据
        sector_data = [
            {"name": "半导体", "change_pct": 2.85, "avg_pe": 65.2, "stock_count": 85, "leader_name": "北方华创", "leader_change": 5.25, "code": "hangye_bandaoti"},
            {"name": "医疗器械", "change_pct": 1.95, "avg_pe": 45.2, "stock_count": 120, "leader_name": "迈瑞医疗", "leader_change": 1.85, "code": "hangye_yiliaoqixie"},
            {"name": "锂电池", "change_pct": 3.25, "avg_pe": 35.2, "stock_count": 95, "leader_name": "宁德时代", "leader_change": 2.65, "code": "hangye_lidianchi"},
            {"name": "光伏设备", "change_pct": 2.45, "avg_pe": 22.5, "stock_count": 65, "leader_name": "阳光电源", "leader_change": 4.25, "code": "hangye_guangfushebei"},
            {"name": "生物制品", "change_pct": -1.25, "avg_pe": 28.5, "stock_count": 80, "leader_name": "智飞生物", "leader_change": -1.25, "code": "hangye_shengwuzhipin"},
            {"name": "软件开发", "change_pct": 1.85, "avg_pe": 85.2, "stock_count": 150, "leader_name": "科大讯飞", "leader_change": 2.85, "code": "hangye_ruanjiankaifa"},
            {"name": "消费电子", "change_pct": -0.85, "avg_pe": 28.5, "stock_count": 110, "leader_name": "立讯精密", "leader_change": 1.85, "code": "hangye_xiaofeidianzi"},
            {"name": "化学制药", "change_pct": 0.65, "avg_pe": 32.5, "stock_count": 95, "leader_name": "药明康德", "leader_change": 2.15, "code": "hangye_huaxuezhiyao"},
        ]
        concept_data = [
            {"name": "创新药", "change_pct": 1.85, "avg_pe": 38.5, "stock_count": 140, "leader_name": "智飞生物", "leader_change": -1.25, "code": "gn_cxy"},
            {"name": "新能源汽车", "change_pct": 3.15, "avg_pe": 42.5, "stock_count": 180, "leader_name": "比亚迪", "leader_change": 4.15, "code": "gn_xinnengyuanqiche"},
            {"name": "人工智能", "change_pct": 2.65, "avg_pe": 125.2, "stock_count": 120, "leader_name": "科大讯飞", "leader_change": 2.85, "code": "gn_rengongzhineng"},
            {"name": "芯片国产化", "change_pct": 3.85, "avg_pe": 85.2, "stock_count": 95, "leader_name": "北方华创", "leader_change": 5.25, "code": "gn_xinpianbaotichan"},
            {"name": "储能", "change_pct": 2.95, "avg_pe": 32.5, "stock_count": 75, "leader_name": "阳光电源", "leader_change": 4.25, "code": "gn_chuneng"},
            {"name": "工业4.0", "change_pct": 1.55, "avg_pe": 35.2, "stock_count": 130, "leader_name": "汇川技术", "leader_change": 2.45, "code": "gn_gongye40"},
            {"name": "数字经济", "change_pct": 2.25, "avg_pe": 55.2, "stock_count": 145, "leader_name": "东方财富", "leader_change": 3.25, "code": "gn_shuzijinji"},
            {"name": "碳中和", "change_pct": 2.15, "avg_pe": 28.5, "stock_count": 165, "leader_name": "隆基绿能", "leader_change": 2.45, "code": "gn_tanzhonghe"},
        ]
        print(f"✓ 离线模式：加载 {len(sector_data)} 个行业板块 + {len(concept_data)} 个概念板块")

    # ---- 4. 从新浪财经获取新闻 ----
    try:
        r = session.get("https://feed.mix.sina.com.cn/api/roll/get",
                         params={"pageid": "153", "lid": "2509", "k": "", "r": "0.5", "page": 1},
                         headers=HEADERS, timeout=10)
        d = r.json()
        if d.get('result') and d['result'].get('data'):
            for item in d['result']['data'][:40]:
                news_list.append({
                    "title": item.get('title', ''),
                    "time": item.get('ctime', ''),
                    "source": item.get('media_name', ''),
                    "summary": item.get('intro', '') or item.get('summary', ''),
                })
    except Exception as e:
        print(f"  获取新闻失败: {e}")

    if not news_list:
        print("⚠️ 新闻获取失败，启用离线模式")
        # 模拟财经新闻数据
        news_list = [
            {"title": "半导体板块集体走强，北方华创领涨", "url": "#", "time": "10:25", "summary": "受国产替代加速推动，半导体板块今日表现强势，北方华创涨停，中微公司涨超8%。"},
            {"title": "新能源汽车销量创新高，比亚迪单月突破30万辆", "url": "#", "time": "11:15", "summary": "比亚迪发布最新销售数据，单月销量突破30万辆，继续领跑新能源汽车市场。"},
            {"title": "光伏行业景气度持续提升，龙头企业订单饱满", "url": "#", "time": "13:30", "summary": "阳光电源、隆基绿能等光伏龙头获大额订单，行业景气度持续向好。"},
            {"title": "AI应用加速落地，科大讯飞股价创年内新高", "url": "#", "time": "14:20", "summary": "科大讯飞发布AI大模型应用成果，股价创年内新高，人工智能板块整体走强。"},
            {"title": "医疗器械国产化进程加快，迈瑞医疗获批量采购订单", "url": "#", "time": "15:05", "summary": "迈瑞医疗获多省份医疗设备集中采购订单，国产医疗器械替代进程加速。"},
            {"title": "锂电池产业链整合加速，宁德时代布局上游资源", "url": "#", "time": "15:45", "summary": "宁德时代宣布投资锂矿资源，完善产业链布局，锂电池板块整体受益。"},
            {"title": "消费电子回暖信号显现，立讯精密获苹果新订单", "url": "#", "time": "16:10", "summary": "立讯精密获得苹果新订单，消费电子产业链回暖信号明显。"},
            {"title": "医药研发外包市场扩张，药明康德业绩超预期", "url": "#", "time": "16:35", "summary": "药明康德发布业绩预告，净利润增长超预期，医药研发外包市场持续扩张。"},
        ]
        print(f"✓ 离线模式：加载 {len(news_list)} 条模拟新闻")

    # ---- 4. 新闻与板块关联分析 ----
    利好词 = ["上涨", "增长", "突破", "超预期", "利好", "政策支持", "补贴", "创新高", "大涨", "暴涨",
              "加速", "提升", "扩大", "向好", "复苏", "回暖", "走强", "拉升", "涨停", "爆发"]
    利空词 = ["下跌", "下滑", "亏损", "收紧", "制裁", "打压", "暴跌", "跌停", "危机", "风险", "利空", "放缓"]

    all_sectors = sector_data + concept_data
    for news in news_list:
        affected = []
        text = news.get("title", "") + " " + news.get("summary", "")
        for sector, keywords in SECTOR_KEYWORDS.items():
            match_count = sum(1 for kw in keywords if kw in text)
            if match_count > 0:
                sector_info = next((s for s in all_sectors if s["name"] == sector or sector in s["name"]), None)
                if any(kw in text for kw in 利好词):
                    impact = "利好"
                elif any(kw in text for kw in 利空词):
                    impact = "利空"
                else:
                    impact = "关注"
                affected.append({
                    "sector": sector,
                    "impact": impact,
                    "match_count": match_count,
                    "change_pct": sector_info["change_pct"] if sector_info else 0,
                    "leader": sector_info.get("leader_name", "") if sector_info else "",
                    "main_net": sector_info.get("amount", 0) if sector_info else 0,
                })
        affected.sort(key=lambda x: x["match_count"], reverse=True)
        news["affected_sectors"] = affected[:5]

    relevant_news = [n for n in news_list if n.get("affected_sectors")]

    # 排名
    top_sectors = sorted(sector_data, key=lambda x: x.get("change_pct", 0), reverse=True)[:15]
    top_concepts = sorted(concept_data, key=lambda x: x.get("change_pct", 0), reverse=True)[:15]
    # 按成交额排序（如果没有amount字段，用change_pct替代）
    top_fund_inflow = sorted(sector_data, key=lambda x: x.get("amount", x.get("change_pct", 0)), reverse=True)[:10]

    return {
        "news": relevant_news[:25] if relevant_news else news_list[:25],
        "all_news": news_list[:40],
        "total_news": len(news_list),
        "top_sectors": top_sectors,
        "top_concepts": top_concepts,
        "top_fund_inflow": top_fund_inflow,
        "sector_count": len(sector_data),
        "concept_count": len(concept_data),
    }

# ========== Flask路由 ==========

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/login')
def login():
    return render_template('login.html')

@app.route('/api/pick')
def api_pick():
    """执行选股并返回JSON

    注意: run_picker()内部会调用get_realtime_quotes()，不要重复调用
    """
    import traceback
    try:
        print("\n" + "="*50, flush=True)
        print("📡 收到选股请求，开始执行...", flush=True)
        print("="*50, flush=True)

        results = run_picker()
        total = results[0].get('_total_scanned', len(get_preset_financials())) if results else len(get_preset_financials())
        # 清理内部字段
        for r in results:
            r.pop('_total_scanned', None)
            # 补充 debt_ratio（如果缺失则从财务数据API获取）
            if 'debt_ratio' not in r or r.get('debt_ratio', 0) == 0:
                fin = get_financial_data_fast(r.get('code', ''))
                if fin and fin.get('debt_ratio', 0) != 0:
                    r['debt_ratio'] = fin['debt_ratio']
                else:
                    r['debt_ratio'] = 0
            # 补充 net_margin
            if 'net_margin' not in r or r.get('net_margin', 0) == 0:
                fin = get_financial_data_fast(r.get('code', ''))
                if fin and fin.get('net_margin', 0) != 0:
                    r['net_margin'] = fin['net_margin']
        return jsonify({
            "success": True,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total_scanned": total,
            "results": results,
        })
    except Exception as e:
        print(f"❌ 选股错误: {e}", flush=True)
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/pick_v5')
def api_pick_v5():
    """执行选股并返回v5多因子评分结果（按v5评分排序）

    返回每个股票的 v5_total, v5_factors(value/quality/growth/momentum/sentiment)
    以及旧评分作为对比
    """
    import traceback
    try:
        print("\n" + "="*50, flush=True)
        print("📡 收到v5选股请求，开始执行...", flush=True)
        print("="*50, flush=True)

        results = run_picker()
        total = results[0].get('_total_scanned', len(get_preset_financials())) if results else len(get_preset_financials())

        # 按v5评分排序
        results.sort(key=lambda x: x.get('v5_score', 0), reverse=True)
        
        # 精选Top 5 + 完整列表Top 50
        top5 = results[:5]
        full_list = results[:50]

        # 清理内部字段
        for r in top5:
            r.pop('_total_scanned', None)
            r.pop('_final_score', None)
            r.pop('_v5_final', None)
            r['score'] = round(r.get('score', 0), 1)
            r['v5_score'] = round(r.get('v5_score', 0), 2)
        
        for r in full_list:
            r.pop('_total_scanned', None)
            r.pop('_final_score', None)
            r.pop('_v5_final', None)
            r['score'] = round(r.get('score', 0), 1)
            r['v5_score'] = round(r.get('v5_score', 0), 2)

        return jsonify({
            "success": True,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total_scanned": total,
            "sort_by": "v5_multi_factor",
            "top5": top5,          # 精选推荐（建议持仓）
            "full_list": full_list, # 完整候选池
            "message": "精选Top 5建议持仓，完整列表展示Top 50供参考",
        })
    except Exception as e:
        print(f"❌ v5选股错误: {e}", flush=True)
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/pick_compare')
def api_pick_compare():
    """对比旧策略和v5策略的选股结果"""
    import traceback
    try:
        results = run_picker()
        total = results[0].get('_total_scanned', 0) if results else 0

        # 按旧策略排序
        old_sorted = sorted(results, key=lambda x: x.get('score', 0), reverse=True)[:10]
        # 按v5排序
        v5_sorted = sorted(results, key=lambda x: x.get('v5_score', 0), reverse=True)[:10]

        # 计算统计
        old_codes = set(r['code'] for r in old_sorted)
        v5_codes = set(r['code'] for r in v5_sorted)
        overlap = old_codes & v5_codes

        # 评分差异
        all_scores = []
        for r in results:
            diff = r.get('v5_score', 0) - r.get('score', 0)
            all_scores.append({
                'code': r['code'], 'name': r['name'],
                'old_score': round(r.get('score', 0), 1),
                'v5_score': round(r.get('v5_score', 0), 2),
                'diff': round(diff, 2),
            })
        biggest_diff = sorted(all_scores, key=lambda x: abs(x['diff']), reverse=True)[:5]

        return jsonify({
            "success": True,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total_scanned": total,
            "overlap": len(overlap),
            "overlap_pct": len(overlap) * 10,
            "old_top10": [{
                'rank': i+1, 'code': r['code'], 'name': r['name'],
                'old_score': round(r.get('score', 0), 1),
                'v5_score': round(r.get('v5_score', 0), 2),
            } for i, r in enumerate(old_sorted)],
            "v5_top10": [{
                'rank': i+1, 'code': r['code'], 'name': r['name'],
                'v5_score': round(r.get('v5_score', 0), 2),
                'old_score': round(r.get('score', 0), 1),
                'v5_factors': r.get('v5_factors', {}),
            } for i, r in enumerate(v5_sorted)],
            "biggest_diff": biggest_diff,
        })
    except Exception as e:
        print(f"❌ 对比错误: {e}", flush=True)
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/news')
def api_news():
    """获取新闻热点与板块分析"""
    try:
        result = get_sector_news()
        return jsonify({
            "success": True,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            **result,
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/sector_stocks')
def api_sector_stocks():
    """获取板块成分股（用于板块详情）
    
    支持两种板块代码格式:
    - 东方财富 BK 代码 (如 BK0420): 通过 datacenter-web + 腾讯API
    - 新浪板块代码 (如 gn_hwqc, hangye_ZA01): 通过新浪 Market_Center API
    """
    sector_code = request.args.get("code", "")
    sector_name = request.args.get("name", "")
    if not sector_code:
        return jsonify({"success": False, "error": "缺少板块代码"}), 400

    try:
        stocks = []
        em_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://quote.eastmoney.com/"
        }
        tx_headers = {'User-Agent': 'Mozilla/5.0'}

        # ---- 优先方案: 新浪板块代码直接用新浪API ----
        if sector_code.startswith('gn_') or sector_code.startswith('hangye_'):
            print(f"  新浪板块成分股: {sector_code} ({sector_name})")
            try:
                url = 'https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData'
                all_stocks = []
                # 分页获取，每页最多返回约40条，获取全部
                for page in range(1, 6):
                    params = {
                        'page': page, 'num': 50,
                        'sort': 'changepercent', 'asc': 0,
                        'node': sector_code,
                        '_s_r_a': 'page'
                    }
                    r = session.get(url, params=params,
                                    headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
                                            'Referer': 'https://finance.sina.com.cn/'}, timeout=10)
                    r.encoding = 'utf-8'
                    page_stocks = r.json()
                    if not page_stocks:
                        break
                    all_stocks.extend(page_stocks)
                
                for item in all_stocks:
                    try:
                        name = item.get('name', '')
                        if 'ST' in name or '*' in name:
                            continue
                        code = item.get('code', '')
                        price = float(item.get('trade', 0) or 0)
                        if price <= 0 or not code:
                            continue
                        pe_raw = item.get('per', 0) or 0
                        pe_val = 0
                        if pe_raw and pe_raw != '-' and float(pe_raw) > 0 and float(pe_raw) < 10000:
                            pe_val = float(pe_raw)
                        pb_raw = item.get('pb', 0) or 0
                        pb_val = 0
                        if pb_raw and pb_raw != '-':
                            try: pb_val = float(pb_raw)
                            except: pb_val = 0
                        stocks.append({
                            "code": code, "name": name, "price": price,
                            "change_pct": float(item.get('changepercent', 0) or 0),
                            "amount": float(item.get('amount', 0) or 0),
                            "pe": pe_val, "pb": pb_val, "roe": 0, "gross_margin": 0,
                            "market_cap": float(item.get('nmc', 0) or 0),
                        })
                    except:
                        continue
            except Exception as e:
                print(f"  新浪板块API失败: {e}")

            if stocks:
                stocks.sort(key=lambda x: x["change_pct"], reverse=True)
                return jsonify({"success": True, "sector_name": sector_name, "stocks": stocks[:50], "total": len(stocks)})
            else:
                return jsonify({"success": False, "error": "无法获取板块成分股"}), 500

        # ---- 东方财富BK代码方案 ----
        # 先尝试从push2 clist/get获取（如果恢复的话）
        try:
            url = "https://push2.eastmoney.com/api/qt/clist/get"
            params = {
                "pn": 1, "pz": 20, "po": 1, "np": 1,
                "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                "fltt": 2, "invt": 2, "fid": "f3",
                "fs": f"b:{sector_code}+f:!50",
                "fields": "f2,f3,f12,f14,f20,f162,f167"
            }
            resp = session.get(url, params=params, headers=EM_HEADERS, timeout=5)
            data = resp.json()
            if data.get("data") and data["data"].get("diff"):
                for item in data["data"]["diff"]:
                    try:
                        code = str(item.get("f12", ""))
                        name = item.get("f14", "")
                        if "ST" in name or "*" in name:
                            continue
                        price = float(item.get("f2", 0))
                        if price <= 0:
                            continue
                        pe_raw = item.get("f162", 0) or item.get("f9", 0)
                        pe_val = 0
                        if pe_raw and pe_raw != "-":
                            try: pe_val = float(pe_raw)
                            except: pe_val = 0
                        pb_raw = item.get("f167", 0) or item.get("f23", 0)
                        pb_val = 0
                        if pb_raw and pb_raw != "-":
                            try: pb_val = float(pb_raw)
                            except: pb_val = 0
                        stocks.append({
                            "code": code, "name": name, "price": price,
                            "change_pct": float(item.get("f3", 0)),
                            "amount": float(item.get("f6", 0)) if item.get("f6", 0) else 0,
                            "pe": pe_val, "pb": pb_val, "roe": 0, "gross_margin": 0,
                            "market_cap": float(item.get("f20", 0)) / 100000000 if item.get("f20", 0) > 0 else 0,
                        })
                    except:
                        continue
                if stocks:
                    stocks.sort(key=lambda x: x["change_pct"], reverse=True)
                    return jsonify({"success": True, "sector_name": sector_name, "stocks": stocks[:20], "total": len(stocks)})
        except:
            pass  # clist/get被拒，用备选方案

        # 备选方案：用datacenter-web获取成分股代码，然后用腾讯API获取行情
        print(f"  clist/get blocked, getting sector {sector_code} stocks...")
        try:
            dc_params = {
                'reportName': 'RPT_INDUSTRY_INDEX',
                'columns': 'BOARD_CODE,SECURITY_CODE,INDICATOR_VALUE',
                'filter': f'(BOARD_CODE="{sector_code}")',
                'pageNumber': 1, 'pageSize': 25,
                'source': 'WEB', 'client': 'WEB',
            }
            resp = session.get('https://datacenter-web.eastmoney.com/api/data/v1/get',
                                params=dc_params, headers=DC_HEADERS, timeout=10)
            d = resp.json()
            member_codes = []
            if d.get('success') and d.get('result') and d['result'].get('data'):
                for item in d['result']['data']:
                    sc = item.get('SECURITY_CODE', '')
                    if sc and len(sc) == 6:
                        member_codes.append(sc)

            if member_codes:
                tx_codes = [f"sh{c}" if c.startswith('6') else f"sz{c}" for c in member_codes]
                url = 'http://qt.gtimg.cn/q=' + ','.join(tx_codes)
                tx_resp = session.get(url, timeout=15)
                lines = tx_resp.text.strip().split(';')
                for line in lines:
                    if not line.strip():
                        continue
                    parts = line.split('~')
                    if len(parts) < 50:
                        continue
                    code = parts[2]
                    try:
                        price = float(parts[3]) if parts[3] else 0
                        if price <= 0: continue
                        stocks.append({
                            "code": code, "name": parts[1], "price": price,
                            "change_pct": float(parts[32]) if parts[32] else 0,
                            "amount": float(parts[43]) * 10000 if parts[43] else 0,
                            "pe": float(parts[39]) if parts[39] and parts[39] != '-' and float(parts[39]) < 10000 else 0,
                            "pb": 0, "roe": 0, "gross_margin": 0,
                            "market_cap": float(parts[44]) if parts[44] else 0,
                        })
                    except:
                        continue

            stocks.sort(key=lambda x: x["change_pct"], reverse=True)
        except Exception as e2:
            print(f"  备选方案也失败: {e2}")

        if stocks:
            stocks.sort(key=lambda x: x["change_pct"], reverse=True)
            return jsonify({
                "success": True,
                "sector_name": sector_name,
                "stocks": stocks[:20],
                "total": len(stocks),
            })
        else:
            return jsonify({"success": False, "error": "无法获取板块成分股"}), 500
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/market')
def api_market():
    """获取全市场概览"""
    stocks = get_realtime_quotes()
    
    # 离线模式：使用预设数据
    if not stocks or len(stocks) < 50:
        print("⚠️ 市场概览：实时行情获取失败，启用离线模式")
        preset = get_preset_financials()
        stocks = []
        for code, fin in preset.items():
            stocks.append({
                "code": code,
                "name": fin["name"],
                "price": fin.get("price", 0),
                "change_pct": fin.get("change_pct", 0),
                "amount": fin.get("market_cap", 0) * 100000000,  # 市值转成交额（模拟）
            })
    
    if not stocks:
        return jsonify({"success": False, "error": "无法获取市场数据"})

    total = len(stocks)
    up_count = len([s for s in stocks if s["change_pct"] > 0])
    down_count = len([s for s in stocks if s["change_pct"] < 0])
    flat_count = total - up_count - down_count
    avg_change = sum(s["change_pct"] for s in stocks) / total if total else 0

    # 涨幅前10
    top_gainers = sorted(stocks, key=lambda x: x["change_pct"], reverse=True)[:10]
    # 跌幅前10
    top_losers = sorted(stocks, key=lambda x: x["change_pct"])[:10]
    # 成交额前10
    top_volume = sorted(stocks, key=lambda x: x.get("amount", 0), reverse=True)[:10]

    return jsonify({
        "success": True,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "summary": {
            "total": total,
            "up": up_count,
            "down": down_count,
            "flat": flat_count,
            "avg_change": round(avg_change, 2),
        },
        "top_gainers": [{"code": s["code"], "name": s["name"], "price": s["price"], "change_pct": s["change_pct"], "amount": s.get("amount", 0)} for s in top_gainers],
        "top_losers": [{"code": s["code"], "name": s["name"], "price": s["price"], "change_pct": s["change_pct"], "amount": s.get("amount", 0)} for s in top_losers],
        "top_volume": [{"code": s["code"], "name": s["name"], "price": s["price"], "change_pct": s["change_pct"], "amount": s.get("amount", 0)} for s in top_volume],
    })

@app.route('/api/search_stock')
def api_search_stock():
    """搜索全市场股票（支持名称或代码模糊匹配）
    
    2026-04-02: push2和searchapi全被封，改用腾讯API + 东方财富财务数据中心
    - 代码搜索: 腾讯API直接查行情
    - 名称搜索: 东方财富智能搜索(smartbox)获取候选，腾讯API获取行情
    - 财务数据: 东方财富datacenter-web
    """
    query = request.args.get("q", "").strip()
    if not query or len(query) < 1:
        return jsonify({"success": False, "error": "请输入搜索关键词"}), 400

    try:
        matched_stocks = []
        tx_headers = {'User-Agent': 'Mozilla/5.0'}
        dc_headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
            'Referer': 'https://data.eastmoney.com/'
        }

        # 1. 名称搜索：东方财富smartbox接口
        if not query.isdigit() or len(query) >= 2:
            try:
                smartbox_url = "https://searchapi.eastmoney.com/api/suggest/get"
                smartbox_params = {
                    "input": query,
                    "type": "14",
                    "token": "D43BF722C8E33BDC906FB84D85E326E8",
                    "count": 10,
                }
                resp = session.get(smartbox_url, params=smartbox_params,
                                    headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://so.eastmoney.com/'},
                                    timeout=10)
                data = resp.json()
                if data.get("QuotationCodeTable") and data["QuotationCodeTable"].get("Data"):
                    for item in data["QuotationCodeTable"]["Data"][:10]:
                        try:
                            code = str(item.get("Code", ""))
                            name = item.get("Name", "")
                            classify = item.get("Classify", "")
                            if not code or not name or classify != "AStock":
                                continue
                            if "ST" in name or "*" in name:
                                continue
                            if code.startswith('8') or code.startswith('4') or code.startswith('920'):
                                continue
                            matched_stocks.append({"code": code, "name": name})
                        except:
                            continue
            except Exception as e:
                print(f"Smartbox搜索失败: {e}")

        # 2. 代码搜索：如果输入是纯数字
        if query.isdigit():
            code = query.zfill(6)
            if code not in [s["code"] for s in matched_stocks]:
                # 判断市场后缀
                tx_code = f"sh{code}" if code.startswith('6') else f"sz{code}"
                matched_stocks.append({"code": code, "name": ""})

        # 3. 批量获取行情：用腾讯API
        if matched_stocks:
            tx_codes = []
            for s in matched_stocks:
                c = s["code"]
                tx_codes.append(f"sh{c}" if c.startswith('6') else f"sz{c}")

            # 腾讯API每批80只
            for i in range(0, len(tx_codes), 80):
                batch = tx_codes[i:i+80]
                try:
                    url = 'http://qt.gtimg.cn/q=' + ','.join(batch)
                    resp = session.get(url, timeout=15)
                    lines = resp.text.strip().split(';')
                    for line in lines:
                        if not line.strip():
                            continue
                        parts = line.split('~')
                        if len(parts) < 50:
                            continue
                        code = parts[2]
                        if not code or len(code) != 6:
                            continue
                        try:
                            price = float(parts[3]) if parts[3] else 0
                        except:
                            price = 0
                        if price <= 0:
                            continue
                        try:
                            change_pct = float(parts[32]) if parts[32] else 0
                        except:
                            change_pct = 0
                        try:
                            pe = float(parts[39]) if parts[39] and parts[39] != '-' else 0
                            if pe > 10000 or pe < 0: pe = 0
                        except:
                            pe = 0
                        try:
                            total_cap_yi = float(parts[44]) if parts[44] else 0
                        except:
                            total_cap_yi = 0
                        try:
                            high = float(parts[33]) if parts[33] else 0
                        except:
                            high = 0
                        try:
                            low = float(parts[34]) if parts[34] else 0
                        except:
                            low = 0
                        try:
                            open_p = float(parts[5]) if parts[5] else 0
                        except:
                            open_p = 0
                        try:
                            prev_close = float(parts[4]) if parts[4] else 0
                        except:
                            prev_close = 0
                        try:
                            volume_gu = float(parts[37]) if parts[37] else 0
                        except:
                            volume_gu = 0
                        try:
                            amount_wan = float(parts[43]) if parts[43] else 0
                        except:
                            amount_wan = 0

                        # 更新matched_stocks中的行情数据
                        for ms in matched_stocks:
                            if ms["code"] == code:
                                ms.update({
                                    "name": parts[1] or ms["name"],
                                    "price": price, "change_pct": change_pct,
                                    "volume": volume_gu, "amount": amount_wan * 10000,
                                    "market_cap": total_cap_yi * 100000000,
                                    "pe": pe, "pb": 0,
                                    "high": high, "low": low, "open": open_p, "prev_close": prev_close,
                                })
                                break
                except Exception as e:
                    print(f"腾讯API搜索行情失败: {e}")

        # 过滤掉没获取到行情的
        matched_stocks = [s for s in matched_stocks if s.get("price", 0) > 0]

        # 4. 补充PB：用腾讯API的PB字段(parts[46])
        # 腾讯API parts[46]有时候是PB
        for ms in matched_stocks:
            if ms.get("pb", 0) == 0 and ms.get("pe", 0) > 0:
                # 腾讯API没有可靠的PB字段，尝试从东方财富datacenter获取
                pass

        # 5. 用财务数据中心补充ROE/毛利率/增速
        for stock in matched_stocks:
            fin_data = get_financial_data(stock["code"])
            if fin_data:
                stock["roe"] = fin_data.get("roe", 0)
                stock["gross_margin"] = fin_data.get("gross_margin", 0)
                stock["rev_growth"] = fin_data.get("rev_growth", 0)
                stock["profit_growth"] = fin_data.get("profit_growth", 0)
            else:
                stock["roe"] = 0
                stock["gross_margin"] = 0
                stock["rev_growth"] = 0
                stock["profit_growth"] = 0
            stock.setdefault("net_margin", 0)
            stock.setdefault("debt_ratio", 0)

            # 预设数据补充
            preset = get_preset_financials()
            code = stock["code"]
            if code in preset:
                fin = preset[code]
                for k in ["roe", "gross_margin", "net_margin", "rev_growth", "profit_growth", "pb"]:
                    if stock.get(k, 0) == 0 and fin.get(k):
                        stock[k] = fin[k]

        # 6. 五维评估
        results = []
        for stock in matched_stocks:
            r = evaluate_stock(stock)
            if r:
                results.append(r)
            else:
                results.append({
                    "code": stock["code"], "name": stock["name"],
                    "price": stock.get("price", 0), "change_pct": stock.get("change_pct", 0),
                    "pe": stock.get("pe", 0), "pb": stock.get("pb", 0),
                    "roe": stock.get("roe", 0), "gross_margin": stock.get("gross_margin", 0),
                    "net_margin": stock.get("net_margin", 0),
                    "rev_growth": stock.get("rev_growth", 0), "profit_growth": stock.get("profit_growth", 0),
                    "market_cap": stock.get("market_cap", 0),  # 已经是亿元单位
                    "score": 0,
                    "dimensions": {"profitability": 0, "growth": 0, "health": 0, "valuation": 0, "cashflow": 0},
                    "reasons": [], "buy_sell": None,
                })

        results.sort(key=lambda x: x["score"], reverse=True)
        return jsonify({
            "success": True,
            "query": query,
            "results": results,
            "total_matched": len(matched_stocks),
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/daily_pick')
def daily_pick():
    """每日推荐页面 - 展示早盘和午盘选股结果"""
    return render_template('daily_pick.html')

@app.route('/api/daily_pick')
def api_daily_pick():
    """获取每日推荐数据"""
    global DAILY_PICK_DATA

    with DAILY_PICK_LOCK:
        data = DAILY_PICK_DATA.copy()

    # 如果没有任何数据，尝试执行一次选股
    if not data.get('morning') and not data.get('afternoon'):
        # 判断当前时间段
        now = datetime.now()
        current_hour = now.hour

        if current_hour >= 14:
            # 下午时段，执行午盘选股
            execute_daily_pick('afternoon')
            # 如果上午还没选，也执行一次
            if not DAILY_PICK_DATA.get('morning'):
                execute_daily_pick('morning')
        elif current_hour >= 9:
            # 上午时段，执行早盘选股
            execute_daily_pick('morning')

        with DAILY_PICK_LOCK:
            data = DAILY_PICK_DATA.copy()

    # 确保 debt_ratio 字段存在
    for session in ['morning', 'afternoon']:
        if data.get(session) and data[session].get('results'):
            for stock in data[session]['results']:
                if 'debt_ratio' not in stock:
                    stock['debt_ratio'] = 0

    return jsonify({
        "success": True,
        "date": data.get('date', datetime.now().strftime('%Y-%m-%d')),
        "morning": data.get('morning'),
        "afternoon": data.get('afternoon'),
        "last_update": data.get('last_update'),
    })

@app.route('/api/stock_detail')
def api_stock_detail():
    """获取单个股票详情 + 相关新闻 - 实时拉取数据"""
    code = request.args.get("code", "")
    if not code:
        return jsonify({"success": False, "error": "缺少股票代码"}), 400

    print(f"获取股票详情: {code}", flush=True)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://quote.eastmoney.com/"
    }

    stock_info = None

    # === 1. 获取实时行情（腾讯API）===
    try:
        tx_code = f"sh{code}" if code.startswith('6') else f"sz{code}"
        url = f'http://qt.gtimg.cn/q={tx_code}'
        tx_resp = session.get(url, timeout=10)
        lines = tx_resp.text.strip().split(';')
        for line in lines:
            if not line.strip():
                continue
            if '=' in line:
                line = line.split('=', 1)[1].strip('"')
            parts = line.split('~')
            if len(parts) < 50:
                continue
            try:
                price = float(parts[3]) if parts[3] else 0
                if price <= 0:
                    continue
                pe_val = 0
                if parts[39] and parts[39] != '-':
                    pe_val = float(parts[39])
                    if pe_val > 10000 or pe_val < 0:
                        pe_val = 0
                pb_val = 0
                if parts[46] and parts[46] != '-':
                    pb_val = float(parts[46])
                turnover_val = 0
                if parts[38] and parts[38] != '-':
                    turnover_val = float(parts[38])
                # 腾讯API: parts[44]=总市值，单位是"亿元"！不是万元
                total_cap_yi = float(parts[44]) if parts[44] and parts[44] != '-' else 0
                print(f"  [DEBUG] 总市值原始字段 parts[44]='{parts[44]}', 解析值={total_cap_yi}亿元", flush=True)
                stock_info = {
                    "code": code,
                    "name": parts[1],
                    "price": price,
                    "change_pct": float(parts[32]) if parts[32] else 0,
                    "high": float(parts[33]) if parts[33] else 0,
                    "low": float(parts[34]) if parts[34] else 0,
                    "open": float(parts[5]) if parts[5] else 0,
                    "prev_close": float(parts[4]) if parts[4] else 0,
                    "volume": float(parts[37]) if parts[37] else 0,
                    "amount": float(parts[43]) * 10000 if parts[43] else 0,
                    "pe": pe_val,
                    "pb": pb_val,
                    "turnover_rate": turnover_val,
                    "roe": 0,
                    "gross_margin": 0,
                    "net_margin": 0,
                    "rev_growth": 0,
                    "profit_growth": 0,
                    "debt_ratio": 0,
                    "market_cap": total_cap_yi,  # 直接存储亿元单位
                }
                print(f"  行情数据获取成功: {parts[1]} 价格={price}, 总市值={total_cap_yi}亿", flush=True)
                break
            except:
                continue
    except Exception as e:
        print(f"  行情获取失败: {e}", flush=True)

    # === 2. 实时获取财务数据（强制拉取，不依赖缓存）===
    if stock_info:
        print(f"  开始获取财务数据...", flush=True)
        fin_data = get_financial_data(code)  # 使用完整版get_financial_data（timeout=5秒）
        if fin_data:
            print(f"  财务数据: ROE={fin_data.get('roe', 0)}, 毛利率={fin_data.get('gross_margin', 0)}", flush=True)
            # 直接覆盖财务指标
            if fin_data.get("roe", 0) != 0:
                stock_info["roe"] = fin_data["roe"]
            if fin_data.get("gross_margin", 0) != 0:
                stock_info["gross_margin"] = fin_data["gross_margin"]
            if fin_data.get("rev_growth", 0) != 0:
                stock_info["rev_growth"] = fin_data["rev_growth"]
            if fin_data.get("profit_growth", 0) != 0:
                stock_info["profit_growth"] = fin_data["profit_growth"]
            if fin_data.get("debt_ratio", 0) != 0:
                stock_info["debt_ratio"] = fin_data["debt_ratio"]
            if fin_data.get("net_margin", 0) != 0:
                stock_info["net_margin"] = fin_data["net_margin"]
            if fin_data.get("pb", 0) != 0:
                stock_info["pb"] = fin_data["pb"]
        else:
            print(f"  财务数据获取失败，尝试备用API...", flush=True)
            # 备用：使用fast版本（更短timeout，可能成功）
            fin_data_fast = get_financial_data_fast(code)
            if fin_data_fast:
                if fin_data_fast.get("roe", 0) != 0:
                    stock_info["roe"] = fin_data_fast["roe"]
                if fin_data_fast.get("gross_margin", 0) != 0:
                    stock_info["gross_margin"] = fin_data_fast["gross_margin"]
                if fin_data_fast.get("debt_ratio", 0) != 0:
                    stock_info["debt_ratio"] = fin_data_fast["debt_ratio"]

        # 如果PE无效但PB和ROE有效，估算PE
        if stock_info.get("pe", 0) == 0 and stock_info.get("pb", 0) > 0 and stock_info.get("roe", 0) > 0:
            stock_info["pe"] = round(stock_info["pb"] / (stock_info["roe"] / 100), 1)

    if not stock_info:
        return jsonify({"success": False, "error": "股票不存在或无法获取数据"}), 404

    # === 3. 优先使用东方财富实时财务数据（真实数据）===
    print(f"  开始加载实时财务数据（东方财富）...", flush=True)
    fin = get_financial_data_fast(code)
    if fin and (fin.get("roe", 0) != 0 or fin.get("gross_margin", 0) != 0):
        # 东方财富真实财务数据优先
        stock_info["roe"] = fin.get("roe", 0)
        stock_info["gross_margin"] = fin.get("gross_margin", 0)
        stock_info["net_margin"] = fin.get("net_margin", 0)
        stock_info["rev_growth"] = fin.get("rev_growth", 0)
        stock_info["profit_growth"] = fin.get("profit_growth", 0)
        stock_info["debt_ratio"] = fin.get("debt_ratio", 0)
        print(f"  东方财富实时: ROE={stock_info['roe']}%, 营收增长={stock_info.get('rev_growth',0)}%", flush=True)
    else:
        # 东方财富拉取失败，降级用离线库
        preset_data = get_preset_financials()
        if code in preset_data:
            preset = preset_data[code]
            stock_info["roe"] = preset.get("roe", 0)
            stock_info["gross_margin"] = preset.get("gross_margin", 0)
            stock_info["net_margin"] = preset.get("net_margin", 0)
            stock_info["rev_growth"] = preset.get("rev_growth", 0)
            stock_info["profit_growth"] = preset.get("profit_growth", 0)
            stock_info["debt_ratio"] = preset.get("debt_ratio", 0)
            print(f"  [降级] 使用离线库: ROE={stock_info['roe']}%", flush=True)
        else:
            print(f"  财务数据不可用，使用默认值", flush=True)

    # === 4. 使用 evaluate_stock() 计算评分（与首页完全一致）===
    print(f"  开始计算评分（使用 evaluate_stock，与首页一致）...", flush=True)
    tech_data = None
    try:
        tech_data = calculate_technical_indicators(code, days=30)
    except Exception as e:
        print(f"  技术指标获取失败: {e}", flush=True)
    
    # 关键修复：使用 evaluate_stock() 而不是直接调用 multi_factor_evaluate()
    # evaluate_stock() 包含行情因子、换手率因子、板块轮动加分、热点因子等
    eval_result = evaluate_stock(stock_info, tech_data=tech_data if tech_data else None)
    
    if not eval_result:
        return jsonify({"success": False, "error": "股票评分计算失败"}), 404
    
    # 从 evaluate_stock 结果提取所有字段
    score = eval_result.get("score", 0)
    v5_score = eval_result.get("v5_score", 0)
    v5_factors = eval_result.get("v5_factors", {})
    v5_reasons = eval_result.get("v5_reasons", [])
    v5_rec = eval_result.get("v5_recommendation", "")
    dimensions = eval_result.get("dimensions", {})
    buy_sell = eval_result.get("buy_sell")
    reasons = eval_result.get("reasons", [])
    
    print(f"  评分完成: score={score}, v5_score={v5_score}", flush=True)

    analysis = []

    # 构建分析详情（供详情页展示）
    roe = stock_info.get("roe", 0)
    gross_margin = stock_info.get("gross_margin", 0)
    net_margin = stock_info.get("net_margin", 0)
    rev_growth = stock_info.get("rev_growth", 0)
    profit_growth = stock_info.get("profit_growth", 0)
    pe = stock_info.get("pe", 0)
    pb = stock_info.get("pb", 0)
    debt_ratio = stock_info.get("debt_ratio", 0)
    market_cap_yi = stock_info.get("market_cap", 0)  # 已经是亿元单位，直接使用

    # 盈利能力分析
    if roe >= 20:
        analysis.append({"dim": "盈利能力", "score": round(dimensions["profitability"]), "max": 35, "detail": f"ROE {roe:.1f}% 优秀（≥20%）", "level": "excellent"})
    elif roe >= 15:
        analysis.append({"dim": "盈利能力", "score": round(dimensions["profitability"]), "max": 35, "detail": f"ROE {roe:.1f}% 良好（≥15%）", "level": "good"})
    elif roe > 0:
        analysis.append({"dim": "盈利能力", "score": round(dimensions["profitability"]), "max": 35, "detail": f"ROE {roe:.1f}% 一般", "level": "fair"})
    else:
        analysis.append({"dim": "盈利能力", "score": 0, "max": 35, "detail": "ROE数据缺失", "level": "unknown"})

    if gross_margin >= 40:
        analysis.append({"dim": "毛利率", "score": "+8", "max": 35, "detail": f"毛利率 {gross_margin:.1f}% 优秀（≥40%）", "level": "excellent"})
    elif gross_margin > 0:
        analysis.append({"dim": "毛利率", "score": "+3", "max": 35, "detail": f"毛利率 {gross_margin:.1f}%", "level": "fair"})

    if net_margin >= 15:
        analysis.append({"dim": "净利率", "score": "+5", "max": 35, "detail": f"净利率 {net_margin:.1f}% 优秀（≥15%）", "level": "excellent"})

    # 成长性分析
    if rev_growth > 0 and profit_growth > 0:
        avg_growth = (rev_growth + profit_growth) / 2
    elif rev_growth > 0:
        avg_growth = rev_growth
    elif profit_growth > 0:
        avg_growth = profit_growth
    else:
        avg_growth = 0

    if avg_growth >= 20:
        analysis.append({"dim": "成长性", "score": 25, "max": 25, "detail": f"平均增速 {avg_growth:.1f}% 优秀（≥20%）", "level": "excellent"})
    elif avg_growth >= 15:
        analysis.append({"dim": "成长性", "score": 20, "max": 25, "detail": f"平均增速 {avg_growth:.1f}% 良好（≥15%）", "level": "good"})
    elif avg_growth >= 10:
        analysis.append({"dim": "成长性", "score": 15, "max": 25, "detail": f"平均增速 {avg_growth:.1f}% 一般（≥10%）", "level": "fair"})
    elif avg_growth > 0:
        analysis.append({"dim": "成长性", "score": 8, "max": 25, "detail": f"平均增速 {avg_growth:.1f}% 较低", "level": "poor"})
    else:
        analysis.append({"dim": "成长性", "score": 0, "max": 25, "detail": "成长性数据缺失", "level": "unknown"})

    # 财务健康分析
    if debt_ratio > 0 and debt_ratio < 1000:
        if debt_ratio <= 50:
            analysis.append({"dim": "财务健康", "score": 20, "max": 20, "detail": f"资产负债率 {debt_ratio:.1f}% 优秀（≤50%）", "level": "excellent"})
        elif debt_ratio <= 70:
            analysis.append({"dim": "财务健康", "score": 12, "max": 20, "detail": f"资产负债率 {debt_ratio:.1f}% 一般（≤70%）", "level": "fair"})
        else:
            analysis.append({"dim": "财务健康", "score": 5, "max": 20, "detail": f"资产负债率 {debt_ratio:.1f}% 偏高", "level": "poor"})
    else:
        analysis.append({"dim": "财务健康", "score": 10, "max": 20, "detail": "资产负债率数据缺失，给中等分", "level": "unknown"})

    # 估值分析
    if pe > 0 and pe < 1000:
        if pe <= 12:  # 优化：15 -> 12
            analysis.append({"dim": "估值", "score": round(dimensions["valuation"]), "max": 20, "detail": f"PE {pe:.1f} 低估（≤15）", "level": "excellent"})
        elif pe <= 20:  # 优化：25 -> 20
            analysis.append({"dim": "估值", "score": round(dimensions["valuation"]), "max": 20, "detail": f"PE {pe:.1f} 合理（≤25）", "level": "good"})
        elif pe <= 35:
            analysis.append({"dim": "估值", "score": round(dimensions["valuation"]), "max": 20, "detail": f"PE {pe:.1f} 偏高（≤35）", "level": "fair"})
        elif pe <= 50:
            analysis.append({"dim": "估值", "score": round(dimensions["valuation"]), "max": 20, "detail": f"PE {pe:.1f} 偏高（≤50）", "level": "poor"})
        else:
            analysis.append({"dim": "估值", "score": round(dimensions["valuation"]), "max": 20, "detail": f"PE {pe:.1f} 高估", "level": "poor"})
    else:
        analysis.append({"dim": "估值", "score": 8, "max": 20, "detail": "无PE数据，成长股给予中等分", "level": "unknown"})

    if 0 < pb <= 3:
        analysis.append({"dim": "市净率", "score": "+5", "max": 20, "detail": f"PB {pb:.2f} 低估（≤3）", "level": "excellent"})
    elif 3 < pb <= 5:
        analysis.append({"dim": "市净率", "score": "+2", "max": 20, "detail": f"PB {pb:.2f} 合理", "level": "good"})

    # 现金流质量分析（与evaluate_stock保持一致的新逻辑）
    cashflow_score = 0
    cashflow_detail = ""

    if pe > 0 and roe > 0:
        if roe >= 20:
            if pe <= 20:
                cashflow_score = 3
                cashflow_detail = f"ROE {roe:.1f}%优秀 + PE低 现金流充裕"
            elif pe <= 35:
                cashflow_score = 2
                cashflow_detail = f"ROE {roe:.1f}%优秀 盈利质量良好"
            else:
                cashflow_score = 1
                cashflow_detail = f"ROE {roe:.1f}%优秀 但PE偏高"
        elif roe >= 10:
            if pe <= 25:
                cashflow_score = 2
                cashflow_detail = f"ROE {roe:.1f}% + PE合理 盈利稳定"
            elif pe <= 40:
                cashflow_score = 1
                cashflow_detail = f"ROE {roe:.1f}% 盈利尚可"
            else:
                cashflow_score = 1
                cashflow_detail = f"ROE {roe:.1f}% 但估值偏高"
        else:
            cashflow_score = 1
            cashflow_detail = f"盈利中 ROE {roe:.1f}%待提升"

        # 加分项
        extras = []
        if gross_margin >= 40:
            cashflow_score += 1
            extras.append("毛利率高")
        if 0 < debt_ratio <= 50:
            cashflow_score += 1
            extras.append("负债率低")
        if extras:
            cashflow_detail += " | " + " ".join(extras)

        level = "excellent" if cashflow_score >= 4 else "good" if cashflow_score >= 3 else "fair" if cashflow_score >= 2 else "poor"
    else:
        cashflow_score = 0
        cashflow_detail = "亏损企业 现金流堪忧"
        level = "poor"

    analysis.append({"dim": "现金流质量", "score": min(cashflow_score, 5), "max": 5, "detail": cashflow_detail, "level": level})

    stock_news = []
    try:
        # 东方财富新闻API已挂，改用新浪财经
        r = session.get("https://feed.mix.sina.com.cn/api/roll/get",
                         params={"pageid": "153", "lid": "2509", "k": "", "r": "0.5", "page": 1},
                         headers=HEADERS, timeout=10)
        d = r.json()
        if d.get('result') and d['result'].get('data'):
            keyword = stock_info["name"]
            for item in d['result']['data'][:30]:
                title = item.get('title', '')
                intro = item.get('intro', '') or ''
                text = title + ' ' + intro
                if keyword in text:
                    impact = "关注"
                    if any(w in text for w in ["上涨", "增长", "突破", "超预期", "利好", "大涨", "暴涨"]):
                        impact = "利好"
                    elif any(w in text for w in ["下跌", "亏损", "下滑", "收紧", "暴跌", "利空"]):
                        impact = "利空"
                    stock_news.append({
                        "title": title,
                        "time": item.get('ctime', ''),
                        "source": item.get('media_name', ''),
                        "summary": intro,
                        "impact": impact,
                    })
    except Exception as e:
        print(f"获取相关新闻失败: {e}")

    # 市值已经是亿元单位，无需转换
    raw_mc = stock_info.get("market_cap", 0)
    print(f"  [DEBUG] 返回前市值: {raw_mc}亿", flush=True)

    # === 5. 获取行业信息 ===
    industry_info = get_stock_industry(code)
    stock_info["industry"] = industry_info.get("industry", "未知")
    stock_info["sector_type"] = industry_info.get("sector_type", "default")

    return jsonify({
        "success": True,
        "stock": stock_info,
        "score": score,
        "v5_score": v5_score,
        "v5_factors": v5_factors,
        "v5_reasons": v5_reasons,
        "v5_recommendation": v5_rec,
        "dimensions": dimensions,
        "analysis": analysis,
        "buy_sell": buy_sell,  # 使用 evaluate_stock() 返回的买卖点
        "news": stock_news[:8],
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })

@app.route('/api/refresh_financials', methods=['POST'])
def api_refresh_financials():
    """刷新指定股票的财务数据，更新离线库
    请求体: {"codes": ["603444", "000001", ...]}
    """
    import time
    data = request.get_json(force=True)
    codes = data.get('codes', [])
    if not codes:
        return jsonify({"success": False, "error": "请提供股票代码列表"}), 400
    
    print(f"\n🔄 开始刷新 {len(codes)} 只股票的财务数据...", flush=True)
    
    # 加载现有离线库
    offline_path = os.path.join(_BASE_DIR, 'offline_stocks.json')
    try:
        with open(offline_path, 'r', encoding='utf-8') as f:
            offline_data = json.load(f)
            offline_stocks = {s['code']: s for s in offline_data.get('stocks', [])}
    except:
        offline_stocks = {}
    
    updated = 0
    failed = 0
    results = []
    
    for i, code in enumerate(codes):
        print(f"  [{i+1}/{len(codes)}] 刷新 {code}...", flush=True)
        try:
            # 先尝试实时拉取财务数据
            fin = get_financial_data(code)
            
            # 也获取行情数据
            tx_code = f"sh{code}" if code.startswith('6') else f"sz{code}"
            tx_resp = session.get(f'http://qt.gtimg.cn/q={tx_code}', timeout=10)
            parts = tx_resp.text.split('~')
            
            name = parts[1] if len(parts) > 1 else code
            price = float(parts[3]) if len(parts) > 3 and parts[3] else 0
            pe_val = float(parts[39]) if len(parts) > 39 and parts[39] and parts[39] != '-' else 0
            pb_val = float(parts[46]) if len(parts) > 46 and parts[46] and parts[46] != '-' else 0
            cap_yi = float(parts[44]) if len(parts) > 44 and parts[44] and parts[44] != '-' else 0
            
            if fin and fin.get('roe', 0) > 0:
                # 更新或创建离线库记录
                existing = offline_stocks.get(code, {})
                existing.update({
                    'code': code,
                    'name': name,
                    'price': price,
                    'pe': pe_val if pe_val > 0 else existing.get('pe', 0),
                    'pb': pb_val if pb_val > 0 else existing.get('pb', 0),
                    'market_cap': cap_yi if cap_yi > 0 else existing.get('market_cap', 0),
                    'change_pct': float(parts[32]) if len(parts) > 32 and parts[32] else 0,
                    'roe': fin.get('roe', existing.get('roe', 0)),
                    'gross_margin': fin.get('gross_margin', existing.get('gross_margin', 0)),
                    'net_margin': fin.get('net_margin', existing.get('net_margin', 0)),
                    'rev_growth': fin.get('rev_growth', existing.get('rev_growth', 0)),
                    'profit_growth': fin.get('profit_growth', existing.get('profit_growth', 0)),
                    'debt_ratio': fin.get('debt_ratio', existing.get('debt_ratio', 0)),
                })
                offline_stocks[code] = existing
                updated += 1
                results.append({"code": code, "status": "updated", "name": name})
                print(f"    ✓ {name}: ROE={fin['roe']}%, 营收增长={fin.get('rev_growth',0)}%", flush=True)
            else:
                failed += 1
                results.append({"code": code, "status": "no_data", "name": name})
                print(f"    ✗ 未获取到财务数据", flush=True)
        except Exception as e:
            failed += 1
            results.append({"code": code, "status": "error", "error": str(e)})
            print(f"    ✗ 失败: {e}", flush=True)
        
        time.sleep(0.3)  # 避免请求过快
    
    # 保存离线库
    offline_data = {
        'update_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'total': len(offline_stocks),
        'stocks': list(offline_stocks.values()),
    }
    with open(offline_path, 'w', encoding='utf-8') as f:
        json.dump(offline_data, f, ensure_ascii=False, indent=2)
    
    print(f"✓ 刷新完成: 成功 {updated}, 失败 {failed}", flush=True)
    
    return jsonify({
        "success": True,
        "updated": updated,
        "failed": failed,
        "total": len(codes),
        "results": results,
    })



# ========== 尾盘强势股选股 ==========

WP2_PICK_DATA = {
    "stocks": [],
    "pick_time": None,
    "last_update": None,
    "filter_stats": [],
    "market_info": {},
    "running": False,
}
WP2_PICK_LOCK = threading.Lock()
WP2_PICK_FILE = os.path.join(_BASE_DIR, 'wp2_pick_cache.json')


def load_wp2_pick_cache():
    global WP2_PICK_DATA
    try:
        if os.path.exists(WP2_PICK_FILE):
            with open(WP2_PICK_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if data.get('date') == datetime.now().strftime('%Y-%m-%d'):
                    WP2_PICK_DATA = data
                    print(f"✓ 加载尾盘选股缓存: {bool(data.get('stocks'))}")
                    return
    except Exception as e:
        print(f"加载尾盘选股缓存失败: {e}")
    WP2_PICK_DATA = {
        "date": datetime.now().strftime('%Y-%m-%d'),
        "stocks": [],
        "pick_time": None,
        "last_update": None,
        "filter_stats": [],
        "market_info": {},
        "running": False,
    }


def save_wp2_pick_cache():
    try:
        with open(WP2_PICK_FILE, 'w', encoding='utf-8') as f:
            json.dump(WP2_PICK_DATA, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"保存尾盘选股缓存失败: {e}")


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
    if vr >= 2.5:
        sc += 30
    elif vr >= 2:
        sc += 25
    elif vr >= 1.5:
        sc += 18
    else:
        sc += 10
    if 3 <= ch <= 6:
        sc += 25
    elif ch >= 2:
        sc += 20
    elif ch >= 1:
        sc += 15
    else:
        sc += 5
    if 55 <= rsi <= 65:
        sc += 25
    elif 50 <= rsi <= 70:
        sc += 20
    else:
        sc += 10
    if 50 <= cap <= 200:
        sc += 20
    elif 30 <= cap <= 300:
        sc += 15
    else:
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
                stocks[pure_code] = {
                    'f2': price, 'f3': pct_change, 'f4': round(price - pre_close, 2),
                    'f5': volume, 'f6': amount, 'f7': round((high - low) / pre_close * 100, 2) if pre_close > 0 else 0,
                    'f8': 0, 'f9': 0, 'f10': 0, 'f12': pure_code, 'f13': market, 'f14': fields[0],
                    'f15': high, 'f16': low, 'f17': open_price, 'f18': pre_close, 'f20': 0, 'f21': 0, 'f23': 0,
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
                'score': _wp2_calc_score({'vr': float(s.get('f10', 0)), 'ch': float(s.get('f3', 0)), 'rsi': rsi, 'cap': float(s.get('f21', 0))}),
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
            save_wp2_pick_cache()

    except Exception as e:
        print(f"  ✗ 尾盘选股失败: {e}")
        with WP2_PICK_LOCK:
            WP2_PICK_DATA['running'] = False
            save_wp2_pick_cache()
        import traceback
        traceback.print_exc()


@app.route('/wp2_pick')
def wp2_pick():
    return render_template('wp2_pick.html')


@app.route('/api/wp2_pick')
def api_wp2_pick():
    global WP2_PICK_DATA
    with WP2_PICK_LOCK:
        data = WP2_PICK_DATA.copy()
    return jsonify({
        "success": True,
        "stocks": data.get('stocks', []),
        "pick_time": data.get('pick_time'),
        "last_update": data.get('last_update'),
        "filter_stats": data.get('filter_stats', []),
        "market_info": data.get('market_info', {}),
        "running": data.get('running', False),
    })


@app.route('/api/wp2_pick_run', methods=['POST'])
def api_wp2_pick_run():
    params = request.get_json(force=True) if request.is_json else {}
    min_cap = params.get('min_cap', 30)
    max_cap = params.get('max_cap', 300)
    min_amt = params.get('min_amt', 3)
    vol_mul = params.get('vol_mul', 1.5)
    break_n = params.get('break_n', 20)
    body_r = params.get('body_r', 0.6)
    rsi_lo = params.get('rsi_lo', 50)
    rsi_hi = params.get('rsi_hi', 75)
    max_out = params.get('max_out', 4)

    def run_async():
        execute_wp2_pick(min_cap, max_cap, min_amt, vol_mul, break_n, body_r, rsi_lo, rsi_hi, max_out)

    t = threading.Thread(target=run_async, daemon=True)
    t.start()

    return jsonify({"success": True, "message": "尾盘选股已启动"})


# ========== 短线竞价选股 ==========

# 竞价选股数据缓存
AUCTION_PICK_DATA = {
    "stocks": [],
    "pick_time": None,
    "last_update": None,
    "market_info": {},
    "candidate_pool": [],
}
AUCTION_PICK_LOCK = threading.Lock()
AUCTION_PICK_FILE = os.path.join(_BASE_DIR, 'auction_pick_cache.json')


def load_auction_pick_cache():
    """加载竞价选股缓存"""
    global AUCTION_PICK_DATA
    try:
        if os.path.exists(AUCTION_PICK_FILE):
            with open(AUCTION_PICK_FILE, 'r', encoding='utf-8') as f:
                AUCTION_PICK_DATA = json.load(f)
    except Exception as e:
        print(f"加载竞价选股缓存失败: {e}")


def save_auction_pick_cache():
    """保存竞价选股缓存"""
    try:
        with open(AUCTION_PICK_FILE, 'w', encoding='utf-8') as f:
            json.dump(AUCTION_PICK_DATA, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"保存竞价选股缓存失败: {e}")


def execute_auction_pick():
    """执行竞价选股策略

    策略核心: 竞价确认·强势早盘
    - 第一阶段: 昨晚预选（趋势、量能、位置筛选）
    - 第二阶段: 竞价确认（高开、量比、竞价额、强于大盘）
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

        # 3. 获取候选池（预选股票）
        candidates = get_auction_candidates()

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

        # 4. 竞价确认筛选
        confirmed_stocks = []
        idx_gap = market_info.get('idx_gap', 0)
        min_gap = 0.02 if idx_gap > 0.015 else 0.01  # 大盘高开>1.5%时，个股高开要求放宽

        for stock in candidates:
            # 竞价四维确认
            gap_ok = min_gap <= stock.get('gap_pct', 0) <= 0.045
            volume_ratio_ok = 1.8 <= stock.get('volume_ratio', 0) <= 5
            auction_amount_ok = stock.get('auction_amount_pct', 0) >= 0.03
            stronger_than_market = stock.get('gap_pct', 0) > idx_gap

            # 四维必须全部满足
            if gap_ok and volume_ratio_ok and auction_amount_ok and stronger_than_market:
                # 计算综合评分
                score = (
                    stock.get('gap_pct', 0) * 100 * 2 +
                    min(stock.get('gap_pct', 0) / max(idx_gap, 0.001), 5) * 10 +
                    stock.get('volume_ratio', 0) * 5
                )
                stock['score'] = score
                stock['gap_ok'] = gap_ok
                stock['volume_ratio_ok'] = volume_ratio_ok
                stock['auction_amount_ok'] = auction_amount_ok
                stock['stronger_than_market'] = stronger_than_market
                confirmed_stocks.append(stock)

        # 5. 按评分排序，最多取3只
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
        import traceback
        traceback.print_exc()


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

        idx300_close = 0
        idx_open = 0

        for line in lines:
            if '=' in line:
                line = line.split('=', 1)[1].strip('"')
            parts = line.split('~')
            if len(parts) > 10:
                idx300_close = float(parts[3]) if parts[3] else 0
                idx_open = float(parts[4]) if parts[4] else idx300_close
                break

        # 计算高开幅度
        prev_close = float(parts[4]) if len(parts) > 4 and parts[4] else idx300_close
        idx_gap = (idx_open / prev_close - 1) if prev_close > 0 else 0

        # 简化版: 用当前价格判断是否在MA20之上（实际需要历史数据）
        # 这里用高开幅度作为简单判断
        market_ok = idx_gap >= -0.015  # 大盘低开不超过1.5%认为可操作

        return {
            "idx300_close": idx300_close,
            "idx_open": idx_open,
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
    """获取竞价候选池 - 使用新浪API"""
    try:
        candidates = []
        print("  正在获取股票数据(新浪API)...", flush=True)

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://finance.sina.com.cn/"
        }

        # 使用新浪财经接口 - 获取多页数据，找涨幅合理的股票
        sina_url = "http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"

        # 获取多页数据：涨幅榜前500只（覆盖更广范围）
        for page in range(1, 6):  # 1-5页，每页100只
            params = {
                "page": page,
                "num": 100,
                "sort": "changepercent",
                "asc": 0,  # 降序，涨幅从高到低
                "node": "hs_a",
                "symbol": "",
                "_s_r_a": "page"
            }

            resp = session.get(sina_url, params=params, headers=headers, timeout=15)
            if resp.status_code != 200:
                continue

            stocks_data = resp.json()
            if not isinstance(stocks_data, list):
                continue

            print(f"  第{page}页获取: {len(stocks_data)}只股票", flush=True)

            for s in stocks_data:
                try:
                    code = s.get("code", "")
                    name = s.get("name", "")

                    # 基础过滤: 剔除科创板(688)、北交所(8开头)、创业板ST
                    # 注意：创业板(300/301开头)是允许的
                    if code.startswith("688"):
                        continue
                    if code.startswith("8") and not code.startswith("30"):
                        continue  # 北交所，但保留创业板301
                    if code.startswith("4"):
                        continue
                    if "ST" in name or "退" in name or "*" in name:
                        continue

                    # 涨幅过滤: 放宽到 -5%~9.5%（排除涨停）
                    change_pct = float(s.get("changepercent", 0)) / 100
                    if change_pct < -0.05 or change_pct > 0.095:  # -5%~9.5%
                        continue

                    # 流通市值过滤: 20~500亿（大幅放宽）
                    circ_cap = float(s.get("nmc", 0)) / 10000  # nmc单位是万元，转为亿元
                    if circ_cap < 20 or circ_cap > 500:
                        continue

                    # 成交额过滤: 1~50亿（大幅放宽）
                    amount = float(s.get("amount", 0)) / 100000000  # amount单位是元，转为亿元
                    if amount < 1 or amount > 50:
                        continue

                    # 避免重复
                    if any(c['code'] == code for c in candidates):
                        continue

                    # 添加候选
                    candidates.append({
                        "code": code,
                        "name": name,
                        "price": float(s.get("trade", 0)) if s.get("trade") else 0,
                        "gap_pct": 0,
                        "change_pct": change_pct,
                        "circ_cap": circ_cap,
                        "amount": amount,
                        # 换手率：从新浪API直接获取（百分比）
                        "turnover_ratio": float(s.get("turnoverratio", 0)) if s.get("turnoverratio") else 0,
                        # 量比：估算公式 = 当日成交量 / 流通股本 * 换手率基准
                        # 简化计算：用换手率估算量比（量比≈换手率/平均换手率，假设平均换手率2%）
                        "volume_ratio": max(1.0, float(s.get("turnoverratio", 0)) / 2) if s.get("turnoverratio") else 2.0,
                        "auction_amount_pct": 0.03,
                        "score": 0,
                    })

                except Exception as e:
                    continue

            # 如果已经找到足够候选，提前结束
            if len(candidates) >= 50:
                break

        print(f"  篛选后候选股票: {len(candidates)}只", flush=True)

        # 如果候选太少，再从成交量榜补充
        if len(candidates) < 20:
            print("  候选较少，从成交量榜补充...", flush=True)

            params2 = {
                "page": 1,
                "num": 100,
                "sort": "amount",  # 按成交额排序
                "asc": 0,
                "node": "hs_a",
            }

            resp2 = session.get(sina_url, params=params2, headers=headers, timeout=15)
            if resp2.status_code == 200:
                stocks2 = resp2.json()
                if isinstance(stocks2, list):
                    for s in stocks2:
                        try:
                            code = s.get("code", "")
                            name = s.get("name", "")

                            # 避免重复
                            if any(c['code'] == code for c in candidates):
                                continue

                            if code.startswith("688"):
                                continue
                            if code.startswith("8") and not code.startswith("30"):
                                continue
                            if code.startswith("4"):
                                continue
                            if "ST" in name or "退" in name or "*" in name:
                                continue

                            # 放宽所有条件
                            change_pct = float(s.get("changepercent", 0)) / 100
                            if change_pct < -0.08 or change_pct > 0.095:
                                continue

                            circ_cap = float(s.get("nmc", 0)) / 10000
                            if circ_cap < 15 or circ_cap > 600:
                                continue

                            amount = float(s.get("amount", 0)) / 100000000
                            if amount < 0.5 or amount > 80:
                                continue

                            candidates.append({
                                "code": code,
                                "name": name,
                                "price": float(s.get("trade", 0)) if s.get("trade") else 0,
                                "gap_pct": 0,
                                "change_pct": change_pct,
                                "circ_cap": circ_cap,
                                "amount": amount,
                                # 换手率
                                "turnover_ratio": float(s.get("turnoverratio", 0)) if s.get("turnoverratio") else 0,
                                # 量比估算
                                "volume_ratio": max(1.0, float(s.get("turnoverratio", 0)) / 2) if s.get("turnoverratio") else 2.0,
                                "auction_amount_pct": 0.03,
                                "score": 0,
                            })
                        except:
                            continue

                    print(f"  补充后总数: {len(candidates)}只", flush=True)

        return candidates

    except Exception as e:
        print(f"获取候选池失败: {e}", flush=True)
        import traceback
        traceback.print_exc()
        return []


@app.route('/auction_pick')
def auction_pick():
    """竞价选股页面"""
    return render_template('auction_pick.html')


@app.route('/api/auction_pick')
def api_auction_pick():
    """竞价选股API"""
    global AUCTION_PICK_DATA

    # 检查是否需要刷新
    now = datetime.now()
    current_hour = now.hour
    current_minute = now.minute

    # 竞价时段: 9:25-9:30
    is_auction_time = (current_hour == 9 and current_minute >= 25) or \
                      (current_hour == 9 and current_minute < 35)

    # 检查缓存是否过期（超过1小时刷新）
    last_update = AUCTION_PICK_DATA.get('last_update')
    need_refresh = False

    if last_update:
        try:
            last_dt = datetime.strptime(last_update, '%Y-%m-%d %H:%M:%S')
            if (now - last_dt).total_seconds() > 3600:
                need_refresh = True
        except:
            need_refresh = True
    else:
        need_refresh = True

    # 如果在竞价时段或需要刷新，执行选股
    if is_auction_time or need_refresh:
        execute_auction_pick()

    with AUCTION_PICK_LOCK:
        data = AUCTION_PICK_DATA.copy()

    return jsonify({
        "success": True,
        "stocks": data.get('stocks', []),
        "pick_time": data.get('pick_time'),
        "last_update": data.get('last_update'),
        "market_info": data.get('market_info', {}),
        "candidate_count": len(data.get('candidate_pool', [])),
    })


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
                except:
                    pass
        except Exception as e:
            print(f"  新浪接口失败: {e}", flush=True)

        # 如果新浪也失败，返回空
        return candidates

    except Exception as e:
        print(f"腾讯备用接口失败: {e}", flush=True)
        import traceback
        traceback.print_exc()
        return []


@app.route('/api/auction_preselect')
def api_auction_preselect():
    """预选股票池API - 执行第一阶段筛选"""
    global AUCTION_PICK_DATA

    try:
        # 执行预选
        candidates = get_auction_candidates()

        # 更新缓存
        with AUCTION_PICK_LOCK:
            AUCTION_PICK_DATA['candidate_pool'] = candidates
            AUCTION_PICK_DATA['last_update'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            save_auction_pick_cache()

        return jsonify({
            "success": True,
            "candidates": candidates,
            "count": len(candidates),
            "last_update": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({
            "success": False,
            "error": str(e),
            "candidates": [],
        })


if __name__ == '__main__':
    print("\n🏛️ 价值投资之王 · 智能选股可视化网站 v16-ROUND4-OPTIMAL")
    print("   访问 http://localhost:5559")
    print("   每日推荐: 自动选股 9:27(早盘) / 14:30(午盘)")

    # 加载缓存
    load_daily_pick_cache()
    load_wp2_pick_cache()

    # 启动定时任务
    start_scheduler()

    # 注册退出时保存
    atexit.register(save_daily_pick_cache)

    app.run(host='0.0.0.0', port=5559, debug=False)
