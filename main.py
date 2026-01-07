import yaml
import time
import logging
import datetime
import os
import argparse
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from core.tushare_client import TushareClient
from core.scanner import MarketScanner
from core.notifier import DingTalkNotifier
from core.trader import Trader
from core.news_client import NewsClient
from core.db_models import init_db, Position
from agents.analyst import AnalystAgent
from agents.decision_maker import DecisionMakerAgent

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("logs/agent.log"),
        logging.StreamHandler()
    ]
)

# 加载配置
with open("config.yaml", "r") as f:
    CONFIG = yaml.safe_load(f)

# 初始化组件
ts_client = TushareClient()
scanner = MarketScanner()
news_client = NewsClient()
notifier = DingTalkNotifier() # 确保 .env 配置了 Token
trader = Trader()
analyst = AnalystAgent()
decision_maker = DecisionMakerAgent()

def run_pre_market_routine(test_mode=False):
    """早盘流程: 扫描 -> 分析 -> 决策 -> 买入"""
    logging.info(">>> Starting Pre-Market Routine")

    # 0. 结算持仓 (T+1 -> 可卖)
    # 每天开盘前，将所有持仓标记为可用
    trader.settle_positions()
    
    # 1. 确定候选池
    candidates = set(CONFIG.get('watchlist', []))
    
    # 2. 自动挖掘 (如果开启)
    if CONFIG['settings'].get('enable_auto_mining'):
        scanned_stocks = scanner.scan_hot_stocks(limit=5)
        candidates.update(scanned_stocks)
        logging.info(f"Added scanned stocks: {scanned_stocks}")

    candidates = list(candidates)
    
    # 3. 逐个分析
    analyst_reports = []
    for ts_code in candidates:
        # 获取最新历史数据 (如不存在则初始化)
        ts_client.init_history_data(ts_code, years=1)
        
        # 获取个股新闻 (AkShare)
        news = news_client.get_stock_news(ts_code, limit=3)
        
        report = analyst.analyze_pre_market(ts_code, news)
        if report:
             logging.info(f"Report for {ts_code}: {report}")
             analyst_reports.append(report)

    # 4. 决策
    max_pos_pct = CONFIG['settings'].get('max_position_per_stock', 1.0)
    buy_orders = decision_maker.make_buy_decision(analyst_reports, max_position_pct=max_pos_pct)
    
    execution_logs = []
    suggested_ops = []

    if buy_orders:
        for order in buy_orders:
            ts_code = order['ts_code']
            budget = order['budget']
            reason = order['reason']
            # 获取参考价格 (昨收)
            price = ts_client.get_latest_price(ts_code)
            stock_name = ts_client.get_stock_name(ts_code)
            
            # 记录建议信息
            suggested_ops.append(f"{ts_code} ({stock_name if stock_name else '未知'}): 预算 {budget}")

            if price > 0:
                res = trader.execute_buy(ts_code, budget, reason, price, stock_name=stock_name)
                if res: execution_logs.append(res)
    
    # 5. 推送
    if suggested_ops:
        msg = "**早盘策略报告** \n\n"
        msg += "💡 **AI决策建议:** \n" + "\n".join([f"- {s}" for s in suggested_ops]) + "\n\n"
        
        if execution_logs:
            msg += "✅ **计划执行买入:** \n" + "\n".join([f"- {l}" for l in execution_logs])
        else:
            msg += "⚠️ **未实际执行** (可能资金不足或价格无效)"
            
        notifier.send_markdown("早盘策略", msg)
    else:
        if test_mode:
            notifier.send_markdown("早盘策略", "**早盘策略报告** \n\n今日无买入计划。")
        logging.info("今日无买入计划，不发送通知。")
    logging.info("<<< Pre-Market Routine Finished")

def run_midday_routine(test_mode=False):
    """午间休盘前分析: 风控(止盈/止损) + 机会(加仓/买入)"""
    logging.info(">>> Starting Midday Routine")
    
    execution_logs = []
    buy_candidates_reports = [] # 收集买入建议

    # 1. 遍历持仓 (检查卖出 或 加仓)
    positions = Position.select()
    held_codes = set()
    for pos in positions:
        held_codes.add(pos.ts_code)
        
        # 获取实时价格
        quote = ts_client.get_realtime_quote(pos.ts_code)
        current_price = 0.0
        if quote:
            try:
                current_price = float(quote.get('price', quote.get('close', 0)))
            except: pass
        if current_price <= 0:
             current_price = ts_client.get_latest_price(pos.ts_code)
        
        if current_price > 0:
            pos.current_price = current_price
            # pos.save() # Optional

            # 分析
            report = analyst.analyze_intra_day(pos.ts_code, current_price, position=pos, quote_data=quote)
            
            if report:
                action = report.get('action')
                # 情况A: 卖出建议
                if action in ['SELL_ALL', 'SELL_HALF']:
                    sell_order = decision_maker.make_sell_decision(report) # 简单透传
                    if sell_order:
                        stock_name = ts_client.get_stock_name(sell_order['ts_code'])
                        res = trader.execute_sell(sell_order['ts_code'], sell_order['action'], sell_order['reason'], current_price, stock_name=stock_name)
                        if res: execution_logs.append(res)
                
                # 情况B: 加仓建议
                elif action == 'BUY':
                    logging.info(f"Analyst suggests ADDING position for {pos.ts_code}")
                    buy_candidates_reports.append(report)

    # 2. 遍历 Watchlist (检查新开仓) - 仅检查非持仓部分
    watchlist = set(CONFIG.get('watchlist', []))
    new_candidates = watchlist - held_codes
    
    for ts_code in new_candidates:
        quote = ts_client.get_realtime_quote(ts_code)
        current_price = 0.0
        if quote:
            try:
                current_price = float(quote.get('price', quote.get('close', 0)))
            except: pass
        
        if current_price > 0:
            # 分析 (非持仓)
            report = analyst.analyze_intra_day(ts_code, current_price, position=None, quote_data=quote)
            if report and report.get('action') == 'BUY':
                logging.info(f"Analyst suggests BUYING new stock {ts_code}")
                buy_candidates_reports.append(report)
                
    # 3. 统一执行买入决策 (资金分配)
    if buy_candidates_reports:
        # 复用 make_buy_decision (注意: 它会检查最大持仓比例)
        # 传入的 reports 已经混合了 加仓 和 新开仓
        max_pos_pct = CONFIG['settings'].get('max_position_per_stock', 1.0)
        buy_orders = decision_maker.make_buy_decision(buy_candidates_reports, max_position_pct=max_pos_pct)
        
        for order in buy_orders:
            ts_code = order['ts_code']
            budget = order['budget']
            reason = order['reason']
            # 重新获取价格或使用之前的
            price = ts_client.get_latest_price(ts_code)
            stock_name = ts_client.get_stock_name(ts_code)
            
            if price > 0:
                res = trader.execute_buy(ts_code, budget, reason, price, stock_name=stock_name)
                if res: execution_logs.append(res)

    # 4. 推送
    if execution_logs:
        msg = "**盘中风控报告(午间)** \n\n"
        msg += "🔔 **执行操作(买/卖):** \n" + "\n".join([f"- {l}" for l in execution_logs])
        notifier.send_markdown("盘中操作", msg)
    else:
        if test_mode:
            notifier.send_markdown("盘中报告", "**盘中分析完成** \n\n无操作建议。")
        logging.info("Midday check finished, no action.")

def run_pre_close_routine(test_mode=False):
    """尾盘流程: 监控持仓 -> 分析 -> 卖出"""
    logging.info(">>> Starting Pre-Close Routine")
    
    positions = Position.select()
    if not positions:
        logging.info("No positions held.")
        return

    execution_logs = []
    
    for pos in positions:
        # 1. 更新最新价格
        current_price = ts_client.get_latest_price(pos.ts_code)
        if current_price > 0:
            pos.current_price = current_price
            pos.save()
        
        # 2. 分析
        report = analyst.analyze_pre_close(pos)
        
        # 3. 决策
        sell_order = decision_maker.make_sell_decision(report)
        
        # 4. 执行
        if sell_order:
            stock_name = ts_client.get_stock_name(sell_order['ts_code'])
            res = trader.execute_sell(sell_order['ts_code'], sell_order['action'], sell_order['reason'], current_price, stock_name=stock_name)
            if res: execution_logs.append(res)

    # 5. 推送
    msg = "**尾盘风控报告** \n\n"
    if execution_logs:
        msg = "**尾盘风控报告** \n\n"
        msg += "⚠️ **触发卖出信号:** \n" + "\n".join([f"- {l}" for l in execution_logs])
        notifier.send_markdown("尾盘风控", msg)
    else:
        if test_mode:
            notifier.send_markdown("尾盘风控", "**尾盘风控报告** \n\n持仓稳健，无需卖出。")
        logging.info("持仓稳健，不发送通知。")

def run_data_sync_routine(test_mode=False):
    """盘后数据同步"""
    logging.info(">>> Starting Data Sync")
    # 同步 Watchlist
    for ts_code in CONFIG.get('watchlist', []):
        ts_client.append_daily_data(ts_code)
    
    # 同步持仓
    for pos in Position.select():
        ts_client.append_daily_data(pos.ts_code)
    logging.info("<<< Data Sync Finished")

if __name__ == "__main__":
    # 初始化数据库
    init_db(CONFIG)

    # 参数解析
    parser = argparse.ArgumentParser(description="Strategy Agent")
    parser.add_argument('--test', action='store_true', help='运行测试模式')
    parser.add_argument('--pre-market', action='store_true', help='立即运行早盘策略')
    parser.add_argument('--midday', action='store_true', help='立即运行午间策略')
    parser.add_argument('--pre-close', action='store_true', help='立即运行尾盘策略')
    parser.add_argument('--sync', action='store_true', help='立即运行数据同步')
    args = parser.parse_args()

    # 手动触发模式
    if args.pre_market or args.midday or args.pre_close or args.sync:
        if args.pre_market:
            run_pre_market_routine(args.test)
        if args.midday:
            run_midday_routine(args.test)
        if args.pre_close:
            run_pre_close_routine(args.test)
        if args.sync:
            run_data_sync_routine(args.test)
        logging.info("Manual execution finished.")
        exit(0)
    
    # 默认模式: 启动调度器init_db()
    
    scheduler = BlockingScheduler(timezone='Asia/Shanghai')
    
    # 从配置读取时间
    t_morning = CONFIG['schedule']['morning_routine'].split(':')
    t_midday = CONFIG['schedule']['midday_routine'].split(':')
    t_afternoon = CONFIG['schedule']['afternoon_routine'].split(':')
    t_sync = CONFIG['schedule']['data_sync'].split(':')

    scheduler.add_job(run_pre_market_routine, 'cron', hour=t_morning[0], minute=t_morning[1], day_of_week='mon-fri')
    scheduler.add_job(run_midday_routine, 'cron', hour=t_midday[0], minute=t_midday[1], day_of_week='mon-fri')
    scheduler.add_job(run_pre_close_routine, 'cron', hour=t_afternoon[0], minute=t_afternoon[1], day_of_week='mon-fri')
    scheduler.add_job(run_data_sync_routine, 'cron', hour=t_sync[0], minute=t_sync[1], day_of_week='mon-fri')

    logging.info("Agent Scheduler Started. Press Ctrl+C to exit.")
    print("Agent is running...")
    
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        pass
