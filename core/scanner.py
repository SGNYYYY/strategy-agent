import datetime
import logging
import tushare as ts
import os
from dotenv import load_dotenv

load_dotenv()

class MarketScanner:
    def __init__(self):
        token = os.getenv("TUSHARE_TOKEN")
        if token:
            ts.set_token(token)
        self.pro = ts.pro_api()

    def scan_hot_stocks(self, limit=5):
        """扫描热门潜力股 (简化版逻辑)"""
        # 获取上一交易日（因开盘前数据未出，需看昨日表现）
        # 实际逻辑应获取最近一个有数据的交易日
        today = datetime.datetime.now().strftime('%Y%m%d')
        # 简单回推几天寻找最近交易日（Tushare API 如果当天没数据返回空）
        trade_date = self._get_last_trade_date()
        
        logging.info(f"Scanning market for date: {trade_date}")

        try:
            # 获取每日指标：换手率、量比、涨跌幅
            df = self.pro.daily_basic(ts_code='', trade_date=trade_date, 
                                      fields='ts_code,close,turnover_rate,volume_ratio,pct_chg')
            
            if df.empty:
                logging.warning("Market scan returned empty data.")
                return []

            # 筛选逻辑：
            # 1. 换手率 > 5% (活跃)
            # 2. 量比 > 1.5 (放量)
            # 3. 涨幅 在 3% - 9% 之间 (非一字涨停，有上车机会)
            candidates = df[
                (df['turnover_rate'] > 5) & 
                (df['volume_ratio'] > 1.5) & 
                (df['pct_chg'] > 3) & 
                (df['pct_chg'] < 9.5)
            ]

            # 按量比排序取前N
            candidates = candidates.sort_values(by='volume_ratio', ascending=False).head(limit)
            
            return candidates['ts_code'].tolist()

        except Exception as e:
            logging.error(f"Market scan failed: {e}")
            return []

    def _get_last_trade_date(self):
        # 简单逻辑：如果是周一早上，取上周五。这里不做复杂日历计算，
        # 如果获取不到数据 Tushare 会报错或空，实际可以用 trade_cal 优化
        now = datetime.datetime.now()
        if now.hour < 15: # 如果是盘中或盘前，取昨天
             delta = 1
        else:
             delta = 0
        
        d = now - datetime.timedelta(days=delta)
        return d.strftime('%Y%m%d')

if __name__ == "__main__":
    scanner = MarketScanner()
    stocks = scanner.scan_hot_stocks()
    print("Scanned stocks:", stocks)
