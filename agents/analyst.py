from agents.base import BaseAgent
from core.db_models import StockDaily
import logging
import datetime

class AnalystAgent(BaseAgent):
    def analyze_pre_market(self, ts_code, news_context=""):
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

        prompt = self.render_prompt('analysis_pre_market.j2', 
                                    ts_code=ts_code, 
                                    history_data=history_data,
                                    news_context=news_context,
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
