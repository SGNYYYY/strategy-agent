import logging
import datetime
import time
from core.db_models import PriceMonitor, db
from core.tushare_client import TushareClient
from agents.analyst import AnalystAgent
from agents.decision_maker import DecisionMakerAgent
from core.trader import Trader
from core.notifier import DingTalkNotifier

logger = logging.getLogger(__name__)

class PriceMonitorService:
    def __init__(self):
        self.ts_client = TushareClient()
        self.analyst = AnalystAgent()
        self.decision_maker = DecisionMakerAgent()
        self.trader = Trader()
        self.notifier = DingTalkNotifier()

    def run_check(self):
        """执行一次监控循环"""
        start_time = time.time()
        # logging.info("Starting Price Monitor Cycle...") # Reduce noise
        
        # 1. 获取活跃监控单
        monitors = list(PriceMonitor.select().where(
            (PriceMonitor.status == 'ACTIVE') & 
            PriceMonitor.is_active
        ))
        
        if not monitors:
            return

        ts_codes = list(set([m.ts_code for m in monitors]))
        
        # 2. 批量获取价格
        prices = self.ts_client.get_batch_realtime_quotes(ts_codes)
        
        triggered_monitors = []
        
        # 3. 检查触发条件
        for m in monitors:
            curr_price = prices.get(m.ts_code)
            if curr_price is None:
                continue
            
            is_triggered = False
            if m.operator == 'gt' and curr_price >= m.trigger_price:
                is_triggered = True
            elif m.operator == 'lt' and curr_price <= m.trigger_price:
                is_triggered = True
            
            if is_triggered:
                triggered_monitors.append((m, curr_price))
            else:
                # 检查是否进入预警区 (Warning Zone) - 距离目标价 1% 以内
                if not m.warning_sent:
                    # 计算差距百分比
                    diff_pct = abs(curr_price - m.trigger_price) / m.trigger_price * 100
                    if diff_pct <= 1.0:
                        # 使用原子更新防止并发导致重复预警
                        # atomic update: UPDATE ... SET warning_sent=True WHERE id=... AND warning_sent=False
                        rows_updated = PriceMonitor.update(warning_sent=True).where(
                            (PriceMonitor.id == m.id) & 
                            (PriceMonitor.warning_sent == False)
                        ).execute()
                        
                        if rows_updated > 0:
                            # 发送预警 (纯文本，不调用LLM)
                            direction = "approaching UP to" if m.operator == 'gt' else "approaching DOWN to"
                            msg = f"⚠️ [Pre-Alert] {m.ts_code} is {direction} {m.trigger_price}. Current: {curr_price} (Diff: {diff_pct:.2f}%)"
                            self.notifier.send_text(msg)
                            
                            m.warning_sent = True
                            logger.info(f"Sent warning for {m.ts_code}: {msg}")

        # 4. 处理触发
        if triggered_monitors:
            logger.info(f"Monitor: Triggered {len(triggered_monitors)} signals.")
            self.handle_triggers(triggered_monitors)
        else:
            # logging.info(f"Monitor: Checked {len(monitors)} items, no triggers.")
            pass

    def handle_triggers(self, triggers):
        """处理触发列表 (串行)"""
        for monitor, price in triggers:
            logger.info(f"Processing trigger for {monitor.ts_code}: Current={price} Target={monitor.trigger_price} ({monitor.operator})")
            
            # A. 立即锁定状态，防止重入 (冷却/消耗机制)
            monitor.status = 'TRIGGERED'
            monitor.triggered_at = datetime.datetime.now()
            monitor.save()
            
            try:
                # B. 获取更详细的盘口数据交给 Analyst
                quote_data = self.ts_client.get_realtime_quote(monitor.ts_code)
                
                # C. 调用分析师进行突发分析
                # 注意：Analyst 需要新增 analyze_trigger 方法
                analysis_result = self.analyst.analyze_trigger(monitor, price, quote_data)
                
                if not analysis_result:
                    logging.warning(f"Analyst returned no result for {monitor.ts_code}")
                    continue
                
                # D. 交给决策者
                # 注意：DecisionMaker 需要新增 decide_on_trigger 方法
                orders = self.decision_maker.decide_on_trigger(analysis_result)
                
                # E. 执行交易
                if orders:
                    results = self.trader.execute_orders(orders)
                    if results:
                        msg_text = "\n".join(results)
                        reason_text = analysis_result.get('reason', 'No specific reason provided.')
                        full_msg = f"**[Trigger Executed]**\n\n{msg_text}\n\n**Reason:** {reason_text}"
                        self.notifier.send_markdown(f"Trigger: {monitor.ts_code}", full_msg)
                        logging.info(f"Trigger execution completed for {monitor.ts_code}")
                else:
                    logging.info(f"Trigger analyzed but no orders generated for {monitor.ts_code}")
                    
            except Exception as e:
                logging.error(f"Error handling trigger for {monitor.ts_code}: {e}", exc_info=True)
