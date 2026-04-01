# run_backtest.py
import os
import time
import json
import pandas as pd
import numpy as np
import ccxt
import ta
import uuid
import random
from tqdm import tqdm
from agent_core import Agent, Position, TradeLog, WealthSnapshot, MarketNews, calculate_gini, archive_dead_agent, \
    generate_backtested_genes, db, init_database
from config import TOTAL_AGENTS, ELITE_CLONE_RATIO, MAX_DRAWDOWN_LIMIT, INITIAL_CAPITAL, DB_PATH

# ==========================================
# ⚙️ 史诗级回测参数设置
# ==========================================
BACKTEST_DAYS = 365 * 5
TIMEFRAME = '5m'
CACHE_FILE = f"market_data_cache_{BACKTEST_DAYS}d_{TIMEFRAME}.pkl"

# 100个全市场真实币对
SYMBOLS = [
    'BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT', 'XRP/USDT', 'DOGE/USDT', 'ADA/USDT', 'AVAX/USDT',
    'SHIB/USDT', 'DOT/USDT', 'LINK/USDT', 'TRX/USDT', 'MATIC/USDT', 'UNI/USDT', 'LTC/USDT', 'BCH/USDT',
    'NEAR/USDT', 'APT/USDT', 'OP/USDT', 'ARB/USDT', 'FIL/USDT', 'INJ/USDT', 'LDO/USDT', 'RNDR/USDT',
    'ATOM/USDT', 'STX/USDT', 'IMX/USDT', 'GRT/USDT', 'VET/USDT', 'MKR/USDT', 'RUNE/USDT', 'SNX/USDT',
    'AAVE/USDT', 'ALGO/USDT', 'QNT/USDT', 'SAND/USDT', 'EGLD/USDT', 'MANA/USDT', 'THETA/USDT', 'AXS/USDT',
    'GALA/USDT', 'NEO/USDT', 'EOS/USDT', 'KAVA/USDT', 'CHZ/USDT', 'CRV/USDT', 'COMP/USDT', 'WLD/USDT'
]

print("===================================================")
print(f"🚀 Crypto Society 终极稳定版引擎 (10000 Agents | 5 Years)")
print("===================================================")

# 强制重置数据库，确保世界观统一
if os.path.exists(DB_PATH): os.remove(DB_PATH)
# 移除可能存在的WAL残留
if os.path.exists(DB_PATH + "-wal"): os.remove(DB_PATH + "-wal")
if os.path.exists(DB_PATH + "-shm"): os.remove(DB_PATH + "-shm")

init_database()


def fetch_historical_data():
    if os.path.exists(CACHE_FILE):
        print(f"📦 极速加载本地缓存 [{CACHE_FILE}]...")
        return pd.read_pickle(CACHE_FILE)

    print(f"📥 准备从币安下载 {BACKTEST_DAYS} 天高精度数据...")
    exchange = ccxt.binance({'enableRateLimit': True})
    since_ms = exchange.parse8601(str(pd.Timestamp.now() - pd.Timedelta(days=BACKTEST_DAYS)))

    valid_symbols = []
    print("🔍 正在嗅探老牌币种...")
    for symbol in SYMBOLS:
        try:
            first_candle = exchange.fetch_ohlcv(symbol, '1d', since=since_ms, limit=1)
            if first_candle and first_candle[0][0] <= since_ms + (30 * 24 * 60 * 60 * 1000):
                valid_symbols.append(symbol)
            time.sleep(0.02)
        except:
            pass

    print(f"🎯 共有 {len(valid_symbols)} 个币种符合5年历史。开始下载，请耐心等待...")

    all_data = {}
    for symbol in tqdm(valid_symbols, desc="全市场下载"):
        all_ohlcv = []
        current_since = since_ms
        while current_since < exchange.milliseconds():
            try:
                ohlcv = exchange.fetch_ohlcv(symbol, TIMEFRAME, since=current_since, limit=1000)
                if not ohlcv: break
                all_ohlcv.extend(ohlcv)
                current_since = ohlcv[-1][0] + 1
            except:
                time.sleep(1)
                continue

        if not all_ohlcv: continue
        df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
        df = df[~df.index.duplicated(keep='first')]

        # 预计算所有流派所需指标
        df['rsi'] = ta.momentum.RSIIndicator(df['close'], window=14).rsi()
        df['atr'] = df['close'].rolling(20).std() * 2
        df['ema20'] = ta.trend.ema_indicator(df['close'], window=20)
        df['ema60'] = ta.trend.ema_indicator(df['close'], window=60)
        macd_obj = ta.trend.MACD(df['close'])
        df['macd'] = macd_obj.macd()
        df['macd_signal'] = macd_obj.macd_signal()
        boll = ta.volatility.BollingerBands(df['close'], window=20, window_dev=2)
        df['bb_high'] = boll.bollinger_hband()
        df['bb_low'] = boll.bollinger_lband()

        df.dropna(inplace=True)
        all_data[symbol.replace('/', '')] = df

    print(f"💾 数据持久化至硬盘...")
    pd.to_pickle(all_data, CACHE_FILE)
    return all_data


data_dict = fetch_historical_data()
common_timestamps = sorted(list(set.union(*[set(df.index) for df in data_dict.values()])))
DEATH_LINE_USDT = INITIAL_CAPITAL * (1.0 - MAX_DRAWDOWN_LIMIT)

print("⚡ 正在构建 O(1) 时空矩阵...")
# 索引: 0:close, 1:rsi, 2:atr, 3:ema20, 4:ema60, 5:macd, 6:macd_sig, 7:bb_h, 8:bb_l
fast_market_timeline = {ts.timestamp(): {} for ts in common_timestamps}
for sym, df in data_dict.items():
    for row in df.itertuples():
        ts = row.Index.timestamp()
        if ts in fast_market_timeline:
            fast_market_timeline[ts][sym] = (row.close, row.rsi, row.atr, row.ema20, row.ema60, row.macd,
                                             row.macd_signal, row.bb_high, row.bb_low)

print("⚡ 万名交易员降维中...")
agents_mem = {}
for a in Agent.select():
    try:
        favs = set(json.loads(a.gene_favorite_symbols))
    except:
        favs = set()
    agents_mem[a.agent_id] = {
        'agent_id': a.agent_id, 'status': a.status,
        'current_balance': a.current_balance, 'peak_balance': a.peak_balance,
        'max_drawdown': a.max_drawdown, 'total_trades': a.total_trades,
        'strategy_type': a.strategy_type, 'gene_leverage': max(1, a.gene_leverage),
        'gene_position_size': a.gene_position_size, 'gene_sl_atr': a.gene_sl_atr,
        'gene_tp_atr': a.gene_tp_atr, 'gene_fomo': a.gene_fomo, 'gene_panic': a.gene_panic,
        'gene_news_weight': a.gene_news_weight, 'gene_martingale': a.gene_martingale,
        'generation': a.generation, 'last_action_time': a.last_action_time, '_fav_syms': favs, 'position': None
    }


def run_simulation():
    # ⚡ 核心：直接使用内存字典，绝不去查数据库
    agents_dict = agents_mem
    positions_mem = {}
    trade_logs_buffer = []
    dead_agent_ids = set()

    DEATH_LINE_USDT = INITIAL_CAPITAL * (1.0 - MAX_DRAWDOWN_LIMIT)
    print(f"⚖️ 绝对死亡线: < ${DEATH_LINE_USDT:.2f}")

    for step, current_time in enumerate(tqdm(common_timestamps, desc="超光速演化中")):
        ts_sec = current_time.timestamp()
        market_state = fast_market_timeline.get(ts_sec, {})
        if not market_state: continue

        btc_data = market_state.get('BTCUSDT', (0, 50, 0, 0, 0, 0, 0, 0, 0))
        btc_rsi = btc_data[1]
        macro_trend = 'BULL' if btc_rsi > 60 else ('BEAR' if btc_rsi < 40 else 'FLAT')

        # 1. 预计算流派基准分
        base_scores = {'grid': {}, 'trend': {}, 'reversion': {}, 'sniper': {}, 'hft': {}}
        for sym, d in market_state.items():
            p, rsi, _, e20, e60, m, ms, bh, bl = d
            base_scores['grid'][sym] = 20 if p < bl else (-20 if p > bh else (15 if p < e20 else -15))
            base_scores['trend'][sym] = 15 if (e20 > e60 and m > ms) else (-15 if (e20 < e60 and m < ms) else 0)
            if macro_trend == 'BEAR' and base_scores['trend'][sym] > 0: base_scores['trend'][sym] -= 10
            base_scores['reversion'][sym] = 20 if (rsi < 30 and p <= bl) else (-20 if (rsi > 70 and p >= bh) else 0)
            base_scores['sniper'][sym] = 15 if (p > bh and rsi > 65) else (-15 if (p < bl and rsi < 35) else 0)
            base_scores['hft'][sym] = 15 if (m > 0 and rsi > 50) else (-15 if (m < 0 and rsi < 50) else 0)

        # 2. 状态机循环
        for agent_id, agent in agents_dict.items():
            if agent['status'] == 'DEAD': continue

            pos = positions_mem.get(agent_id)
            equity = agent['current_balance']

            # --- A. 结算与生存校验 ---
            if pos:
                p_curr = market_state.get(pos['symbol'], (pos['entry_price'], 50))[0]
                pnl = (p_curr - pos['entry_price']) * pos['size'] if pos['side'] == 'LONG' else (pos[
                                                                                                     'entry_price'] - p_curr) * \
                                                                                                pos['size']
                locked_margin = (pos['entry_price'] * pos['size']) / agent['gene_leverage']
                cur_equity = equity + locked_margin + pnl

                if cur_equity > agent['peak_balance']: agent['peak_balance'] = cur_equity
                dd = (agent['peak_balance'] - cur_equity) / agent['peak_balance'] if agent['peak_balance'] > 0 else 0

                if cur_equity <= DEATH_LINE_USDT or cur_equity < 10 or dd > MAX_DRAWDOWN_LIMIT:
                    agent['status'] = 'DEAD';
                    agent['current_balance'] = 0;
                    dead_agent_ids.add(agent_id)
                    trade_logs_buffer.append(
                        {'agent_id': agent_id, 'symbol': pos['symbol'], 'action': 'LIQUIDATED_OR_BANKRUPT',
                         'price': p_curr, 'size': pos['size'], 'leverage': agent['gene_leverage'], 'pnl': -cur_equity,
                         'timestamp': ts_sec})
                    del positions_mem[agent_id]
                    continue

                close_reason = None
                if pos['side'] == 'LONG' and p_curr <= pos['sl_price']:
                    close_reason = 'SL'
                elif pos['side'] == 'LONG' and p_curr >= pos['tp_price']:
                    close_reason = 'TP'
                elif pos['side'] == 'SHORT' and p_curr >= pos['sl_price']:
                    close_reason = 'SL'
                elif pos['side'] == 'SHORT' and p_curr <= pos['tp_price']:
                    close_reason = 'TP'

                if close_reason:
                    net_pnl = pnl - ((p_curr * pos['size']) * 0.0004)
                    agent['current_balance'] += (locked_margin + net_pnl);
                    agent['total_trades'] += 1
                    agent['last_action_time'] = ts_sec;
                    del positions_mem[agent_id]
                    trade_logs_buffer.append(
                        {'agent_id': agent_id, 'symbol': pos['symbol'], 'action': f"CLOSE_{pos['side']}",
                         'price': p_curr, 'size': pos['size'], 'leverage': agent['gene_leverage'], 'pnl': net_pnl,
                         'timestamp': ts_sec, 'reason': close_reason})

            # --- B. 建仓校验 ---
            else:
                dd = (agent['peak_balance'] - equity) / agent['peak_balance'] if agent['peak_balance'] > 0 else 0
                if equity <= DEATH_LINE_USDT or dd > MAX_DRAWDOWN_LIMIT:
                    agent['status'] = 'DEAD';
                    agent['current_balance'] = 0;
                    dead_agent_ids.add(agent_id)
                    trade_logs_buffer.append(
                        {'agent_id': agent_id, 'symbol': 'N/A', 'action': 'LIQUIDATED_OR_BANKRUPT', 'price': 0,
                         'size': 0, 'leverage': 0, 'pnl': 0, 'timestamp': ts_sec})
                    continue

                stype = agent['strategy_type']
                cands = base_scores[stype]
                favs = agent['_fav_syms']

                for sym, b_score in cands.items():
                    if favs and sym not in favs: continue
                    data = market_state[sym]
                    score = b_score
                    if data[1] > 85:
                        score += 10 if agent['gene_fomo'] > 0.8 else -5
                    elif data[1] < 15:
                        score -= 10 if agent['gene_panic'] > 0.8 else 5

                    if abs(score) >= 15:
                        side = 'LONG' if score > 0 else 'SHORT'
                        p_in, atr_in = data[0], data[2]
                        intended_margin = agent['current_balance'] * agent['gene_position_size']
                        if intended_margin < 10: break
                        notional = min(intended_margin * agent['gene_leverage'], 500000.0)
                        fee = notional * 0.0004
                        if (notional / agent['gene_leverage']) + fee > agent['current_balance']: break

                        agent['current_balance'] -= ((notional / agent['gene_leverage']) + fee)
                        size = notional / p_in
                        sl = p_in - (atr_in * agent['gene_sl_atr']) if side == 'LONG' else p_in + (
                                    atr_in * agent['gene_sl_atr'])
                        tp = p_in + (atr_in * agent['gene_tp_atr']) if side == 'LONG' else p_in - (
                                    atr_in * agent['gene_tp_atr'])
                        positions_mem[agent_id] = {'symbol': sym, 'side': side, 'entry_price': p_in, 'size': size,
                                                   'leverage': agent['gene_leverage'], 'sl_price': sl, 'tp_price': tp}
                        agent['last_action_time'] = ts_sec
                        trade_logs_buffer.append(
                            {'agent_id': agent_id, 'symbol': sym, 'action': side, 'price': p_in, 'size': size,
                             'leverage': agent['gene_leverage'], 'pnl': 0.0, 'timestamp': ts_sec})
                        break

        if step % 20 == 0:
            live_active = [a for a in agents_mem.values() if a['status'] == 'ACTIVE']

            def get_eq(a):
                e = a['current_balance']
                p = positions_mem.get(a['agent_id'])
                if p:
                    p_c = market_state.get(p['symbol'], (p['entry_price'], 50))[0]
                    pnl = (p_c - p['entry_price']) * p['size'] if p['side'] == 'LONG' else (p['entry_price'] - p_c) * p[
                        'size']
                    locked_margin = (p['entry_price'] * p['size']) / a['gene_leverage']
                    e += (locked_margin + pnl)
                return e

            for a in live_active: a['_tmp_eq'] = get_eq(a)
            # 排序前 100 名 (Top 1%)
            rich_list = sorted(live_active, key=lambda x: x['_tmp_eq'], reverse=True)[:100]

            top_100_json = [{'agent_id': r['agent_id'], 'strategy': r['strategy_type'], 'equity': r['_tmp_eq'],
                             'trades': r['total_trades'], 'leverage': r['gene_leverage'], 'generation': r['generation']}
                            for r in rich_list]
            total_wealth = sum(a['_tmp_eq'] for a in live_active)

            with open("dashboard_data.json", "w") as f:
                json.dump({
                    "alive_count": len(live_active),
                    "dead_history_count": len(agents_mem) - len(live_active),
                    "total_actions": sum(a['total_trades'] for a in agents_mem.values()),
                    "total_wealth": total_wealth,
                    "richest_wealth": top_100_json[0]['equity'] if top_100_json else INITIAL_CAPITAL,
                    "top_100": top_100_json,  # 🚀 改为输出 Top 100
                    "last_update_time": ts_sec
                }, f)


        # --- 3. 每日结算、演化与【战报发布】 ---
        if step % 288 == 0 and step > 0:
            with db.atomic():
                if trade_logs_buffer:
                    for i in range(0, len(trade_logs_buffer), 500): TradeLog.insert_many(
                        trade_logs_buffer[i:i + 500]).execute()
                    trade_logs_buffer.clear()

                # 🚀 定义 alive_list 用于后续所有逻辑
                alive_list = [a for a in agents_dict.values() if a['status'] == 'ACTIVE']
                equity_dict = {a['agent_id']: a['current_balance'] for a in alive_list}

                for a_id, p in positions_mem.items():
                    if a_id in equity_dict:
                        m_data = market_state.get(p['symbol'], (p['entry_price'],))
                        p_pos = m_data[0]
                        pnl_f = (p_pos - p['entry_price']) * p['size'] if p['side'] == 'LONG' else (p[
                                                                                                        'entry_price'] - p_pos) * \
                                                                                                   p['size']
                        equity_dict[a_id] += max(0, ((p['entry_price'] * p['size']) / p['leverage']) + pnl_f)

                wealths = sorted(list(equity_dict.values()), reverse=True)
                if wealths:
                    total_w = sum(wealths);
                    gini = calculate_gini(wealths)
                    WealthSnapshot.create(timestamp=ts_sec, total_wealth=total_w, gini_coefficient=gini,
                                          top_1_percent_wealth_ratio=sum(
                                              wealths[:max(1, len(wealths) // 100)]) / total_w,
                                          bottom_50_percent_wealth_ratio=sum(wealths[-(len(wealths) // 2):]) / total_w,
                                          alive_count=len(wealths))
                    # 发布战报
                    with open("dashboard_data.json", "w") as f:
                        json.dump({"alive_count": len(wealths), "dead_history_count": TradeLog.select().where(
                            TradeLog.action == 'LIQUIDATED_OR_BANKRUPT').count(),
                                   "total_actions": TradeLog.select().count(), "total_wealth": total_w,
                                   "richest_wealth": wealths[0], "last_update_time": ts_sec}, f)

                if dead_agent_ids:
                    # 🚀 这里使用统一的 alive_list
                    elites = sorted(alive_list, key=lambda x: x['current_balance'], reverse=True)[:10]
                    new_borns = []
                    for d_id in list(dead_agent_ids):
                        is_clone = random.random() < ELITE_CLONE_RATIO and len(elites) > 0
                        if is_clone:
                            p = random.choice(elites)
                            genes = {'strategy_type': p['strategy_type'],
                                     'gene_leverage': max(1, p['gene_leverage'] + random.randint(-2, 2)),
                                     'gene_sl_atr': max(0.1, p['gene_sl_atr'] + random.uniform(-0.5, 0.5)),
                                     'gene_tp_atr': max(0.5, p['gene_tp_atr'] + random.uniform(-0.5, 0.5)),
                                     'gene_position_size': p['gene_position_size'], 'gene_fomo': p['gene_fomo'],
                                     'gene_panic': p['gene_panic'], 'gene_news_weight': p['gene_news_weight'],
                                     'gene_martingale': p['gene_martingale'],
                                     'gene_favorite_symbols': json.dumps(list(p['_fav_syms']))}
                            g_num = p['generation'] + 1
                        else:
                            genes = generate_backtested_genes(
                                random.choice(['hft', 'grid', 'trend', 'reversion', 'sniper']))
                            g_num = 0
                        del agents_dict[d_id]
                        new_id = f"TR_{step}_{genes['strategy_type'].upper()}_{uuid.uuid4().hex}"
                        genes.update({'agent_id': new_id, 'generation': g_num, 'status': 'ACTIVE',
                                      'initial_balance': INITIAL_CAPITAL, 'current_balance': INITIAL_CAPITAL,
                                      'peak_balance': INITIAL_CAPITAL, 'max_drawdown': 0.0, 'total_trades': 0,
                                      'last_action_time': ts_sec})
                        new_borns.append(genes)
                    if new_borns:
                        for i in range(0, len(new_borns), 500): Agent.insert_many(new_borns[i:i + 500]).execute()
                        for n in new_borns:
                            try:
                                fv = set(json.loads(n['gene_favorite_symbols']))
                            except:
                                fv = set()
                            n['_fav_syms'] = fv;
                            n['position'] = None;
                            agents_dict[n['agent_id']] = n
                    dead_id_list = list(dead_agent_ids)
                    for i in range(0, len(dead_id_list), 500): Agent.delete().where(
                        Agent.agent_id.in_(dead_id_list[i:i + 500])).execute()
                    dead_agent_ids.clear()

            # 释放连接
            try:
                db.execute_sql("PRAGMA wal_checkpoint(PASSIVE);")
            except:
                pass
            if not db.is_closed(): db.close()

    # --- 终极落盘 ---
    with db.atomic():
        if trade_logs_buffer: TradeLog.insert_many(trade_logs_buffer).execute()
        Position.delete().execute()
        final_pos = [{'agent_id': a['agent_id'], 'symbol': a['position']['symbol'], 'side': a['position']['side'],
                      'entry_price': a['position']['entry_price'], 'size': a['position']['size'],
                      'leverage': a['gene_leverage'], 'sl_price': a['position']['sl_price'],
                      'tp_price': a['position']['tp_price']} for a in agents_dict.values() if a['position']]
        if final_pos: Position.insert_many(final_pos).execute()
        upd = [(a['current_balance'], a['peak_balance'], a['max_drawdown'], a['total_trades'], a['last_action_time'],
                a['status'], a['generation'], a['agent_id']) for a in agents_dict.values()]
        from peewee import Case
        for i in range(0, len(upd), 1000):
            b = upd[i:i + 1000];
            ids = [x[7] for x in b]
            Agent.update(current_balance=Case(Agent.agent_id, [(x[7], x[0]) for x in b]),
                         peak_balance=Case(Agent.agent_id, [(x[7], x[1]) for x in b]),
                         max_drawdown=Case(Agent.agent_id, [(x[7], x[2]) for x in b]),
                         total_trades=Case(Agent.agent_id, [(x[7], x[3]) for x in b]),
                         last_action_time=Case(Agent.agent_id, [(x[7], x[4]) for x in b]),
                         status=Case(Agent.agent_id, [(x[7], x[5]) for x in b]),
                         generation=Case(Agent.agent_id, [(x[7], x[6]) for x in b])).where(
                Agent.agent_id.in_(ids)).execute()


if __name__ == '__main__':
    if len(common_timestamps) > 0:
        run_simulation()
        print("\n🎉 推演圆满结束！")