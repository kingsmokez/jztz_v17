# -*- coding: utf-8 -*-
"""选股核心逻辑 - 板块轮动 + 热门股票 + 全市场扫描 + 主流程"""

import time
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import traceback

from .config import STOCK_SECTOR_MAP, SECTOR_PE_RANGES
from .http_client import session, HEADERS
from .data_fetcher import (
    get_realtime_quotes, get_financial_data_fast, get_preset_financials,
    fetch_sina_sectors, get_stock_industry,
)
from .scoring import (
    evaluate_stock, calculate_buy_sell, multi_factor_evaluate,
    get_hot_sectors_and_news, calculate_hot_factor,
    detect_strategy, STRATEGY_WEIGHTS, STRATEGY_LABELS,
)
from .technical import calculate_technical_indicators

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


def run_picker():
    """执行选股主流程 - 全市场扫描 + 热点因子 + 技术面 + 板块轮动"""
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
        print(f"\n🔍 基础过滤（换手率>0.3%，非ST，非新股）...", flush=True)
        filtered_pool = []
        for stock in stock_pool:
            turnover = stock.get('turnover_rate', 0)
            name = stock.get('name', '')
            # 换手率>0.3% 或 数据缺失(turnover=0)，排除ST、退市股、新股
            if (turnover >= 0.3 or turnover == 0):
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
    tech_cache = {}

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

    # === 8. 动态排序（v5.2: 分策略排序，短线优先展示）===
    for r in results:
        change = r.get("change_pct", 0)
        if change > 0:
            change_adj = min(change * 0.5, 3)
        else:
            change_adj = max(change * 0.3, -2)
        r["_final_score"] = r["score"] + change_adj
        r["_v5_final"] = r.get("v5_score", 0)

    # 分策略排序: 短线/中短线/长线各自排序，然后合并
    short_term_results = [r for r in results if r.get('strategy') == 'short_term']
    swing_results = [r for r in results if r.get('strategy') == 'swing']
    long_term_results = [r for r in results if r.get('strategy') == 'long_term']

    short_term_results.sort(key=lambda x: x.get("v5_score", 0), reverse=True)
    swing_results.sort(key=lambda x: x.get("v5_score", 0), reverse=True)
    long_term_results.sort(key=lambda x: x.get("v5_score", 0), reverse=True)

    # 去重：swing股票不与short_term重复
    short_codes = {r['code'] for r in short_term_results}
    swing_deduped = [r for r in swing_results if r['code'] not in short_codes]
    long_codes = {r['code'] for r in long_term_results}
    swing_deduped = [r for r in swing_deduped if r['code'] not in long_codes]

    # 合并: 短线取前10, 中短线取前20(去重后), 长线取前20
    merged_results = short_term_results[:10] + swing_deduped[:20] + long_term_results[:20]

    # 剩余按v5_score排序补充到50
    merged_codes = {r['code'] for r in merged_results}
    remaining = [r for r in results if r['code'] not in merged_codes]
    remaining.sort(key=lambda x: x.get("v5_score", 0), reverse=True)
    merged_results += remaining

    results = merged_results

    # === 9. 结果处理 ===
    max_hot_count = min(int(len(results) * 0.3), len(hot_stock_results))

    for r in results:
        r.pop("_final_score", None)
        r["score"] = round(r["score"], 1)
        if "debt_ratio" not in r:
            r["debt_ratio"] = 0
        if r.get("buy_sell"):
            r["buy_sell"]["buy"] = round(r["buy_sell"]["buy"], 2)
            r["buy_sell"]["sell"] = round(r["buy_sell"]["sell"], 2)
            r["buy_sell"]["current"] = round(r["buy_sell"]["current"], 2)

    # 精选Top 5: 短线取前2 + 中短线取前2(去重后) + 长线取前1
    top5_short = short_term_results[:2]
    top5_swing_codes = {r['code'] for r in top5_short}
    top5_swing = [r for r in swing_deduped[:5] if r['code'] not in top5_swing_codes][:2]
    top5_long_codes = {r['code'] for r in top5_short} | {r['code'] for r in top5_swing}
    top5_long = [r for r in long_term_results[:3] if r['code'] not in top5_long_codes][:1]
    top5 = top5_short + top5_swing + top5_long

    final_results = results[:50]

    # === 10. 批量补充行业信息（新增）===
    print("\n🏭 批量获取行业信息...", flush=True)

    def fetch_industry_for_stock(stock):
        """获取单只股票的行业信息"""
        try:
            info = get_stock_industry(stock['code'])
            stock['industry'] = info.get('industry', '未知')
            stock['sector_type'] = info.get('sector_type', 'default')
            return stock['code'], info
        except Exception:
            return stock['code'], None

    # 并发获取前50只股票的行业信息
    start_time = time.time()
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(fetch_industry_for_stock, r) for r in final_results]
        for future in as_completed(futures):
            try:
                code, info = future.result(timeout=10)
                if info:
                    # 更新stock中的行业信息
                    for r in final_results:
                        if r['code'] == code:
                            r['industry'] = info.get('industry', '未知')
                            break
            except Exception:
                pass

    elapsed = time.time() - start_time
    print(f"  ✓ 行业信息获取完成，耗时 {elapsed:.1f}s", flush=True)

    # 在第一个结果中附带精选Top 5信息
    if final_results:
        final_results[0]['_total_scanned'] = total_scanned
        # 只存代码和分数，避免循环引用
        final_results[0]['_top5_codes'] = [r['code'] for r in top5]
        final_results[0]['_top5_scores'] = [round(r.get('v5_score', 0), 2) for r in top5]

    print(f"\n{'='*50}", flush=True)
    print(f"选股完成!", flush=True)
    print(f"  扫描股票: {total_scanned} 只", flush=True)
    print(f"  符合条件: {len(results)} 只", flush=True)
    print(f"  返回结果: {len(final_results)} 只", flush=True)
    print(f"  热门股票: {len(hot_stock_results)} 只", flush=True)
    print(f"  短线强势: {len(short_term_results)} 只 (Top3: {[r['name'] for r in short_term_results[:3]]})", flush=True)
    print(f"  中短线波段: {len(swing_results)} 只 (Top3: {[r['name'] for r in swing_results[:3]]})", flush=True)
    print(f"  长线价值: {len(long_term_results)} 只 (Top3: {[r['name'] for r in long_term_results[:3]]})", flush=True)
    print(f"{'='*50}\n", flush=True)

    return final_results
