# trading_engine.py
import asyncio
import time
import json
from agent_core import Agent, Position, TradeLog, WealthSnapshot, calculate_gini, db
from config import MAX_DRAWDOWN_LIMIT
from llm_workers import request_reflection

# 虚拟的本地市场状态 (由回测器或WebSocket更新)
market_state = {}


def get_macro_trend():
    """宏观大盘过滤：根据 BTC 的 RSI 判断当前市场环境"""
    btc_data = market_state.get('BTCUSDT', {})
    btc_rsi = btc_data.get('rsi', 50)
    if btc_rsi > 60:
        return 'BULL'
    elif btc_rsi < 40:
        return 'BEAR'
    return 'FLAT'


async def evaluate_agent_signals(agent, symbol, data):
    rsi = data.get('rsi', 50.0)
    score = 0
    macro_trend = get_macro_trend()

    # 宏观过滤：熊市中，趋势跟随者极其谨慎做多
    if macro_trend == 'BEAR' and agent.strategy_type == 'trend':
        if rsi > 65:
            score -= 5  # 压制做多冲动
        elif rsi < 35:
            score -= 20  # 顺势做空

    # 原有逻辑
    if agent.strategy_type == 'reversion':
        if rsi < 25:
            score += 15
        elif rsi > 75:
            score -= 15
    elif agent.strategy_type in ['trend', 'sniper']:
        if rsi > 65 and rsi < 85:
            score += 15
        elif rsi < 35 and rsi > 15:
            score -= 15

    if rsi > 85:
        score += 10 if agent.gene_fomo > 0.8 else -5
    elif rsi < 15:
        score -= 10 if agent.gene_panic > 0.8 else 5

    return score


async def trading_loop():
    print("⚔️ 社会化宏观撮合引擎已启动...")
    last_snapshot_time = 0

    while True:
        await asyncio.sleep(0.1)  # 提高引擎空转速度，适配回测
        if not market_state: continue
        current_time = time.time()

        # 1. 每日社会财富快照 (按模拟时间，每 24 小时记录一次)
        if current_time - last_snapshot_time > 86400:  # 86400秒 = 1天
            alive_agents = list(Agent.select().where(Agent.status == 'ACTIVE'))
            wealths = [a.current_balance for a in alive_agents]
            if wealths:
                wealths.sort(reverse=True)
                total_w = sum(wealths)
                gini = calculate_gini(wealths)
                top_1_idx = max(1, len(wealths) // 100)
                bottom_50_idx = len(wealths) // 2

                WealthSnapshot.create(
                    timestamp=current_time,
                    total_wealth=total_w,
                    gini_coefficient=gini,
                    top_1_percent_wealth_ratio=sum(wealths[:top_1_idx]) / total_w,
                    bottom_50_percent_wealth_ratio=sum(wealths[-bottom_50_idx:]) / total_w if bottom_50_idx > 0 else 0,
                    alive_count=len(wealths)
                )
            last_snapshot_time = current_time

        # 2. 检查已有持仓与清算
        positions = list(Position.select())
        for pos in positions:
            if pos.symbol not in market_state: continue
            current_price = market_state[pos.symbol]['price']
            agent = Agent.get_or_none(Agent.agent_id == pos.agent_id)
            if not agent: continue

            pnl = (current_price - pos.entry_price) * pos.size if pos.side == 'LONG' else (
                                                                                                      pos.entry_price - current_price) * pos.size

            # 【加速淘汰规则】：回撤超过设定值 (比如50%) 直接破产清理！
            current_equity = agent.current_balance + pnl
            if current_equity > agent.peak_balance:
                agent.peak_balance = current_equity

            drawdown = (agent.peak_balance - current_equity) / agent.peak_balance if agent.peak_balance > 0 else 0

            margin_used = (pos.entry_price * pos.size) / max(1, pos.leverage)

            if margin_used + pnl <= 0 or current_equity < 10 or drawdown > MAX_DRAWDOWN_LIMIT:
                agent.status = 'DEAD'
                agent.max_drawdown = max(agent.max_drawdown, drawdown)
                agent.current_balance = 0
                agent.save()
                pos.delete_instance()
                TradeLog.create(agent_id=agent.agent_id, symbol=pos.symbol, action='LIQUIDATED_OR_BANKRUPT',
                                price=current_price, size=pos.size, leverage=pos.leverage, pnl=-margin_used,
                                timestamp=current_time)

                # 触发个人档案归档
                from agent_core import archive_dead_agent
                archive_dead_agent(agent)
                continue

            close_reason = None
            if pos.side == 'LONG' and current_price <= pos.sl_price:
                close_reason = 'SL'
            elif pos.side == 'LONG' and current_price >= pos.tp_price:
                close_reason = 'TP'
            elif pos.side == 'SHORT' and current_price >= pos.sl_price:
                close_reason = 'SL'
            elif pos.side == 'SHORT' and current_price <= pos.tp_price:
                close_reason = 'TP'

            if close_reason:
                agent.current_balance += pnl
                agent.total_trades += 1
                agent.max_drawdown = max(agent.max_drawdown, drawdown)
                agent.last_action_time = current_time
                agent.save()
                pos.delete_instance()
                TradeLog.create(agent_id=agent.agent_id, symbol=pos.symbol, action=f'CLOSE_{pos.side}',
                                price=current_price, size=pos.size, leverage=pos.leverage, pnl=pnl,
                                timestamp=current_time, reason=close_reason)

                if close_reason == 'SL' and pnl < 0:
                    asyncio.create_task(request_reflection(agent.agent_id))

        # 3. 寻找建仓机会
        active_agents = Agent.select().where(Agent.status == 'ACTIVE')
        for agent in active_agents:
            if Position.select().where(Position.agent_id == agent.agent_id).count() >= 1:
                continue
            try:
                favorite_symbols = json.loads(agent.gene_favorite_symbols)
            except:
                favorite_symbols = []

            for symbol, data in market_state.items():
                if favorite_symbols and symbol not in favorite_symbols: continue
                score = await evaluate_agent_signals(agent, symbol, data)

                if abs(score) >= 15:
                    side = 'LONG' if score > 0 else 'SHORT'
                    price = data['price'];
                    atr = data['atr']
                    trade_capital = agent.current_balance * agent.gene_position_size
                    if trade_capital < 10: continue

                    safe_lev = max(1, agent.gene_leverage)
                    size = (trade_capital * safe_lev) / price

                    sl = price - (atr * agent.gene_sl_atr) if side == 'LONG' else price + (atr * agent.gene_sl_atr)
                    tp = price + (atr * agent.gene_tp_atr) if side == 'LONG' else price - (atr * agent.gene_tp_atr)

                    Position.create(agent_id=agent.agent_id, symbol=symbol, side=side, entry_price=price, size=size,
                                    leverage=safe_lev, sl_price=sl, tp_price=tp)
                    agent.last_action_time = current_time
                    agent.save()
                    TradeLog.create(agent_id=agent.agent_id, symbol=symbol, action=side, price=price, size=size,
                                    leverage=safe_lev, timestamp=current_time)
                    break