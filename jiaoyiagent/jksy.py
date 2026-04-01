import os
import time
import datetime
import re
import json
import pandas as pd
import pandas_ta as ta
import akshare as ak
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from concurrent.futures import ThreadPoolExecutor, as_completed

# ================= 0. 企业级网络引擎 =================
os.environ["trust_env"] = "False"
os.environ["no_proxy"] = "*"
_original_session_init = requests.Session.__init__


def _patched_session_init(self, *args, **kwargs):
    _original_session_init(self, *args, **kwargs)
    retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry, pool_connections=150, pool_maxsize=150)
    self.mount('http://', adapter)
    self.mount('https://', adapter)
    self.headers.update({'User-Agent': 'Mozilla/5.0', 'Connection': 'close', 'X-Forwarded-For': '114.114.114.114'})


requests.Session.__init__ = _patched_session_init

_original_request = requests.Session.request


def _patched_request(self, method, url, **kwargs):
    if 'timeout' not in kwargs or kwargs['timeout'] is None: kwargs['timeout'] = 8
    return _original_request(self, method, url, **kwargs)


requests.Session.request = _patched_request

# ================= 1. 配置区 =================
FEISHU_WEBHOOK = ""
AI_API_KEY = ""
AI_API_URL = ""

CACHED_NAMES_FILE = "cached_names.json"
RADAR_HISTORY_FILE = "radar_history.json"
SHARED_OPP_FILE = "shared_opportunities.json"
SYMBOL_NAME_MAP, US_SYMBOL_PREFIX_MAP = {}, {}


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


def load_radar_history():
    if os.path.exists(RADAR_HISTORY_FILE):
        try:
            with open(RADAR_HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            pass
    return {}


def save_radar_history(history):
    with open(RADAR_HISTORY_FILE, "w", encoding="utf-8") as f: json.dump(history, f, ensure_ascii=False)


def get_stock_name_dynamic(ticker, market="US", default_name=""):
    if ticker in SYMBOL_NAME_MAP: return SYMBOL_NAME_MAP[ticker]
    cn_name = default_name if default_name else ticker
    if re.search('[\u4e00-\u9fa5]', cn_name):
        SYMBOL_NAME_MAP[ticker] = cn_name
        save_cached_names()
        return cn_name
    try:
        prefix = "us" if market == "US" else "hk"
        resp = requests.get(f"http://qt.gtimg.cn/q={prefix}{ticker.lower()}", timeout=5)
        if resp.status_code == 200 and len(resp.text) > 20:
            parts = resp.text.split("~")
            if len(parts) > 1 and parts[1]: cn_name = parts[1]
    except:
        pass
    SYMBOL_NAME_MAP[ticker] = cn_name
    save_cached_names()
    return cn_name


# ================= 2. 全网扫描引擎 =================
def normalize_spot_df(df):
    if df.empty: return df
    if '代码' not in df.columns: df['代码'] = df.get('symbol', df.index)
    if '名称' not in df.columns: df['名称'] = df.get('cname', df.get('name', df['代码']))
    if '最新价' not in df.columns: df['最新价'] = df.get('price', df.get('lasttrade', 0))
    if '涨跌幅' not in df.columns: df['涨跌幅'] = df.get('chg', df.get('changepercent', 0))
    if '总市值' not in df.columns: df['总市值'] = df.get('mktcap', 0)
    if '成交额' not in df.columns: df['成交额'] = df.get('amount', 0)

    df['最新价'] = pd.to_numeric(df['最新价'], errors='coerce').fillna(0)
    df['涨跌幅'] = pd.to_numeric(df['涨跌幅'], errors='coerce').fillna(0)
    df['总市值'] = pd.to_numeric(df['总市值'], errors='coerce').fillna(0)
    df['成交额'] = pd.to_numeric(df['成交额'], errors='coerce').fillna(0)
    return df


def get_spot_3tier(market):
    try:
        df = ak.stock_us_spot_em() if market == "US" else ak.stock_hk_spot_em()
        if df is not None and not df.empty: return normalize_spot_df(df)
    except:
        pass
    try:
        df = ak.stock_us_spot() if market == "US" else ak.stock_hk_spot()
        if df is not None and not df.empty: return normalize_spot_df(df)
    except:
        pass
    return pd.DataFrame()


def scan_whole_market_fast():
    print("\n[阶段一] 启动全网扫描 (寻找高市值与高活跃度标的)...")
    candidates = []
    for market in ["US", "HK"]:
        spot_df = get_spot_3tier(market)
        if not spot_df.empty:
            cond_base = spot_df['最新价'] >= 5.0
            giants = spot_df[cond_base].sort_values('总市值', ascending=False).head(300 if market == "US" else 150)
            actives = spot_df[cond_base].sort_values('成交额', ascending=False).head(100)
            merged_top = pd.concat([giants, actives]).drop_duplicates(subset=['代码'])

            for _, row in merged_top.iterrows():
                code_raw = str(row['代码'])
                if market == "US" and "." in code_raw:
                    prefix, ticker = code_raw.split(".", 1)
                    ticker = ticker.upper()
                else:
                    ticker = code_raw.upper()
                name = get_stock_name_dynamic(ticker, market, str(row['名称']))
                candidates.append({"market": market, "code": ticker, "name": name, "change": row['涨跌幅']})
    return candidates


# ================= 3. MACD 技术面复核引擎 =================
def format_kline_df(df):
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
        try:
            prefix = "us" if market == "US" else "hk"
            sym = ticker.replace(".", "") if market == "US" else ticker
            r = requests.get(
                f"http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={prefix}{sym.lower()},day,,,1000,qfq",
                timeout=5).json()
            data = r.get('data', {}).get(f"{prefix}{sym.lower()}")
            if data:
                k_data = data.get('qfqday', data.get('day', []))
                parsed = [row[:6] for row in k_data if len(row) >= 6]
                df = pd.DataFrame(parsed, columns=['date', 'open', 'close', 'high', 'low', 'volume'])
        except:
            pass

        if df is None or df.empty:
            try:
                df = ak.stock_us_daily(symbol=ticker.upper()) if market == "US" else ak.stock_hk_daily(symbol=ticker)
                if df is not None and not df.empty:
                    df = format_kline_df(df)
                    df['date'] = pd.to_datetime(df['date'])
                    df = df[df['date'] >= pd.to_datetime(start_date)]
            except:
                pass

        if df is None or df.empty: return None

        df = format_kline_df(df)
        df['RSI'] = ta.rsi(df['close'], length=14)
        df['VOL_MA20'] = ta.sma(df['volume'], length=20)

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


def process_kline(cand):
    cand['full_display_name'] = f"{cand['name']} ({cand['code']})"
    metrics = get_kline_data(cand['code'], cand['market'])
    if metrics is None: return None

    c_hist = safe_float(metrics.get('HIST'))
    c_hist_prev = safe_float(metrics.get('HIST_PREV'))
    c_rsi = safe_float(metrics.get('RSI'))
    c_vol = safe_float(metrics.get('volume'))
    c_vol_ma20 = safe_float(metrics.get('VOL_MA20'))

    is_golden_cross = (c_hist > 0 and c_hist_prev <= 0)
    is_rsi_ok = (20 < c_rsi < 65)
    is_volume_breakout = (c_vol_ma20 > 0 and c_vol > c_vol_ma20 * 1.5)

    if (is_golden_cross and is_rsi_ok) or (is_volume_breakout and c_rsi < 45):
        cand['metrics'] = metrics
        base_score = 50 if is_golden_cross else 20
        vol_score = 30 if is_volume_breakout else 0
        rsi_score = 20 if c_rsi < 40 else 0
        cand['tech_score'] = base_score + vol_score + rsi_score

        reason = "MACD金叉" if is_golden_cross else "底部放量"
        print(f"   🎯 发现动能股: {cand['full_display_name']} -> {reason} | 技术得分: {cand['tech_score']}")
        return cand
    return None


# ================= 4. AI 并发阅卷引擎 =================
def get_ai_score_concurrent(cand):
    c_close = safe_float(cand['metrics'].get('close'))
    c_rsi = safe_float(cand['metrics'].get('RSI'))
    c_macd = safe_float(cand['metrics'].get('MACD'))
    c_hist = safe_float(cand['metrics'].get('HIST'))

    prompt = f"""标的：{cand['full_display_name']}。现价 {c_close:.2f}，MACD柱 {c_hist:.2f}，RSI {c_rsi:.1f}。
    请判断此处的 MACD 金叉或异动是“有效反转”还是“诱多陷阱”。严格输出（绝不使用星号）：\n分数：[0-100整数]\n理由：[20字内核心理由]"""

    headers = {"Authorization": f"Bearer {AI_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": "deepseek-chat", "messages": [{"role": "user", "content": prompt}], "temperature": 0.3}
    for _ in range(3):
        try:
            resp = requests.post(AI_API_URL, json=payload, headers=headers, timeout=20).json()
            ai_resp = resp['choices'][0]['message']['content']
            cand['score'] = int(re.search(r"分数：\s*(\d+)", ai_resp).group(1))
            cand['reason'] = re.search(r"理由：\s*(.*)", ai_resp).group(1).strip()
            return cand
        except:
            time.sleep(2)
    cand['score'], cand['reason'] = 0, "AI打分失败"
    return cand


def generate_deep_report(stock_info):
    full_name, metrics, score = stock_info['full_display_name'], stock_info['metrics'], stock_info['score']
    c_close = safe_float(metrics.get('close'))
    c_rsi = safe_float(metrics.get('RSI'))
    c_macd = safe_float(metrics.get('MACD'))

    prompt = f"""{full_name} 获 {score} 分买入评分。现价 {c_close:.2f}，MACD {c_macd:.2f}，RSI {c_rsi:.1f}。
    请严格按格式输出深度研报，绝不使用星号或井号：
    【基本面与消息面】：简要分析基本面护城河及近期催化剂。
    【MACD与量价解析】：解析当前的右侧动能，判断反转有效性。
    【核心价格指导】：必须给出具体的【建议买入区间】、【止损价格】和【止盈卖出目标价】。"""

    headers = {"Authorization": f"Bearer {AI_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": "deepseek-reasoner", "messages": [{"role": "user", "content": prompt}]}
    for _ in range(3):
        try:
            resp = requests.post(AI_API_URL, json=payload, headers=headers, timeout=90).json()['choices'][0]['message']
            reasoning = resp.get('reasoning_content', '')
            content = resp.get('content', '').replace("*", "").replace("#", "")
            return f"🤔 【AI 思考博弈】\n{reasoning.replace('*', '')}\n\n================\n🎯 【正式报告】\n{content}"
        except:
            time.sleep(3)
    return "生成超时。"


# ================= 5. 核心调度任务 =================
def is_trading_time():
    now = datetime.datetime.now()
    if (now.weekday() == 5 and now.hour >= 6) or now.weekday() == 6 or (
            now.weekday() == 0 and now.hour < 8): return False
    return True


def radar_job():
    if not is_trading_time(): return True

    print(f"\n[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 启动全新 MACD 动能雷达扫描...")
    load_cached_names()
    radar_history = load_radar_history()
    today_str = datetime.date.today().strftime("%Y-%m-%d")

    candidates = scan_whole_market_fast()
    if not candidates: return False

    print(f"\n[阶段二] 开始技术面并发复核，寻找 MACD 金叉标的...")
    valid_stocks = []
    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = [executor.submit(process_kline, cand) for cand in candidates]
        for future in as_completed(futures):
            res = future.result()
            if res: valid_stocks.append(res)

    if not valid_stocks:
        print("当前全市场无 MACD 金叉或量价爆点的优质标的。")
        with open(SHARED_OPP_FILE, "w", encoding="utf-8") as f: json.dump([], f)
        return True

    valid_stocks = sorted(valid_stocks, key=lambda x: x['tech_score'], reverse=True)

    # 输送弹药给模拟盘
    export_list = [{"market": c["market"], "code": c["code"], "name": c["name"]} for c in valid_stocks[:50]]
    with open(SHARED_OPP_FILE, "w", encoding="utf-8") as f:
        json.dump(export_list, f, ensure_ascii=False)
    print(f"🌉 已将 {len(export_list)} 只右侧动能标的推入共享仓库！")

    top_10 = valid_stocks[:10]
    print(f"\n[阶段三] 对最强的 10 只标的进行 AI 综合阅卷...")
    scored_list = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(get_ai_score_concurrent, cand) for cand in top_10]
        for future in as_completed(futures):
            res = future.result()
            print(f" -> {res['full_display_name']} AI评分: {res['score']}分")
            if res['score'] >= 50: scored_list.append(res)

    if not scored_list: return True

    scored_list = sorted(scored_list, key=lambda x: x['score'], reverse=True)

    # ++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
    # 【核心升级】：防连发轰炸机制 (比对本周期标的组合与上周期是否一致)
    current_codes = [item['code'] for item in scored_list]
    last_codes = radar_history.get("last_pushed_codes", [])

    if current_codes == last_codes:
        print(f"🔕 本周期甄选标的 {current_codes} 与上个半小时完全一致，触发防轰炸机制，静默跳过飞书推送。")
        return True

    # 只要不一致，就记录本周期的代码，覆盖旧数据
    radar_history["last_pushed_codes"] = current_codes
    # ++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++

    top_stock = scored_list[0]
    top_code = top_stock['code']

    deep_report = None
    # 修改这里的Key，防止与上面新加的 "last_pushed_codes" 键名冲突
    report_key = f"report_{top_code}"

    if radar_history.get(report_key) != today_str:
        print(f"\n[阶段四] 正在为榜首 {top_stock['full_display_name']} 撰写深度报告...")
        deep_report = generate_deep_report(top_stock)
        radar_history[report_key] = today_str

    save_radar_history(radar_history)  # 保存所有的历史记录 (报告日期+上一周期的股票列表)

    # 推送飞书
    content = f"🔍 挖掘到 {len(scored_list)} 只 MACD 金叉/放量标的：\n\n"
    for i, item in enumerate(scored_list):
        content += f"{i + 1}. {item['full_display_name']} - {item['score']}分 | 💡 {item['reason']}\n"
    if deep_report:
        content += f"\n🏆 【首选推荐】\n{deep_report}"
    else:
        content += f"\n(注：今日已对榜首 {top_stock['full_display_name']} 出具过深度报告，为防打扰不再重复分析。)"

    payload = {"msg_type": "post", "content": {
        "post": {"zh_cn": {"title": "📡 MACD 动能雷达", "content": [[{"tag": "text", "text": content}]]}}}}
    try:
        requests.post(FEISHU_WEBHOOK, json=payload)
    except:
        pass

    print("✅ 抄底雷达报告推送成功！")
    return True


if __name__ == "__main__":
    print("🚀 全市场超级雷达已启动！(加入智能防轰炸防打扰机制)")
    while True:
        if radar_job():
            time.sleep(1800)
        else:
            time.sleep(300)