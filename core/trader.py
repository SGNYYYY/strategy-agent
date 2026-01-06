from core.db_models import db, Position, Account, Order
import logging
import uuid
import datetime

class Trader:
    def execute_buy(self, ts_code, budget, reason, price_estimate, stock_name=None):
        """执行买入"""
        volume = int(budget / price_estimate / 100) * 100 # 向下取整到100股
        if volume == 0:
            logging.warning(f"Budget {budget} too low for {ts_code} at {price_estimate}")
            return None

        cost = volume * price_estimate
        name_str = f"({stock_name})" if stock_name else ""
        
        with db.atomic():
            account = Account.select().first()
            if account.cash < cost:
                logging.warning("Insufficient funds")
                return None
            
            # 更新账户
            account.cash -= cost
            account.total_assets = account.cash + account.market_value + cost # 简化计算
            account.save()

            # 更新持仓 (Upsert)
            try:
                pos = Position.get(Position.ts_code == ts_code)
                new_total_cost = (pos.avg_price * pos.volume) + cost
                pos.volume += volume
                pos.avg_price = new_total_cost / pos.volume
                pos.current_price = price_estimate
                pos.save()
            except Position.DoesNotExist:
                Position.create(ts_code=ts_code, volume=volume, avg_price=price_estimate, current_price=price_estimate)

            # 记录订单
            Order.create(
                order_id=str(uuid.uuid4()),
                ts_code=ts_code,
                action='BUY',
                price=price_estimate,
                volume=volume,
                reason=reason
            )
            logging.info(f"Executed BUY {ts_code}{name_str}: {volume} shares at {price_estimate}")
            return f"BUY {ts_code}{name_str}: {volume} @ {price_estimate}"

    def execute_sell(self, ts_code, action, reason, price_estimate, stock_name=None):
        """执行卖出"""
        with db.atomic():
            try:
                pos = Position.get(Position.ts_code == ts_code)
            except Position.DoesNotExist:
                return None

            sell_volume = 0
            if action == 'SELL_ALL':
                sell_volume = pos.volume
            elif action == 'SELL_HALF':
                sell_volume = pos.volume // 2
            
            if sell_volume == 0:
                return None

            income = sell_volume * price_estimate
            name_str = f"({stock_name})" if stock_name else ""
            
            # 更新账户
            account = Account.select().first()
            account.cash += income
            account.save()

            # 更新持仓
            pos.volume -= sell_volume
            if pos.volume == 0:
                pos.delete_instance()
            else:
                pos.save()

            # 记录订单
            Order.create(
                order_id=str(uuid.uuid4()),
                ts_code=ts_code,
                action='SELL',
                price=price_estimate,
                volume=sell_volume,
                reason=reason
            )
            logging.info(f"Executed SELL {ts_code}{name_str}: {sell_volume} shares at {price_estimate}")
            return f"SELL {ts_code}{name_str}: {sell_volume} @ {price_estimate}"
