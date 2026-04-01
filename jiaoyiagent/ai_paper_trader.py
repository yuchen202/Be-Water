import os
import time
import datetime
import json
import pandas as pd
import pandas_ta as ta
import requests
import re
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ================= 0. 企业级网络防假死 =================
os.environ["trust_env"] = "False"
os.environ["no_proxy"] = "*"
_original_session_init = requests.Session.__init__


def _patched_session_init(self, *args, **kwargs):
    _original_session_init(self, *args, **kwargs)
    retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry, pool_connections=100, pool_maxsize=100)
    self.mount('http://', adapter)
    self.mount('https://', adapter)
    self.headers.update({'User-Agent': 'Mozilla/5.0', 'Connection': 'close', 'X-Forwarded-For': '114.114.114.114'})


requests.Session.__init__ = _patched_session_init

_original_request = requests.Session.request


def _patched_request(self, method, url, **kwargs):
    if 'timeout' not in kwargs or kwargs['timeout'] is None: kwargs['timeout'] = 8
    return _original_request(self, method, url, **kwargs)


requests.Session.request = _patched_request

# ================= 1. 核心配置 =================
FEISHU_WEBHOOK = ""
AI_API_KEY = ""
AI_API_URL = ""

PORTFOLIO_FILE = "ai_portfolio.json"
PARAMS_FILE = "ai_params.json"
SHARED_OPP_FILE = "shared_opportunities.json"
EXCHANGE_RATE_HKD_TO_USD = 7.80

# ================= 2. 基金账本与 AI 参数管理 =================
DEFAULT_PARAMS = {
    "risk_per_trade": 0.02,
    "atr_multiplier": 2.0,
    "rsi_threshold": 65.0,  # MACD 右侧趋势策略允许在较高 RSI 买入，只要不超过 65 (非严重超买) 即可
    "take_profit_atr": 4.0
}


def load_json(filepath, default_val):
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            pass
    return default_val


def save_json(filepath, data):
    with open(filepath, "w", encoding="utf-8") as f: json.dump(data, f, ensure_ascii=False, indent=4)


def to_usd(amount_local, market): return amount_local / EXCHANGE_RATE_HKD_TO_USD if market == "HK" else amount_local


def safe_float(val): return 0.0 if val is None or pd.isna(val) else float(val)


# ================= 3. 数据与 MACD 计算引擎 =================
def get_kline_with_macd(ticker, market):
    try:
        guess_secids = []
        if market == "US":
            guess_secids = [f"105.{ticker}", f"106.{ticker}", f"107.{ticker}"]
        else:
            guess_secids = [f"116.{ticker}", f"128.{ticker}"]

        df = None
        for secid in guess_secids:
            url = f"http://push2his.eastmoney.com/api/qt/stock/kline/get?secid={secid}&klt=101&fqt=1&lmt=500&end=20500000&iscca=1&fields1=f1,f2,f3,f4,f5,f6,f7,f8&fields2=f51,f52,f53,f54,f55,f56,f57,f58"
            try:
                resp = requests.get(url, timeout=5).json()
                if resp and resp.get("data") and resp["data"].get("klines"):
                    klines = resp["data"]["klines"]
                    parsed = [{'date': k.split(',')[0], 'open': float(k.split(',')[1]), 'close': float(k.split(',')[2]),
                               'high': float(k.split(',')[3]), 'low': float(k.split(',')[4]),
                               'volume': float(k.split(',')[5])} for k in klines]
                    df = pd.DataFrame(parsed)
                    break
            except:
                continue

        if df is None or df.empty: return None

        df['RSI'] = ta.rsi(df['close'], length=14)
        df['ATR'] = ta.atr(df['high'], df['low'], df['close'], length=14)
        df['MA200'] = ta.sma(df['close'], length=200)

        macd = ta.macd(df['close'])
        if macd is not None and not macd.empty:
            df['MACD'] = macd.iloc[:, 0]
            df['SIGNAL'] = macd.iloc[:, 1]
            df['HIST'] = macd.iloc[:, 2]
            df['HIST_PREV'] = df['HIST'].shift(1)
        else:
            df['MACD'] = df['SIGNAL'] = df['HIST'] = df['HIST_PREV'] = 0.0

        return df.iloc[-1]
    except:
        return None


def calc_position_size(total_equity_usd, price_local, atr_local, market, params):
    if pd.isna(atr_local) or atr_local <= 0: return 0, 0, 0
    max_loss_usd = total_equity_usd * params["risk_per_trade"]
    stop_distance_local = atr_local * params["atr_multiplier"]
    stop_loss_price = price_local - stop_distance_local
    take_profit_price = price_local + (atr_local * params["take_profit_atr"])
    stop_distance_usd = to_usd(stop_distance_local, market)
    if stop_distance_usd <= 0: return 0, 0, 0
    shares = int(max_loss_usd / stop_distance_usd)
    max_cash_usd = total_equity_usd * 0.25
    if to_usd(shares * price_local, market) > max_cash_usd: shares = int(max_cash_usd / to_usd(price_local, market))
    return shares, stop_loss_price, take_profit_price


def is_trading_time():
    now = datetime.datetime.now()
    if (now.weekday() == 5 and now.hour >= 6) or now.weekday() == 6 or (
            now.weekday() == 0 and now.hour < 8): return False
    return True


# ================= 4. 24小时自治交易核心 =================
def execute_trading():
    pf = load_json(PORTFOLIO_FILE,
                   {"cash_usd": 10000.0, "holdings": {}, "realized_pnl": 0.0, "total_trades": 0, "winning_trades": 0})
    params = load_json(PARAMS_FILE, DEFAULT_PARAMS)
    opps = load_json(SHARED_OPP_FILE, [])

    unrealized_pnl_usd = 0.0
    current_equity_usd = pf['cash_usd']
    market_updates, trade_actions = {}, []

    # ---------------- 1. 平仓巡逻 (加入 MACD 死叉逃顶) ----------------
    holdings_to_remove = []
    for uid, pos in pf['holdings'].items():
        market, ticker = pos['market'], pos['code']
        m = get_kline_with_macd(ticker, market)
        if m is None: continue

        current_price = safe_float(m.get('close'))
        hist = safe_float(m.get('HIST'))
        hist_prev = safe_float(m.get('HIST_PREV'))
        shares = pos['shares']

        pnl_local = (current_price - pos['cost']) * shares
        pnl_usd = to_usd(pnl_local, market)
        unrealized_pnl_usd += pnl_usd
        current_equity_usd += to_usd(current_price * shares, market)
        market_updates[uid] = {"pnl_usd": pnl_usd, "price": current_price}

        is_death_cross = (hist < 0 and hist_prev >= 0)

        if current_price <= pos['stop_loss']:
            msg = f"🔴 [止损平仓] {pos['name']}({ticker}) 击穿风控底线 ${pos['stop_loss']:.2f}。卖价: ${current_price:.2f}, 盈亏: ${pnl_usd:.2f}"
            holdings_to_remove.append((uid, pnl_usd, current_price, msg))
        elif current_price >= pos['take_profit']:
            msg = f"🟢 [止盈平仓] {pos['name']}({ticker}) 达到止盈目标 ${pos['take_profit']:.2f}！卖价: ${current_price:.2f}, 盈利: ${pnl_usd:.2f}"
            holdings_to_remove.append((uid, pnl_usd, current_price, msg))
        elif is_death_cross:
            # 不管盈亏，只要出现 MACD 死叉，说明动能终结，直接清仓逃命！
            msg = f"⚠️ [死叉逃顶] {pos['name']}({ticker}) 出现 MACD 死叉信号，动能终结，坚决平仓！卖价: ${current_price:.2f}, 盈亏: ${pnl_usd:.2f}"
            holdings_to_remove.append((uid, pnl_usd, current_price, msg))

    for uid, pnl_usd, price, msg in holdings_to_remove:
        shares = pf['holdings'][uid]['shares']
        market = pf['holdings'][uid]['market']
        pf['cash_usd'] += to_usd(price * shares, market)
        pf['realized_pnl'] += pnl_usd
        pf['total_trades'] += 1
        if pnl_usd > 0: pf['winning_trades'] += 1
        trade_actions.append(msg)
        print(f"   {msg}")
        del pf['holdings'][uid]

    # ---------------- 2. 建仓巡逻 (必须是金叉) ----------------
    MAX_POSITIONS = 5
    available_slots = MAX_POSITIONS - len(pf['holdings'])

    if available_slots > 0 and pf['cash_usd'] >= (current_equity_usd * 0.1) and opps:
        qualified = []
        for opp in opps:
            market, ticker, name = opp['market'], opp['code'], opp['name']
            uid = f"{market}_{ticker}"
            if uid in pf['holdings']: continue

            m = get_kline_with_macd(ticker, market)
            if m is None: continue

            price, rsi, atr = safe_float(m.get('close')), safe_float(m.get('RSI')), safe_float(m.get('ATR'))
            ma200 = safe_float(m.get('MA200'))
            hist, hist_prev = safe_float(m.get('HIST')), safe_float(m.get('HIST_PREV'))

            # 【基金专属纪律】：仅买入 MACD 水下金叉，且 RSI 在健康非超买区间 (<65) 的股票
            is_golden_cross = (hist > 0 and hist_prev <= 0)

            if is_golden_cross and rsi <= params['rsi_threshold']:
                # 用 (MA200 - 现价) 衡量未来反弹的赔率空间
                expectancy = (ma200 - price) / (atr * params['atr_multiplier']) if (atr > 0 and ma200 > price) else 0.1
                qualified.append(
                    {"uid": uid, "market": market, "code": ticker, "name": name, "price": price, "atr": atr,
                     "exp": expectancy})
                print(f"   ✓ 金叉确认: {name}({ticker}) | 期望盈亏比: {expectancy:.2f}")

        if qualified:
            qualified.sort(key=lambda x: x['exp'], reverse=True)
            buy_limit = min(3, available_slots)

            for target in qualified[:buy_limit]:
                shares, sl, tp = calc_position_size(current_equity_usd, target['price'], target['atr'],
                                                    target['market'], params)
                cost_usd = to_usd(shares * target['price'], target['market'])

                if shares > 0 and cost_usd <= pf['cash_usd']:
                    pf['cash_usd'] -= cost_usd
                    pf['holdings'][target['uid']] = {
                        "market": target['market'], "code": target['code'], "name": target['name'],
                        "shares": shares, "cost": target['price'], "stop_loss": sl, "take_profit": tp
                    }
                    curr_sym = "HKD" if target['market'] == "HK" else "USD"
                    msg = f"🔵 [动能建仓] MACD金叉确认！买入 {target['name']} {shares}股 @ {target['price']:.2f} {curr_sym}。止损: {sl:.2f}, 止盈: {tp:.2f}。"
                    trade_actions.append(msg)
                    print(f"   {msg}")
                    current_equity_usd = pf['cash_usd'] + sum(
                        [to_usd(pos['shares'] * pos['cost'], pos['market']) for pos in pf['holdings'].values()])

    save_json(PORTFOLIO_FILE, pf)
    return pf, current_equity_usd, unrealized_pnl_usd, market_updates, trade_actions


# ================= 5. 飞书报告与 AI 自我进化 =================
def push_report(pf, current_equity_usd, unrealized_pnl_usd, market_updates, trade_actions, is_daily_summary=False):
    win_rate = (pf['winning_trades'] / pf['total_trades'] * 100) if pf['total_trades'] > 0 else 0
    title = "🤖 模拟量化基金 | 每日终结盘战报" if is_daily_summary else "⚡ 模拟量化基金 | 交易动作触发"
    report = f"**💵 基金总净值**: ${current_equity_usd:.2f} USD (初始 $10000)\n**💰 可用现金**: ${pf['cash_usd']:.2f} USD\n**📈 累计已实现盈亏**: ${pf['realized_pnl']:.2f} USD (历史胜率: {win_rate:.1f}%)\n**📊 当前浮动盈亏**: ${unrealized_pnl_usd:.2f} USD\n\n"

    if trade_actions:
        report += "🔔 **本轮执行的交易动作**:\n"
        for act in trade_actions: report += f"- {act}\n"
        report += "\n"

    report += "📋 **当前在手持仓明细**:\n"
    if pf['holdings']:
        for uid, pos in pf['holdings'].items():
            curr_sym = "HKD" if pos['market'] == "HK" else "USD"
            p_data = market_updates.get(uid, {})
            p_price = p_data.get('price', pos['cost'])
            p_pnl = p_data.get('pnl_usd', 0)
            status = "🔴" if p_pnl < 0 else "🟢"
            report += f"- **{pos['name']}**: {pos['shares']}股 | 成本 {pos['cost']:.2f} -> 现价 {p_price:.2f} {curr_sym} | 浮动: {status}${p_pnl:.2f} USD\n"
    else:
        report += "- 基金目前空仓休息中\n"

    payload = {"msg_type": "post",
               "content": {"post": {"zh_cn": {"title": title, "content": [[{"tag": "text", "text": report}]]}}}}
    try:
        requests.post(FEISHU_WEBHOOK, json=payload)
    except:
        pass
    return win_rate


def ai_evolve_parameters(pf, current_equity_usd, win_rate):
    params = load_json(PARAMS_FILE, DEFAULT_PARAMS)
    prompt = f"""
    你是一个量化基金的风控官。
    初始资金$10000，当前净值${current_equity_usd:.2f}，交易{pf['total_trades']}次，胜率{win_rate:.1f}%。
    当前参数：
    - rsi_threshold: {params['rsi_threshold']} (限制不可买入超买区，最大值不可超过65)
    - atr_multiplier: {params['atr_multiplier']} (止损垫，范围 1.5 - 3.0)

    如果亏损，调低RSI阈值并放大ATR止损防震荡；如果盈利丰厚，可微调激进。
    必须直接输出JSON：{{"rsi_threshold": 60.0, "atr_multiplier": 2.5}}
    """
    headers = {"Authorization": f"Bearer {AI_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": "deepseek-chat", "messages": [{"role": "user", "content": prompt}], "temperature": 0.5}
    try:
        ai_resp = requests.post(AI_API_URL, json=payload, headers=headers, timeout=20).json()['choices'][0]['message'][
            'content']
        new_params = json.loads(ai_resp[ai_resp.find("{"):ai_resp.find("}") + 1])
        params['rsi_threshold'] = float(new_params.get('rsi_threshold', params['rsi_threshold']))
        params['atr_multiplier'] = float(new_params.get('atr_multiplier', params['atr_multiplier']))
        save_json(PARAMS_FILE, params)
        print(
            f"\n🧠 AI 风控官进化完成！明日参数: RSI入场阈值={params['rsi_threshold']}, ATR止损={params['atr_multiplier']}")
    except:
        print("\n⚠️ AI 进化网络波动，维持原风控参数。")


# ================= 6. 主循环调度 =================
if __name__ == "__main__":
    print("🚀 量化 AI 独立模拟基金已启动！(MACD 金叉买 / 死叉跑 / ATR风控)")
    last_daily_report_date = ""
    while True:
        if not is_trading_time():
            time.sleep(30 * 60)
            continue

        now = datetime.datetime.now()
        print(f"\n[{now.strftime('%Y-%m-%d %H:%M:%S')}] 基金主理人开始巡逻...")

        pf, equity, un_pnl, updates, actions = execute_trading()

        today_str = now.strftime("%Y-%m-%d")
        is_daily_time = (now.hour == 8 and last_daily_report_date != today_str)

        if actions or is_daily_time:
            win_rate = push_report(pf, equity, un_pnl, updates, actions, is_daily_summary=is_daily_time)
            if is_daily_time:
                print(" -> 开始执行 AI 风控参数反思进化...")
                ai_evolve_parameters(pf, equity, win_rate)
                last_daily_report_date = today_str
        else:
            print(" -> 本轮未发现 MACD 金叉/死叉，管住手，继续静默。")

        time.sleep(15 * 60)