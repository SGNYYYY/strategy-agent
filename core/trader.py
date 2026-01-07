from core.db_models import db, Position, Account, Order
import logging
import uuid
import datetime

class Trader:
    def settle_positions(self):
        """盘前/盘后结算: 将所有持仓转为可用 (T+1 -> T)"""
        with db.atomic():
            query = Position.update(volume_available=Position.volume)
            rows = query.execute()
            logging.info(f"Positions settled: Updated {rows} records. All holdings are now available.")

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
                # pos.volume_available 保持不变 (T+1规则)
                pos.avg_price = new_total_cost / pos.volume
                pos.current_price = price_estimate
                pos.save()
            except Position.DoesNotExist:
                Position.create(ts_code=ts_code, volume=volume, avg_price=price_estimate, current_price=price_estimate, volume_available=0)

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

            available_vol = pos.volume_available
            sell_volume = 0
            if action == 'SELL_ALL':
                sell_volume = available_vol
            elif action == 'SELL_HALF':
                sell_volume = available_vol // 2
            
            if sell_volume == 0:
                logging.warning(f"Cannot sell {ts_code}: available volume is 0 (T+1).")
                return None

            income = sell_volume * price_estimate
            name_str = f"({stock_name})" if stock_name else ""
            
            # 更新账户
            account = Account.select().first()
            account.cash += income
            account.save()

            # 更新持仓
            pos.volume -= sell_volume
            pos.volume_available -= sell_volume
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

    def execute_orders(self, orders):
        """批量执行订单"""
        results = []
        for order in orders:
            try:
                ts_code = order['ts_code']
                action = order['action']
                reason = order.get('reason', '')
                price = order.get('price', 0)
                # 如果没有指定价格，可能需要再次获取或者在调用前传进来，这里假设必须传
                
                res = None
                if action == 'BUY':
                    budget = order.get('budget', 0)
                    res = self.execute_buy(ts_code, budget, reason, price)
                elif action in ['SELL', 'SELL_HALF', 'SELL_ALL', 'STOP_LOSS', 'TAKE_PROFIT']:
                    # 映射 action
                    act = action
                    if action in ['STOP_LOSS', 'TAKE_PROFIT']:
                        act = 'SELL_ALL'
                    
                    res = self.execute_sell(ts_code, act, reason, price)
                
                if res:
                    results.append(res)
            except Exception as e:
                logging.error(f"Failed to execute order {order}: {e}")
        return results
