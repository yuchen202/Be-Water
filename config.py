
# config.py
import os

# --- 社会与种群规模参数 ---
TOTAL_AGENTS = 10000               # 扩大社会规模到 2000 人
INITIAL_CAPITAL = 10000.0         # 初始分配资金 (USDT)
DB_PATH = 'crypto_society_5000.db'     # 换一个全新的数据库名字
ARCHIVE_DIR = 'traders_archive_5000'   # 交易员生平档案库文件夹

# --- 回测与市场参数 ---
TOP_N_COINS = 100                  # 回测期间建议监控前50，保证速度
BACKTEST_TIMEFRAME = '5m'         # 使用 1小时K线 跑长线回测
BACKTEST_DAYS = 365 * 5              # 回测过去 1 年的数据！

# =======================================================
# 🧠 大模型 A：新闻分析面 API (使用智谱 GLM)
# 负责：将全网英文/中文资讯转化为 -1 到 1 的情绪分
# =======================================================
ZHIPU_API_KEY = os.getenv("ZHIPU_API_KEY", "")
NEWS_MODEL = "glm-4-flash"        # 免费，速度极快
NEWS_FETCH_INTERVAL = 300         # 每 300 秒 (5分钟) 抓取一次新闻流
NEWS_RSS_URLS =[
    "https://cryptopanic.com/news/rss/" # 币圈最大的聚合新闻源
]

# =======================================================
# 🧠 大模型 B：交易员反思/参数调整 API (使用硅基流动)
# 负责：读取爆仓或连亏日志，深度推理并输出新的交易基因
# =======================================================
SILICONFLOW_API_KEY = os.getenv("SILICONFLOW_API_KEY", "")
SILICONFLOW_BASE_URL = "https://api.siliconflow.cn/v1"
# 使用免费的 DeepSeek 蒸馏版，具有极强的推理 (Reasoning) 能力
REFLECTION_MODEL = "deepseek-ai/DeepSeek-R1-Distill-Qwen-7B"

# 为了防止免费 API 被限流 (HTTP 429 Error)
REFLECTION_WORKER_SLEEP = 15      # 每次请求完休眠 15 秒排队

# --- 演化与残酷淘汰机制 ---
ELITE_CLONE_RATIO = 0.5           # 20%是富人阶层/精英的后代传承
MAX_DRAWDOWN_LIMIT = 0.3          # 【加速淘汰】最大回撤超过50%直接宣告破产，不用等归零！