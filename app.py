import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import streamlit as st
import pandas as pd
import plotly.express as px

st.set_page_config(page_title="Fund-Advisor", page_icon="📊", layout="wide")

from dataclasses import asdict

from src.data.storage import MarketDB
from src.data.portfolio import load_portfolio
from src.config import load_config


@st.cache_resource
def get_db():
    config = load_config()
    return MarketDB(config.data.storage.path)


@st.cache_data(ttl=300)
def get_latest_data():
    db = get_db()
    last_date = db.get_latest_date()
    if not last_date:
        return None, None, None
    etfs = db.get_latest_etfs(100)
    indices = db.get_latest_indices()
    return last_date, etfs, indices


@st.cache_data(ttl=300)
def get_etf_history(code: str, days: int = 60):
    return get_db().get_historical_etf(code, days)


@st.cache_data(ttl=300)
def get_index_history(code: str, days: int = 252):
    return get_db().get_historical_index(code, days)


def run_async(coro):
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(asyncio.run, coro)
                return future.result(timeout=120)
        return asyncio.run(coro)
    except RuntimeError:
        return asyncio.run(coro)


def render_sidebar():
    last_date, _, _ = get_latest_data()
    with st.sidebar:
        st.title("Fund-Advisor")
        st.caption("个人基金ETF投资建议系统")
        st.divider()
        st.metric("最新数据日期", last_date or "暂无数据")
        st.divider()
        return st.radio("导航", ["📈 ETF排行榜", "🔥 行业热力图", "💰 持仓收益", "📋 日报回顾", "⚡ 手动触发"],
                         label_visibility="collapsed")


def render_etf_rankings():
    st.header("ETF 排行榜")
    _, etfs, _ = get_latest_data()
    if not etfs:
        st.info("暂无数据，请先运行数据采集: `uv run python main.py once`")
        return

    df = pd.DataFrame(etfs)
    search = st.text_input("搜索代码或名称", placeholder="例如: 510300 或 沪深300")
    if search:
        df = df[df["code"].str.contains(search, case=False) | df["name"].str.contains(search, case=False)]

    df["abs_change"] = df["change_pct"].abs()
    df = df.sort_values("abs_change", ascending=False).head(30)

    st.dataframe(
        df[["code", "name", "price", "change_pct", "amount", "pe_ratio", "pb_ratio"]].rename(columns={
            "code": "代码", "name": "名称", "price": "最新价", "change_pct": "涨跌幅%",
            "amount": "成交额", "pe_ratio": "PE", "pb_ratio": "PB",
        }),
        column_config={
            "涨跌幅%": st.column_config.NumberColumn(format="%+.2f%%"),
            "最新价": st.column_config.NumberColumn(format="¥%.3f"),
            "成交额": st.column_config.NumberColumn(format="¥%.0f"),
            "PE": st.column_config.NumberColumn(format="%.1f"),
            "PB": st.column_config.NumberColumn(format="%.2f"),
        },
        use_container_width=True, hide_index=True, height=700,
    )

    st.subheader("Top 10 涨跌幅")
    top10 = df.head(10).copy()
    top10["color"] = top10["change_pct"].apply(lambda x: "green" if x > 0 else "red")
    fig = px.bar(top10, x="change_pct", y="name", orientation="h",
                 color="change_pct", color_continuous_scale=["red", "lightgray", "green"],
                 labels={"change_pct": "涨跌幅%", "name": ""})
    fig.update_layout(height=400, coloraxis_showscale=False)
    st.plotly_chart(fig, use_container_width=True)


def render_sector_heatmap():
    st.header("行业轮动热力图")
    db = get_db()
    last_date = db.get_latest_date()
    if not last_date:
        st.info("暂无数据")
        return

    import sqlite3
    conn = sqlite3.connect(str(db.db_path))
    df = pd.read_sql_query(f"SELECT * FROM sector_daily WHERE date='{last_date}' ORDER BY change_pct DESC", conn)
    conn.close()

    if df.empty:
        st.info("暂无行业数据")
        return

    df = df.head(20)
    df["color"] = df["change_pct"].apply(lambda x: "green" if x > 0 else "red")
    fig = px.bar(df, x="change_pct", y="name", orientation="h",
                 color="change_pct", color_continuous_scale=["red", "lightgray", "green"],
                 labels={"change_pct": "涨跌幅%", "name": ""})
    fig.update_layout(height=500, coloraxis_showscale=False)
    st.plotly_chart(fig, use_container_width=True)

    st.dataframe(
        df[["name", "change_pct", "momentum_1m", "momentum_3m", "momentum_6m"]].rename(columns={
            "name": "行业", "change_pct": "今日涨跌%", "momentum_1m": "近1月动量",
            "momentum_3m": "近3月动量", "momentum_6m": "近6月动量",
        }),
        column_config={
            "今日涨跌%": st.column_config.NumberColumn(format="%+.2f%%"),
            "近1月动量": st.column_config.NumberColumn(format="%+.2f%%"),
            "近3月动量": st.column_config.NumberColumn(format="%+.2f%%"),
            "近6月动量": st.column_config.NumberColumn(format="%+.2f%%"),
        },
        use_container_width=True, hide_index=True,
    )


def render_portfolio():
    st.header("持仓收益")
    holdings = load_portfolio()
    if not holdings:
        st.info("请先配置 portfolio.yaml")
        return

    last_date, etfs, indices = get_latest_data()
    if not etfs:
        st.info("暂无行情数据")
        return

    etf_map = {e["code"]: e for e in etfs}
    idx_map = {i["code"]: i for i in (indices or [])}

    rows = []
    total_value = 0.0
    total_cost = 0.0
    for h in holdings:
        price = 0.0
        change_pct = 0.0
        if h.code in etf_map:
            price = float(etf_map[h.code].get("price", 0))
            change_pct = float(etf_map[h.code].get("change_pct", 0))
        elif h.code in idx_map:
            price = float(idx_map[h.code].get("price", 0))
            change_pct = float(idx_map[h.code].get("change_pct", 0))

        market_val = price * h.shares if price > 0 else 0
        cost_val = h.cost_basis * h.shares
        pnl_pct = ((price - h.cost_basis) / h.cost_basis * 100) if price > 0 and h.cost_basis > 0 else 0
        total_value += market_val
        total_cost += cost_val

        rows.append({
            "代码": h.code, "名称": h.name, "类别": h.category.value,
            "成本价": h.cost_basis, "最新价": price, "涨跌幅%": change_pct,
            "持仓盈亏%": pnl_pct, "市值": market_val, "份额": h.shares,
        })

    df = pd.DataFrame(rows)
    total_pnl = total_value - total_cost
    total_change = (total_pnl / total_cost * 100) if total_cost > 0 else 0

    cols = st.columns(4)
    cols[0].metric("总市值", f"¥{total_value:,.0f}")
    cols[1].metric("总成本", f"¥{total_cost:,.0f}")
    cols[2].metric("总盈亏", f"¥{total_pnl:+,.0f}")
    cols[3].metric("总收益率", f"{total_change:+.2f}%")

    st.divider()
    st.dataframe(
        df[["代码", "名称", "类别", "最新价", "涨跌幅%", "成本价", "持仓盈亏%", "市值"]],
        column_config={
            "涨跌幅%": st.column_config.NumberColumn(format="%+.2f%%"),
            "持仓盈亏%": st.column_config.NumberColumn(format="%+.2f%%"),
            "最新价": st.column_config.NumberColumn(format="¥%.3f"),
            "成本价": st.column_config.NumberColumn(format="¥%.3f"),
            "市值": st.column_config.NumberColumn(format="¥%.0f"),
        },
        use_container_width=True, hide_index=True,
    )

    cat_agg = df.groupby("类别")["市值"].sum().reset_index()
    fig = px.pie(cat_agg, values="市值", names="类别", title="类别配置")
    fig.update_layout(height=350)
    st.plotly_chart(fig, use_container_width=True)

    for h in holdings:
        hist = get_etf_history(h.code, 60)
        if not hist:
            continue
        hist_df = pd.DataFrame(hist)
        if "date" in hist_df.columns and "price" in hist_df.columns:
            hist_df["date"] = pd.to_datetime(hist_df["date"])
            hist_df = hist_df.sort_values("date")
            fig = px.line(hist_df, x="date", y="price", title=f"{h.name} ({h.code}) - 近60日走势")
            fig.update_layout(height=200, margin=dict(l=0, r=0, t=30, b=0))
            st.plotly_chart(fig, use_container_width=True)


def render_report_history():
    st.header("日报回顾")
    db = get_db()
    last_date = db.get_latest_date()
    if not last_date:
        st.info("暂无数据")
        return

    _, etfs, indices = get_latest_data()
    if not indices:
        st.info("暂无数据")
        return

    st.subheader(f"最近交易日: {last_date}")

    col1, col2 = st.columns(2)
    with col1:
        st.subheader("主要指数")
        idx_df = pd.DataFrame(indices)
        st.dataframe(
            idx_df[["name", "price", "change_pct", "pe_ratio", "pb_ratio"]].rename(columns={
                "name": "指数", "price": "点位", "change_pct": "涨跌%",
                "pe_ratio": "PE", "pb_ratio": "PB",
            }),
            column_config={
                "涨跌%": st.column_config.NumberColumn(format="%+.2f%%"),
                "点位": st.column_config.NumberColumn(format="%.2f"),
            },
            use_container_width=True, hide_index=True,
        )

    with col2:
        holdings = load_portfolio()
        if holdings and etfs:
            st.subheader("你的持仓")
            etf_map = {e["code"]: e for e in etfs}
            hold_rows = []
            for h in holdings:
                e = etf_map.get(h.code, {})
                hold_rows.append({
                    "代码": h.code, "名称": h.name,
                    "现价": e.get("price", 0), "涨跌%": e.get("change_pct", 0),
                })
            hold_df = pd.DataFrame(hold_rows)
            st.dataframe(hold_df, column_config={
                "涨跌%": st.column_config.NumberColumn(format="%+.2f%%"),
                "现价": st.column_config.NumberColumn(format="¥%.3f"),
            }, use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("报告质量追踪")
    try:
        from src.reporting import ReportAuditLog

        audit_log = ReportAuditLog()
        audit_summary = audit_log.summary(limit=20)
        audit_records = audit_log.read_recent(limit=10)
    except Exception as exc:
        st.warning(f"报告审计日志读取失败：{type(exc).__name__}")
        audit_summary = None
        audit_records = []

    if audit_summary and audit_records:
        metric_cols = st.columns(4)
        latest_score = "暂无" if audit_summary.latest_score is None else str(audit_summary.latest_score)
        average_score = "暂无" if audit_summary.average_score is None else f"{audit_summary.average_score:.1f}"
        pass_rate = f"{audit_summary.verification_pass_rate * 100:.0f}%"
        metric_cols[0].metric("最新评分", f"{audit_summary.latest_grade or '-'} {latest_score}")
        metric_cols[1].metric("平均分", average_score)
        metric_cols[2].metric("验证通过率", pass_rate)
        metric_cols[3].metric("审计记录", str(audit_summary.total))

        rows = []
        for score in reversed(audit_summary.recent_scores[-10:]):
            finding_codes = score.get("finding_codes") or []
            rows.append({
                "日期": score.get("as_of_date"),
                "周期": score.get("report_period"),
                "来源": score.get("source"),
                "评分": score.get("score"),
                "等级": score.get("grade") or "-",
                "验证": "通过" if score.get("verification_passed") else "需复核",
                "问题": ", ".join(finding_codes) if isinstance(finding_codes, list) else "",
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        latest_record = audit_records[-1]
        if latest_record.finding_codes:
            st.warning("最近报告需复核：" + "、".join(latest_record.finding_codes[:5]))
        if latest_record.missing_data:
            st.caption("最近报告缺失数据：" + "、".join(latest_record.missing_data[:5]))
        if audit_summary.blocker_counts:
            blocker_text = "、".join(f"{name}×{count}" for name, count in audit_summary.blocker_counts.items())
            st.error("近期阻断项：" + blocker_text)
        with st.expander("查看最近报告正文", expanded=False):
            st.markdown(latest_record.report_text or "暂无正文")
    else:
        st.info("暂无报告审计记录。运行 `uv run python main.py once` 后会自动生成质量追踪。")


def render_generated_report_quality(bundle):
    st.subheader("质量校验")
    score = bundle.quality_score
    cols = st.columns(4)
    cols[0].metric("评分", f"{score.grade} {score.overall}")
    cols[1].metric("来源", "LLM" if bundle.source == "llm" else "Fallback")
    cols[2].metric("验证", "通过" if bundle.verification.passed else "需复核")
    cols[3].metric("置信度", f"{bundle.verification.confidence * 100:.0f}%")

    if bundle.verification.findings:
        findings = [
            {
                "级别": finding.level,
                "代码": finding.code,
                "说明": finding.message,
            }
            for finding in bundle.verification.findings
        ]
        st.dataframe(pd.DataFrame(findings), use_container_width=True, hide_index=True)

    if score.blockers:
        st.error("阻断项：" + "、".join(score.blockers))
    if score.notes:
        st.caption("评分备注：" + "；".join(score.notes))

    changes = bundle.change_summary
    if changes.has_previous:
        if changes.key_changes:
            st.info("相较上一期：" + "；".join(changes.key_changes[:4]))
        if changes.cautions:
            st.caption("历史参考提示：" + "；".join(changes.cautions[:3]))
    else:
        st.caption("暂无同周期历史报告，变化分析将在下一次生成后出现。")


def render_manual_trigger():
    st.header("手动触发即时分析")

    period_choice = st.selectbox(
        "分析窗口",
        options=["auto", "daily", "weekly", "monthly"],
        format_func=lambda p: {"auto": "自动选择", "daily": "日报", "weekly": "周报", "monthly": "月报"}[p],
    )
    st.write("点击下方按钮触发数据采集 → 指标计算 → LLM报告生成 → 推送")

    if st.button("🚀 立即分析", type="primary", use_container_width=True):
        config = load_config()
        status = st.status("正在执行...", expanded=True)

        async def do_analysis():
            status.update(label="1/4 采集数据...", state="running")
            from src.data.pipeline import DataPipeline
            pipeline = DataPipeline(config)
            snapshot = await pipeline.run_daily_collection()
            portfolio = pipeline.calc_holding_status(snapshot)
            status.update(label="2/4 计算指标...", state="running")

            from src.analysis.engine import AnalysisEngine
            from src.analysis.aggregator import build_window_snapshot
            from src.llm.report_period import normalize_report_period, report_period_label, select_report_period, ReportPeriod

            report_period: ReportPeriod = "daily"
            if period_choice == "auto":
                report_period = select_report_period(snapshot.date)
            else:
                report_period = normalize_report_period(period_choice)

            window_overrides = build_window_snapshot(pipeline.db, str(snapshot.date), report_period)
            engine = AnalysisEngine(db=pipeline.db)
            analysis = engine.analyze(asdict(snapshot), window_snapshot=window_overrides)
            analysis["portfolio_status"] = {
                "holdings": [{"code": h.code, "name": h.name, "current_price": h.current_price,
                               "change_pct": h.change_pct, "profit_loss_pct": h.profit_loss_pct,
                               "cost_basis": h.cost_basis, "suggestion": h.suggestion}
                              for h in portfolio.holdings],
                "total_value": portfolio.total_value,
                "total_change_pct": portfolio.total_change_pct,
                "total_profit_loss": portfolio.total_profit_loss,
            }
            status.update(label="3/4 生成报告...", state="running")

            from src.llm.client import LLMClient
            from src.llm.report_generator import ReportGenerator
            llm_client = LLMClient.from_config(config.llm)
            report_gen = ReportGenerator(llm_client)
            report_label = report_period_label(report_period)
            report_bundle = await report_gen.generate_daily_report_bundle(analysis, report_period=report_period)
            report_text = report_bundle.text
            status.update(label="4/4 推送通知...", state="running")

            import os
            from src.notify.channels import NotificationManager, WeChatWorkChannel, FeishuChannel
            nm = NotificationManager()
            wx_url = os.environ.get(config.notify.wechat_work.webhook_url_env, "")
            fs_url = os.environ.get(config.notify.feishu.webhook_url_env, "")
            if config.notify.wechat_work.enabled and wx_url:
                nm.add_channel("wechat_work", WeChatWorkChannel(wx_url))
            if config.notify.feishu.enabled and fs_url:
                nm.add_channel("feishu", FeishuChannel(fs_url))
            if nm.channels:
                await nm.broadcast(report_text, title=f"投资{report_label} {snapshot.date}")

            status.update(label="完成!", state="complete")
            return snapshot, portfolio, report_bundle, report_label

        result = run_async(do_analysis())
        snapshot, portfolio, report_bundle, report_label = result
        report_text = report_bundle.text

        st.success(f"{report_label}分析完成: {snapshot.date}")
        st.metric("数据量", f"{len(snapshot.etfs)} ETFs, {len(snapshot.indices)} 指数, {len(snapshot.sectors)} 行业")
        if portfolio.total_value > 0:
            st.metric("持仓市值", f"¥{portfolio.total_value:,.0f}")
        render_generated_report_quality(report_bundle)
        st.divider()
        st.subheader(f"📋 {report_label}内容")
        st.markdown(report_text)


def main():
    tab = render_sidebar()
    if tab == "📈 ETF排行榜":
        render_etf_rankings()
    elif tab == "🔥 行业热力图":
        render_sector_heatmap()
    elif tab == "💰 持仓收益":
        render_portfolio()
    elif tab == "📋 日报回顾":
        render_report_history()
    elif tab == "⚡ 手动触发":
        render_manual_trigger()


if __name__ == "__main__":
    main()
