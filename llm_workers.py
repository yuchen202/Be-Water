# llm_workers.py
import asyncio
import json
import re
import time
import feedparser
from openai import AsyncOpenAI
from agent_core import Agent, TradeLog, MarketNews, db
from config import *

# =========================================================
# 1. 兼容性处理智谱大模型的客户端导入 (支持 zai 或 zhipuai)
# =========================================================
ZhipuClientClass = None
try:
    from zai import ZhipuAiClient

    ZhipuClientClass = ZhipuAiClient
    print("✅ 成功加载智谱客户端: zai.ZhipuAiClient")
except ImportError:
    try:
        from zhipuai import ZhipuAI

        ZhipuClientClass = ZhipuAI
        print("✅ 成功加载智谱客户端: zhipuai.ZhipuAI")
    except ImportError:
        print("⚠️ 未安装智谱AI SDK (zai 或 zhipuai)，新闻打分功能将暂停。")

# 初始化智谱客户端
zhipu_client = None
if ZHIPU_API_KEY and ZHIPU_API_KEY != "your_zhipu_api_key" and ZhipuClientClass:
    zhipu_client = ZhipuClientClass(api_key=ZHIPU_API_KEY)

# =========================================================
# 2. 初始化硅基流动客户端和反思队列
# =========================================================
silicon_client = AsyncOpenAI(api_key=SILICONFLOW_API_KEY, base_url=SILICONFLOW_BASE_URL)
reflection_queue = asyncio.Queue()


# =========================================================
# Worker 1: 新闻分析员
# =========================================================
async def news_analyzer_worker():
    print("📰 新闻情绪分析员已就绪...")
    while True:
        try:
            feed = feedparser.parse(NEWS_RSS_URLS[0])
            if feed.entries and zhipu_client:
                latest_news = feed.entries[0].title

                # 如果新闻没分析过，则调用大模型
                if not MarketNews.select().where(MarketNews.title == latest_news).exists():
                    prompt = f"分析新闻对加密货币影响，严格返回JSON: {{\"sentiment_score\": 0.8, \"summary\": \"简评\"}}。新闻:{latest_news}"

                    response = await asyncio.to_thread(
                        zhipu_client.chat.completions.create,
                        model=NEWS_MODEL,
                        messages=[{"role": "user", "content": prompt}],
                        temperature=0.3
                    )

                    content = response.choices[0].message.content
                    json_match = re.search(r'\{.*\}', content, re.DOTALL)

                    if json_match:
                        res = json.loads(json_match.group())
                        MarketNews.create(
                            timestamp=time.time(),
                            title=latest_news,
                            sentiment_score=res.get('sentiment_score', 0.0),
                            ai_summary=res.get('summary', '无')
                        )
                        print(f"📊 新闻打分: {latest_news[:20]}... -> 分数: {res.get('sentiment_score')}")
        except Exception as e:
            pass  # 忽略网络或API请求错误，等待下一次循环

        await asyncio.sleep(NEWS_FETCH_INTERVAL)


# =========================================================
# Worker 2: 交易员深度复盘
# =========================================================
async def reflection_worker():
    print("🧠 硅基流动 DeepSeek 反思导师已就绪...")
    while True:
        agent_id = await reflection_queue.get()
        try:
            agent = Agent.get(Agent.agent_id == agent_id)
            trades = list(
                TradeLog.select().where(TradeLog.agent_id == agent_id).order_by(TradeLog.timestamp.desc()).limit(5))
            history = "\n".join([f"{t.action} {t.symbol} 盈亏:{t.pnl:.2f}" for t in trades])

            # 【核心修复 1】Prompt 注入边界意识与“不准同质化”的强指令
            prompt = f"""
            你是一位顶级的华尔街对冲基金经理，正在指导你手下的交易员微调参数。
            【极其重要的警告】：千万不要把所有交易员都教成保守派！市场需要狼性！
            如果他是激进流派（如sniper/hft），你可以让他继续保持高杠杆，只需微调止损。
            如果他是稳健流派（如grid/reversion），才建议降低杠杆。

            学徒当前基因：
            流派: {agent.strategy_type}
            当前杠杆: {agent.gene_leverage}x
            当前止损ATR倍数: {agent.gene_sl_atr:.2f}
            当前单次仓位比例: {agent.gene_position_size:.2f}

            最近交易记录：
            {history}

            请严格按照以下JSON格式返回微调后的参数，务必遵守数值范围限制（不要加任何废话）：
            {{
                "new_leverage": 建议杠杆 (整数, 范围 1 到 125),
                "new_sl_atr": 止损宽容度 (浮点数, 范围 0.5 到 8.0。基于ATR倍数，数值越小止损越快。绝不可超过 10.0！),
                "new_position_size": 仓位比例 (浮点数, 范围 0.01 到 0.5)
            }}
            """

            response = await silicon_client.chat.completions.create(
                model=REFLECTION_MODEL,
                messages=[{'role': 'user', 'content': prompt}],
                stream=False
            )

            reply = response.choices[0].message.content
            # 过滤掉 DeepSeek-R1 的 <think> 思考过程
            json_text = reply.split("</think>")[-1].strip() if "</think>" in reply else reply
            json_match = re.search(r'\{.*\}', json_text, re.DOTALL)

            if json_match:
                new_params = json.loads(json_match.group())

                # 【核心修复 2】加上绝对的物理边界锁，防止大模型幻觉输出 4000 倍的离谱数据
                # 杠杆限制在 1x 到 125x 之间
                raw_leverage = int(new_params.get('new_leverage', agent.gene_leverage))
                agent.gene_leverage = max(1, min(125, raw_leverage))

                # 止损 ATR 限制在 0.2 到 10.0 之间 (正常策略极少超过10)
                raw_sl = float(new_params.get('new_sl_atr', agent.gene_sl_atr))
                agent.gene_sl_atr = max(0.2, min(10.0, raw_sl))

                # 仓位比例限制在 1% 到 50% 之间
                raw_pos = float(new_params.get('new_position_size', agent.gene_position_size))
                agent.gene_position_size = max(0.01, min(0.5, raw_pos))

                agent.status = 'ACTIVE'  # 唤醒交易员
                agent.save()
                print(f"✨[{agent_id}] 深度反思完毕！已注入新参数。")
            else:
                agent.status = 'ACTIVE'
                agent.save()

        except Exception as e:
            # 万一出错，强制唤醒，不能让交易员一直卡死
            try:
                Agent.update(status='ACTIVE').where(Agent.agent_id == agent_id).execute()
            except:
                pass

        reflection_queue.task_done()
        await asyncio.sleep(REFLECTION_WORKER_SLEEP)

# =========================================================
# 触发反思接口 (供交易引擎调用)
# =========================================================
async def request_reflection(agent_id: str):
    Agent.update(status='PAUSED_REFLECTING').where(Agent.agent_id == agent_id).execute()
    await reflection_queue.put(agent_id)