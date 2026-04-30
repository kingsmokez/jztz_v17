# -*- coding: utf-8 -*-
"""定时任务 - 早盘/午盘选股 + 竞价预选"""

import time
import threading
from datetime import datetime
import traceback

from .cache_manager import DAILY_PICK_DATA, DAILY_PICK_LOCK, save_daily_pick_cache
from .cache_manager import AUCTION_PICK_DATA, AUCTION_PICK_LOCK, save_auction_pick_cache
from .stock_picker import run_picker
from .auction_picker import get_auction_candidates

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
                DAILY_PICK_DATA.clear()
                DAILY_PICK_DATA.update({
                    "date": today,
                    "morning": None,
                    "afternoon": None,
                    "last_update": None,
                })
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

        # 竞价预选: 15:30（收盘后自动执行第一阶段预选）
        elif current_time == "15:30" and last_executed.get("auction_preselect") != today:
            last_executed["auction_preselect"] = today
            threading.Thread(target=auto_auction_preselect, daemon=True).start()
            print("⏰ 竞价预选任务已触发 (15:30)")

        # 每分钟检查一次
        time.sleep(60)


def auto_auction_preselect():
    """收盘后自动执行竞价预选（第一阶段）"""
    global AUCTION_PICK_DATA
    try:
        print("🌙 自动执行竞价预选（15:30定时任务）...", flush=True)
        candidates = get_auction_candidates()
        with AUCTION_PICK_LOCK:
            AUCTION_PICK_DATA['candidate_pool'] = candidates
            AUCTION_PICK_DATA['preselect_time'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            AUCTION_PICK_DATA['last_update'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            save_auction_pick_cache()
        print(f"  ✓ 自动预选完成: {len(candidates)}只候选", flush=True)
    except Exception as e:
        print(f"  ✗ 自动预选失败: {e}")
        traceback.print_exc()


def start_scheduler():
    """启动定时任务线程"""
    scheduler_thread = threading.Thread(target=schedule_daily_pick, daemon=True)
    scheduler_thread.start()
    print("✓ 定时选股任务已启动 (9:27 早盘, 14:30 午盘, 15:30 竞价预选)")
    return scheduler_thread
