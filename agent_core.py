# agent_core.py
import json
import random
import time
import os
from peewee import SqliteDatabase, Model, CharField, FloatField, IntegerField, TextField
from config import DB_PATH, TOTAL_AGENTS, INITIAL_CAPITAL, ARCHIVE_DIR

# 确保档案文件夹存在
if not os.path.exists(ARCHIVE_DIR):
    os.makedirs(ARCHIVE_DIR)

db = SqliteDatabase(DB_PATH, pragmas={
    'journal_mode': 'wal',
    'cache_size': -1024 * 64,  # 64MB 缓存
    'busy_timeout': 30000,     # 30000 毫秒 = 30 秒等待
    'synchronous': 'NORMAL'    # 提升性能的同时保证安全
})


class BaseModel(Model):
    class Meta: database = db


class Agent(BaseModel):
    agent_id = CharField(unique=True, index=True)
    generation = IntegerField(default=0)
    status = CharField(default='ACTIVE')
    initial_balance = FloatField(default=INITIAL_CAPITAL)
    current_balance = FloatField(default=INITIAL_CAPITAL)
    peak_balance = FloatField(default=INITIAL_CAPITAL)  # 记录巅峰财富，用于计算回撤
    max_drawdown = FloatField(default=0.0)
    total_trades = IntegerField(default=0)
    strategy_type = CharField()
    gene_fomo = FloatField();
    gene_panic = FloatField();
    gene_leverage = IntegerField()
    gene_martingale = FloatField();
    gene_sl_atr = FloatField();
    gene_tp_atr = FloatField()
    gene_position_size = FloatField();
    gene_news_weight = FloatField()
    gene_favorite_symbols = TextField(default="[]")
    last_action_time = FloatField(default=0.0)


class TradeLog(BaseModel):
    agent_id = CharField(index=True);
    symbol = CharField();
    action = CharField();
    price = FloatField();
    size = FloatField();
    leverage = IntegerField();
    pnl = FloatField(default=0.0);
    timestamp = FloatField();
    reason = CharField(null=True)


class Position(BaseModel):
    agent_id = CharField(index=True);
    symbol = CharField();
    side = CharField();
    entry_price = FloatField();
    size = FloatField();
    leverage = IntegerField();
    sl_price = FloatField();
    tp_price = FloatField()


class WealthSnapshot(BaseModel):
    """【社会学统计】记录整个社会在不同时间点的贫富差距"""
    timestamp = FloatField(index=True)
    total_wealth = FloatField()  # 社会总财富
    gini_coefficient = FloatField()  # 基尼系数 (0=绝对平均, 1=绝对不平等)
    top_1_percent_wealth_ratio = FloatField()  # 顶层1%富人掌握的财富比例
    bottom_50_percent_wealth_ratio = FloatField()  # 底层50%掌握的财富比例
    alive_count = IntegerField()  # 当前存活人口

class MarketNews(BaseModel):
    """用于存储大模型解析后的实时新闻，供面板展示"""
    timestamp = FloatField(index=True)
    title = CharField()
    sentiment_score = FloatField()
    ai_summary = CharField()

def calculate_gini(wealth_array):
    """计算基尼系数核心算法"""
    if len(wealth_array) == 0: return 0
    wealth_array = sorted(wealth_array)
    n = len(wealth_array)
    coef_ = 2.0 * sum((i + 1) * wealth_array[i] for i in range(n)) / (n * sum(wealth_array)) - (n + 1.0) / n
    return coef_


def archive_dead_agent(agent):
    """【个体记录】交易员死亡/破产时，将他一生的记录写成本地 JSON 档案"""
    trades = list(TradeLog.select().where(TradeLog.agent_id == agent.agent_id).dicts())
    archive_data = {
        "agent_id": agent.agent_id,
        "generation": agent.generation,
        "strategy": agent.strategy_type,
        "lifespan_trades": agent.total_trades,
        "final_balance": agent.current_balance,
        "max_drawdown": agent.max_drawdown,
        "genes": {
            "leverage": agent.gene_leverage,
            "sl_atr": agent.gene_sl_atr,
            "tp_atr": agent.gene_tp_atr,
            "fomo": agent.gene_fomo,
            "panic": agent.gene_panic
        },
        "trade_history": trades
    }
    file_path = os.path.join(ARCHIVE_DIR, f"{agent.agent_id}_archive.json")
    with open(file_path, 'w', encoding='utf-8') as f:
        json.dump(archive_data, f, ensure_ascii=False, indent=2)


def generate_backtested_genes(strategy_type):
    genes = {
        'strategy_type': strategy_type,
        'gene_fomo': max(0.1, min(0.9, random.gauss(0.3, 0.2))),
        'gene_panic': max(0.1, min(0.9, random.gauss(0.4, 0.2))),
        'gene_news_weight': max(0.1, min(0.9, random.gauss(0.5, 0.3)))
    }
    big_caps = ["BTCUSDT", "ETHUSDT"];
    high_vol = ["SOLUSDT", "DOGEUSDT", "AVAXUSDT", "LINKUSDT"];
    alt_coins = ["XRPUSDT", "ADAUSDT", "DOTUSDT", "MATICUSDT"]

    # 策略生成逻辑保持一致，略微收紧杠杆防止光速死亡
    if strategy_type == 'sniper':
        genes['gene_favorite_symbols'] = json.dumps(big_caps)
        genes['gene_leverage'] = int(max(10, min(50, random.gauss(25, 10))))
        genes['gene_sl_atr'] = max(0.2, random.gauss(0.5, 0.2))
        genes['gene_tp_atr'] = max(3.0, random.gauss(8.0, 2.0))
        genes['gene_position_size'] = max(0.01, random.gauss(0.05, 0.02))
        genes['gene_martingale'] = 0.0
    elif strategy_type == 'trend':
        pool = high_vol + alt_coins
        genes['gene_favorite_symbols'] = json.dumps(random.sample(pool, k=4))
        genes['gene_leverage'] = int(max(2, min(8, random.gauss(4, 2))))
        genes['gene_sl_atr'] = max(1.5, random.gauss(2.5, 0.5))
        genes['gene_tp_atr'] = max(2.5, random.gauss(4.0, 1.0))
        genes['gene_position_size'] = max(0.05, random.gauss(0.15, 0.05))
        genes['gene_martingale'] = 0.0
    elif strategy_type == 'reversion':
        genes['gene_favorite_symbols'] = json.dumps(big_caps + high_vol[:2])
        genes['gene_leverage'] = int(max(1, min(5, random.gauss(3, 1))))
        genes['gene_sl_atr'] = max(2.0, random.gauss(3.5, 0.8))
        genes['gene_tp_atr'] = max(0.5, random.gauss(1.2, 0.3))
        genes['gene_position_size'] = max(0.1, random.gauss(0.2, 0.05))
        genes['gene_martingale'] = max(0.0, min(0.6, random.gauss(0.3, 0.2)))
    elif strategy_type == 'grid':
        genes['gene_favorite_symbols'] = json.dumps(big_caps + alt_coins)
        genes['gene_leverage'] = int(max(1, min(5, random.gauss(2, 1))))
        genes['gene_sl_atr'] = max(4.0, random.gauss(6.0, 1.0))
        genes['gene_tp_atr'] = max(0.3, random.gauss(0.8, 0.2))
        genes['gene_position_size'] = max(0.2, random.gauss(0.3, 0.1))
        genes['gene_martingale'] = max(0.5, min(1.0, random.gauss(0.8, 0.1)))
    else:  # hft
        genes['gene_favorite_symbols'] = json.dumps(high_vol)
        genes['gene_leverage'] = int(max(5, min(15, random.gauss(8, 3))))
        genes['gene_sl_atr'] = max(0.5, random.gauss(1.0, 0.3))
        genes['gene_tp_atr'] = max(0.8, random.gauss(1.5, 0.4))
        genes['gene_position_size'] = max(0.05, random.gauss(0.1, 0.05))
        genes['gene_martingale'] = 0.0

    return genes


def init_database():
    db.connect()
    db.create_tables([Agent, TradeLog, Position, WealthSnapshot, MarketNews])
    if Agent.select().where(Agent.status != 'DEAD').count() > 0:
        return
    print(f"🧬 创世纪：初始化 {TOTAL_AGENTS} 名初代交易员社会...")
    styles = ['hft', 'grid', 'trend', 'reversion', 'sniper']
    agents_to_insert = []
    for i in range(TOTAL_AGENTS):
        style = random.choice(styles)
        genes = generate_backtested_genes(style)
        genes['agent_id'] = f"TRADER_{i:04d}_{style.upper()}_G0"
        agents_to_insert.append(genes)
    with db.atomic():
        for batch in range(0, len(agents_to_insert), 100):
            Agent.insert_many(agents_to_insert[batch:batch + 100]).execute()