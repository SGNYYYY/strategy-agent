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
from core.db_models import init_db, Position, PriceMonitor, Account
from core.monitor import PriceMonitorService
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
# 自定义日志过滤器：屏蔽 run_monitor_task 的 apscheduler 日志
class MonitorTaskFilter(logging.Filter):
    def filter(self, record):
        return "run_monitor_task" not in record.getMessage()

# 必须添加到具体的子 logger，因为 logging 的 filter 不会向下传播，
# 而 apscheduler 的日志是从 apscheduler.executors.default 发出的，会向上传播到 root
logging.getLogger('apscheduler.executors.default').addFilter(MonitorTaskFilter())
# logging.getLogger('apscheduler.scheduler').addFilter(MonitorTaskFilter())

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
monitor_service = PriceMonitorService()

def update_account_stats():
    """根据最新持仓市值更新账户总资产"""
    try:
        total_mv = 0.0
        # 重新计算所有持仓的市值
        for pos in Position.select():
            if pos.market_value:
                total_mv += pos.market_value
        
        account = Account.select().first()
        if account:
            account.market_value = total_mv
            # total_assets = cash + market_value
            account.total_assets = account.cash + total_mv
            account.updated_at = datetime.datetime.now()
            account.save()
            logging.info(f"Account synced: Assets={account.total_assets:.2f}, MV={total_mv:.2f}, Cash={account.cash:.2f}")
    except Exception as e:
        logging.error(f"Failed to sync account stats: {e}")

def run_pre_market_routine(test_mode=False):
    """早盘流程: 扫描 -> 分析 -> 决策 -> 买入"""
    logging.info(">>> Starting Pre-Market Routine")

    # 0. 结算持仓 (T+1 -> 可卖)
    # 每天开盘前，将所有持仓标记为可用
    trader.settle_positions()

    # 0.5 更新持仓状态 (刷新最新价格/开盘价)
    try:
        current_positions = Position.select()
        updated_count = 0
        for pos in current_positions:
            # 尝试获取实时行情(含竞价开盘)
            quote = ts_client.get_realtime_quote(pos.ts_code)
            current_price = 0.0
            
            if quote:
                try:
                    # 优先取当前价(price)，如果是0(集合竞价刚开始可能)，则取open，还不行取pre_close
                    current_price = float(quote.get('price', 0))
                    if current_price <= 0:
                        current_price = float(quote.get('open', 0))
                    if current_price <= 0:
                        current_price = float(quote.get('pre_close', 0))
                except: 
                    pass
            
            # 降级
            if current_price <= 0:
                current_price = ts_client.get_latest_price(pos.ts_code)

            if current_price > 0:
                pos.current_price = current_price
                pos.market_value = pos.volume * current_price
                if pos.volume > 0:
                    pos.profit = pos.market_value - (pos.avg_price * pos.volume)
                pos.last_updated = datetime.datetime.now()
                pos.save()
                updated_count += 1
        logging.info(f"Updated status for {updated_count} positions.")
    except Exception as e:
        logging.error(f"Failed to update positions in pre-market: {e}")
    
    # 0.6 清理旧监控 (每天都是新的开始)
    try:
        deleted = PriceMonitor.delete().where(PriceMonitor.status == 'ACTIVE').execute()
        logging.info(f"Cleared {deleted} expired monitors from previous day.")
    except Exception as e:
        logging.error(f"Failed to clear old monitors: {e}")
    
    # [新增] 同步账户资产 (基于0.5更新的持仓)
    update_account_stats()

    # 1. 确定候选池
    whitelist = set(CONFIG.get('watchlist', []))
    candidates = set(whitelist)

    # [自动补充] 将所有持仓加入候选池，确保盘中能监控到持仓的异动
    try:
        current_holdings = Position.select()
        held_codes = {p.ts_code for p in current_holdings}
        if held_codes:
            candidates.update(held_codes)
            logging.info(f"Added {len(held_codes)} held stocks to monitor candidates: {held_codes}")
    except Exception as e:
        logging.error(f"Failed to add holdings to candidates: {e}")
    
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
        
        # 获取实时竞价行情
        quote = ts_client.get_realtime_quote(ts_code)
        
        report = analyst.analyze_pre_market(ts_code, news, realtime_quote=quote)
        if report:
             logging.info(f"Report for {ts_code}: {report}")
             analyst_reports.append(report)

             # Setup Price Monitor (New Feature)
             # Restriction: All candidates (whitelist + auto-mined) are eligible for monitoring
             if 'monitor_setup' in report and isinstance(report['monitor_setup'], dict):
                 setup = report['monitor_setup']
                 try:
                     trig_price = float(setup.get('trigger_price', 0))
                     if trig_price > 0:
                         # 过滤掉过于接近当前价的无效监控 (比如偏差 < 0.5%)
                         current_p = 0.0
                         try:
                             if quote:
                                 current_p = float(quote.get('price', quote.get('open', 0)))
                         except Exception:
                             pass
                         
                         is_valid = True
                         if current_p > 0:
                             diff_pct = abs(trig_price - current_p) / current_p * 100
                             if diff_pct < 0.5:
                                logging.warning(f"Monitor skipped for {ts_code}: Target {trig_price} is too close to current {current_p} (<0.5%)")
                                is_valid = False
                         
                         if is_valid:
                             PriceMonitor.create(
                                ts_code=ts_code,
                                trigger_price=trig_price,
                                operator=setup.get('operator', 'gt'),
                                monitor_type=setup.get('monitor_type', 'signal'),
                                reason=setup.get('reason', 'Pre-market setup'),
                                status='ACTIVE'
                             )
                             logging.info(f"Monitor SETUP: {ts_code} at {trig_price}")
                 except Exception as e:
                     logging.error(f"Failed to create monitor: {e}")

    # 4. 决策
    max_pos_pct = CONFIG['settings'].get('max_position_per_stock', 1.0)
    buy_orders = decision_maker.make_buy_decision(analyst_reports, max_position_pct=max_pos_pct)
    
    execution_logs = []
    recommendations_msg = []
    
    # 提取所有高信心的分析师推荐
    for report in analyst_reports:
        if report.get('action') == 'BUY' and float(report.get('confidence', 0)) >= 7.0:
            ts_code = report['ts_code']
            stock_name = ts_client.get_stock_name(ts_code) or ts_code
            recommendations_msg.append(f"**{stock_name} ({ts_code})** - 信心: {report.get('confidence')}\n   _Reason: {report.get('reason')}_")

    if buy_orders:
        for order in buy_orders:
            ts_code = order['ts_code']
            budget = order['budget']
            reason = order['reason']
            # 获取参考价格 (昨收)
            price = ts_client.get_latest_price(ts_code)
            stock_name = ts_client.get_stock_name(ts_code)
            
            if price > 0:
                res = trader.execute_buy(ts_code, budget, reason, price, stock_name=stock_name)
                if res: 
                    # 增加理由到通知
                    execution_logs.append(f"{res}")
                else:
                    execution_logs.append(f"❌ Failed to buy {stock_name}: Check logs for details.")
    
    # 5. 推送
    if recommendations_msg or execution_logs:
        msg = "**早盘策略报告** \n\n"
        
        if recommendations_msg:
             msg += "💡 **AI 重点推荐 (关注):** \n" + "\n".join([f"- {s}" for s in recommendations_msg]) + "\n\n"
        else:
             msg += "💡 **AI 重点推荐:** 本次扫描无高信心标的。\n\n"
             
        if execution_logs:
            msg += "✅ **机器人执行操作:** \n" + "\n".join([f"- {l}" for l in execution_logs])
        else:
            msg += "✋ **机器人执行操作:** 无 (未满足资金/风控条件)"
            
        notifier.send_markdown("早盘策略", msg)
    else:
        if test_mode:
            notifier.send_markdown("早盘策略", "**早盘策略报告** \n\n今日无买入计划，亦无推荐。")
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
            
            # 更新持仓市值和盈亏
            pos.market_value = pos.volume * current_price
            if pos.volume > 0:
                pos.profit = pos.market_value - (pos.avg_price * pos.volume)
            pos.last_updated = datetime.datetime.now()
            pos.save()

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
                        if res: 
                            execution_logs.append(f"{res}\n  _Reason: {sell_order['reason']}_")
                        else:
                            execution_logs.append(f"❌ Failed to SELL {sell_order['ts_code']}: Check logs.")
                
                # 情况B: 加仓建议
                elif action == 'BUY':
                    logging.info(f"Analyst suggests ADDING position for {pos.ts_code}")
                    buy_candidates_reports.append(report)
    # [新增] 更新账户市值
    update_account_stats()
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
                if res: 
                    execution_logs.append(f"{res}\n  _Reason: {reason}_")
                else:
                    execution_logs.append(f"❌ Failed to BUY {ts_code}: Check logs.")

    # 4. 推送
    midday_recs = []
    for r in buy_candidates_reports:
         if float(r.get('confidence', 0)) >= 7.0:
             n = ts_client.get_stock_name(r['ts_code']) or r['ts_code']
             midday_recs.append(f"{n} ({r['ts_code']}) - Buy Signal (Conf: {r.get('confidence')})")

    if midday_recs or execution_logs:
        msg = "**盘中策略报告(午间)** \n\n"
        
        if midday_recs:
            msg += "💡 **发现买入机会:** \n" + "\n".join([f"- {s}" for s in midday_recs]) + "\n\n"
            
        if execution_logs:
            msg += "🔔 **执行操作(买/卖):** \n" + "\n".join([f"- {l}" for l in execution_logs])
        else:
            msg += "🔔 **执行操作:** 无 (未满足条件)."
            
        notifier.send_markdown("盘中操作", msg)
    else:
        if test_mode:
            notifier.send_markdown("盘中报告", "**盘中分析完成** \n\n无重磅信号。")
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
            pos.market_value = pos.volume * current_price
            if pos.volume > 0:
                pos.profit = pos.market_value - (pos.avg_price * pos.volume)
            pos.last_updated = datetime.datetime.now()
            pos.save()
        
        # 2. 分析
        report = analyst.analyze_pre_close(pos)
        
        # 3. 决策
        sell_order = decision_maker.make_sell_decision(report)
        
        # 4. 执行
        if sell_order:
            stock_name = ts_client.get_stock_name(sell_order['ts_code'])
            res = trader.execute_sell(sell_order['ts_code'], sell_order['action'], sell_order['reason'], current_price, stock_name=stock_name)
            if res: 
                execution_logs.append(f"{res}\n  _Reason: {sell_order['reason']}_")
            else:
                execution_logs.append(f"❌ Failed to SELL {sell_order['ts_code']} ({stock_name}): Check logs.")

    # [新增] 更新账户市值
    update_account_stats()

    # 5. 推送
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

def run_monitor_task():
    """实时价格监控任务"""
    now_dt = datetime.datetime.now()
    
    # 1. 排除周末 (0-4 为周一到周五)
    if now_dt.weekday() > 4:
        return

    now = now_dt.time()
    # 简单的交易时间判断 (9:30 - 11:29, 13:00 - 14:49)
    # 放宽前后几分钟，确保集合竞价和尾盘数据能覆盖
    start_am = datetime.time(9, 30)
    end_am = datetime.time(11, 29)
    start_pm = datetime.time(13, 0)
    end_pm = datetime.time(14, 49) 
    
    if (start_am <= now <= end_am) or (start_pm <= now <= end_pm):
        try:
            monitor_service.run_check()
        except Exception as e:
            logging.error(f"Monitor task error: {e}")

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
    parser.add_argument('--init-data', action='store_true', help='初始化历史数据')
    args = parser.parse_args()

    # 手动触发模式
    if args.pre_market or args.midday or args.pre_close or args.sync or args.init_data:
        if args.init_data:
            logging.info("Initializing history data for watchlist...")
            for stock in CONFIG.get('watchlist', []):
                ts_client.init_history_data(stock)
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

    # 监控任务 (默认 120s)
    monitor_interval = 120
    if 'settings' in CONFIG and 'monitor_interval' in CONFIG['settings']:
        monitor_interval = CONFIG['settings']['monitor_interval']
    # IntervalTrigger DOES NOT support day_of_week argument directly. 
    # Logic for checking weekday/time is already inside run_monitor_task.
    scheduler.add_job(run_monitor_task, 'interval', seconds=monitor_interval)

    logging.info("Agent Scheduler Started. Press Ctrl+C to exit.")
    print("Agent is running...")
    
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        pass
