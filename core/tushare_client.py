import tushare as ts
import pandas as pd
import datetime
import time
import logging
import os
from dotenv import load_dotenv
from peewee import IntegrityError, chunked
from core.db_models import StockDaily, db

load_dotenv()

class TushareClient:
    def __init__(self):
        token = os.getenv("TUSHARE_TOKEN")
        if not token:
            logging.warning("TUSHARE_TOKEN not found in .env")
        else:
            ts.set_token(token)
        self.pro = ts.pro_api()

    def get_stock_name(self, ts_code):
        """获取股票名称"""
        try:
            df = self.pro.stock_basic(ts_code=ts_code, fields='ts_code,name')
            if not df.empty:
                return df.iloc[0]['name']
        except Exception as e:
            logging.error(f"Failed to get name for {ts_code}: {e}")
        return None

    def get_trade_cal(self, start_date, end_date):
        """获取交易日历"""
        df = self.pro.trade_cal(exchange='', start_date=start_date, end_date=end_date)
        return df[df['is_open'] == 1]['cal_date'].tolist()

    def fetch_daily(self, ts_code, start_date, end_date):
        """获取日线行情"""
        try:
            # Tushare daily 接口
            df = self.pro.daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
            if df.empty:
                return None
            # 翻转，按日期升序
            df = df.iloc[::-1]
            return df
        except Exception as e:
            logging.error(f"Tushare fetch_daily failed for {ts_code}: {e}")
            time.sleep(1) # Simple retry delay
            return None

    def save_to_db(self, df):
        """将DataFrame数据保存到StockDaily"""
        if df is None or df.empty:
            return

        data_source = []
        for index, row in df.iterrows():
            data_source.append({
                'ts_code': row['ts_code'],
                'trade_date': row['trade_date'],
                'open': row['open'],
                'high': row['high'],
                'low': row['low'],
                'close': row['close'],
                'pre_close': row['pre_close'],
                'change': row['change'],
                'pct_chg': row['pct_chg'],
                'vol': row['vol'],
                'amount': row['amount']
            })

        with db.atomic():
            # 使用 chunked 批量插入以避免 SQL 变量限制
            for batch in chunked(data_source, 100):
                # 这里的 conflict handling 很重要
                StockDaily.insert_many(batch).on_conflict_ignore().execute()

    def init_history_data(self, ts_code, years=3):
        """初始化历史数据"""
        logging.info(f"Initializing history data for {ts_code} ({years} years)...")
        end_date = datetime.datetime.now().strftime('%Y%m%d')
        start_date = (datetime.datetime.now() - datetime.timedelta(days=365 * years)).strftime('%Y%m%d')
        
        df = self.fetch_daily(ts_code, start_date, end_date)
        if df is not None:
            self.save_to_db(df)
            logging.info(f"Saved {len(df)} records for {ts_code}.")
        else:
            logging.warning(f"No history data found for {ts_code}")

    def append_daily_data(self, ts_code, execution_date=None):
        """追加单日数据"""
        if not execution_date:
            execution_date = datetime.datetime.now().strftime('%Y%m%d')
        
        logging.info(f"Appending daily data for {ts_code} on {execution_date}...")
        df = self.fetch_daily(ts_code, start_date=execution_date, end_date=execution_date)
        if df is not None and not df.empty:
            self.save_to_db(df)
            logging.info(f"Appended data for {ts_code}.")
        else:
            logging.info(f"No data for {ts_code} on {execution_date} (Market might be closed or data delay).")

    def get_realtime_quote(self, ts_code):
        """获取实时行情所有数据(dict)"""
        try:
            df = ts.realtime_quote(ts_code=ts_code)
            if df is not None and not df.empty:
                # 统一列名小写
                data = df.iloc[0].to_dict()
                return {k.lower(): v for k, v in data.items()}
        except Exception as e:
            logging.warning(f"Realtime full quote failed for {ts_code}: {e}")
        return None

    def get_latest_price(self, ts_code):
        """获取最新价格 (优先使用实时接口)"""
        # 1. 尝试使用实时接口 (需要 tushare >= 1.3.3)
        try:
            # ts.realtime_quote 是爬虫接口，数据实时性较好
            df = ts.realtime_quote(ts_code=ts_code)
            if df is not None and not df.empty:
                row = df.iloc[0]
                # 不同版本/来源可能列名不同，常见为 price 或 close
                for col in ['price', 'PRICE', 'close', 'CLOSE', 'trade']:
                    if col in row:
                        val = float(row[col])
                        if val > 0:
                            return val
        except Exception as e:
            logging.warning(f"Realtime quote failed for {ts_code}, falling back to daily: {e}")

        # 2. 降级: Daily 接口 (可能有延迟或需盘后)
        today = datetime.datetime.now().strftime('%Y%m%d')
        df = self.fetch_daily(ts_code, start_date=today, end_date=today)
        if df is None or df.empty:
            # 尝试获取上一交易日
            # 简化处理，直接取库里最新一条
            last_record = StockDaily.select().where(StockDaily.ts_code == ts_code).order_by(StockDaily.trade_date.desc()).first()
            if last_record:
                return last_record.close
            return 0.0
        return float(df.iloc[0]['close'])

    def get_batch_realtime_quotes(self, ts_code_list):
        """批量获取实时行情, 返回 {ts_code: price}"""
        if not ts_code_list:
            return {}
        try:
            # tushare realtime_quote interface works best with joined string of full codes (e.g. "000001.SZ,600000.SH")
            # It returns a DataFrame with 'TS_CODE' (or 'ts_code') column matching the input.
            all_dfs = []
            chunk_size = 80 
            
            for i in range(0, len(ts_code_list), chunk_size):
                chunk = ts_code_list[i:i+chunk_size]
                codes_str = ','.join(chunk)
                try:
                    sub_df = ts.realtime_quote(ts_code=codes_str)
                    if sub_df is not None and not sub_df.empty:
                        all_dfs.append(sub_df)
                except Exception as chunk_e:
                    logging.warning(f"Batch quote chunk failed: {chunk_e}")

            if not all_dfs:
                return {}
            
            df = pd.concat(all_dfs, ignore_index=True)
            if df.empty:
                return {}
            
            result = {}
            df.columns = [c.lower() for c in df.columns]
            
            for _, row in df.iterrows():
                # Prefer 'ts_code', fallback to 'code' if necessary (though ts_code should be present)
                code = row.get('ts_code', row.get('code'))
                # Handle price column variations
                price = 0.0
                for col in ['price', 'close', 'trade']:
                    if col in row:
                        val = float(row[col])
                        if val > 0:
                            price = val
                            break
                            
                if code and price > 0:
                    result[code] = price
            return result
        except Exception as e:
            logging.error(f"Batch realtime quote failed: {e}")
            return {}

if __name__ == "__main__":
    from core.db_models import init_db
    init_db()
    client = TushareClient()
    # Test
    client.init_history_data("000001.SZ", years=1)
