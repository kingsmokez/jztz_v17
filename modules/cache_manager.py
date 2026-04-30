# -*- coding: utf-8 -*-
"""缓存管理 - 每日推荐/竞价/尾盘缓存"""

import os
import json
import threading
from datetime import datetime

from .config import BASE_DIR, DAILY_PICK_FILE, WP2_PICK_FILE, AUCTION_PICK_FILE

DAILY_PICK_DATA = {"morning": None, "afternoon": None, "last_update": None}
DAILY_PICK_LOCK = threading.Lock()
AUCTION_PICK_DATA = {"candidate_pool": [], "results": [], "preselect_time": None, "confirm_time": None, "last_update": None}
AUCTION_PICK_LOCK = threading.Lock()
WP2_PICK_DATA = {"results": [], "last_update": None}
WP2_PICK_LOCK = threading.Lock()



def load_daily_pick_cache():
    """从文件加载每日推荐缓存"""
    global DAILY_PICK_DATA
    try:
        if os.path.exists(DAILY_PICK_FILE):
            with open(DAILY_PICK_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                # 检查是否是今天的数据
                if data.get('date') == datetime.now().strftime('%Y-%m-%d'):
                    DAILY_PICK_DATA.clear()
                    DAILY_PICK_DATA.update(data)
                    print(f"✓ 加载今日选股缓存: 早上 {bool(data.get('morning'))}, 下午 {bool(data.get('afternoon'))}")
                    return
                else:
                    print("⚠️ 缓存日期不是今天，将重新选股")
    except Exception as e:
        print(f"加载缓存失败: {e}")
    # 重置为今天的空数据
    DAILY_PICK_DATA.clear()
    DAILY_PICK_DATA.update({
        "date": datetime.now().strftime('%Y-%m-%d'),
        "morning": None,
        "afternoon": None,
        "last_update": None,
    })


def save_daily_pick_cache():
    """保存每日推荐缓存到文件"""
    try:
        with open(DAILY_PICK_FILE, 'w', encoding='utf-8') as f:
            json.dump(DAILY_PICK_DATA, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"保存缓存失败: {e}")


def load_wp2_pick_cache():
    global WP2_PICK_DATA
    try:
        if os.path.exists(WP2_PICK_FILE):
            with open(WP2_PICK_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if data.get('date') == datetime.now().strftime('%Y-%m-%d'):
                    WP2_PICK_DATA.clear()
                    WP2_PICK_DATA.update(data)
                    print(f"✓ 加载尾盘选股缓存: {bool(data.get('stocks'))}")
                    return
    except Exception as e:
        print(f"加载尾盘选股缓存失败: {e}")
    WP2_PICK_DATA.clear()
    WP2_PICK_DATA.update({
        "date": datetime.now().strftime('%Y-%m-%d'),
        "stocks": [],
        "pick_time": None,
        "last_update": None,
        "filter_stats": [],
        "market_info": {},
        "running": False,
    })


def save_wp2_pick_cache():
    try:
        with open(WP2_PICK_FILE, 'w', encoding='utf-8') as f:
            json.dump(WP2_PICK_DATA, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"保存尾盘选股缓存失败: {e}")


def load_auction_pick_cache():
    """加载竞价选股缓存（检查日期，过期则重置）"""
    global AUCTION_PICK_DATA
    try:
        if os.path.exists(AUCTION_PICK_FILE):
            with open(AUCTION_PICK_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                today = datetime.now().strftime('%Y-%m-%d')
                preselect_time = data.get('preselect_time', '')
                if preselect_time and preselect_time.startswith(today):
                    AUCTION_PICK_DATA.clear()
                    AUCTION_PICK_DATA.update(data)
                    print(f"✓ 加载今日竞价缓存: {len(data.get('candidate_pool', []))}只候选")
                else:
                    print("⚠️ 竞价缓存日期不是今天，将重新预选")
                    AUCTION_PICK_DATA.clear()
                    AUCTION_PICK_DATA.update({
                        "candidate_pool": [], "results": [],
                        "preselect_time": None, "confirm_time": None,
                        "last_update": None,
                    })
    except Exception as e:
        print(f"加载竞价选股缓存失败: {e}")


def save_auction_pick_cache():
    """保存竞价选股缓存"""
    try:
        with open(AUCTION_PICK_FILE, 'w', encoding='utf-8') as f:
            json.dump(AUCTION_PICK_DATA, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"保存竞价选股缓存失败: {e}")


def load_all_caches():
    load_daily_pick_cache()
    load_wp2_pick_cache()
    load_auction_pick_cache()

def save_all_caches():
    save_daily_pick_cache()
    save_wp2_pick_cache()
    save_auction_pick_cache()
