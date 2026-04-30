# -*- coding: utf-8 -*-
"""数据获取模块 - 行情、财务、行业、板块数据"""

import os
import json
import re
import time
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

from .config import (
    BASE_DIR, SECTOR_PE_RANGES,
    INDUSTRY_CACHE, INDUSTRY_CACHE_TIME, INDUSTRY_CACHE_TTL,
    _cache, _cache_preset_financials, _cache_preset_financials_time,
)
from .http_client import session, HEADERS, EM_HEADERS, DC_HEADERS

# TODO: fetch_batch

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
            except Exception:
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
        except Exception:
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
                    except Exception:
                        price = 0
                    if price <= 0:
                        continue
                    try:
                        change_pct = float(parts[32]) if parts[32] else 0
                    except Exception:
                        change_pct = 0
                    try:
                        pe = float(parts[39]) if parts[39] and parts[39] != '-' else 0
                        if pe > 10000 or pe < 0: pe = 0
                    except Exception:
                        pe = 0
                    try:
                        # 腾讯API: parts[44]=总市值，单位是"亿元"（已验证）
                        total_cap_yi = float(parts[44]) if parts[44] else 0
                    except Exception:
                        total_cap_yi = 0
                    try:
                        amount_wan = float(parts[43]) if parts[43] else 0
                    except Exception:
                        amount_wan = 0
                    try:
                        high = float(parts[33]) if parts[33] else 0
                    except Exception:
                        high = 0
                    try:
                        low = float(parts[34]) if parts[34] else 0
                    except Exception:
                        low = 0
                    try:
                        open_p = float(parts[5]) if parts[5] else 0
                    except Exception:
                        open_p = 0
                    try:
                        prev_close = float(parts[4]) if parts[4] else 0
                    except Exception:
                        prev_close = 0
                    try:
                        volume_gu = float(parts[37]) if parts[37] else 0
                    except Exception:
                        volume_gu = 0
                    try:
                        turnover_rate = float(parts[38]) if parts[38] else 0
                    except Exception:
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


def get_stock_industry(code):
    """获取股票所属行业（从东方财富API）
    
    返回: {'industry': '半导体', 'sector_type': 'semiconductor', ...}
    """
    # 检查缓存（含过期机制）
    if code in INDUSTRY_CACHE:
        cache_time = INDUSTRY_CACHE_TIME.get(code, 0)
        if time.time() - cache_time < INDUSTRY_CACHE_TTL:
            return INDUSTRY_CACHE[code]
        else:
            INDUSTRY_CACHE.pop(code, None)
            INDUSTRY_CACHE_TIME.pop(code, None)
    
    result = {'industry': '未知', 'sector_type': 'default', 'pe_fair_max': 30, 'pe_fair_low': 15}
    
    try:
        market = 'SH' if code.startswith('6') else 'SZ'
        url = f'https://emweb.eastmoney.com/PC_HSF10/CompanySurvey/CompanySurveyAjax?code={market}{code}'
        resp = session.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)  # timeout改为10秒
        data = resp.json()
        
        if 'jbzl' in data and data['jbzl']:
            jbzl = json.loads(data['jbzl']) if isinstance(data['jbzl'], str) else data['jbzl']
            if not jbzl:
                return result
            industry = jbzl.get('sshy', '') if isinstance(jbzl, dict) else ''
            name = jbzl.get('agjc', '') if isinstance(jbzl, dict) else ''
            
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
                
                # 缓存结果（含时间戳）
                INDUSTRY_CACHE[code] = result
                INDUSTRY_CACHE_TIME[code] = time.time()
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
    except Exception:
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
    except Exception:
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
    except Exception:
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
    except Exception:
        pass

    # 至少有一个有效数据才返回
    if result['roe'] != 0 or result['rev_growth'] != 0 or result['profit_growth'] != 0 or result['debt_ratio'] != 0:
        return result
    return None


def get_preset_financials():
    """预设高质量中小盘股票财务数据 - 从离线数据库加载（带缓存）"""
    global _cache_preset_financials, _cache_preset_financials_time
    offline_path = os.path.join(BASE_DIR, 'offline_stocks.json')
    # 缓存5分钟
    if _cache_preset_financials is not None and time.time() - _cache_preset_financials_time < 300:
        if os.path.exists(offline_path) and os.path.getmtime(offline_path) < _cache_preset_financials_time:
            return _cache_preset_financials
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
                _cache_preset_financials = result
                _cache_preset_financials_time = time.time()
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
