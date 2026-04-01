# evolution.py
import asyncio
from agent_core import Agent, Position, TradeLog, generate_backtested_genes, db
from config import TOTAL_AGENTS, ELITE_CLONE_RATIO


async def evolution_loop():
    print("🧬 达尔文演化引擎已启动 (周期: 1小时)...")
    while True:
        await asyncio.sleep(900) # 900秒 = 15分钟。

        dead_agents = list(Agent.select().where(Agent.status == 'DEAD'))
        dead_count = len(dead_agents)
        if dead_count < 10:
            continue

        print(f"💀 发现 {dead_count} 名交易员爆仓阵亡，启动演化补充机制...")

        # 找出最赚钱的精英
        elites = list(Agent.select().where(Agent.status == 'ACTIVE').order_by(Agent.current_balance.desc()).limit(5))

        new_agents = []
        for dead in dead_agents:
            import random
            is_clone = random.random() < ELITE_CLONE_RATIO and len(elites) > 0

            if is_clone:
                elite = random.choice(elites)
                # 微小变异
                genes = {
                    'strategy_type': elite.strategy_type,
                    'gene_leverage': max(1, elite.gene_leverage + random.randint(-2, 2)),
                    'gene_sl_atr': abs(elite.gene_sl_atr + random.uniform(-0.5, 0.5)),
                    'gene_tp_atr': abs(elite.gene_tp_atr + random.uniform(-0.5, 0.5)),
                    'gene_position_size': elite.gene_position_size,
                    'gene_fomo': elite.gene_fomo,
                    'gene_panic': elite.gene_panic,
                    'gene_news_weight': elite.gene_news_weight,
                    'gene_martingale': elite.gene_martingale,
                    'gene_favorite_symbols': elite.gene_favorite_symbols  # 继承老一代熟悉的币种
                }
            else:
                # 纯新人，使用最新的预训练基因生成！
                style = random.choice(['hft', 'grid', 'trend', 'reversion', 'sniper'])
                genes = generate_backtested_genes(style)

            # 删除旧死者，插入新生命
            dead.delete_instance()
            genes[
                'agent_id'] = f"TR_NEW_{random.randint(1000, 9999)}_{genes['strategy_type'].upper()}_G{elite.generation + 1 if is_clone else 0}"
            genes['generation'] = elite.generation + 1 if is_clone else 0
            new_agents.append(genes)

        with db.atomic():
            Agent.insert_many(new_agents).execute()
        print(f"🌱 已成功繁衍/招募 {dead_count} 名新交易员进入市场！")