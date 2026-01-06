from peewee import *
import datetime
import os

# 确保 data 目录存在
os.makedirs('data', exist_ok=True)

db = SqliteDatabase('data/strategy.db', pragmas={'journal_mode': 'wal'})

class BaseModel(Model):
    class Meta:
        database = db

class StockDaily(BaseModel):
    """日线行情数据"""
    ts_code = CharField(index=True)     # 股票代码
    trade_date = CharField(index=True)  # 交易日期 YYYYMMDD
    open = FloatField()
    high = FloatField()
    low = FloatField()
    close = FloatField()
    pre_close = FloatField()
    change = FloatField()               # 涨跌额
    pct_chg = FloatField()              # 涨跌幅
    vol = FloatField()                  # 成交量
    amount = FloatField()               # 成交额

    class Meta:
        indexes = (
            (('ts_code', 'trade_date'), True), # 联合唯一索引
        )

class Position(BaseModel):
    """当前持仓"""
    ts_code = CharField(unique=True)    # 股票代码
    symbol_name = CharField(null=True)  # 股票名称
    volume = IntegerField(default=0)    # 持股数量
    avg_price = FloatField(default=0.0) #不仅包含买入价格，建议每次买入加权平均
    current_price = FloatField(null=True) # 最新价格(更新用)
    market_value = FloatField(null=True)  # 最新市值
    profit = FloatField(default=0.0)      # 浮动盈亏
    last_updated = DateTimeField(default=datetime.datetime.now)

class Order(BaseModel):
    """交易记录"""
    order_id = CharField(unique=True)   # 订单ID
    ts_code = CharField()
    action = CharField()                # BUY / SELL
    price = FloatField()                # 成交价格
    volume = IntegerField()             # 成交数量
    commission = FloatField(default=0.0)# 手续费
    reason = TextField(null=True)       # 交易理由 (LLM产生)
    status = CharField(default='FILLED')# FILLED, CANCELLED
    time = DateTimeField(default=datetime.datetime.now)

class Account(BaseModel):
    """账户资金"""
    id = IntegerField(primary_key=True)
    total_assets = FloatField(default=0.0) # 总资产
    cash = FloatField(default=0.0)         # 可用资金
    market_value = FloatField(default=0.0) # 持仓市值
    updated_at = DateTimeField(default=datetime.datetime.now)

def init_db(CONFIG):
    db.connect()
    db.create_tables([StockDaily, Position, Order, Account], safe=True)
    # 初始化账户资金 (如果不存在)
    if Account.select().count() == 0:
        # 从配置读取初始资金
        initial_cash = CONFIG.get('initial_cash', 1000000.0)
        Account.create(id=1, total_assets=initial_cash, cash=initial_cash, market_value=0.0)
    db.close()

if __name__ == '__main__':
    init_db()
    print("Database initialized.")
