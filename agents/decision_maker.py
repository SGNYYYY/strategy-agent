from agents.base import BaseAgent
from core.db_models import Account, Position
import logging

class DecisionMakerAgent(BaseAgent):
    def make_buy_decision(self, analyst_reports, max_position_pct=1.0):
        """生成买入决策"""
        # 过滤掉 WAIT 的报告，且信心分数需大于某个阈值 (例如 7.0)以增强鲁棒性
        buy_candidates = [
            r for r in analyst_reports 
            if r and r.get('action') == 'BUY' and float(r.get('confidence', 0)) >= 7.0
        ]
        
        if not buy_candidates:
            return []

        account = Account.select().first()
        if not account:
            logging.error("Account not found!")
            return []
        
        # 计算总资产和单只个股限额
        total_assets = account.total_assets
        max_single_position = round(total_assets * max_position_pct, 2)
        
        # 获取当前持仓用于上下文(避免重复买入同类?)
        positions = Position.select()
        holdings_summary = ", ".join([p.ts_code for p in positions])

        # 渲染Prompt
        prompt = self.render_prompt('decision_maker.j2',
                                    cash=account.cash,
                                    holdings_summary=holdings_summary,
                                    analyst_reports=str(buy_candidates),
                                    max_buy_count=5,
                                    max_single_position=max_single_position) # 假设每次最多买5只

        logging.info("Decision Maker evaluating buy candidates...")
        result = self.call_llm(prompt, json_mode=True)
        
        if result and 'orders' in result:
            orders = result['orders']
            # 双重检查: 强制执行风控限制
            for order in orders:
                if order['budget'] > max_single_position:
                    logging.warning(f"Order budget {order['budget']} exceeds max limit {max_single_position}. Capped.")
                    order['budget'] = max_single_position
            return orders
        return []

    def make_sell_decision(self, analysis_result):
        """生成卖出决策 (针对单只股票)"""
        # 这里逻辑比较简单，直接透传分析师的特定指令，或者在此处增加资金管理层判断
        if not analysis_result:
            return None
            
        action = analysis_result.get('action')
        if action in ['SELL_ALL', 'SELL_HALF']:
            return {
                'ts_code': analysis_result['ts_code'],
                'action': action,
                'reason': analysis_result.get('reason')
            }
        return None

    def decide_on_trigger(self, analysis_result):
        """决策：基于即时触发分析结果"""
        if not analysis_result:
            return []
        
        action = analysis_result.get('action', 'HOLD')
        ts_code = analysis_result.get('ts_code')
        confidence = float(analysis_result.get('confidence', 0))
        reason = analysis_result.get('reason', '')
        limit_price = analysis_result.get('price_limit', 0.0)
        
        logging.info(f"DecisionMaker evaluating trigger: {action} (Conf={confidence})")
        
        if confidence < 7.0 and action != 'HOLD':
            logging.info(f"Trigger action {action} rejected due to low confidence {confidence}")
            return []
        
        orders = []
        if action == 'BUY':
             # 简单的风控：单笔不超过 20% 现金 或 50000
             try:
                 account = Account.select().first()
                 if account and account.cash > 5000:
                     budget = min(50000, account.cash * 0.2)
                     orders.append({
                         'ts_code': ts_code,
                         'action': 'BUY',
                         'budget': budget,
                         'price': limit_price,
                         'reason': f"Trigger Exec: {reason}"
                     })
             except Exception:
                 pass

        elif action in ['SELL', 'STOP_LOSS', 'TAKE_PROFIT']:
             try:
                 pos = Position.select().where(Position.ts_code == ts_code).first()
                 if pos and pos.volume_available > 0:
                     orders.append({
                         'ts_code': ts_code,
                         'action': 'SELL',
                         'volume': pos.volume_available, # 默认清仓，后续可精细化
                         'price': limit_price,
                         'reason': f"Trigger Exec: {reason}"
                     })
             except Exception:
                 pass
                 
        return orders
