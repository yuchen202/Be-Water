import os
import time
import datetime
import re
import json
import random
import threading
import pandas as pd
import pandas_ta as ta
import akshare as ak
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from concurrent.futures import ThreadPoolExecutor, as_completed

# ================= 0. 企业级网络防假死与伪装引擎 =================
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

# ================= 1. 核心配置区 =================
FEISHU_WEBHOOK = ""
AI_API_KEY = ""
AI_API_URL = ""

HISTORY_FILE = "alert_history.json"
CACHED_NAMES_FILE = "cached_names.json"
SYMBOL_NAME_MAP, US_SYMBOL_PREFIX_MAP = {}, {}


def safe_float(val, default=0.0):
    if val is None or pd.isna(val): return default
    try:
        return float(val)
    except:
        return default


# ================= 2. 状态记忆与字典同步 =================
def load_cached_names():
    global SYMBOL_NAME_MAP
    if os.path.exists(CACHED_NAMES_FILE):
        try:
            with open(CACHED_NAMES_FILE, "r", encoding="utf-8") as f:
                SYMBOL_NAME_MAP = json.load(f)
        except:
            pass


def save_cached_names():
    with open(CACHED_NAMES_FILE, "w", encoding="utf-8") as f: json.dump(SYMBOL_NAME_MAP, f, ensure_ascii=False)


def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            pass
    return {}


def save_history(history):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f: json.dump(history, f, ensure_ascii=False)


def get_stock_name_dynamic(ticker, market="US"):
    if ticker in SYMBOL_NAME_MAP: return SYMBOL_NAME_MAP[ticker]
    cn_name = ticker
    try:
        prefix = "us" if market == "US" else "hk"
        resp = requests.get(f"http://qt.gtimg.cn/q={prefix}{ticker.lower()}", timeout=5)
        if resp.status_code == 200 and len(resp.text) > 20:
            parts = resp.text.split("~")
            if len(parts) > 1 and parts[1]: cn_name = parts[1]
    except:
        pass
    if cn_name == ticker or not re.search('[\u4e00-\u9fa5]', cn_name):
        try:
            prompt = f"直接告诉我股票 {ticker} 的中文名。只输出名字。"
            headers = {"Authorization": f"Bearer {AI_API_KEY}", "Content-Type": "application/json"}
            payload = {"model": "deepseek-chat", "messages": [{"role": "user", "content": prompt}], "temperature": 0.1}
            ai_resp = requests.post(AI_API_URL, json=payload, headers=headers, timeout=10).json()
            if 'choices' in ai_resp:
                ai_name = ai_resp['choices'][0]['message']['content'].strip().replace("。", "")
                if len(ai_name) < 20: cn_name = ai_name
        except:
            pass
    SYMBOL_NAME_MAP[ticker] = cn_name
    save_cached_names()
    return cn_name


def load_watchlist():
    watchlist = {"US": [], "HK": []}
    if not os.path.exists("us_stocks.txt"):
        with open("us_stocks.txt", "w", encoding="utf-8") as f: f.write("AAPL\nTSLA\nNVDA\nBRK.B\n")
    if not os.path.exists("hk_stocks.txt"):
        with open("hk_stocks.txt", "w", encoding="utf-8") as f: f.write("00700\n09988\n")
    with open("us_stocks.txt", "r", encoding="utf-8") as f:
        for line in f:
            t = line.strip().upper()
            if t and not t.startswith("#") and t not in watchlist["US"]: watchlist["US"].append(t)
    with open("hk_stocks.txt", "r", encoding="utf-8") as f:
        for line in f:
            t = line.strip()
            if t and not t.startswith("#") and t not in watchlist["HK"]: watchlist["HK"].append(t)
    return watchlist


# ================= 3. 数据与 MACD 计算 =================
def format_df(df):
    if '日期' in df.columns: df.rename(
        columns={'日期': 'date', '开盘': 'open', '收盘': 'close', '最高': 'high', '最低': 'low', '成交量': 'volume'},
        inplace=True)
    df.columns = [str(c).lower() for c in df.columns]
    for col in ['open', 'close', 'high', 'low', 'volume']:
        if col in df.columns: df[col] = pd.to_numeric(df[col], errors='coerce')
    return df.dropna(subset=['close'])


def get_kline_data(ticker, market="US"):
    try:
        start_date = (datetime.datetime.now() - datetime.timedelta(days=1100)).strftime("%Y%m%d")
        df = None

        # 1. 腾讯接口 (带3次重试，完美适配海外，修复 BRK.B 格式)
        for _ in range(3):
            try:
                prefix = "us" if market == "US" else "hk"
                sym = ticker.lower()  # 绝不能用 replace(".", "")
                r = requests.get(
                    f"http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={prefix}{sym},day,,,1000,qfq",
                    timeout=5).json()
                data = r.get('data', {}).get(f"{prefix}{sym}")
                if data:
                    k_data = data.get('qfqday', data.get('day', []))
                    if k_data:
                        parsed = [row[:6] for row in k_data if len(row) >= 6]
                        df = pd.DataFrame(parsed, columns=['date', 'open', 'close', 'high', 'low', 'volume'])
                        break  # 成功则跳出重试
            except:
                time.sleep(1)  # 限流等待

        # 2. 东方财富接口兜底 (带重试)
        if df is None or df.empty:
            guess_secids = []
            if market == "US":
                guess_secids = [f"105.{ticker}", f"106.{ticker}", f"107.{ticker}"]
            else:
                guess_secids = [f"116.{ticker}", f"128.{ticker}"]

            for secid in guess_secids:
                for _ in range(2):
                    try:
                        url = f"http://push2his.eastmoney.com/api/qt/stock/kline/get?secid={secid}&klt=101&fqt=1&lmt=1000&end=20500000&iscca=1&fields1=f1,f2,f3,f4,f5,f6,f7,f8&fields2=f51,f52,f53,f54,f55,f56,f57,f58"
                        resp = requests.get(url, timeout=5).json()
                        if resp and resp.get("data") and resp["data"].get("klines"):
                            klines = resp["data"]["klines"]
                            parsed = []
                            for k in klines:
                                parts = k.split(',')
                                parsed.append({'date': parts[0], 'open': float(parts[1]), 'close': float(parts[2]),
                                               'high': float(parts[3]), 'low': float(parts[4]),
                                               'volume': float(parts[5])})
                            df = pd.DataFrame(parsed)
                            break
                    except:
                        time.sleep(0.5)
                if df is not None and not df.empty: break

        # 3. 新浪最底线兜底
        if df is None or df.empty:
            try:
                df = ak.stock_us_daily(symbol=ticker.upper()) if market == "US" else ak.stock_hk_daily(symbol=ticker)
                if df is not None and not df.empty:
                    df = format_df(df)
                    df['date'] = pd.to_datetime(df['date'])
                    df = df[df['date'] >= pd.to_datetime(start_date)]
            except:
                pass

        if df is None or df.empty: return None

        df = format_df(df)
        df['date'] = pd.to_datetime(df['date'])

        df['MA60'] = ta.sma(df['close'], length=60)
        df['MA200'] = ta.sma(df['close'], length=200)
        df['RSI'] = ta.rsi(df['close'], length=14)
        df['VOL_MA20'] = ta.sma(df['volume'], length=20)

        macd = ta.macd(df['close'], fast=12, slow=26, signal=9)
        if macd is not None and not macd.empty:
            df['MACD'] = macd.iloc[:, 0]
            df['SIGNAL'] = macd.iloc[:, 1]
            df['HIST'] = macd.iloc[:, 2]
            df['HIST_PREV'] = df['HIST'].shift(1)
        else:
            df['MACD'] = df['SIGNAL'] = df['HIST'] = df['HIST_PREV'] = 0.0

        if len(df) >= 2:
            df['change'] = (df['close'].iloc[-1] - df['close'].iloc[-2]) / df['close'].iloc[-2] * 100
        else:
            df['change'] = 0.0

        return df.iloc[-1]
    except Exception as e:
        return None


# ================= 4. MACD 触发器核心逻辑 =================
def check_triggers(metrics):
    if metrics is None: return False, [], {}

    current_price = safe_float(metrics.get('close'))
    rsi = safe_float(metrics.get('RSI'), 50.0)
    vol = safe_float(metrics.get('volume'))
    vol_ma20 = safe_float(metrics.get('VOL_MA20'))
    hist_today = safe_float(metrics.get('HIST'))
    hist_prev = safe_float(metrics.get('HIST_PREV'))

    triggered = False
    reasons = []

    if hist_prev <= 0 and hist_today > 0:
        reasons.append(f"MACD水下金叉(上升动能确立)")
        triggered = True
    if hist_prev >= 0 and hist_today < 0:
        reasons.append(f"MACD高位死叉(下跌动能确立)")
        triggered = True
    if rsi < 35:
        reasons.append(f"RSI极限超卖({rsi:.1f})")
        triggered = True
    elif rsi > 75:
        reasons.append(f"RSI极限超买({rsi:.1f})")
        triggered = True
    if vol_ma20 > 0 and vol >= (vol_ma20 * 2.0):
        reasons.append(f"成交量异常放大({vol / vol_ma20:.1f}倍均量)")
        triggered = True

    return triggered, reasons, metrics


# ================= 5. AI 深度分析大脑 (无废话极简版) =================
def analyze_with_ai(stock_name, ticker, reasons, metrics):
    reason_str = "；".join(reasons)

    c_close = safe_float(metrics.get('close'))
    c_change = safe_float(metrics.get('change'))
    c_rsi = safe_float(metrics.get('RSI'))
    c_macd = safe_float(metrics.get('MACD'))
    c_signal = safe_float(metrics.get('SIGNAL'))
    c_hist = safe_float(metrics.get('HIST'))
    vol_ratio = safe_float(metrics.get('volume')) / safe_float(metrics.get('VOL_MA20')) if safe_float(
        metrics.get('VOL_MA20')) > 0 else 1.0

    prompt = f"""
    我正在监控标的：{stock_name}({ticker})。今日触发预警：{reason_str}。
    数据：现价 {c_close:.2f}，日内涨跌幅 {c_change:.2f}%，MACD线 {c_macd:.2f}，Signal线 {c_signal:.2f}，柱状图 {c_hist:.2f}，RSI {c_rsi:.1f}，量比 {vol_ratio:.1f}倍。

    你是一位顶级的量化基金经理。请基于“MACD动能+量价关系+基本面/消息面”进行综合研判，严格输出以下格式的深度研报（不要用星号，不要添加任何额外寒暄）：

    【基本面与消息面】：简述近期是否有重大消息面或财报影响。
    【MACD与量价解析】：判断当前是反转趋势确立，还是下跌中继/顶部派发。
    【综合多空研判】：给出明确结论（如：强烈看涨、逢高做空、耐心观望等）。
    【核心价格指导】：直接给出合适的建仓买入区间、止损价格，及未来的卖出/止盈目标价。
    """
    headers = {"Authorization": f"Bearer {AI_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": "deepseek-reasoner", "messages": [{"role": "user", "content": prompt}]}

    for attempt in range(3):
        try:
            resp = requests.post(AI_API_URL, json=payload, headers=headers, timeout=150).json()
            if 'choices' in resp:
                msg = resp['choices'][0]['message']
                final_content = msg.get('content', '')
                final_content = re.sub(r'<think>.*?</think>', '', final_content, flags=re.DOTALL).strip()
                final_content = final_content.replace("*", "").replace("#", "")
                return f"🎯 【核心操作决议】\n\n{final_content}"
            else:
                print(f"      [{ticker}] 警告: AI 返回非正常格式数据: {resp}")
        except Exception as e:
            print(f"[{ticker}] AI 生成拥堵/超时 (尝试 {attempt + 1}/3)...")
            time.sleep(3)
    return "AI 诊断多次重试后依然超时。"


def push_to_feishu(full_display_name, ai_report):
    title = f"🚨 动能监控: {full_display_name} 指标触发！"
    payload = {"msg_type": "post",
               "content": {"post": {"zh_cn": {"title": title, "content": [[{"tag": "text", "text": ai_report}]]}}}}
    try:
        requests.post(FEISHU_WEBHOOK, json=payload)
    except:
        pass


def is_trading_time():
    now = datetime.datetime.now()
    weekday = now.weekday()
    hour = now.hour
    if (weekday == 5 and hour >= 6) or weekday == 6 or (weekday == 0 and hour < 8): return False
    return True


# ================= 6. 核心并发调度 =================
def process_single_stock(ticker, market, today_str, alerted_history):
    # 【核心修复】：随机打散并发请求，彻底防止被腾讯防火墙拉黑
    time.sleep(random.uniform(0.1, 2.0))

    stock_name = get_stock_name_dynamic(ticker, market)
    full_display_name = f"{stock_name} ({ticker})"

    print(f" -> 正在检测: {full_display_name} ...")
    if alerted_history.get(ticker) == today_str:
        print(f"   [已跳过] {full_display_name} 今日已发过预警。")
        return

    # 加入3次容错重试机制
    metrics = None
    for attempt in range(3):
        metrics = get_kline_data(ticker, market)
        if metrics is not None: break
        print(f"   ⚠️ 获取 {full_display_name} 数据失败 (尝试 {attempt + 1}/3)，1秒后重试...")
        time.sleep(1)

    if metrics is None:
        print(f"   ❌ 获取 {full_display_name} 彻底失败 (可能遭遇网络阻断或退市)，已跳过。")
        return

    is_triggered, reasons, m_data = check_triggers(metrics)

    if is_triggered:
        print(f"🚨 [{full_display_name}] 触发条件: {'+'.join(reasons)}！正在呼叫 DeepSeek-R1 (需 30-90 秒)...")
        ai_report = analyze_with_ai(stock_name, ticker, reasons, m_data)
        push_to_feishu(full_display_name, ai_report)
        alerted_history[ticker] = today_str
        print(f"✅[{full_display_name}] 精简研报已成功推送飞书！")
    else:
        print(f"   ✓[指标平稳] {full_display_name} 无 MACD 或量价异动信号，继续潜伏。")


def job():
    if not is_trading_time():
        print(f"[{datetime.datetime.now().strftime('%H:%M')}] 周末停盘时间，系统休眠。")
        return True

    print(f"\n[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 开始执行 MACD 定点监控扫描...")
    load_cached_names()
    current_watchlist = load_watchlist()
    alerted_history = load_history()
    today_str = datetime.date.today().strftime("%Y-%m-%d")

    tasks = []
    for market, tickers in current_watchlist.items():
        for ticker in tickers: tasks.append((ticker, market))

    if not tasks: return True

    # 为了绝对安全，将并发数降低到 3，宁慢勿断
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(process_single_stock, t[0], t[1], today_str, alerted_history) for t in tasks]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:
                print(f"线程执行报错: {e}")

    save_history(alerted_history)
    print("🎯 本轮自选股扫描与复核全部完成！进入 30 分钟静默等待...")
    return True


if __name__ == "__main__":
    print("🚀 自选股动能监控已启动！(完美防断连极速版)")
    while True:
        is_network_ok = job()
        time.sleep(1800)