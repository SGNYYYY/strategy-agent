from agents.base import BaseAgent
from core.db_models import StockDaily
import logging
import datetime

class AnalystAgent(BaseAgent):
    def analyze_pre_market(self, ts_code, news_context="", realtime_quote=None):
        """开盘前分析"""
        current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 获取最近30天数据 (约1.5个月)
        # 30天数据足以让LLM识别近期趋势(如20日均线形态)和关键支撑/压力位，
        # 同时显著降低Token消耗和上下文噪音，提高分析响应速度。
        records = StockDaily.select().where(StockDaily.ts_code == ts_code).order_by(StockDaily.trade_date.desc()).limit(30)
        # 将记录反转，按时间正序排列
        records = sorted(records, key=lambda r: r.trade_date)
        
        history_data = ""
        for r in records:
            history_data += f"Date: {r.trade_date}, Open: {r.open}, Close: {r.close}, High: {r.high}, Low: {r.low}, Vol: {r.vol}, Pct: {r.pct_chg}%\n"

        # 整理实时竞价数据
        auction_info = "N/A"
        if realtime_quote:
            try:
                open_price = float(realtime_quote.get('open', 0))
                pre_close = float(realtime_quote.get('pre_close', 0))
                current = float(realtime_quote.get('price', 0))
                bid1 = float(realtime_quote.get('bid1', 0)) # 买一
                ask1 = float(realtime_quote.get('ask1', 0)) # 卖一
                auction_info = f"Open: {open_price}, Pre_Close: {pre_close}, Current: {current}, Bid1: {bid1}, Ask1: {ask1}"
                if pre_close > 0:
                     pct = round((open_price - pre_close) / pre_close * 100, 2)
                     auction_info += f", Open Pct: {pct}%"
            except:
                pass

        prompt = self.render_prompt('analysis_pre_market.j2', 
                                    ts_code=ts_code, 
                                    history_data=history_data,
                                    news_context=news_context,
                                    auction_info=auction_info,
                                    current_time=current_time)
        
        logging.info(f"Analyst processing {ts_code}...")
        result = self.call_llm(prompt, json_mode=True)
        return result

    def analyze_pre_close(self, position):
        """收盘前分析"""
        current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # 假设 position 已包含最新实时价格信息(在外部循环更新过)
        pnl_pct = 0.0
        if position.avg_price > 0:
            pnl_pct = round((position.current_price - position.avg_price) / position.avg_price * 100, 2)

        prompt = self.render_prompt('analysis_pre_close.j2',
                                    ts_code=position.ts_code,
                                    volume=position.volume,
                                    avg_price=position.avg_price,
                                    current_price=position.current_price,
                                    pnl_pct=pnl_pct,
                                    open=position.current_price, # 暂用当前价代替，实际应从Tushare取当日Open
                                    high=position.current_price,
                                    low=position.current_price,
                                    close=position.current_price,
                                    current_time=current_time)
        
        logging.info(f"Analyst reviewing holding {position.ts_code}...")
        result = self.call_llm(prompt, json_mode=True)
        return result

    def analyze_intra_day(self, ts_code, current_price, position=None, quote_data=None):
        """盘中(午间)分析: 支持持仓和非持仓"""
        current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        is_holding = False
        volume = 0
        avg_price = 0.0
        pnl_pct = 0.0

        if position:
            is_holding = True
            volume = position.volume
            avg_price = position.avg_price
            if avg_price > 0:
                pnl_pct = round((current_price - avg_price) / avg_price * 100, 2)
        
        # 默认值
        open_p = current_price
        high_p = current_price
        low_p = current_price
        close_p = current_price

        if quote_data:
            try:
                open_p = float(quote_data.get('open', open_p))
                high_p = float(quote_data.get('high', high_p))
                low_p = float(quote_data.get('low', low_p))
                # 兼容 price 或 close
                close_p = float(quote_data.get('price', quote_data.get('close', close_p)))
            except Exception as e:
                logging.warning(f"Error parsing quote data: {e}")

        prompt = self.render_prompt('analysis_intra_day.j2', 
                                    ts_code=ts_code, 
                                    is_holding=is_holding,
                                    volume=volume,
                                    avg_price=avg_price,
                                    current_price=current_price,
                                    pnl_pct=pnl_pct,
                                    open=open_p, 
                                    high=high_p,
                                    low=low_p,
                                    close=close_p,
                                    current_time=current_time)
        
        logging.info(f"Analyst (Intra-day) reviewing {ts_code} (Holding: {is_holding})...")
        result = self.call_llm(prompt, json_mode=True)
        return result

    def analyze_trigger(self, monitor, current_price, quote_data):
        """处理价格触发事件"""
        current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        quote_info = "N/A"
        open_p, high_p, low_p = 0, 0, 0
        
        if quote_data:
            open_p = quote_data.get('open', 0)
            high_p = quote_data.get('high', 0)
            low_p = quote_data.get('low', 0)
            b1 = quote_data.get('bid1', 0)
            a1 = quote_data.get('ask1', 0)
            v = quote_data.get('volume', 0)
            quote_info = f"Bid1: {b1}, Ask1: {a1}, Vol: {v}, Open: {open_p}, High: {high_p}, Low: {low_p}"

        operator_text = "GREATER" if monitor.operator == 'gt' else "LOWER"

        prompt = self.render_prompt('analysis_trigger.j2',
            ts_code=monitor.ts_code,
            monitor_type=monitor.monitor_type,
            trigger_price=monitor.trigger_price,
            operator=operator_text,
            reason=monitor.reason,
            current_price=current_price,
            current_time=current_time,
            quote_info=quote_info,
            open=open_p,
            high=high_p,
            low=low_p,
            history_trend="N/A"
        )
        
        logging.info(f"Analyst analyzing trigger for {monitor.ts_code}...")
        result = self.call_llm(prompt, json_mode=True)
        return result
