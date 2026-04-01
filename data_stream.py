# data_stream.py
import asyncio
import json
import websockets
import pandas as pd
import ta
import time
import os
import requests
from config import TOP_N_COINS

# 🌐 代理设置：确保开启全局/TUN模式，或在此指定
#os.environ["http_proxy"] = "http://127.0.0.1:7890"
#os.environ["https_proxy"] = "http://127.0.0.1:7890"
#os.environ["all_proxy"] = "http://127.0.0.1:7890"

market_state = {}
last_calc_time = {}  # CPU 保护机制：记录上次计算指标的时间


def fetch_top_100_symbols():
    """直接内置全网 Top 100 币种，彻底绕过 Windows 网络请求报错"""
    print(f"🌍 正在加载全网前 {TOP_N_COINS} 大交易对...")
    top_100 =[
        'btcusdt', 'ethusdt', 'solusdt', 'bnbusdt', 'xrpusdt', 'dogeusdt', 'adausdt', 'avaxusdt',
        'shibusdt', 'dotusdt', 'linkusdt', 'trxusdt', 'maticusdt', 'uniusdt', 'ltcusdt', 'bchusdt',
        'nearusdt', 'aptusdt', 'opusdt', 'arbusdt', 'filusdt', 'injusdt', 'ldousdt', 'rndrusdt',
        'atomusdt', 'stxusdt', 'imxusdt', 'grtusdt', 'vetusdt', 'mkrusdt', 'runeusdt', 'snxusdt',
        'aaveusdt', 'algousdt', 'qntusdt', 'sandusdt', 'egldusdt', 'manausdt', 'thetausdt', 'axsusdt',
        'galausdt', 'neousdt', 'eosusdt', 'kavausdt', 'chzusdt', 'crvusdt', 'compusdt', 'wldusdt',
        'ordiusdt', 'suiusdt', 'seiusdt', 'tiausdt', 'dymusdt', 'jupusdt', 'pythusdt', 'mantausdt',
        'altusdt', 'xaiusdt', 'memeusdt', 'pepeusdt', 'wifusdt', 'flokiusdt', 'bonkusdt', 'bomeusdt',
        'strkusdt', 'ethfiusdt', 'enausdt', 'rezusdt', 'tnsrusdt', 'omniusdt', 'sagausdt', 'wusdt',
        'zetausdt', 'dydxusdt', 'ilvusdt', 'magicusdt', 'ensusdt', 'yggusdt', 'pendleusdt', 'ondousdt',
        'trbusdt', 'umausdt', 'api3usdt', 'bandusdt', 'blurusdt', 'lptusdt', 'superusdt', 'bigtimeusdt',
        'orcausdt', 'rayusdt', 'jtousdt', 'gmxusdt', 'ssvusdt', 'metisusdt', 'mavusdt', 'rdntusdt',
        'idusdt', 'eduusdt'
    ]
    return top_100[:TOP_N_COINS]


# 获取全网 Top 100
SYMBOLS = fetch_top_100_symbols()


async def binance_ws_stream():
    print(f"📈 正在连接币安 WebSocket {len(SYMBOLS)} 币种实时合并流...")
    # 🔥 使用币安 Combined Streams 协议，防止多币种连接被踢
    uri = "wss://stream.binance.com:9443/stream?streams=" + "/".join([f"{s}@ticker" for s in SYMBOLS])
    price_history = {s: [] for s in SYMBOLS}

    while True:
        try:
            async with websockets.connect(uri, ping_interval=20, ping_timeout=20) as ws:
                print("🟢 100 币种并发数据流已打通，引擎满载运转中！")
                while True:
                    try:
                        data = await ws.recv()
                        raw_msg = json.loads(data)

                        # 兼容 Combined Stream 的两层嵌套数据格式
                        msg = raw_msg.get('data', raw_msg)
                        if 's' not in msg: continue

                        symbol = msg['s'].upper()
                        price = float(msg['c'])
                        lower_symbol = symbol.lower()

                        price_history[lower_symbol].append(price)
                        if len(price_history[lower_symbol]) > 50:
                            price_history[lower_symbol].pop(0)

                        current_t = time.time()
                        # 🔥 CPU 节流阀：每个币种最多每 2 秒计算一次 RSI，防止卡死
                        if current_t - last_calc_time.get(lower_symbol, 0) > 2.0 and len(
                                price_history[lower_symbol]) == 50:
                            series = pd.Series(price_history[lower_symbol])
                            rsi_val = ta.momentum.RSIIndicator(series, window=14).rsi().iloc[-1]
                            atr_val = series.std() * 2

                            market_state[symbol] = {
                                'price': price,
                                'rsi': rsi_val if not pd.isna(rsi_val) else 50.0,
                                'atr': atr_val if not pd.isna(atr_val) else (price * 0.01),
                                'timestamp': current_t
                            }
                            last_calc_time[lower_symbol] = current_t
                        elif symbol not in market_state:  # 初始化第一笔数据
                            market_state[symbol] = {'price': price, 'rsi': 50.0, 'atr': price * 0.01,
                                                    'timestamp': current_t}
                        else:
                            # 没到计算时间，只更新价格，不浪费 CPU 算指标
                            market_state[symbol]['price'] = price
                            market_state[symbol]['timestamp'] = current_t

                    except Exception as inner_e:
                        print(f"⚠️ 数据流局部异常，重连中... ({inner_e})")
                        break
        except Exception as e:
            print(f"🔴 币安总线断开，3秒后自动重试... ({e})")
            await asyncio.sleep(3)