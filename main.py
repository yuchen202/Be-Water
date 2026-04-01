# main.py
import asyncio
from agent_core import init_database
from llm_workers import news_analyzer_worker, reflection_worker
from data_stream import binance_ws_stream
from trading_engine import trading_loop
from evolution import evolution_loop


async def main():
    print("===================================================")
    print("🚀 Crypto Multi-Agent Simulation System Starting...")
    print("===================================================")

    # 1. 挂载本地数据库
    init_database()

    # 2. 注册所有异步并发任务
    tasks = [
        asyncio.create_task(binance_ws_stream()),  # 实时行情接入
        asyncio.create_task(trading_loop()),  # 核心虚拟撮合
        asyncio.create_task(news_analyzer_worker()),  # LLM 新闻读取
        asyncio.create_task(reflection_worker()),  # LLM 亏损反思排队
        asyncio.create_task(evolution_loop())  # 种群演化
    ]

    # 3. 永久运行
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 模拟系统已安全终止，所有状态已保存在 SQLite 中！")