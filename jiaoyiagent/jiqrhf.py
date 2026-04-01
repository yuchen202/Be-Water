import os
import time
import datetime
import re
import json
import threading
import pandas as pd
import pandas_ta as ta
import akshare as ak
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import lark_oapi as lark
from lark_oapi.api.im.v1 import *

# ================= 0. 纯净网络环境 (飞书专线) =================
os.environ["trust_env"] = "False"
os.environ["no_proxy"] = "*"

_original_request = requests.Session.request


def _patched_request(self, method, url, **kwargs):
    url_str = str(url).lower()
    # 仅对国内股票数据源进行伪装，绝不干扰飞书的 WebSocket
    if any(domain in url_str for domain in ["sina", "eastmoney", "gtimg"]):
        headers = kwargs.get('headers', {})
        if headers is None: headers = {}
        headers['User-Agent'] = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        headers['Connection'] = 'close'
        headers['X-Forwarded-For'] = '114.114.114.114'
        headers['X-Real-IP'] = '114.114.114.114'
        headers['Referer'] = 'http://finance.sina.com.cn/'
        kwargs['headers'] = headers
        if 'timeout' not in kwargs or kwargs['timeout'] is None: kwargs['timeout'] = 8
    return _original_request(self, method, url, **kwargs)


requests.Session.request = _patched_request

# ================= 1. 核心配置区 =================
APP_ID = ""
APP_SECRET = ""

AI_API_KEY = ""
AI_API_URL = ""

CACHED_NAMES_FILE = "cached_names.json"
SYMBOL_NAME_MAP = {}

lark_client = lark.Client.builder().app_id(APP_ID).app_secret(APP_SECRET).build()


# ================= 2. 数据处理与拉取引擎 (终极完美版) =================
def safe_float(val, default=0.0):
    if val is None or pd.isna(val): return default
    try:
        return float(val)
    except:
        return default


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
            prompt = f"直接告诉我股票 {ticker} (市场:{market}) 的中文名。只输出名字，不要解释。"
            headers = {"Authorization": f"Bearer {AI_API_KEY}", "Content-Type": "application/json"}
            payload = {"model": "deepseek-chat", "messages": [{"role": "user", "content": prompt}], "temperature": 0.1}
            ai_resp = requests.post(AI_API_URL, json=payload, headers=headers, timeout=10).json()
            if 'choices' in ai_resp:
                ai_name = ai_resp['choices'][0]['message']['content'].strip().replace("。", "")
                if len(ai_name) < 20 and "抱歉" not in ai_name: cn_name = ai_name
        except:
            pass
    SYMBOL_NAME_MAP[ticker] = cn_name
    save_cached_names()
    return cn_name


def get_kline_data(ticker, market="US"):
    """【彻底修复】使用动态键名提取腾讯复权数据"""
    try:
        df = None

        # 1. 尝试腾讯接口 (速度最快，海外无阻力，带前复权)
        for _ in range(2):
            try:
                prefix = "us" if market == "US" else "hk"
                sym = ticker.replace(".", "") if market == "US" else ticker
                url = f"http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={prefix}{sym.lower()},day,,,1000,qfq"
                resp = requests.get(url, timeout=5).json()
                data = resp.get('data', {})
                if data:
                    # 【致命Bug修复点】：直接拿字典里的第一个 Key，不再死板匹配
                    key = list(data.keys())[0]
                    k_data = data[key].get('qfqday', data[key].get('day', []))
                    if k_data:
                        parsed = [row[:6] for row in k_data if len(row) >= 6]
                        if parsed:
                            df = pd.DataFrame(parsed, columns=['date', 'open', 'close', 'high', 'low', 'volume'])
                            print(f"   ✅ [数据源] 成功连接【腾讯极速节点】拉取 {ticker}")
                            break
            except:
                time.sleep(1)

        # 2. 东方财富兜底
        if df is None or df.empty:
            guess_secids = []
            if market == "US":
                guess_secids = [f"105.{ticker}", f"106.{ticker}", f"107.{ticker}"]
            else:
                guess_secids = [f"116.{ticker}", f"128.{ticker}"]

            for secid in guess_secids:
                try:
                    url = f"http://push2his.eastmoney.com/api/qt/stock/kline/get?secid={secid}&klt=101&fqt=1&lmt=1000&end=20500000&iscca=1&fields1=f1,f2,f3,f4,f5,f6,f7,f8&fields2=f51,f52,f53,f54,f55,f56,f57,f58"
                    resp = requests.get(url, timeout=5).json()
                    if resp and resp.get("data") and resp["data"].get("klines"):
                        klines = resp["data"]["klines"]
                        parsed = []
                        for k in klines:
                            parts = k.split(',')
                            parsed.append({
                                'date': parts[0], 'open': float(parts[1]), 'close': float(parts[2]),
                                'high': float(parts[3]), 'low': float(parts[4]), 'volume': float(parts[5])
                            })
                        df = pd.DataFrame(parsed)
                        print(f"   ✅[数据源] 成功连接【东方财富兜底节点】拉取 {ticker}")
                        break
                except:
                    pass

        # 3. 新浪兜底
        if df is None or df.empty:
            if market == "US":
                try:
                    df = ak.stock_us_daily(symbol=ticker.upper())
                except:
                    df = ak.stock_us_daily(symbol=ticker.lower())
            else:
                try:
                    df = ak.stock_hk_daily(symbol=ticker)
                except:
                    pass

            if df is not None and not df.empty:
                if '日期' in df.columns:
                    df.rename(columns={'日期': 'date', '开盘': 'open', '收盘': 'close', '最高': 'high', '最低': 'low',
                                       '成交量': 'volume'}, inplace=True)
                print(f"   ✅ [数据源] 成功连接【新浪备用节点】拉取 {ticker}")

        if df is None or df.empty: return None

        # 统一清洗数据并计算指标
        df.columns = [str(c).lower() for c in df.columns]
        df['date'] = pd.to_datetime(df['date'])
        for col in ['open', 'close', 'high', 'low', 'volume']: df[col] = pd.to_numeric(df[col], errors='coerce')
        df = df.dropna(subset=['close']).sort_values('date').reset_index(drop=True)
        if df.empty: return None

        # 安全防暴雷：次新股如果没有200天，也让它通过，只需把 MA200 置零
        df['MA60'] = ta.sma(df['close'], length=60)
        df['MA200'] = ta.sma(df['close'], length=200) if len(df) >= 200 else pd.Series([0.0] * len(df))
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
        print(f"解析报错: {e}")
        return None


# ================= 3. AI 深度思考大脑 =================
def call_ai_for_analysis(full_name, metrics):
    c_close = safe_float(metrics.get('close'))
    c_change = safe_float(metrics.get('change'))
    c_ma200 = safe_float(metrics.get('MA200'))
    c_ma60 = safe_float(metrics.get('MA60'))
    c_rsi = safe_float(metrics.get('RSI'), 50.0)

    c_vol = safe_float(metrics.get('volume'))
    c_vol_ma20 = safe_float(metrics.get('VOL_MA20'))
    vol_ratio = (c_vol / c_vol_ma20) if c_vol_ma20 > 0 else 1.0

    c_macd = safe_float(metrics.get('MACD'))
    c_signal = safe_float(metrics.get('SIGNAL'))
    c_hist = safe_float(metrics.get('HIST'))
    c_hist_prev = safe_float(metrics.get('HIST_PREV'))

    cross_status = "动能平缓"
    if c_hist > 0 and c_hist_prev <= 0:
        cross_status = "【MACD 刚刚水下金叉！】"
    elif c_hist < 0 and c_hist_prev >= 0:
        cross_status = "【MACD 刚刚高位死叉！】"

    prompt = f"""我正在关注标的：{full_name}。
    今日行情数据：现价 {c_close:.2f}，日内涨跌幅 {c_change:.2f}%, 量比 {vol_ratio:.1f}倍。MA200牛熊线 {c_ma200:.2f} (若为0代表上市不足200天)，RSI {c_rsi:.1f}。
    MACD指标：快慢线差值 {c_macd:.2f}，信号线 {c_signal:.2f}，MACD柱状图 {c_hist:.2f}。形态提示：{cross_status}。

    【你的角色设定与纪律】：
    你是一位顶级的量化基金经理。你信奉“MACD右侧动能”与“基本面护城河”的结合。
    你需要像手术刀一样剖析：当前的走势或MACD形态是有效的趋势反转，还是诱多/诱空的陷阱？
    请绝不盲目，结合RSI是否超买超卖、以及量价配合情况给出客观决断。

    请经过深度思考后，出具一份个股诊断报告。严格按以下格式输出（不要用星号或井号）：

    【基本面与消息面】：简要分析该公司近期是否有重大消息面或财报影响，基本面护城河是否健康。
    【MACD与量价解析】：重点解析MACD动能（金叉/死叉），结合RSI和量比，判断当前是反转趋势确立，还是下跌中继/顶部派发。
    【综合多空研判】：给出明确的判断结论（如：强烈看涨、逢高做空、耐心观望等）。
    【操作与价格指导】：必须给出具体的【建议买入区间】、【止损价格】和【止盈卖出目标价】。
    """
    headers = {"Authorization": f"Bearer {AI_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": "deepseek-reasoner", "messages": [{"role": "user", "content": prompt}]}

    for _ in range(3):
        try:
            resp = requests.post(AI_API_URL, json=payload, headers=headers, timeout=90).json()
            msg = resp['choices'][0]['message']
            reasoning = msg.get('reasoning_content', '')
            content = msg.get('content', '')

            if not reasoning and "<think>" in content:
                parts = content.split("</think>")
                reasoning = parts[0].replace("<think>", "").strip()
                content = parts[1].strip() if len(parts) > 1 else ""

            return reasoning.replace("*", "").replace("#", ""), content.replace("*", "").replace("#", "")
        except:
            time.sleep(3)
    return "", "AI 深度思考接口网络抖动或算力拥堵，请稍后再试。"


# ================= 4. 飞书消息互动引擎 =================
def reply_to_user(message_id, text_content):
    body = ReplyMessageRequestBody.builder().content(json.dumps({"text": text_content})).msg_type("text").build()
    req = ReplyMessageRequest.builder().message_id(message_id).request_body(body).build()
    lark_client.im.v1.message.reply(req)


def process_user_query(message_id, user_text):
    clean_text = re.sub(r'[\u4e00-\u9fa5@]', ' ', user_text).strip()
    words = clean_text.split()

    ticker, market = "", ""
    for word in words:
        if re.fullmatch(r'\d{4,5}', word):
            ticker = word.zfill(5)
            market = "HK"
            break
        elif re.fullmatch(r'[a-zA-Z]{1,6}', word):
            ticker = word.upper()
            market = "US"
            break

    if not ticker:
        reply_to_user(message_id, "❌ 抱歉老板，我没能识别出股票代码。请在消息中包含代码 (如 TSLA, 00700)。")
        return

    print(f"\n[AI助理] 收到群聊查询：{ticker} ({market}市场)。")
    reply_to_user(message_id,
                  f"⏳ 收到！正在拉取 {ticker} 最新量价与动能数据，并触发 DeepSeek-R1 深度思考。\n(系统将重点分析右侧反转动能，预计需 30-60 秒...)")

    cn_name = get_stock_name_dynamic(ticker, market)
    full_name = f"{cn_name} ({ticker})"

    metrics = None
    for attempt in range(3):
        metrics = get_kline_data(ticker, market)
        if metrics is not None: break
        print(f"获取 {ticker} 失败，第 {attempt + 1} 次重试...")
        time.sleep(2)

    if metrics is None:
        reply_to_user(message_id,
                      f"❌ 获取 {full_name} 的完整行情数据失败。\n原因：您的网络代理节点异常，或遭遇数据源封锁。")
        return

    ai_reasoning, ai_report = call_ai_for_analysis(full_name, metrics)

    if ai_reasoning:
        reason_msg = f"🤔 【{full_name} 动能逻辑推演】\n\n{ai_reasoning}"
        reply_to_user(message_id, reason_msg)
        time.sleep(1.5)

    final_msg = f"🎯 【{full_name} MACD动能诊断报告】\n\n{ai_report}"
    reply_to_user(message_id, final_msg)
    print(f"[AI助理] {full_name} 诊断报告已成功发出！")


def do_im_message_receive_v1(data: P2ImMessageReceiveV1) -> None:
    msg_type = data.event.message.message_type
    if msg_type == "text":
        content_str = data.event.message.content
        try:
            user_text = json.loads(content_str).get("text", "")
            msg_id = data.event.message.message_id
            threading.Thread(target=process_user_query, args=(msg_id, user_text)).start()
        except:
            pass


if __name__ == "__main__":
    load_cached_names()
    event_handler = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(
        do_im_message_receive_v1).build()
    cli = lark.ws.Client(APP_ID, APP_SECRET, event_handler=event_handler)
    print("🚀 深度思考 AI 聊天机器人已启动！(纯净网络 / 腾讯首发兜底)")
    cli.start()