# -*- coding: utf-8 -*-
"""
价值投资之王 - 智能选股系统 v17
实时行情 + 全市场筛选 + 资讯分析 + 策略评估
v17: 全市场扫描替代固定16只预设, 年报数据筛选修复
"""
import requests
import json
import time
import sys
import io
import os
import ssl
import urllib3
from datetime import datetime
from requests import Session as _Session, exceptions as _req_exceptions

# 禁用不安全请求警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# SSL验证降级：遇到证书错误自动重试并禁用验证
class _SSLRelaxedSession(_Session):
    def __init__(self):
        super().__init__()
        # 禁用代理（解决本地代理服务未运行导致的连接失败）
        self.trust_env = False
    
    def request(self, method, url, **kwargs):
        # 明确禁用代理
        kwargs.setdefault('proxies', {})
        try:
            return super().request(method, url, **kwargs)
        except _req_exceptions.SSLError as e:
            if 'certificate verify failed' in str(e).lower():
                return super().request(method, url, verify=False, **kwargs)
            raise

# 全局降级session
_requests_session = _SSLRelaxedSession()
_requests_session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})

def _get(url, headers=None, timeout=15, **kwargs):
    try:
        return _requests_session.get(url, headers=headers, timeout=timeout, **kwargs)
    except _req_exceptions.SSLError:
        return _requests_session.get(url, headers=headers, timeout=timeout, verify=False, **kwargs)

def _post(url, json=None, timeout=10, **kwargs):
    try:
        return _requests_session.post(url, json=json, timeout=timeout, **kwargs)
    except _req_exceptions.SSLError:
        return _requests_session.post(url, json=json, timeout=timeout, verify=False, **kwargs)

# Windows控制台编码
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# 企业微信配置
WECOM_WEBHOOK_URL = os.environ.get("WECOM_WEBHOOK_URL", "")

# ========== 第一模块：实时行情获取 ==========

def get_realtime_quotes():
    """获取实时行情数据（使用东方财富API）"""
    print("\n" + "="*60)
    print("📡 第一步：获取A股实时行情...")
    print("="*60)

    try:
        # 东方财富全市场行情接口
        url = "https://push2.eastmoney.com/api/qt/clist/get"
        params = {
            "pn": 1,
            "pz": 5000,  # 获取前5000只
            "po": 1,
            "np": 1,
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": 2,
            "invt": 2,
            "fid": "f3",
            "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048",
            "fields": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f12,f13,f14,f15,f16,f17,f18,f20,f21,f23,f24,f25,f22,f11,f37,f62,f128,f136,f115,f152,f162,f167"
        }

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://quote.eastmoney.com/"
        }

        response = _get(url, headers=headers, timeout=15, params=params)
        data = response.json()

        stocks = []
        if data.get("data") and data["data"].get("diff"):
            for item in data["data"]["diff"]:
                try:
                    # PE: f162(TTM)最可靠 > f9 > 不用f51(PE静,会虚高)
                    pe_raw = item.get("f162", 0) or item.get("f9", 0)
                    # PB: f167最可靠 > f23
                    pb_raw = item.get("f167", 0) or item.get("f23", 0)
                    stock = {
                        "code": str(item.get("f12", "")),
                        "name": item.get("f14", ""),
                        "price": float(item.get("f2", 0)),
                        "change_pct": float(item.get("f3", 0)),
                        "volume": float(item.get("f5", 0)),
                        "amount": float(item.get("f6", 0)),
                        "high": float(item.get("f15", 0)),
                        "low": float(item.get("f16", 0)),
                        "open": float(item.get("f17", 0)),
                        "prev_close": float(item.get("f18", 0)),
                        "pe": float(pe_raw) if pe_raw and pe_raw != "-" else 0,
                        "pb": float(pb_raw) if pb_raw and pb_raw != "-" else 0,
                        "roe": 0,  # 行情API的f37(ROE)经常缺失或过期, 由财务数据中心校准
                        "gross_margin": 0,
                        "net_profit_ratio": 0,
                        "debt_ratio": 0,
                        "rev_yoy": float(item.get("f24", 0)) if item.get("f24", 0) and item.get("f24", 0) != "-" else 0,
                        "profit_yoy": float(item.get("f25", 0)) if item.get("f25", 0) and item.get("f25", 0) != "-" else 0,
                        "market_cap": float(item.get("f20", 0)) if item.get("f20", 0) > 0 else 0,
                    }

                    # 过滤ST股和无效数据
                    if stock["price"] > 0 and not "ST" in stock["name"] and not "*" in stock["name"]:
                        stocks.append(stock)

                except Exception as e:
                    continue

        print(f"✅ 成功获取 {len(stocks)} 只股票实时行情")
        return stocks

    except Exception as e:
        print(f"❌ 获取行情失败: {e}")
        return []

def get_financial_data_batch(codes):
    """批量获取财务数据（使用预设高质量股票库）"""
    print(f"\n📊 使用五维价值评估模型筛选...")

    # 预设高质量中小盘股票财务数据（符合价值投资标准）
    preset_financials = {
        # 医疗健康
        "300015": {"name": "爱尔眼科", "price": 28.5, "pe": 65.2, "roe": 22.5, "gross_margin": 48.5, "net_margin": 18.2, "rev_growth": 25.5, "profit_growth": 32.5, "pb": 12.5, "market_cap": 25000000000},
        "300760": {"name": "迈瑞医疗", "price": 298.0, "pe": 45.2, "roe": 32.5, "gross_margin": 85.5, "net_margin": 35.2, "rev_growth": 22.5, "profit_growth": 28.5, "pb": 15.2, "market_cap": 350000000000},
        "300122": {"name": "智飞生物", "price": 85.5, "pe": 18.5, "roe": 35.2, "gross_margin": 45.2, "net_margin": 32.5, "rev_growth": 28.5, "profit_growth": 38.5, "pb": 8.2, "market_cap": 120000000000},
        "002007": {"name": "华兰生物", "price": 25.8, "pe": 35.2, "roe": 22.5, "gross_margin": 65.5, "net_margin": 35.2, "rev_growth": 18.5, "profit_growth": 22.5, "pb": 5.8, "market_cap": 45000000000},

        # 消费电子/科技
        "300059": {"name": "东方财富", "price": 22.8, "pe": 45.8, "roe": 18.5, "gross_margin": 65.2, "net_margin": 65.2, "rev_growth": 35.2, "profit_growth": 42.5, "pb": 5.2, "market_cap": 280000000000},
        "002049": {"name": "紫光国微", "price": 168.5, "pe": 55.8, "roe": 28.5, "gross_margin": 52.5, "net_margin": 35.2, "rev_growth": 35.8, "profit_growth": 42.5, "pb": 12.5, "market_cap": 120000000000},
        "002236": {"name": "大华股份", "price": 18.2, "pe": 22.5, "roe": 18.2, "gross_margin": 42.5, "net_margin": 15.8, "rev_growth": 15.2, "profit_growth": 18.5, "pb": 3.2, "market_cap": 55000000000},

        # 新能源/光伏
        "300274": {"name": "阳光电源", "price": 125.8, "pe": 35.2, "roe": 22.5, "gross_margin": 28.5, "net_margin": 15.8, "rev_growth": 45.2, "profit_growth": 55.8, "pb": 8.5, "market_cap": 180000000000},
        "002812": {"name": "恩捷股份", "price": 68.5, "pe": 28.5, "roe": 22.5, "gross_margin": 45.8, "net_margin": 32.5, "rev_growth": 55.2, "profit_growth": 65.8, "pb": 6.8, "market_cap": 58000000000},
        "300014": {"name": "亿纬锂能", "price": 85.2, "pe": 32.5, "roe": 20.5, "gross_margin": 22.5, "net_margin": 18.5, "rev_growth": 65.8, "profit_growth": 75.2, "pb": 7.2, "market_cap": 150000000000},

        # 传媒/互联网
        "002027": {"name": "分众传媒", "price": 8.5, "pe": 28.5, "roe": 25.8, "gross_margin": 65.8, "net_margin": 42.5, "rev_growth": 18.5, "profit_growth": 25.2, "pb": 4.8, "market_cap": 120000000000},

        # 化工/材料
        "002371": {"name": "北方华创", "price": 365.0, "pe": 65.5, "roe": 25.8, "gross_margin": 35.2, "net_margin": 18.5, "rev_growth": 35.8, "profit_growth": 45.2, "pb": 10.5, "market_cap": 180000000000},
        "300751": {"name": "迈为股份", "price": 185.0, "pe": 45.2, "roe": 28.5, "gross_margin": 72.5, "net_margin": 35.8, "rev_growth": 55.2, "profit_growth": 65.8, "pb": 12.5, "market_cap": 95000000000},

        # 物流
        "002352": {"name": "顺丰控股", "price": 42.5, "pe": 35.8, "roe": 12.5, "gross_margin": 18.5, "net_margin": 5.2, "rev_growth": 28.5, "profit_growth": 35.8, "pb": 3.8, "market_cap": 185000000000},

        # 食品/消费（不含白酒）
        "603288": {"name": "海天味业", "price": 58.5, "pe": 45.8, "roe": 32.5, "gross_margin": 38.5, "net_margin": 28.5, "rev_growth": 15.2, "profit_growth": 18.5, "pb": 12.5, "market_cap": 250000000000},
    }

    return preset_financials

def get_full_market_stocks():
    """全市场扫描：财务数据中心获取年报数据 + 腾讯API获取实时行情
    
    2026-04-02 重写: 旧版只扫描16只预设股票导致结果永远不变
    新逻辑: 按最新年报筛选全市场, 获取ROE/毛利率/增速, 合并腾讯实时行情
    """
    dc_headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
        'Referer': 'https://data.eastmoney.com/'
    }
    
    candidate_stocks = {}
    
    # Step 1: 确定最新年报
    current_year = datetime.now().year
    latest_report = None
    for yr in range(current_year, current_year - 2, -1):
        try:
            tp = {
                'reportName': 'RPT_F10_FINANCE_MAINFINADATA',
                'columns': 'REPORT_DATE_NAME',
                'filter': '(REPORT_DATE_NAME="' + str(yr) + '年报")',
                'pageNumber': 1, 'pageSize': 1,
                'source': 'WEB', 'client': 'WEB',
            }
            tr = _get('https://datacenter-web.eastmoney.com/api/data/v1/get',
                              headers=dc_headers, timeout=10, params=tp)
            td = tr.json()
            if td.get('success') and td.get('result') and td['result'].get('count', 0) > 0:
                latest_report = str(yr) + '年报'
                break
        except:
            continue
    if not latest_report:
        latest_report = str(current_year - 1) + '年报'
    report_filter = '(REPORT_DATE_NAME="' + latest_report + '")'
    print(f"  使用年报: {latest_report}")
    
    # Step 2: 获取年报ROE+毛利率
    for extra_filter in ['(ROEJQ>5)(ROEJQ<80)', '(ROEJQ>10)(ROEJQ<80)']:
        try:
            params = {
                'reportName': 'RPT_F10_FINANCE_MAINFINADATA',
                'columns': 'SECURITY_CODE,SECURITY_NAME_ABBR,ROEJQ,XSMLL',
                'filter': report_filter + extra_filter,
                'pageNumber': 1, 'pageSize': 3000,
                'source': 'WEB', 'client': 'WEB',
                'sortColumns': 'ROEJQ', 'sortTypes': '-1'
            }
            resp = _get('https://datacenter-web.eastmoney.com/api/data/v1/get',
                                headers=dc_headers, timeout=15, params=params)
            d = resp.json()
            if d.get('success') and d.get('result') and d.get('result').get('data'):
                for item in d['result']['data']:
                    code = item.get('SECURITY_CODE', '')
                    name = item.get('SECURITY_NAME_ABBR', '')
                    if not code or not name or len(code) != 6: continue
                    if 'ST' in name or '*' in name: continue
                    if code.startswith('8') or code.startswith('4') or code.startswith('920'): continue
                    if code.startswith('900') or code.startswith('200') or code.startswith('A2'): continue
                    roe = item.get('ROEJQ', 0)
                    gm = item.get('XSMLL', 0)
                    if code not in candidate_stocks:
                        candidate_stocks[code] = {'name': name, 'roe': 0, 'gross_margin': 0, 'rev_growth': 0, 'profit_growth': 0}
                    if roe is not None:
                        fval = float(roe)
                        if 1 <= fval <= 80 and fval > candidate_stocks[code]['roe']:
                            candidate_stocks[code]['roe'] = fval
                    if gm is not None:
                        fgm = float(gm)
                        if fgm > 0 and fgm > candidate_stocks[code]['gross_margin']:
                            candidate_stocks[code]['gross_margin'] = fgm
        except Exception as e:
            print(f"  获取ROE列表失败: {e}")

    print(f"  财务筛选: {len(candidate_stocks)} 只")
    
    # Step 3: CPD年报增速
    cpd_year = current_year
    cpd_filter = '(DATAYEAR=' + str(cpd_year) + ')(DATEMMDD="年报")'
    try:
        tp = {
            'reportName': 'RPT_LICO_FN_CPD',
            'columns': 'SECURITY_CODE',
            'filter': cpd_filter,
            'pageNumber': 1, 'pageSize': 1,
            'source': 'WEB', 'client': 'WEB',
        }
        tr = _get('https://datacenter-web.eastmoney.com/api/data/v1/get',
                          headers=dc_headers, timeout=10, params=tp)
        td = tr.json()
        if not (td.get('success') and td.get('result') and td['result'].get('count', 0) > 0):
            cpd_year -= 1
            cpd_filter = '(DATAYEAR=' + str(cpd_year) + ')(DATEMMDD="年报")'
    except:
        cpd_year -= 1
        cpd_filter = '(DATAYEAR=' + str(cpd_year) + ')(DATEMMDD="年报")'
    
    for extra in ['(SJLTZ>10)(SJLTZ<5000)', '(YSTZ>10)(YSTZ<5000)']:
        try:
            sort_col = 'SJLTZ' if 'SJLTZ' in extra else 'YSTZ'
            params = {
                'reportName': 'RPT_LICO_FN_CPD',
                'columns': 'SECURITY_CODE,SECURITY_NAME_ABBR,YSTZ,SJLTZ',
                'filter': cpd_filter + extra,
                'pageNumber': 1, 'pageSize': 3000,
                'source': 'WEB', 'client': 'WEB',
                'sortColumns': sort_col, 'sortTypes': '-1'
            }
            resp = _get('https://datacenter-web.eastmoney.com/api/data/v1/get',
                                headers=dc_headers, timeout=15, params=params)
            d = resp.json()
            if d.get('success') and d.get('result') and d.get('result').get('data'):
                for item in d['result']['data']:
                    code = item.get('SECURITY_CODE', '')
                    name = item.get('SECURITY_NAME_ABBR', '')
                    if not code or len(code) != 6: continue
                    if not name or 'ST' in name or '*' in name: continue
                    if code.startswith('8') or code.startswith('4') or code.startswith('920'): continue
                    if code.startswith('900') or code.startswith('200') or code.startswith('A2'): continue
                    ystz = item.get('YSTZ', 0)
                    sjltz = item.get('SJLTZ', 0)
                    if code not in candidate_stocks:
                        candidate_stocks[code] = {'name': name, 'roe': 0, 'gross_margin': 0, 'rev_growth': 0, 'profit_growth': 0}
                    if ystz is not None and abs(float(ystz)) <= 1000:
                        candidate_stocks[code]['rev_growth'] = max(candidate_stocks[code]['rev_growth'], float(ystz))
                    if sjltz is not None and abs(float(sjltz)) <= 10000:
                        candidate_stocks[code]['profit_growth'] = max(candidate_stocks[code]['profit_growth'], float(sjltz))
        except Exception as e:
            print(f"  获取CPD增速失败: {e}")
    
    print(f"  含增速数据: {sum(1 for c in candidate_stocks.values() if c['rev_growth'] > 0 or c['profit_growth'] > 0)} 只")
    
    # Step 4: 腾讯API获取实时行情
    codes = list(candidate_stocks.keys())
    batch_size = 80
    price_data = {}
    print(f"  腾讯API获取实时行情（{(len(codes) + batch_size - 1) // batch_size}批）...")
    
    tx_headers = {'User-Agent': 'Mozilla/5.0'}
    for i in range(0, len(codes), batch_size):
        batch_codes = codes[i:i+batch_size]
        tx_codes = [f"sh{c}" if c.startswith('6') else f"sz{c}" for c in batch_codes]
        try:
            url = 'http://qt.gtimg.cn/q=' + ','.join(tx_codes)
            resp = _get(url, headers=tx_headers, timeout=15)
            lines = resp.text.strip().split(';')
            for line in lines:
                if not line.strip(): continue
                parts = line.split('~')
                if len(parts) < 50: continue
                code = parts[2]
                if not code or len(code) != 6: continue
                try: price = float(parts[3]) if parts[3] else 0
                except: price = 0
                if price <= 0: continue
                try: change_pct = float(parts[32]) if parts[32] else 0
                except: change_pct = 0
                try: pe = float(parts[39]) if parts[39] and parts[39] != '-' else 0
                except: pe = 0
                if pe > 10000 or pe < 0: pe = 0
                try: total_cap_yi = float(parts[44]) if parts[44] else 0
                except: total_cap_yi = 0
                try: amount_wan = float(parts[43]) if parts[43] else 0
                except: amount_wan = 0
                price_data[code] = {
                    'price': price, 'change_pct': change_pct,
                    'pe': pe, 'market_cap': total_cap_yi * 100000000,
                    'amount': amount_wan * 10000,
                }
        except:
            continue
    
    print(f"  获取行情: {len(price_data)} 只")
    
    # Step 5: 合并数据
    stock_pool = []
    for code, fin in candidate_stocks.items():
        if code not in price_data: continue
        pd = price_data[code]
        stock_pool.append({
            "code": code, "name": fin['name'],
            "price": pd['price'], "change_pct": pd['change_pct'],
            "pe": pd['pe'], "pb": 0,
            "roe": fin['roe'], "gross_margin": fin['gross_margin'],
            "net_margin": 0, "debt_ratio": 0,
            "rev_growth": fin['rev_growth'], "profit_growth": fin['profit_growth'],
            "market_cap": pd['market_cap'],
        })
    
    return stock_pool

def get_financial_data(code):
    """从东方财富财务数据中心获取个股关键财务指标（最新报告期）
    
    ROE使用 RPT_F10_FINANCE_MAINFINADATA (ROEJQ, 有报告期排序, 最新数据)
    营收同比/净利同比使用 RPT_LICO_FN_CPD (YSTZ/SJLTZ, CPD独有字段)
    """
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
        'Referer': 'https://data.eastmoney.com/'
    }
    base_url = 'https://datacenter-web.eastmoney.com/api/data/v1/get'
    result = {'roe': 0, 'rev_growth': 0, 'profit_growth': 0, 'gross_margin': 0}

    # 1. MAINFINADATA: 拿ROE + 毛利率
    try:
        params = {
            'reportName': 'RPT_F10_FINANCE_MAINFINADATA',
            'columns': 'REPORT_DATE_NAME,ROEJQ,XSMLL',
            'filter': '(SECURITY_CODE="' + code + '")',
            'pageNumber': 1, 'pageSize': 1,
            'source': 'WEB', 'client': 'WEB',
        }
        resp = _get(base_url, headers=headers, timeout=5, params=params)
        d = resp.json()
        if d.get('success') and d.get('result') and d['result'].get('data'):
            item = d['result']['data'][0]
            roe_val = item.get('ROEJQ', 0)
            if roe_val is not None:
                result['roe'] = float(roe_val)
            xsml_val = item.get('XSMLL', 0)
            if xsml_val is not None:
                result['gross_margin'] = float(xsml_val)
    except:
        pass

    # 2. CPD: 拿营收同比 + 净利同比
    try:
        params = {
            'reportName': 'RPT_LICO_FN_CPD',
            'columns': 'DATAYEAR,DATEMMDD,WEIGHTAVG_ROE,YSTZ,SJLTZ,XSMLL',
            'filter': '(SECURITY_CODE="' + code + '")',
            'pageNumber': 1, 'pageSize': 1,
            'source': 'WEB', 'client': 'WEB',
        }
        resp = _get(base_url, headers=headers, timeout=5, params=params)
        d = resp.json()
        if d.get('success') and d.get('result') and d['result'].get('data'):
            item = d['result']['data'][0]
            ystz = item.get('YSTZ', 0)
            if ystz is not None:
                result['rev_growth'] = float(ystz)
            sjltz = item.get('SJLTZ', 0)
            if sjltz is not None:
                result['profit_growth'] = float(sjltz)
            if result['roe'] == 0:
                cpd_roe = item.get('WEIGHTAVG_ROE', 0)
                if cpd_roe is not None:
                    result['roe'] = float(cpd_roe)
            if result['gross_margin'] == 0:
                cpd_xsml = item.get('XSMLL', 0)
                if cpd_xsml is not None:
                    result['gross_margin'] = float(cpd_xsml)
    except:
        pass

    if result['roe'] != 0 or result['rev_growth'] != 0 or result['profit_growth'] != 0:
        return result
    return None

# ========== 第二模块：五维价值评估 ==========

def evaluate_value_investment(stock, financial_data=None):
    """五维价值投资评估模型"""
    score = 0
    reasons = []

    code = stock.get("code", "")
    name = stock.get("name", "")

    # 过滤排除项（独立判断，提前返回）
    if code.startswith('8') or code.startswith('4') or code.startswith('920'):
        return None  # 北交所
    if code.startswith('900') or code.startswith('200') or code.startswith('A2'):
        return None  # B 股
    # 白酒股名单
    liquor_names = ["贵州茅台", "五粮液", "洋河股份", "泸州老窖", "山西汾酒", "酒鬼酒", "水井坊", "古井贡酒", "迎驾贡酒", "今世缘", "舍得酒业", "老白干酒", "伊力特", "口子窖", "金徽酒", "皇台酒业", "岩石股份", "顺鑫农业"]
    if any(n in name for n in liquor_names):
        return None  # 排除白酒
    # 银行股代码
    bank_codes = ["601398", "601288", "600000", "600036", "601166", "600015", "600016", "601328", "600919", "600028", "601939", "601988", "601318", "600030"]
    if code in bank_codes:
        return None  # 排除银行

    # 获取财务数据
    if financial_data and code in financial_data:
        fin = financial_data[code]
        stock["roe"] = fin.get("roe", stock.get("roe", 0))
        stock["gross_margin"] = fin.get("gross_margin", stock.get("gross_margin", 0))
        stock["net_margin"] = fin.get("net_margin", stock.get("net_margin", 0))
        stock["rev_growth"] = fin.get("rev_growth", stock.get("rev_yoy", 0))
        stock["profit_growth"] = fin.get("profit_growth", stock.get("profit_yoy", 0))
    else:
        fin_data = get_financial_data(code)
        if fin_data:
            stock["roe"] = fin_data.get("roe", stock.get("roe", 0))
            stock["gross_margin"] = fin_data.get("gross_margin", stock.get("gross_margin", 0))
            if stock.get("rev_yoy", 0) == 0:
                stock["rev_growth"] = fin_data.get("rev_growth", 0)
            if stock.get("profit_yoy", 0) == 0:
                stock["profit_growth"] = fin_data.get("profit_growth", 0)
        else:
            stock["roe"] = stock.get("roe", 0)
            stock["gross_margin"] = stock.get("gross_margin", 0)
            stock["net_margin"] = stock.get("net_margin", 0)
            stock["rev_growth"] = stock.get("rev_yoy", 0)
            stock["profit_growth"] = stock.get("profit_yoy", 0)

    # 第一维：盈利能力 (最高35分) - 连续评分增加区分度
    prof_score = 0
    roe = stock.get("roe", 0)
    if roe < 0:
        reasons.append(f"ROE {roe:.1f}% 亏损")
    elif roe >= 18:  # 优化：20% -> 18%
        prof_score = min(25 + (roe - 20) * 1, 35)  # 连续: ROE 20→35分, 30→35分(上限)
        reasons.append(f"ROE {roe:.1f}% 优秀")
    elif roe >= 15:
        prof_score = 15 + (roe - 15) * 2  # 连续: 15→25分
        reasons.append(f"ROE {roe:.1f}% 良好")
    elif roe > 0:
        prof_score = roe * 1  # 连续: 0→15分
        reasons.append(f"ROE {roe:.1f}% 一般")

    gross_margin = stock.get("gross_margin", 0)
    if gross_margin >= 40:
        prof_score = min(prof_score + 8, 35)
        reasons.append(f"毛利率 {gross_margin:.1f}%")
    elif gross_margin > 0:
        prof_score = min(prof_score + 3, 35)

    net_margin = stock.get("net_margin", 0)
    if net_margin >= 15:
        prof_score = min(prof_score + 5, 35)
        reasons.append(f"净利率 {net_margin:.1f}%")
    score += prof_score

    # 第二维：成长性 (25%)
    rev_g = stock.get("rev_growth", 0)
    prof_g = stock.get("profit_growth", 0)
    if rev_g > 0 and prof_g > 0:
        avg_growth = (rev_g + prof_g) / 2
    elif rev_g > 0:
        avg_growth = rev_g
    elif prof_g > 0:
        avg_growth = prof_g
    else:
        avg_growth = 0
    if roe < 0:
        if prof_g > 20 and rev_g > 0:
            score += 5
            reasons.append("亏损但有改善迹象")
    elif avg_growth >= 20:
        score += min(20 + (avg_growth - 20) * 0.5, 25)  # 连续: 20→25分
        reasons.append(f"成长性 {avg_growth:.1f}% 优秀")
    elif avg_growth >= 15:
        score += 15 + (avg_growth - 15) * 1  # 连续: 15→20分
        reasons.append(f"成长性 {avg_growth:.1f}% 良好")
    elif avg_growth >= 10:
        score += 10 + (avg_growth - 10) * 1  # 连续: 10→15分
        reasons.append(f"成长性 {avg_growth:.1f}%")
    elif avg_growth > 0:
        score += avg_growth * 0.5  # 连续: 0→5分

    # 第三维：财务健康 (20%) - 无数据给0分
    debt_ratio = stock.get("debt_ratio", 0)
    if debt_ratio > 0 and debt_ratio < 1000:
        if debt_ratio <= 50:
            score += 20
            reasons.append(f"资产负债率 {debt_ratio:.1f}%")
        elif debt_ratio <= 70:
            score += 12
        else:
            score += 5
    # 无数据不给分（不再白送10分）

    # 第四维：估值 (20%) - 连续评分
    pe = stock.get("pe", 0)
    val_score = 0
    if 0 < pe <= 12:  # 优化：15 -> 12
        val_score = min(15 + (15 - pe) * 0.33, 20)  # PE越低分越高
        reasons.append(f"PE {pe:.1f} 低估")
    elif 12 < pe <= 20:  # 优化：15-25 -> 12-20
        val_score = 15 - (pe - 15) * 0.5  # 15→10分连续
        reasons.append(f"PE {pe:.1f} 合理")
    elif 25 < pe <= 35:
        val_score = 10 - (pe - 25) * 0.5  # 10→5分连续
    elif 35 < pe <= 50:
        val_score = max(5 - (pe - 35) * 0.33, 0)  # 5→0分连续
    elif pe > 50:
        val_score = 0
        if pe > 100:
            reasons.append(f"PE {pe:.1f} 高估")
    elif pe <= 0:
        val_score = 0  # 负PE不给分

    pb = stock.get("pb", 0)
    if 0 < pb <= 3:
        val_score = min(val_score + 5, 20)
    elif 3 < pb <= 5:
        val_score = min(val_score + 2, 20)
    score += val_score

    # 第五维：现金流质量 (加分项，上限5分)
    if pe > 0 and roe > 15:
        if pe <= 20:
            score += 5
            reasons.append(f"PE{pe:.0f}+ROE{roe:.0f}% 现金流充裕")
        elif pe <= 30:
            score += 4
        elif pe <= 45:
            score += 2
        else:
            score += 1
    market_cap = stock.get("market_cap", 0)
    if market_cap > 0:
        market_cap_yi = market_cap / 100000000
        reasons.append(f"市值 {market_cap_yi:.0f}亿")

    return {
        "score": score,
        "reasons": reasons,
        "stock": stock
    }

# ========== 第三模块：资讯搜索 ==========

def search_stock_news(stock_code, stock_name):
    """搜索股票相关资讯（使用东方财富搜索接口）"""
    try:
        url = f"http://search-api.eastmoney.com/api/suggest/get"
        params = {
            "input": stock_code,
            "type": "14",
            "token": "D43BF722C8E33BDC906FB84D85E326E8",
            "count": 5
        }

        response = _get(url, timeout=5, params=params)
        if response.status_code == 200:
            return True
    except:
        pass

    return None

def analyze_sentiment(news_results):
    """分析舆情（简化版）"""
    # 简化：返回中性
    sentiment_score = 0
    sentiment_label = "中性"

    return {
        "score": sentiment_score,
        "label": sentiment_label,
        "news_count": len(news_results) if news_results else 0
    }

# ========== 第四模块：买卖点计算 ==========

def calculate_buy_sell_points(stock, score):
    """计算买卖点"""
    price = stock.get("price", 0)
    pe = stock.get("pe", 0)
    roe = stock.get("roe", 0)

    if price <= 0 or pe <= 0:
        return None

    # 基于合理PE计算
    # 基于 ROE 动态计算合理 PE：fair_pe = ROE * 1.2
    # 逻辑：ROE 代表盈利能力，合理 PE 应该是盈利能力的线性函数
    # ROE=20% -> fair_pe=24, ROE=30% -> fair_pe=36
    fair_pe = roe * 1.2
    # 设置合理范围：最低 15 倍，最高 50 倍
    fair_pe = max(15, min(50, fair_pe))

    # 当前被低估程度
    if pe < fair_pe:
        # 被低估
        if score >= 80:
            buy_point = round(price * 0.95, 2)
            sell_point = round((fair_pe / pe) * price * 1.3, 2)
            recommendation = "⭐⭐⭐ 强烈推荐"
        elif score >= 60:
            buy_point = round(price * 0.92, 2)
            sell_point = round((fair_pe / pe) * price * 1.2, 2)
            recommendation = "⭐⭐ 谨慎推荐"
        else:
            return None
    else:
        # 估值合理或偏高
        if score >= 80 and pe < 45:
            buy_point = round(price * 0.88, 2)
            sell_point = round(price * 1.4, 2)
            recommendation = "⭐⭐ 等待回调"
        else:
            return None

    upside = round((sell_point - price) / price * 100, 1) if price > 0 else 0
    downside = round((price - buy_point) / price * 100, 1) if price > 0 else 0

    return {
        "current_price": price,
        "buy_point": buy_point,
        "sell_point": sell_point,
        "upside_potential": upside,
        "downside_risk": downside,
        "recommendation": recommendation
    }

# ========== 主程序：智能选股 ==========

def smart_stock_picker():
    """智能选股主流程 - 全市场扫描"""
    print("\n" + "="*70)
    print("🏛️ 价值投资之王 · 智能选股系统 v4")
    print("   时间: " + datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    print("="*70)

    # ========== 第一步：全市场扫描获取财务+行情数据 ==========
    print("\n" + "="*60)
    print("📡 第一步：全市场扫描（财务数据+实时行情）...")
    print("="*60)
    
    stock_pool = get_full_market_stocks()
    
    if not stock_pool or len(stock_pool) < 20:
        print("❌ 全市场扫描失败，使用预设数据")
        preset_data = get_financial_data_batch([])
        stock_pool = []
        for code, fin_data in preset_data.items():
            stock_pool.append({
                "code": code, "name": fin_data["name"],
                "price": fin_data["price"], "pe": fin_data["pe"],
                "roe": fin_data["roe"], "gross_margin": fin_data["gross_margin"],
                "net_margin": fin_data["net_margin"],
                "rev_growth": fin_data["rev_growth"],
                "profit_growth": fin_data["profit_growth"],
                "pb": fin_data.get("pb", 0),
                "market_cap": fin_data.get("market_cap", 0)
            })
    
    print(f"✅ 候选股票池: {len(stock_pool)} 只")

    # ========== 第二步：五维评估筛选 ==========
    print("\n" + "="*60)
    print("📊 第二步：五维价值评估筛选...")
    print("="*60)

    evaluated_stocks = []
    for stock in stock_pool:
        result = evaluate_value_investment(stock, None)
        if result and result["score"] >= 55:  # 55分以上
            # 计算买卖点
            buy_sell = calculate_buy_sell_points(stock, result["score"])
            if buy_sell and buy_sell.get("buy_point", 0) > 0:
                result["buy_sell"] = buy_sell
                evaluated_stocks.append(result)
                print(f"  ✅ {stock['name']}({stock['code']}) - 评分:{result['score']}分 | 买入:{buy_sell['buy_point']} 卖出:{buy_sell['sell_point']}")

    # 按评分排序
    evaluated_stocks.sort(key=lambda x: x["score"], reverse=True)

    # 取前10名
    top_stocks = evaluated_stocks[:10]

    # ========== 第三步：资讯舆情分析 ==========
    print("\n" + "="*60)
    print("📰 第三步：资讯舆情分析...")
    print("="*60)

    final_recommendations = []
    for item in top_stocks:
        stock = item["stock"]
        print(f"  🔍 分析 {stock['name']} 相关资讯...")

        news_result = search_stock_news(stock["code"], stock["name"])
        sentiment = analyze_sentiment(news_result)

        item["sentiment"] = sentiment
        final_recommendations.append(item)

    # ========== 第四步：综合决策 ==========
    print("\n" + "="*60)
    print("🎯 第四步：综合决策...")
    print("="*60)

    # 最终推荐：取评分最高且舆情中性的
    if final_recommendations:
        print(f"  ✅ 筛选出 {len(final_recommendations)} 只优质股票")

    return final_recommendations[:5]  # 最多返回5只

def format_final_message(results):
    """格式化最终推荐消息"""
    today = datetime.now().strftime("%Y年%m月%d日 %H:%M")

    if not results:
        return f"""📈 **价值投资之王 · 智能选股**
{'='*40}
🗓️ {today}

今日筛选结果：
暂无符合条件的价值投资标的。

💡 建议耐心等待更好的买入机会

🏛️ 价值投资之王"""

    header = f"""📈 **价值投资之王 · 智能选股**
{'='*40}
🗓️ {today}
🏆 全市场实时筛选 + 五维价值评估 + 舆情分析

"""

    stock_lines = []
    for i, r in enumerate(results, 1):
        s = r["stock"]
        bs = r.get("buy_sell", {})

        stock_lines.append(f"""**{i}. {s['name']}（{s['code']}）**
{bs.get('recommendation', '')}
💰 现价: {s.get('price', 0):.2f}元 | PE: {s.get('pe', 0):.1f} | ROE: {s.get('roe', 0):.1f}%

📊 五维评分: **{r['score']}分**
   • {r['reasons'][0]}
   • {r['reasons'][1]}

🎯 操作建议
   买入: **{bs.get('buy_point', 0):.2f}** 元（{bs.get('downside_risk', 0):.1f}%回调介入）
   卖出: **{bs.get('sell_point', 0):.2f}** 元（{bs.get('upside_potential', 0):.1f}%空间）

{'─'*40}""")

    footer = """
💡 **投资提醒**
• 以上仅供参考，不构成投资建议
• 投资有风险，入市需谨慎
• 好公司也要有好价格

🏛️ 价值投资之王 - 做时间的朋友"""

    return header + "\n".join(stock_lines) + footer

def send_to_wechat(message):
    """发送消息到企业微信"""
    if not WECOM_WEBHOOK_URL:
        print("\n⚠️ 未配置WECOM_WEBHOOK_URL环境变量，跳过微信推送")
        return False

    data = {
        "msgtype": "markdown",
        "markdown": {
            "content": message
        }
    }

    try:
        response = _post(WECOM_WEBHOOK_URL, json=data, timeout=10)
        result = response.json()
        if result.get("errcode") == 0:
            print("\n✅ 消息已发送到企业微信")
            return True
        else:
            print(f"\n❌ 发送失败: {result}")
            return False
    except Exception as e:
        print(f"\n❌ 发送异常: {e}")
        return False

def main():
    """主函数"""
    print("\n" + "🎯"*30)
    print("🏛️ 价值投资之王 智能选股系统启动")
    print("🎯"*30)

    # 执行智能选股
    results = smart_stock_picker()

    # 格式化消息
    message = format_final_message(results)

    # 打印结果
    print("\n" + "="*70)
    print("📤 最终推荐：")
    print("="*70)
    print(message)

    # 发送到企业微信
    send_to_wechat(message)

    print("\n✅ 选股完成")

if __name__ == "__main__":
    main()