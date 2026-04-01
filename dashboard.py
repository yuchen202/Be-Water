# dashboard.py
import sys
import subprocess
import json
import os
import time
import pandas as pd
import numpy as np
import plotly.express as px
import plotly.graph_objects as go
import sqlite3
import streamlit as st
from agent_core import Agent, TradeLog, MarketNews, WealthSnapshot, db
from config import INITIAL_CAPITAL, TOTAL_AGENTS, DB_PATH
from streamlit.runtime.scriptrunner import get_script_run_ctx

# --------------------------------------------------------------------------
# 1. 启动器与环境配置
# --------------------------------------------------------------------------
if not get_script_run_ctx():
    print("🚀 检测到直接使用 Python 运行，正在自动唤起 Streamlit Web 服务...")
    subprocess.run([sys.executable, "-m", "streamlit", "run", sys.argv[0]])
    sys.exit()

st.set_page_config(layout="wide", page_title="加密社会学：万人数字生命观察站", initial_sidebar_state="auto")

# 侧边栏上帝控制台
st.sidebar.title("🛠️ 上帝控制台")
if st.sidebar.button("💥 清除所有缓存并硬刷新"):
    st.cache_data.clear()
    st.rerun()

st.sidebar.markdown("---")
st.sidebar.info("💡 论文提示：\n1. 顶层指标实时刷新。\n2. 深度图表每分钟自动重构。\n3. 使用侧边栏按钮同步新实验。")

# 强制全屏滑动 CSS
st.markdown("""
    <style>
    [data-testid="stAppViewContainer"], .main { overflow-y: scroll !important; }
    ::-webkit-scrollbar { width: 12px; background-color: #0E1117; }
    ::-webkit-scrollbar-thumb { background-color: #4CAF50; border-radius: 10px; }
    .block-container { padding-bottom: 100px; }
    </style>
""", unsafe_allow_html=True)


# --------------------------------------------------------------------------
# 2. 核心数据引擎 (JSON 实时 + SQL 考古)
# --------------------------------------------------------------------------

def load_realtime_metrics():
    """读取时光机发布的实时 JSON 战报"""
    default_state = {
        "alive_count": TOTAL_AGENTS, "dead_history_count": 0, "total_actions": 0,
        "total_wealth": TOTAL_AGENTS * INITIAL_CAPITAL, "richest_wealth": INITIAL_CAPITAL, "top_100": []
    }
    if os.path.exists("dashboard_data.json"):
        try:
            with open("dashboard_data.json", "r") as f:
                return json.load(f)
        except:
            return default_state
    return default_state


@st.cache_data(ttl=60, show_spinner="⏳ 正在进行深度数据加载与数字考古 (首次约需 10-30 秒)...")
def load_deep_db_data(db_path):
    """深度考古：从数千万笔记录中还原历史 (耗时操作)"""
    try:
        conn = sqlite3.connect(db_path)

        # 1. 提取所有代理的基本信息
        df_agents = pd.read_sql_query("SELECT * FROM agent", conn)

        # 2. 🚀 核心修复：通过交易日志，精准算出每个人的真实 PnL 总和！
        query_pnl = "SELECT agent_id, SUM(pnl) as total_pnl, COUNT(id) as real_trades FROM tradelog GROUP BY agent_id"
        df_pnl = pd.read_sql_query(query_pnl, conn)

        # 将算出的真实盈亏合并回 Agent 表，强制覆盖错误的 current_balance
        if not df_agents.empty and not df_pnl.empty:
            df_agents = pd.merge(df_agents, df_pnl, on='agent_id', how='left')
            df_agents['total_pnl'] = df_agents['total_pnl'].fillna(0)
            df_agents['real_trades'] = df_agents['real_trades'].fillna(0)
            # 真实余额 = 初始 10000 + 历史总盈亏
            df_agents['current_balance'] = INITIAL_CAPITAL + df_agents['total_pnl']
            df_agents['total_trades'] = df_agents['real_trades']

        # 3. 提取财富快照
        df_snapshots = pd.read_sql_query("SELECT * FROM wealthsnapshot ORDER BY timestamp ASC", conn)

        # 4. 提取生命周期与首富轨迹
        query_life = """
        SELECT agent_id, MIN(timestamp) as birth_time, 
               MAX(CASE WHEN action = 'LIQUIDATED_OR_BANKRUPT' THEN timestamp ELSE NULL END) as death_time 
        FROM tradelog GROUP BY agent_id
        """
        df_life = pd.read_sql_query(query_life, conn)

        df_rich_trace = pd.DataFrame()
        if not df_agents.empty:
            rid = df_agents.loc[df_agents['current_balance'].idxmax()]['agent_id']
            df_rich_trace = pd.read_sql_query(f"SELECT timestamp, pnl FROM tradelog WHERE agent_id='{rid}'", conn)

        conn.close()
        return df_agents, df_snapshots, df_life, df_rich_trace
    except:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame()


def build_succession_data(df_life):
    """将考古碎片拼接成演化长卷"""
    if df_life.empty: return pd.DataFrame()

    def get_strat(aid):
        for s in ['SNIPER', 'TREND', 'REVERSION', 'GRID', 'HFT']:
            if s in aid.upper(): return s.lower()
        return 'unknown'

    df_life['strategy'] = df_life['agent_id'].apply(get_strat)

    # 🚀 核心修复点 1：找到宇宙大爆炸的第一秒
    t_min = df_life['birth_time'].min()
    if pd.isna(t_min): return pd.DataFrame()

    # 🚀 核心修复点 2：强行为所有 G0 代上户口！不管他们什么时候开的第一单，都算在第一秒出生！
    df_life.loc[df_life['agent_id'].str.contains('_G0'), 'birth_time'] = t_min

    t_max = df_life['birth_time'].max()

    # 时间轴切片采样 (50个点)
    time_slices = np.linspace(t_min, t_max, 50)
    results = []
    for ts in time_slices:
        alive_mask = (df_life['birth_time'] <= ts) & (df_life['death_time'].fillna(float('inf')) > ts)
        counts = df_life[alive_mask]['strategy'].value_counts().to_dict()
        counts['time'] = pd.to_datetime(ts, unit='s')
        results.append(counts)

    df_res = pd.DataFrame(results).fillna(0)
    df_melt = df_res.melt(id_vars=['time'], var_name='strategy', value_name='count')
    df_melt['percentage'] = df_melt.groupby('time')['count'].transform(
        lambda x: x / x.sum() * 100 if x.sum() > 0 else 0)
    return df_melt


# --------------------------------------------------------------------------
# 3. 界面渲染
# --------------------------------------------------------------------------

# 加载数据 (函数名已彻底统一)
rt = load_realtime_metrics()
df_agents, df_snapshots, df_life, df_rich_trace = load_deep_db_data(DB_PATH)

st.title("🧬 Crypto Society: 万人数字生命演化观察站")

# 3.1 顶部实时指标 (心跳)
col1, col2, col3, col4, col5 = st.columns(5)
col1.metric("👥 存活居民", f"{rt['alive_count']} / {TOTAL_AGENTS}")
col2.metric("💀 累计淘汰", rt['dead_history_count'])
col3.metric("⚔️ 累计交易动作", f"{rt['total_actions']:,}")
cur_roi = (rt['total_wealth'] - (TOTAL_AGENTS * INITIAL_CAPITAL)) / (TOTAL_AGENTS * INITIAL_CAPITAL) * 100
col4.metric("💰 社会总财富", f"${rt['total_wealth']:,.0f}", f"{cur_roi:+.2f}%")
col5.metric("👑 首富资产", f"${rt['richest_wealth']:,.2f}")

# 3.2 核心 Tab 阵列
tab1, tab2, tab3, tab4, tab5 = st.tabs(
    ["📊 宏观总览", "⚖️ 财富分配", "🧬 物种演替与胜率", "🌌 3D 时空隧道", "🏆 实时排行榜"])

with tab1:
    c1, c2 = st.columns([1.5, 1])
    with c1:
        st.markdown("### 🗺️ 万人基因热力图 (风险 vs 收益)")
        if not df_agents.empty:
            df_agents['profit_pct'] = (df_agents['current_balance'] - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100
            st.plotly_chart(px.scatter(
                df_agents, x="gene_sl_atr", y="gene_leverage", color="profit_pct",
                size="current_balance", hover_name="agent_id",
                color_continuous_scale="RdYlGn", color_continuous_midpoint=0,
                opacity=0.4, size_max=12, render_mode='webgl'
            ).update_layout(height=500, template="plotly_dark", plot_bgcolor="rgba(0,0,0,0)"), use_container_width=True,
                            config={'scrollZoom': False})
    with c2:
        st.markdown("### 📈 社会总财富历史曲线")
        if not df_snapshots.empty:
            df_snapshots['time'] = pd.to_datetime(df_snapshots['timestamp'], unit='s')
            fig_wealth = px.line(df_snapshots, x='time', y='total_wealth')
            fig_wealth.update_traces(line=dict(color='#00FF00', width=3))
            st.plotly_chart(fig_wealth.update_layout(height=500, template="plotly_dark"), use_container_width=True,
                            config={'scrollZoom': False})

with tab2:
    if not df_snapshots.empty:
        c2_1, c2_2 = st.columns(2)
        with c2_1:
            st.markdown("### ⚖️ 基尼系数演化 (阶层固化度)")
            st.plotly_chart(
                px.line(df_snapshots, x='time', y='gini_coefficient').update_traces(line=dict(color='#FFA500')),
                use_container_width=True, config={'scrollZoom': False})
        with c2_2:
            st.markdown("### 🏛️ Top 1% 精英资产指数")
            df_snapshots['top1_val'] = df_snapshots['total_wealth'] * df_snapshots['top_1_percent_wealth_ratio']
            st.plotly_chart(px.line(df_snapshots, x='time', y='top1_val').update_traces(line=dict(color='#E1AD01')),
                            use_container_width=True, config={'scrollZoom': False})

        # 洛伦兹曲线
        st.markdown("### 🏹 实时社会洛伦兹曲线 (Lorenz Curve)")
        if not df_agents.empty:
            wealths = sorted(df_agents[df_agents['status'] == 'ACTIVE']['current_balance'].tolist())
            if wealths:
                cum_wealth = np.cumsum(wealths) / sum(wealths)
                cum_pop = np.arange(1, len(wealths) + 1) / len(wealths)
                fig_lorenz = go.Figure()
                fig_lorenz.add_trace(
                    go.Scatter(x=cum_pop, y=cum_wealth, fill='tozeroy', name='实际分布', line=dict(color='#FF4B4B')))
                fig_lorenz.add_trace(
                    go.Scatter(x=[0, 1], y=[0, 1], mode='lines', name='绝对公平', line=dict(dash='dash', color='gray')))
                st.plotly_chart(fig_lorenz.update_layout(template="plotly_dark", height=400), use_container_width=True,
                                config={'scrollZoom': False})

with tab3:
    st.markdown("### 🧬 达尔文法则：物种演替与收益分析")

    alive_df = df_agents[df_agents['status'] == 'ACTIVE'].copy() if not df_agents.empty else pd.DataFrame()

    # 🚀 新增：各流派人均财富与胜率深度剖析
    if not alive_df.empty:
        st.markdown("#### 📊 各流派人均财富与胜率深度剖析")
        strat_stats = alive_df.groupby('strategy_type').agg(
            存活人数=('agent_id', 'count'),
            平均资产=('current_balance', 'mean'),
            最高资产=('current_balance', 'max'),
            人均交易数=('total_trades', 'mean')
        ).reset_index()
        strat_stats['平均收益率'] = (strat_stats['平均资产'] - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100

        st.dataframe(
            strat_stats.style.format(
                {'平均资产': '${:,.2f}', '最高资产': '${:,.2f}', '平均收益率': '{:+.2f}%', '人均交易数': '{:.0f}'})
            .background_gradient(subset=['平均收益率'], cmap='RdYlGn'),
            use_container_width=True
        )

    c3_1, c3_2 = st.columns(2)
    with c3_1:
        st.markdown("#### 🥧 存活流派占比")
        if not alive_df.empty:
            strat_counts = alive_df['strategy_type'].value_counts().reset_index()
            st.plotly_chart(px.pie(strat_counts, names='strategy_type', values='count', hole=0.4,
                                   color_discrete_sequence=px.colors.qualitative.Set2), use_container_width=True)
    with c3_2:
        st.markdown("#### 🔬 代际与财富关联 (Old Money)")
        if not alive_df.empty:
            st.plotly_chart(
                px.scatter(alive_df, x='generation', y='current_balance', color='strategy_type', size='current_balance',
                           size_max=15, opacity=0.6, render_mode='webgl'), use_container_width=True)

    st.markdown("#### 🌊 种群生态演替长卷 (Ecological Succession)")
    df_melt = build_succession_data(df_life)
    if not df_melt.empty:
        st.plotly_chart(px.area(df_melt, x='time', y='percentage', color='strategy',
                                color_discrete_sequence=px.colors.qualitative.Set2).update_layout(height=500,
                                                                                                  template="plotly_dark",
                                                                                                  yaxis=dict(
                                                                                                      range=[0, 100])),
                        use_container_width=True, config={'scrollZoom': False})

with tab4:
    st.markdown("### 🌌 阶层跃迁时空隧道 (Top 30 精英资产轨迹)")
    if not df_agents.empty:
        top_30_ids = df_agents.nlargest(30, 'current_balance')['agent_id'].tolist()
        try:
            conn = sqlite3.connect(DB_PATH)
            df_3d = pd.read_sql_query(
                f"SELECT agent_id, timestamp, pnl FROM tradelog WHERE agent_id IN ({','.join(['?'] * len(top_30_ids))})",
                conn, params=top_30_ids)
            conn.close()
            if not df_3d.empty:
                df_3d['equity'] = df_3d.groupby('agent_id')['pnl'].transform(pd.Series.cumsum) + INITIAL_CAPITAL
                df_3d['time'] = pd.to_datetime(df_3d['timestamp'], unit='s')
                st.plotly_chart(
                    px.line_3d(df_3d, x='time', y='agent_id', z='equity', color='agent_id').update_layout(height=700,
                                                                                                          template="plotly_dark",
                                                                                                          scene=dict(
                                                                                                              bgcolor="black")),
                    use_container_width=True)
        except:
            st.info("3D 数据重构中...")

with tab5:
    st.markdown("### 🏆 Top 1% 精英阶层实时排行榜 (Real-time)")
    top_data = rt.get('top_100', rt.get('top_50', []))
    if top_data:
        elite_df = pd.DataFrame(top_data)
        st.dataframe(
            elite_df.style.format({'equity': '${:,.2f}'}).background_gradient(subset=['equity'], cmap='Greens'),
            use_container_width=True, height=400)
    else:
        st.warning("等待第一份实时战报...")

    st.markdown("---")
    st.markdown("### 📜 全社会万名居民详细档案 (Census Registry)")
    if not df_agents.empty:
        census_df = df_agents.copy()
        census_df['ROI %'] = (census_df['current_balance'] - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100
        cols = ['agent_id', 'strategy_type', 'current_balance', 'ROI %', 'generation', 'total_trades', 'gene_leverage',
                'gene_sl_atr']
        st.dataframe(
            census_df[cols].sort_values('current_balance', ascending=False).reset_index(drop=True).style.format(
                {'current_balance': '${:,.2f}', 'ROI %': '{:+.2f}%'}), use_container_width=True, height=500)

    st.markdown("---")
    st.markdown("#### 👑 当前首富历史资产轨迹")
    if not df_rich_trace.empty:
        df_rich_trace['equity'] = df_rich_trace['pnl'].cumsum() + INITIAL_CAPITAL
        df_rich_trace['time'] = pd.to_datetime(df_rich_trace['timestamp'], unit='s')
        st.plotly_chart(px.line(df_rich_trace, x='time', y='equity', template="plotly_dark").update_traces(
            line=dict(color='#FFD700', width=3)), use_container_width=True, config={'scrollZoom': False})

# --------------------------------------------------------------------------
# 4. 实时刷新控制器
# --------------------------------------------------------------------------
st.markdown("<div style='height: 100px;'></div>", unsafe_allow_html=True)
time.sleep(10000)
st.rerun()