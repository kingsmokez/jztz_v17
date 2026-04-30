# -*- coding: utf-8 -*-
"""
Value Investment King - Smart Stock Selection System v17-MODULAR
Flask Backend - 路由入口（模块化重构版）

模块结构:
  modules/config.py        - 全局配置、常量、行业PE配置
  modules/http_client.py   - HTTP会话、请求头
  modules/data_fetcher.py  - 数据获取（行情、财务、行业）
  modules/scoring.py       - 多因子评分系统
  modules/technical.py     - 技术面筛选模块
  modules/stock_picker.py  - 选股核心逻辑 + 板块轮动
  modules/cache_manager.py - 缓存管理（每日推荐、竞价、尾盘）
  modules/scheduler.py     - 定时任务
  modules/auction_picker.py- 竞价选股模块
  modules/wp2_picker.py    - 尾盘选股模块
  modules/news.py          - 新闻热点模块
"""

import json
import os
import re
import time
import threading
import atexit
import traceback
from datetime import datetime, timedelta

from flask import Flask, render_template, request, jsonify

from modules.config import (
    BASE_DIR, FLASK_PORT, FLASK_HOST, _cache,
    DAILY_PICK_FILE, WP2_PICK_FILE, AUCTION_PICK_FILE,
    LIQUOR_NAMES, BANK_CODES,
)
from modules.http_client import session, HEADERS, EM_HEADERS, DC_HEADERS
from modules.data_fetcher import (
    get_realtime_quotes, get_financial_data, get_financial_data_fast,
    get_preset_financials, get_stock_industry, fetch_sina_sectors,
)
from modules.scoring import (
    evaluate_stock, calculate_buy_sell, multi_factor_evaluate,
    get_hot_sectors_and_news, calculate_hot_factor,
)
from modules.technical import calculate_technical_indicators
from modules.stock_picker import run_picker
from modules.cache_manager import (
    DAILY_PICK_DATA, DAILY_PICK_LOCK,
    WP2_PICK_DATA, WP2_PICK_LOCK,
    AUCTION_PICK_DATA, AUCTION_PICK_LOCK,
    load_daily_pick_cache, save_daily_pick_cache,
    load_wp2_pick_cache, save_wp2_pick_cache,
    load_auction_pick_cache, save_auction_pick_cache,
    load_all_caches, save_all_caches,
)
from modules.scheduler import (
    execute_daily_pick, start_scheduler, auto_auction_preselect,
)
from modules.auction_picker import (
    execute_auction_pick, get_market_status, get_auction_candidates,
    get_candidates_from_tencent,
)
from modules.wp2_picker import execute_wp2_pick
from modules.news import get_sector_news

app = Flask(__name__, static_folder='static', template_folder='templates')
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.jinja_env.auto_reload = True
app.jinja_env.cache = {}

@app.before_request
def log_request_info():
    from datetime import datetime
    ip = request.remote_addr
    path = request.path
    now = datetime.now().strftime('%H:%M:%S')
    if not path.startswith('/static') and path != '/favicon.ico':
        print(f"  📥 [{now}] {ip} -> {path}", flush=True)

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
            # debt_ratio 和 net_margin 已在 run_picker() 中校准，无需重复获取
            if 'debt_ratio' not in r:
                r['debt_ratio'] = 0
            if 'net_margin' not in r:
                r['net_margin'] = 0
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
    """获取全市场概览（60秒缓存）"""
    _market_cache_key = 'market_overview'
    if _market_cache_key in _cache and time.time() - _cache[_market_cache_key][0] < 60:
        return jsonify(_cache[_market_cache_key][1])
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

    result = {
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
    }
    _cache[_market_cache_key] = (time.time(), result)
    return jsonify(result)


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
                                    "market_cap": total_cap_yi,  # 腾讯API parts[44]已是亿元单位
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

        # 5. 批量获取财务数据（并发，避免逐只串行请求）
        from concurrent.futures import ThreadPoolExecutor, as_completed
        preset = get_preset_financials()

        def fetch_fin_for_stock(stock):
            code = stock["code"]
            fin_data = get_financial_data_fast(code)
            if fin_data:
                for key in ["roe", "gross_margin", "rev_growth", "profit_growth", "debt_ratio", "net_margin"]:
                    if fin_data.get(key, 0) != 0:
                        stock[key] = fin_data[key]
            stock.setdefault("net_margin", 0)
            stock.setdefault("debt_ratio", 0)
            if code in preset:
                fin = preset[code]
                for k in ["roe", "gross_margin", "net_margin", "rev_growth", "profit_growth", "pb"]:
                    if stock.get(k, 0) == 0 and fin.get(k):
                        stock[k] = fin[k]
            return stock

        with ThreadPoolExecutor(max_workers=5) as executor:
            list(executor.map(fetch_fin_for_stock, matched_stocks))

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

        # 7. 批量获取行业信息
        from concurrent.futures import ThreadPoolExecutor
        def fetch_industry(stock):
            try:
                info = get_stock_industry(stock['code'])
                stock['industry'] = info.get('industry', '未知')
                return stock
            except:
                stock['industry'] = '未知'
                return stock

        with ThreadPoolExecutor(max_workers=5) as executor:
            list(executor.map(fetch_industry, results))

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


@app.route('/api/daily_pick_run', methods=['POST'])
def api_daily_pick_run():
    """手动执行选股 - 用户点击按钮触发"""
    session_type = request.json.get('session_type', 'morning')
    if session_type not in ['morning', 'afternoon']:
        return jsonify({"success": False, "error": "无效的选股时段"})

    try:
        execute_daily_pick(session_type)
        return jsonify({
            "success": True,
            "message": f"{'早盘' if session_type == 'morning' else '午盘'}选股已完成",
            "session_type": session_type
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


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
    for sess in ['morning', 'afternoon']:
        if data.get(sess) and isinstance(data.get(sess), dict) and data.get(sess).get('results'):
            for stock in data[sess]['results']:
                if 'debt_ratio' not in stock:
                    stock['debt_ratio'] = 0
                # 补充行业信息
                if 'industry' not in stock or stock.get('industry') == '未知':
                    try:
                        info = get_stock_industry(stock['code'])
                        stock['industry'] = info.get('industry', '未知')
                    except:
                        stock['industry'] = '未知'

    return jsonify({
        "success": True,
        "date": data.get('date', datetime.now().strftime('%Y-%m-%d')),
        "morning": data.get('morning'),
        "afternoon": data.get('afternoon'),
        "last_update": data.get('last_update'),

    })




def _fetch_stock_quote(code):
    """获取单只股票实时行情（腾讯API）"""
    stock_info = None
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
                total_cap_yi = float(parts[44]) if parts[44] and parts[44] != '-' else 0
                stock_info = {
                    "code": code, "name": parts[1], "price": price,
                    "change_pct": float(parts[32]) if parts[32] else 0,
                    "high": float(parts[33]) if parts[33] else 0,
                    "low": float(parts[34]) if parts[34] else 0,
                    "open": float(parts[5]) if parts[5] else 0,
                    "prev_close": float(parts[4]) if parts[4] else 0,
                    "volume": float(parts[37]) if parts[37] else 0,
                    "amount": float(parts[43]) * 10000 if parts[43] else 0,
                    "pe": pe_val, "pb": pb_val, "turnover_rate": turnover_val,
                    "roe": 0, "gross_margin": 0, "net_margin": 0,
                    "rev_growth": 0, "profit_growth": 0, "debt_ratio": 0,
                    "market_cap": total_cap_yi,
                }
                break
            except Exception:
                continue
    except Exception as e:
        print(f"  行情获取失败: {e}", flush=True)
    return stock_info


def _enrich_financial_data(stock_info, code):
    """补充财务数据（实时API -> 备用API -> 离线库）"""
    if not stock_info:
        return
    fin_data = get_financial_data(code)
    if fin_data:
        for key in ["roe", "gross_margin", "rev_growth", "profit_growth", "debt_ratio", "net_margin", "pb"]:
            if fin_data.get(key, 0) != 0:
                stock_info[key] = fin_data[key]
    else:
        fin_data_fast = get_financial_data_fast(code)
        if fin_data_fast:
            for key in ["roe", "gross_margin", "debt_ratio"]:
                if fin_data_fast.get(key, 0) != 0:
                    stock_info[key] = fin_data_fast[key]

    if stock_info.get("pe", 0) == 0 and stock_info.get("pb", 0) > 0 and stock_info.get("roe", 0) > 0:
        stock_info["pe"] = round(stock_info["pb"] / (stock_info["roe"] / 100), 1)

    if stock_info.get("roe", 0) == 0 and stock_info.get("gross_margin", 0) == 0:
        preset_data = get_preset_financials()
        if code in preset_data:
            preset = preset_data[code]
            for key in ["roe", "gross_margin", "net_margin", "rev_growth", "profit_growth", "debt_ratio"]:
                if stock_info.get(key, 0) == 0 and preset.get(key, 0) != 0:
                    stock_info[key] = preset[key]


def _build_analysis_detail(stock_info, dimensions):
    """构建分析详情（供详情页展示）"""
    analysis = []
    roe = stock_info.get("roe", 0)
    gross_margin = stock_info.get("gross_margin", 0)
    net_margin = stock_info.get("net_margin", 0)
    rev_growth = stock_info.get("rev_growth", 0)
    profit_growth = stock_info.get("profit_growth", 0)
    pe = stock_info.get("pe", 0)
    pb = stock_info.get("pb", 0)
    debt_ratio = stock_info.get("debt_ratio", 0)

    # 盈利能力
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

    # 成长性
    avg_growth = max(rev_growth, profit_growth) if rev_growth > 0 and profit_growth > 0 else (rev_growth if rev_growth > 0 else profit_growth)
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

    # 财务健康
    if debt_ratio > 0 and debt_ratio < 1000:
        if debt_ratio <= 50:
            analysis.append({"dim": "财务健康", "score": 20, "max": 20, "detail": f"资产负债率 {debt_ratio:.1f}% 优秀（≤50%）", "level": "excellent"})
        elif debt_ratio <= 70:
            analysis.append({"dim": "财务健康", "score": 12, "max": 20, "detail": f"资产负债率 {debt_ratio:.1f}% 一般（≤70%）", "level": "fair"})
        else:
            analysis.append({"dim": "财务健康", "score": 5, "max": 20, "detail": f"资产负债率 {debt_ratio:.1f}% 偏高", "level": "poor"})
    else:
        analysis.append({"dim": "财务健康", "score": 10, "max": 20, "detail": "资产负债率数据缺失，给中等分", "level": "unknown"})

    # 估值
    if pe > 0 and pe < 1000:
        if pe <= 12:
            analysis.append({"dim": "估值", "score": round(dimensions["valuation"]), "max": 20, "detail": f"PE {pe:.1f} 低估（≤15）", "level": "excellent"})
        elif pe <= 20:
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

    # 现金流质量
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
            else:
                cashflow_score = 1
                cashflow_detail = f"ROE {roe:.1f}% 盈利尚可"
        else:
            cashflow_score = 1
            cashflow_detail = f"盈利中 ROE {roe:.1f}%待提升"
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

    return analysis


def _fetch_stock_news(stock_name):
    """获取相关新闻"""
    stock_news = []
    try:
        r = session.get("https://feed.mix.sina.com.cn/api/roll/get",
                         params={"pageid": "153", "lid": "2509", "k": "", "r": "0.5", "page": 1},
                         headers=HEADERS, timeout=10)
        d = r.json()
        if d.get('result') and d['result'].get('data'):
            for item in d['result']['data'][:30]:
                title = item.get('title', '')
                intro = item.get('intro', '') or ''
                text = title + ' ' + intro
                if stock_name in text:
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
    return stock_news[:8]

@app.route('/api/stock_detail')
def api_stock_detail():
    """获取单个股票详情 + 相关新闻 - 实时拉取数据（重构版）"""
    code = request.args.get("code", "")
    if not code:
        return jsonify({"success": False, "error": "缺少股票代码"}), 400

    print(f"获取股票详情: {code}", flush=True)

    # === 1. 获取实时行情 ===
    stock_info = _fetch_stock_quote(code)
    if stock_info:
        print(f"  行情: {stock_info['name']} 价格={stock_info['price']}, 市值={stock_info['market_cap']}亿", flush=True)

    # === 2. 补充财务数据 ===
    _enrich_financial_data(stock_info, code)

    if not stock_info:
        return jsonify({"success": False, "error": "股票不存在或无法获取数据"}), 404

    # === 3. 计算评分 ===
    print(f"  开始计算评分...", flush=True)
    tech_data = None
    try:
        tech_data = calculate_technical_indicators(code, days=30)
    except Exception as e:
        print(f"  技术指标获取失败: {e}", flush=True)

    eval_result = evaluate_stock(stock_info, tech_data=tech_data)

    if not eval_result:
        _name = stock_info.get("name", "")
        is_excluded = any(n in _name for n in LIQUOR_NAMES) or code in BANK_CODES
        if is_excluded:
            return jsonify({"success": False, "error": "该股票属于白酒/银行板块，不在评估范围内", "stock": stock_info}), 200
        return jsonify({"success": False, "error": "股票评分计算失败"}), 404

    score = eval_result.get("score", 0)
    v5_score = eval_result.get("v5_score", 0)
    v5_factors = eval_result.get("v5_factors", {})
    v5_reasons = eval_result.get("v5_reasons", [])
    v5_rec = eval_result.get("v5_recommendation", "")
    dimensions = eval_result.get("dimensions", {})
    buy_sell = eval_result.get("buy_sell")
    reasons = eval_result.get("reasons", [])
    print(f"  评分完成: score={score}, v5_score={v5_score}", flush=True)

    # === 4. 构建分析详情 ===
    analysis = _build_analysis_detail(stock_info, dimensions)
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

    # === 5. 获取相关新闻 ===
    stock_news = _fetch_stock_news(stock_info.get("name", ""))

    # 市值已经是亿元单位，无需转换
    raw_mc = stock_info.get("market_cap", 0)
    print(f"  [DEBUG] 返回前市值: {raw_mc}亿", flush=True)

    # === 5. 行业信息已由 evaluate_stock -> mf_score_value 获取并写入 stock_info ===
    if not stock_info.get("industry"):
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
    offline_path = os.path.join(BASE_DIR, 'offline_stocks.json')
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
        "progress": data.get('progress', ''),
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


@app.route('/wp2_pick')
def wp2_pick():
    """尾盘选股页面"""
    return render_template('wp2_pick.html')


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

    # 只在竞价时段执行选股，非交易时间直接返回缓存
    if is_auction_time:
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
        "preselect_time": data.get('preselect_time'),
        "confirm_time": data.get('confirm_time'),
    })


@app.route('/api/auction_confirm')
def api_auction_confirm():
    """第二阶段竞价确认API - 只执行竞价四维确认筛选"""
    global AUCTION_PICK_DATA

    try:
        now = datetime.now()
        print(f"⏰ 执行竞价确认（第二阶段）... {now.strftime('%H:%M:%S')}", flush=True)

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
            return jsonify({
                "success": True,
                "stocks": [],
                "pick_time": now.strftime('%H:%M:%S'),
                "market_info": market_info,
                "message": "大盘环境不佳，不开新仓"
            })

        # 3. 使用缓存的候选池（预选阶段已筛选），同时刷新实时行情数据
        candidates = AUCTION_PICK_DATA.get('candidate_pool', [])
        if not candidates:
            candidates = get_auction_candidates()
        else:
            # 刷新候选池的实时行情数据（gap_pct等）
            refreshed = get_auction_candidates()
            if refreshed:
                candidates = refreshed
        print(f"  候选池: {len(candidates)}只", flush=True)

        if not candidates:
            return jsonify({
                "success": True,
                "stocks": [],
                "pick_time": now.strftime('%H:%M:%S'),
                "market_info": market_info,
                "message": "无候选股票，请先执行第一阶段预选"
            })

        # 4. 竞价确认筛选（第二阶段核心逻辑）
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

        # 5. 按评分排序，最多取5只
        confirmed_stocks.sort(key=lambda x: x.get('score', 0), reverse=True)
        confirmed_stocks = confirmed_stocks[:5]

        with AUCTION_PICK_LOCK:
            AUCTION_PICK_DATA['stocks'] = confirmed_stocks
            AUCTION_PICK_DATA['pick_time'] = now.strftime('%H:%M:%S')
            AUCTION_PICK_DATA['last_update'] = now.strftime('%Y-%m-%d %H:%M:%S')
            AUCTION_PICK_DATA['confirm_time'] = now.strftime('%Y-%m-%d %H:%M:%S')
            AUCTION_PICK_DATA['market_info'] = market_info
            if not AUCTION_PICK_DATA.get('candidate_pool'):
                AUCTION_PICK_DATA['candidate_pool'] = candidates
            save_auction_pick_cache()

        print(f"  ✓ 竞价确认: {len(confirmed_stocks)}只股票", flush=True)

        return jsonify({
            "success": True,
            "stocks": confirmed_stocks,
            "pick_time": now.strftime('%H:%M:%S'),
            "last_update": now.strftime('%Y-%m-%d %H:%M:%S'),
            "market_info": market_info,
            "candidate_count": len(candidates),
        })

    except Exception as e:
        print(f"  ✗ 竞价确认失败: {e}", flush=True)
        import traceback
        traceback.print_exc()
        return jsonify({
            "success": False,
            "error": str(e),
            "stocks": []
        })


@app.route('/api/auction_preselect')
def api_auction_preselect():
    """预选股票池API - 执行第一阶段筛选
    
    时间限制：只能在收盘后 15:30~22:00 执行
    如果当天已在 21:30 前执行过，则不允许重复执行
    """
    global AUCTION_PICK_DATA

    try:
        now = datetime.now()
        current_hour = now.hour
        current_minute = now.minute
        current_time_val = current_hour * 60 + current_minute

        # 时间校验：只能在 15:30~22:00 之间执行
        if current_time_val < 15 * 60 + 30 or current_time_val > 22 * 60:
            return jsonify({
                "success": False,
                "error": "预选只能在收盘后 15:30~22:00 之间执行",
                "candidates": AUCTION_PICK_DATA.get('candidate_pool', []),
                "count": len(AUCTION_PICK_DATA.get('candidate_pool', [])),
                "preselect_time": AUCTION_PICK_DATA.get('preselect_time'),
                "last_update": AUCTION_PICK_DATA.get('last_update'),
            })

        # 检查今天是否已执行过预选（当天执行过就不允许重复）
        preselect_time_str = AUCTION_PICK_DATA.get('preselect_time')
        if preselect_time_str:
            try:
                preselect_dt = datetime.strptime(preselect_time_str, '%Y-%m-%d %H:%M:%S')
                if preselect_dt.date() == now.date():
                    return jsonify({
                        "success": False,
                        "error": "今天已执行过预选，无需重复执行",
                        "candidates": AUCTION_PICK_DATA.get('candidate_pool', []),
                        "count": len(AUCTION_PICK_DATA.get('candidate_pool', [])),
                        "preselect_time": preselect_time_str,
                        "last_update": AUCTION_PICK_DATA.get('last_update'),
                    })
            except ValueError:
                pass

        # 执行预选
        candidates = get_auction_candidates()

        # 更新缓存
        with AUCTION_PICK_LOCK:
            AUCTION_PICK_DATA['candidate_pool'] = candidates
            AUCTION_PICK_DATA['preselect_time'] = now.strftime('%Y-%m-%d %H:%M:%S')
            AUCTION_PICK_DATA['last_update'] = now.strftime('%Y-%m-%d %H:%M:%S')
            save_auction_pick_cache()

        return jsonify({
            "success": True,
            "candidates": candidates,
            "count": len(candidates),
            "preselect_time": now.strftime('%Y-%m-%d %H:%M:%S'),
            "last_update": now.strftime('%Y-%m-%d %H:%M:%S'),
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({
            "success": False,
            "error": str(e),
            "candidates": [],
        })


@app.route('/api/auction_status')
def api_auction_status():
    """查询竞价选股状态（前端用于判断按钮是否可用）"""
    global AUCTION_PICK_DATA

    now = datetime.now()
    current_hour = now.hour
    current_minute = now.minute
    current_time_val = current_hour * 60 + current_minute

    in_preselect_window = 15 * 60 + 30 <= current_time_val <= 22 * 60

    preselect_time_str = AUCTION_PICK_DATA.get('preselect_time')
    preselect_done_today = False
    if preselect_time_str:
        try:
            preselect_dt = datetime.strptime(preselect_time_str, '%Y-%m-%d %H:%M:%S')
            preselect_done_today = preselect_dt.date() == now.date()
        except ValueError:
            pass

    can_preselect = in_preselect_window and not preselect_done_today

    return jsonify({
        "success": True,
        "can_preselect": can_preselect,
        "in_preselect_window": in_preselect_window,
        "preselect_done_today": preselect_done_today,
        "preselect_time": preselect_time_str,
        "confirm_time": AUCTION_PICK_DATA.get('confirm_time'),
        "candidate_count": len(AUCTION_PICK_DATA.get('candidate_pool', [])),
    })


@app.route('/api/auction_candidate_pool')
def api_auction_candidate_pool():
    """获取缓存的候选池数据（不触发新的预选）"""
    global AUCTION_PICK_DATA
    candidates = AUCTION_PICK_DATA.get('candidate_pool', [])
    return jsonify({
        "success": True,
        "candidates": candidates,
        "count": len(candidates),
        "preselect_time": AUCTION_PICK_DATA.get('preselect_time'),
        "last_update": AUCTION_PICK_DATA.get('last_update'),
    })




# ========== 启动入口 ==========

if __name__ == '__main__':
    print("\n🏛️ 价值投资之王 · 智能选股可视化网站 v16-ROUND4-OPTIMAL")
    print("   访问 http://localhost:5559")
    print("   每日推荐: 自动选股 9:27(早盘) / 14:30(午盘)")

    # 加载缓存
    load_daily_pick_cache()
    load_wp2_pick_cache()
    load_auction_pick_cache()

    # 启动定时任务
    start_scheduler()

    # 注册退出时保存
    atexit.register(save_daily_pick_cache)
    atexit.register(save_auction_pick_cache)
    atexit.register(save_wp2_pick_cache)

    app.run(host='0.0.0.0', port=5559, debug=False)


