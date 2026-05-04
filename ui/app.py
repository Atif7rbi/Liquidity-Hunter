"""Liquidity Hunter Bot — NiceGUI Dashboard v2.0 (Sniper Radar Style)"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Optional

from nicegui import app, ui
from sqlalchemy import desc, select

from src.core.database import (
    AsyncSessionLocal,
    PortfolioSnapshot,
    ScanSnapshot,
    Trade,
    TradeStatus,
    init_db,
)
from src.core.logger import logger
from src.alerts.telegram_bot import TelegramAlerter
from src.layers.paper_executor import PaperExecutor
from src.main import LiquidityHunterBot
from ui.components.widgets import (
    direction_pill,
    empty_state,
    fmt_money_short,
    fmt_pct,
    fmt_price,
    kpi_card,
    regime_pill,
    scan_progress_bar,
    score_bar,
    score_bar_cell,
    section_header,
    state_pill,
)
from ui.theme import COLORS, GLOBAL_CSS


# ══════════════════════════════════════
#  GLOBAL STATE
# ══════════════════════════════════════
bot           = LiquidityHunterBot()
executor      = PaperExecutor()
_running_lock = asyncio.Lock()
_uptime_start = datetime.now()

_scan_state: dict = {"step": 0, "counts": {}}
_scan_version: int = 0


# ══════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════

def _safe_notify(message: str, **kwargs) -> None:
    try:
        ui.notify(message, **kwargs)
    except Exception:
        pass


def _uptime_str() -> str:
    elapsed = datetime.now() - _uptime_start
    h, r    = divmod(int(elapsed.total_seconds()), 3600)
    m, s    = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _build_progress_html() -> str:
    step   = _scan_state.get("step", 0)
    counts = _scan_state.get("counts", {})
    steps  = ["Fetch", "Quality", "Volume", "OI", "Done"]
    pct    = int((step / 5) * 100)

    if step == 0:
        label, dot_color, animating = "Idle",       "rgba(255,255,255,.25)", False
    elif step >= 5:
        label, dot_color, animating = "Complete ✓", "var(--accent)",         False
    else:
        label, dot_color, animating = f"{steps[step-1]}…", "var(--info)",    True

    anim = "animation:pulse 1s infinite;" if animating else ""
    counts_html = ""
    if counts:
        total = counts.get("total", 0)
        final = counts.get("final", 0)
        counts_html = (
            f"<div style='font-size:13px;color:var(--text-muted);"
            f"font-family:var(--font-mono);margin-top:2px;'>"
            f"{total} scanned · "
            f"<span style='color:var(--accent);'>{final} final</span></div>"
        )

    return f"""
<div style="display:flex;flex-direction:column;gap:6px;padding:4px 0;">
  <div style="display:flex;align-items:center;gap:6px;width:100%;">
    <span style="width:7px;height:7px;border-radius:50%;background:{dot_color};
                 flex-shrink:0;{anim}"></span>
    <span style="font-size:13px;color:var(--text-muted);">{label}</span>
    <span style="margin-left:auto;font-family:var(--font-mono);font-size:13px;
                 color:var(--info);">{pct}%</span>
  </div>
  <div style="width:100%;height:4px;background:rgba(255,255,255,.08);
              border-radius:2px;overflow:hidden;">
    <div style="height:100%;width:{pct}%;background:var(--accent);
                border-radius:2px;transition:width .4s ease;"></div>
  </div>
  {counts_html}
</div>"""


# ══════════════════════════════════════
#  SCAN TRIGGER
# ══════════════════════════════════════

async def trigger_scan_now() -> None:
    global _scan_version
    if _running_lock.locked():
        _safe_notify("A cycle is already running", type="warning")
        return
    async with _running_lock:
        _scan_state.update({"step": 1, "counts": {}})
        _safe_notify("Cycle started…", type="info")
        try:
            result = await bot.run_cycle()
            diag   = getattr(bot.scanner, "last_diagnostics", None)
            _scan_state["step"] = 5
            _scan_state["counts"] = {
                "total":    getattr(diag, "total_symbols",    0),
                "excluded": getattr(diag, "excluded_quality", 0),
                "vol":      getattr(diag, "passed_volume",    0),
                "oi":       getattr(diag, "passed_oi",        0),
                "final":    getattr(diag, "final_shortlist",  0),
            } if diag else {}
            _scan_version += 1
            new = result.get("new_setups", 0)
            _safe_notify(f"✅ Scan done — {new} new setup{'s' if new != 1 else ''}", type="positive")
        except Exception as e:
            _scan_state["step"] = 0
            logger.exception("Manual cycle failed")
            _safe_notify(f"❌ Error: {e}", type="negative", multi_line=True)


# ══════════════════════════════════════
#  SIDEBAR
# ══════════════════════════════════════

def render_sidebar(active: str) -> None:
    routes = [
        ("Overview",  "/",         "◈"),
        ("Scanner",   "/scanner",  "⌖"),
        ("Signals",   "/signals",  "◎"),
        ("Trades",    "/trades",   "◇"),
        ("Backtest",  "/backtest", "△"),
        ("Settings",  "/settings", "⊕"),
    ]
    with ui.element("aside").classes("sidebar"):
        ui.html("""
          <div class="logo-block">
            <div class="logo-title">LIQUIDITY HUNTER</div>
            <div class="logo-sub">PRO v2.0 · Paper Mode</div>
          </div>
        """)
        ui.html("<div class='sidebar-section'><div class='sidebar-label'>Navigation</div>")
        for label, path, icon in routes:
            cls = "nav-item active" if label == active else "nav-item"
            ui.html(f"""<a href='{path}' style='text-decoration:none;'>
              <div class='{cls}'>
                <span style='font-size:14px;'>{icon}</span>
                <span>{label}</span>
              </div></a>""")
        ui.html("</div>")

        ui.html("<div class='sidebar-section'><div class='sidebar-label'>Last Scan</div>")
        prog_el = ui.html(_build_progress_html())
        ui.timer(1.0, lambda: prog_el.set_content(_build_progress_html()))
        ui.html("</div>")


# ══════════════════════════════════════
#  TOPBAR
# ══════════════════════════════════════

def render_topbar(subtitle: str = "") -> None:
    with ui.element("div").classes("topbar"):
        ui.html(
            '<button onclick="toggleSidebar()" '
            'style="background:none;border:none;color:var(--text-muted);'
            'cursor:pointer;padding:4px 10px;font-size:22px;line-height:1;'
            'margin-right:4px;flex-shrink:0;" title="Toggle sidebar">&#9776;</button>'
        )
        ui.html(f"""
          <div>
            <div class="topbar-title">LIQUIDITY HUNTER PRO</div>
            <div class="topbar-sub">BINANCE FUTURES · {subtitle}</div>
          </div>
        """)
        with ui.row().classes("items-center gap-3"):
            uptime_el = ui.html("")
            def _refresh_uptime():
                uptime_el.set_content(f"""
                  <div style="text-align:right;">
                    <div style="font-size:13px;color:var(--text-faint);letter-spacing:.06em;text-transform:uppercase;">UPTIME</div>
                    <div class="uptime">{_uptime_str()}</div>
                  </div>""")
            _refresh_uptime()
            ui.timer(1.0, _refresh_uptime)

            ui.html("""<div class="status-pill"><div class="status-dot"></div> LIVE</div>""")

            from src.core.config import settings as _s_cfg
            from datetime import timezone as _tz
            _scan_interval = int(_s_cfg.section("scanner").get("scan_interval_seconds", 300))
            _countdown_el = ui.html("")

            def _refresh_countdown():
                _now = datetime.now(_tz.utc)
                if bot.last_cycle_at:
                    _last = bot.last_cycle_at
                    if _last.tzinfo is None:
                        _last = _last.replace(tzinfo=_tz.utc)
                    elapsed   = (_now - _last).total_seconds()
                    remaining = max(0, _scan_interval - int(elapsed))
                else:
                    remaining = _scan_interval
                m2, s2 = divmod(remaining, 60)
                if remaining > 60:
                    clr = "var(--accent)"
                elif remaining > 15:
                    clr = "#f59e0b"
                else:
                    clr = "#ef4444"
                _countdown_el.set_content(
                    f"<div style='text-align:right;'>"
                    f"<div style='font-size:11px;color:var(--text-faint);letter-spacing:.06em;"
                    f"text-transform:uppercase;'>NEXT SCAN</div>"
                    f"<div style='font-size:14px;font-weight:700;color:{clr};"
                    f"font-family:var(--font-mono);'>{m2:02d}:{s2:02d}</div>"
                    f"</div>"
                )

            _refresh_countdown()
            ui.timer(1.0, _refresh_countdown)

            ui.button("↻ Refresh", on_click=lambda: ui.navigate.reload()).props("flat").style(
                "background:rgba(255,255,255,.06);color:var(--text-muted);"
                "border:1px solid var(--border);border-radius:5px;"
                "font-size:14px;padding:4px 10px;"
            )
            ui.button("▶ Run Scan", on_click=trigger_scan_now).style(
                "background:var(--accent);color:#000;font-weight:700;"
                "font-size:14px;border-radius:5px;padding:4px 12px;border:none;"
            )


# ══════════════════════════════════════
#  PAGE SHELL
# ══════════════════════════════════════

def page_shell(page_title: str, subtitle: str = ""):
    ui.add_head_html(f"<style>{GLOBAL_CSS}</style>")
    ui.add_head_html('''<script>
function toggleSidebar() {
    var s = document.querySelector('.sidebar');
    var m = document.querySelector('.main-content');
    if (!s) return;
    var hidden = s.classList.toggle('sidebar--hidden');
    if (m) m.style.marginLeft = hidden ? '0px' : '200px';
    if (m) m.style.transition  = 'margin-left 0.25s ease';
}
</script>
<style>
html body .sidebar {
    transition: transform 0.25s ease, width 0.25s ease, min-width 0.25s ease !important;
    overflow: hidden !important;
}
html body .sidebar.sidebar--hidden {
    transform: translateX(-200px) !important;
    width: 0 !important;
    min-width: 0 !important;
    border: none !important;
}
html body .main-content {
    transition: margin-left 0.25s ease !important;
}
</style>''')
    ui.add_head_html("""<style>
      @keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:0.3; } }
      html, body          { font-size:16px !important; }
      .logo-title         { font-size:17px !important; }
      .logo-sub           { font-size:13px !important; }
      .nav-item           { font-size:15px !important; padding:11px 14px !important; }
      .topbar-title       { font-size:22px !important; }
      .topbar-sub,
      .kpi-label,
      .kpi-change         { font-size:13px !important; }
      .card-title         { font-size:17px !important; }
      .kpi-val            { font-size:26px !important; }
      .lh-table td,
      .lh-table th        { font-size:14px !important; }
      code, .mono         { font-size:14px !important; }
      .uptime             { font-size:14px !important; }
      .status-pill        { font-size:13px !important; }
      .pill               { font-size:12px !important; }
      input,
      .q-field__native,
      select              { font-size:15px !important; }
    </style>""")
    ui.add_head_html(
        "<link rel='preconnect' href='https://fonts.googleapis.com'>"
        "<link rel='preconnect' href='https://fonts.gstatic.com' crossorigin>"
    )
    with ui.element("div").style("display:flex;width:100%;min-height:100vh;"):
        render_sidebar(page_title)
        with ui.element("div").classes("main-content"):
            render_topbar(subtitle)
            content = ui.element("div").classes("page-body")
            return content


# ══════════════════════════════════════
#  AUTO-REFRESH HELPER
# ══════════════════════════════════════

def _auto_refresh_on_scan(interval: float = 6.0) -> None:
    seen_version = {"v": _scan_version}

    def _check():
        if _scan_version != seen_version["v"]:
            seen_version["v"] = _scan_version
            ui.navigate.reload()

    ui.timer(interval, _check)


# ══════════════════════════════════════
#  OVERVIEW PAGE
# ══════════════════════════════════════

@ui.page("/")
async def page_overview() -> None:
    container = page_shell("Overview", "PORTFOLIO · RECENT ACTIVITY")
    _auto_refresh_on_scan()

    with container:
        equity     = await executor.get_equity()
        open_count = await executor.get_open_count()
        kill       = await executor.is_kill_switch_active()

        async with AsyncSessionLocal() as s:
            res    = await s.execute(select(Trade).where(
                Trade.status.in_([TradeStatus.CLOSED_TP.value, TradeStatus.CLOSED_SL.value])
            ))
            closed = res.scalars().all()

        wins      = [t for t in closed if (t.pnl_usd or 0) > 0]
        win_rate  = len(wins) / len(closed) if closed else 0.0
        total_pnl = sum((t.pnl_usd or 0) for t in closed)
        initial   = executor.initial_capital
        eq_pct    = (equity - initial) / initial * 100 if initial else 0

        eq_cls   = "green"  if eq_pct >= 0    else "red"
        pnl_cls  = "green"  if total_pnl >= 0 else "red"
        wr_cls   = "green"  if win_rate >= 0.5 else "yellow"
        kill_cls = "red"    if kill            else "green"

        ui.html(f"""
        <div class="kpi-row">
          <div class="kpi">
            <div class="kpi-label">Equity</div>
            <div class="kpi-val {eq_cls}">${equity:,.0f}</div>
            <div class="kpi-change {'up' if eq_pct>=0 else 'down'}">{"↑" if eq_pct>=0 else "↓"} {abs(eq_pct):.2f}%</div>
          </div>
          <div class="kpi">
            <div class="kpi-label">Realized P/L</div>
            <div class="kpi-val {pnl_cls}">${total_pnl:+,.2f}</div>
            <div class="kpi-change {'up' if total_pnl>=0 else 'down'}">{"↑" if total_pnl>=0 else "↓"} {len(closed)} trades</div>
          </div>
          <div class="kpi">
            <div class="kpi-label">Win Rate</div>
            <div class="kpi-val {wr_cls}">{win_rate*100:.1f}%</div>
            <div class="kpi-change">{len(wins)}/{len(closed)} wins</div>
          </div>
          <div class="kpi">
            <div class="kpi-label">Open Positions</div>
            <div class="kpi-val cyan">{open_count}</div>
            <div class="kpi-change">{"ACTIVE" if open_count else "Idle"}</div>
          </div>
          <div class="kpi">
            <div class="kpi-label">Kill Switch</div>
            <div class="kpi-val {kill_cls}">{"ARMED" if kill else "SAFE"}</div>
            <div class="kpi-change {'down' if kill else 'up'}">{"Daily limit hit" if kill else "Trading OK"}</div>
          </div>
        </div>
        """)

        with ui.element("div").classes("card"):
            last_str = bot.last_cycle_at.strftime("%H:%M:%S") if bot.last_cycle_at else "—"
            ui.html(
                f"<div class='card-title'>Recent Setups "
                f"<span style='color:var(--text-faint);font-weight:400;font-size:13px;'>last scan {last_str}</span></div>"
            )
            if not bot.last_decisions:
                ui.html("<div style='padding:32px;text-align:center;color:var(--text-muted);font-size:15px;'>No scans run yet — click ▶ Run Scan to begin.</div>")
            else:
                rows = sorted(bot.last_decisions.values(), key=lambda d: d["score"], reverse=True)
                tbl  = "<div class='table-wrap'><table class='lh-table'><thead><tr>"
                tbl += "<th>Symbol</th><th>Direction</th><th>Score</th><th>State</th><th>Regime</th><th>Funding</th><th>Volume 24h</th><th>Open Interest</th>"
                tbl += "</tr></thead><tbody>"
                for d in rows:
                    vol = d.get('volume_24h_usd', 0)
                    oi  = d.get('open_interest_usd', 0)
                    tbl += (
                        f"<tr>"
                        f"<td class='sym-cell'>{d['symbol']}</td>"
                        f"<td>{direction_pill(d['direction'])}</td>"
                        f"<td>{score_bar_cell(d['score'])}</td>"
                        f"<td>{state_pill(d['state'])}</td>"
                        f"<td>{regime_pill(d['regime'])}</td>"
                        f"<td class='mono tabular-nums'>{d['funding_rate']*100:+.3f}%</td>"
                        f"<td class='mono tabular-nums'>{fmt_money_short(vol)}</td>"
                        f"<td class='mono tabular-nums'>{fmt_money_short(oi)}</td>"
                        f"</tr>"
                    )
                tbl += "</tbody></table></div>"
                ui.html(tbl)

        with ui.element("div").classes("card"):
            ui.html("<div class='card-title'>Recent Closed Trades</div>")
            async with AsyncSessionLocal() as s:
                res    = await s.execute(
                    select(Trade)
                    .where(Trade.status.in_([TradeStatus.CLOSED_TP.value, TradeStatus.CLOSED_SL.value]))
                    .order_by(desc(Trade.closed_at))
                    .limit(20)
                )
                recent = res.scalars().all()

            if not recent:
                ui.html("<div style='padding:24px;text-align:center;color:var(--text-muted);font-size:15px;'>No trades closed yet.</div>")
            else:
                tbl = "<div class='table-wrap'><table class='lh-table'><thead><tr>"
                tbl += "<th>Symbol</th><th>Dir</th><th>Status</th><th>P/L (USD)</th><th>R</th><th>Closed At</th>"
                tbl += "</tr></thead><tbody>"
                for t in recent:
                    pnl = t.pnl_usd or 0
                    cls = "text-success" if pnl > 0 else "text-danger"
                    closed_at = t.closed_at.strftime("%m/%d %H:%M") if t.closed_at else "—"
                    tbl += (
                        f"<tr>"
                        f"<td class='sym-cell'>{t.symbol}</td>"
                        f"<td>{direction_pill(t.direction)}</td>"
                        f"<td><span class='pill pill-muted'>{t.status.replace('CLOSED_','')}</span></td>"
                        f"<td class='mono tabular-nums {cls}'>${pnl:+,.2f}</td>"
                        f"<td class='mono tabular-nums {cls}'>{(t.pnl_r or 0):+.2f}R</td>"
                        f"<td class='mono text-muted'>{closed_at}</td>"
                        f"</tr>"
                    )
                tbl += "</tbody></table></div>"
                ui.html(tbl)


# ══════════════════════════════════════
#  SCANNER PAGE
# ══════════════════════════════════════

@ui.page("/scanner")
async def page_scanner() -> None:
    last      = bot.last_cycle_at.strftime("%H:%M:%S") if bot.last_cycle_at else "never"
    container = page_shell("Scanner", f"LAST SCAN: {last}")
    _auto_refresh_on_scan()

    with container:
        with ui.element("div").classes("card"):
            sym_count = len(bot.last_scan_results)
            ui.html(f"<div class='card-title'>Shortlist <span class='pill pill-muted' style='margin-left:6px;'>{sym_count}</span></div>")
            if not bot.last_scan_results:
                ui.html("<div style='padding:48px;text-align:center;color:var(--text-muted);font-size:15px;'>No scan results yet. Run a scan first.</div>")
            else:
                rows = sorted(bot.last_scan_results, key=lambda r: r["extremity_score"], reverse=True)
                tbl  = "<div class='table-wrap'><table class='lh-table'><thead><tr>"
                tbl += "<th>Symbol</th><th>Price</th><th>24h Volume</th><th>OI</th><th>Funding</th><th>L/S</th><th>OI Δ4h</th><th>Score</th><th>Reasons</th>"
                tbl += "</tr></thead><tbody>"
                for r in rows:
                    reasons = ", ".join(r["reasons"]) if r["reasons"] else "—"
                    tbl += (
                        f"<tr>"
                        f"<td class='sym-cell'>{r['symbol']}</td>"
                        f"<td class='mono'>{fmt_price(r['price'])}</td>"
                        f"<td class='mono tabular-nums'>{fmt_money_short(r['volume_24h_usd'])}</td>"
                        f"<td class='mono tabular-nums'>{fmt_money_short(r['open_interest_usd'])}</td>"
                        f"<td class='mono tabular-nums'>{r['funding_rate']*100:+.3f}%</td>"
                        f"<td class='mono tabular-nums'>{r['long_short_ratio']:.2f}</td>"
                        f"<td class='mono tabular-nums'>{r['oi_change_4h_pct']*100:+.1f}%</td>"
                        f"<td>{score_bar_cell(r['extremity_score'])}</td>"
                        f"<td style='color:var(--text-muted);font-size:14px;max-width:220px;white-space:normal;'>{reasons}</td>"
                        f"</tr>"
                    )
                tbl += "</tbody></table></div>"
                ui.html(tbl)


# ══════════════════════════════════════
#  SIGNALS PAGE
# ══════════════════════════════════════

@ui.page("/signals")
async def page_signals() -> None:
    container = page_shell("Signals", "DECISION ENGINE · SCORE BREAKDOWN")
    _auto_refresh_on_scan()

    with container:
        if not bot.last_decisions:
            with ui.element("div").classes("card"):
                ui.html("<div style='padding:48px;text-align:center;color:var(--text-muted);font-size:15px;'>No decisions computed yet. Run a scan first.</div>")
            return

        sorted_decs = sorted(bot.last_decisions.values(), key=lambda d: d["score"], reverse=True)
        comp_labels = {
            "liquidity_imbalance":     "Liquidity",
            "positioning_extremity":   "Positioning",
            "oi_behavior":             "OI Behavior",
            "funding_extreme":         "Funding",
            "price_action_confluence": "Price Action",
        }

        with ui.element("div").style("display:grid;grid-template-columns:repeat(2,1fr);gap:12px;width:100%;"):
            for d in sorted_decs:
                comps_html = ""
                for k, label in comp_labels.items():
                    val = d["components"].get(k, 0)
                    comps_html += (
                        f"<div>"
                        f"<div class='kpi-label'>{label}</div>"
                        f"<div style='font-size:15px;font-weight:700;font-family:var(--font-mono);margin:2px 0;'>{val:.0f}</div>"
                        f"{score_bar(val)}"
                        f"</div>"
                    )

                ctx_html = (
                    f"<div><div class='kpi-label'>Funding</div><div class='mono'>{d['funding_rate']*100:+.3f}%</div></div>"
                    f"<div><div class='kpi-label'>L/S Ratio</div><div class='mono'>{d['ls_ratio']:.2f}</div></div>"
                    f"<div><div class='kpi-label'>Open Interest</div><div class='mono'>{fmt_money_short(d['oi_usd'])}</div></div>"
                    f"<div><div class='kpi-label'>Liq. Bias</div><div class='mono'>{d['dominant_side']} ({d['imbalance']:+.2f})</div></div>"
                )

                reasoning_html = ""
                if d.get("reasoning"):
                    reasoning_html = "<hr class='divider'><div class='kpi-label'>Reasoning</div><ul style='margin:4px 0 0;padding-left:16px;color:var(--text-muted);font-size:14px;line-height:1.8;'>"
                    for r in d["reasoning"][:5]:
                        reasoning_html += f"<li>{r}</li>"
                    reasoning_html += "</ul>"

                ui.html(f"""
                <div class="card" style="margin:0;">
                  <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:12px;">
                    <div>
                      <div style="font-size:16px;font-weight:700;letter-spacing:-.01em;">
                        {d["symbol"]}
                        <span class="text-muted" style="font-size:15px;margin-left:6px;font-weight:400;
                              font-family:var(--font-mono);">{fmt_price(d["price"])}</span>
                      </div>
                      <div style="margin-top:6px;display:flex;gap:5px;flex-wrap:wrap;">
                        {direction_pill(d["direction"])}{state_pill(d["state"])}{regime_pill(d["regime"])}
                      </div>
                    </div>
                    <div style="text-align:right;flex-shrink:0;">
                      <div class="kpi-label">Score</div>
                      <div style="font-size:24px;font-weight:700;font-family:var(--font-mono);">{d["score"]:.1f}</div>
                      <div style="margin-top:4px;min-width:80px;">{score_bar(d["score"])}</div>
                    </div>
                  </div>
                  <hr class="divider">
                  <div style="display:grid;grid-template-columns:repeat(5,1fr);gap:10px;">{comps_html}</div>
                  <hr class="divider">
                  <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px;font-size:14px;">{ctx_html}</div>
                  {reasoning_html}
                </div>""")


# ══════════════════════════════════════
#  TRADES PAGE
# ══════════════════════════════════════

@ui.page("/trades")
async def page_trades() -> None:
    container = page_shell("Trades", "OPEN & CLOSED POSITIONS")
    _auto_refresh_on_scan()

    with container:
        open_trades = await executor.get_open_trades()

        with ui.element("div").classes("card"):
            pill_cls = "pill-long" if open_trades else "pill-muted"
            ui.html(
                f"<div class='card-title'>Open Positions "
                f"<span class='pill {pill_cls}' style='margin-left:6px;'>{len(open_trades)}</span></div>"
            )
            if not open_trades:
                ui.html("<div style='padding:24px;text-align:center;color:var(--text-muted);font-size:15px;'>No open positions.</div>")
            else:
                tbl  = "<div class='table-wrap'><table class='lh-table'><thead><tr>"
                tbl += "<th>Symbol</th><th>Dir</th><th>Entry</th><th>SL</th><th>TP1</th><th>TP2</th><th>Size $</th><th>Opened</th>"
                tbl += "</tr></thead><tbody>"
                for t in open_trades:
                    tbl += (
                        f"<tr>"
                        f"<td class='sym-cell'>{t.symbol}</td>"
                        f"<td>{direction_pill(t.direction)}</td>"
                        f"<td class='mono'>{fmt_price(t.entry_price or 0)}</td>"
                        f"<td class='mono text-danger'>{fmt_price(t.stop_loss or 0)}</td>"
                        f"<td class='mono text-success'>{fmt_price(t.tp1 or 0)}</td>"
                        f"<td class='mono text-success'>{fmt_price(t.tp2 or 0)}</td>"
                        f"<td class='mono tabular-nums'>{fmt_money_short(t.position_size_usd or 0)}</td>"
                        f"<td class='mono text-muted'>{t.opened_at.strftime('%m/%d %H:%M') if t.opened_at else '—'}</td>"
                        f"</tr>"
                    )
                tbl += "</tbody></table></div>"
                ui.html(tbl)

        async with AsyncSessionLocal() as s:
            res    = await s.execute(
                select(Trade)
                .where(Trade.status.in_([
                    TradeStatus.CLOSED_TP.value,
                    TradeStatus.CLOSED_SL.value,
                    TradeStatus.CANCELLED.value,
                    TradeStatus.EXPIRED.value,
                ]))
                .order_by(desc(Trade.closed_at))
                .limit(50)
            )
            closed = res.scalars().all()

        with ui.element("div").classes("card"):
            ui.html(
                f"<div class='card-title'>Closed Trades "
                f"<span class='pill pill-muted' style='margin-left:6px;'>{len(closed)}</span></div>"
            )
            if not closed:
                ui.html("<div style='padding:24px;text-align:center;color:var(--text-muted);font-size:15px;'>No closed trades yet.</div>")
            else:
                tbl  = "<div class='table-wrap'><table class='lh-table'><thead><tr>"
                tbl += "<th>Symbol</th><th>Dir</th><th>Status</th><th>Entry</th><th>Exit</th><th>P/L USD</th><th>R</th><th>Closed</th>"
                tbl += "</tr></thead><tbody>"
                for t in closed:
                    pnl = t.pnl_usd or 0
                    cls = "text-success" if pnl > 0 else "text-danger"
                    _entry  = t.actual_entry_price or (
                        (t.entry_zone_low + t.entry_zone_high) / 2 if t.entry_zone_low else 0
                    )
                    _exit   = t.exit_price or 0
                    _closed = t.closed_at
                    _status_lbl = (t.status or "").replace("CLOSED_", "")
                    _pill_cls   = "pill-long" if "TP" in (t.status or "") else "pill-short"
                    tbl += (
                        f"<tr>"
                        f"<td class='sym-cell'>{t.symbol}</td>"
                        f"<td>{direction_pill(t.direction)}</td>"
                        f"<td><span class='pill {_pill_cls}'>{_status_lbl}</span></td>"
                        f"<td class='mono'>{fmt_price(_entry)}</td>"
                        f"<td class='mono'>{fmt_price(_exit)}</td>"
                        f"<td class='mono tabular-nums {cls}'>${pnl:+,.2f}</td>"
                        f"<td class='mono tabular-nums {cls}'>{(t.pnl_r or 0):+.2f}R</td>"
                        f"<td class='mono text-muted'>{_closed.strftime('%m/%d %H:%M') if _closed else '—'}</td>"
                        f"</tr>"
                    )
                tbl += "</tbody></table></div>"
                ui.html(tbl)


# ══════════════════════════════════════
#  BACKTEST PAGE
# ══════════════════════════════════════

@ui.page("/backtest")
async def page_backtest() -> None:
    container = page_shell("Backtest", "HISTORICAL SIMULATION")
    with container:
        ui.html("""
        <div class="kpi-row">
          <div class="kpi"><div class="kpi-label">Mode</div>
            <div class="kpi-val cyan">CLI BACKTEST</div>
            <div class="kpi-change">Runs via terminal</div></div>
          <div class="kpi"><div class="kpi-label">Default Symbol</div>
            <div class="kpi-val">BTCUSDT</div>
            <div class="kpi-change">Edit below</div></div>
          <div class="kpi"><div class="kpi-label">Default Range</div>
            <div class="kpi-val yellow">60 Days</div>
            <div class="kpi-change">Historical candles</div></div>
          <div class="kpi"><div class="kpi-label">Fee</div>
            <div class="kpi-val">0.04%</div>
            <div class="kpi-change">Taker per config</div></div>
          <div class="kpi"><div class="kpi-label">Warmup</div>
            <div class="kpi-val">100</div>
            <div class="kpi-change">Candles</div></div>
        </div>
        """)

        with ui.element("div").classes("card"):
            ui.html("<div class='card-title'>Backtest Launcher</div>")
            cmd_box = ui.html("")

            with ui.row().classes("items-end gap-4").style("flex-wrap:wrap;margin-bottom:16px;"):
                symbol_inp = ui.input(
                    label="Symbol", value="BTCUSDT", placeholder="e.g. ETHUSDT"
                ).props("outlined dense").style("min-width:180px;")
                days_inp = ui.number(
                    label="Days", value=60, min=7, max=365, step=1, format="%.0f"
                ).props("outlined dense").style("min-width:120px;")
                tf_sel = ui.select(
                    label="Timeframe", options=["15m", "1h", "4h"], value="1h"
                ).props("outlined dense").style("min-width:130px;")

            def refresh_cmd():
                sym  = (symbol_inp.value or "BTCUSDT").strip().upper()
                days = int(days_inp.value or 60)
                tf   = tf_sel.value or "1h"
                cmd_box.set_content(f"""
                <div style="background:rgba(0,0,0,.35);border:1px solid var(--border);
                            border-radius:10px;padding:18px 22px;margin-top:8px;">
                  <div style="font-size:13px;color:var(--text-muted);margin-bottom:10px;
                              letter-spacing:.04em;text-transform:uppercase;">Command — copy & run in terminal</div>
                  <code style="font-family:var(--font-mono);font-size:15px;line-height:2;
                               color:#7dd3fc;display:block;word-break:break-all;">
                    python -m src.main backtest --symbol {sym} --days {days} --timeframe {tf}
                  </code>
                </div>
                <div style="margin-top:14px;padding:14px 18px;border-radius:8px;
                            background:rgba(255,255,255,.03);border:1px solid var(--border);
                            font-size:14px;color:var(--text-muted);line-height:2;">
                  <span style="color:var(--accent);font-weight:700;">نصائح:</span><br>
                  • النتائج تُحفظ في <code style="font-family:var(--font-mono);">data/backtest_results.json</code><br>
                  • استخدم <code style="font-family:var(--font-mono);">--days 180</code> لعينة أوسع<br>
                  • يمكنك تشغيل عدة رموز بالتتالي
                </div>
                """)

            symbol_inp.on("update:model-value", lambda e: refresh_cmd())
            days_inp.on("update:model-value",   lambda e: refresh_cmd())
            tf_sel.on("update:model-value",     lambda e: refresh_cmd())
            refresh_cmd()


# ══════════════════════════════════════
#  SETTINGS PAGE
# ══════════════════════════════════════

@ui.page("/settings")
async def page_settings() -> None:
    container = page_shell("Settings", "CONFIGURATION OVERVIEW")

    from src.core.config import settings as cfg

    def cfg_table(title: str, rows: list) -> None:
        html = (
            f"<div class='card-title'>{title}</div>"
            f"<table class='lh-table'><tbody>"
        )
        for k, v in rows:
            html += (
                f"<tr>"
                f"<td style='color:var(--text-muted);padding:10px 14px;font-size:14px;'>{k}</td>"
                f"<td class='mono' style='padding:10px 14px;font-size:14px;'>{v}</td>"
                f"</tr>"
            )
        html += "</tbody></table>"
        with ui.element("div").classes("card"):
            ui.html(html)

    pe     = cfg.section("paper_executor")
    de     = cfg.section("decision_engine")
    sc     = cfg.section("scanner")
    bt     = cfg.section("backtest")
    ui_cfg = cfg.section("ui")
    tr     = cfg.section("trigger_confirmation")
    tg_cfg = cfg.section("alerts")

    with container:
        with ui.element("div").style(
            "display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:16px;width:100%;"
        ):
            cfg_table("Environment", [
                ("Mode",            cfg.env.env),
                ("Database URL",    cfg.env.database_url),
                ("Telegram",        "✓ Configured" if cfg.env.telegram_bot_token else "✗ Not set"),
                ("Telegram Alerts", "✓ On" if tg_cfg.get("telegram_enabled") else "✗ Off"),
                ("UI Host",         ui_cfg.get("host", "0.0.0.0")),
                ("UI Port",         str(ui_cfg.get("port", 8090))),
            ])
            cfg_table("Scanner", [
                ("Scan Interval",     f"{sc.get('scan_interval_seconds', '—')}s"),
                ("Top N Monitor",     str(sc.get("top_n_to_monitor", "—"))),
                ("Min Quote Vol 24h", fmt_money_short(sc.get("min_quote_volume_24h_usd", 0))),
                ("Min Open Interest", fmt_money_short(sc.get("min_open_interest_usd", 0))),
                ("Funding Extreme",   f"{sc.get('funding_extreme_threshold', 0) * 100:.3f}%"),
                ("OI Change 4h Thr.", f"{sc.get('oi_change_4h_threshold', 0) * 100:.0f}%"),
            ])
            cfg_table("Execution Risk", [
                ("Initial Capital",        f"${pe.get('initial_capital_usd', 0):,.0f}"),
                ("Risk Per Trade",         f"{pe.get('risk_per_trade_pct', 0) * 100:.1f}%"),
                ("Max Concurrent Trades",  str(pe.get("max_concurrent_trades", "—"))),
                ("Daily Max Loss",         f"{pe.get('daily_max_loss_pct', 0) * 100:.1f}%"),
                ("Max Consecutive Losses", str(pe.get("daily_max_consecutive_losses", "—"))),
                ("Slippage Entry",         f"{pe.get('slippage_entry_pct', 0) * 100:.3f}%"),
                ("Spread",                 f"{pe.get('spread_pct', 0) * 100:.3f}%"),
            ])
            cfg_table("Decision Engine", [
                ("Min Score to Signal",    str(de.get("min_score_to_signal", "—"))),
                ("Min Score Full Size",    str(de.get("min_score_full_size", "—"))),
                ("Trend Reversal Penalty", f"{de.get('trending_market_penalty_on_reversal', 0) * 100:.0f}%"),
                ("Range Reversal Bonus",   f"{de.get('range_market_bonus_on_reversal', 0) * 100:.0f}%"),
            ])
            cfg_table("Trigger Confirmation", [
                ("Required Confirmations",  str(tr.get("required_confirmations", "—"))),
                ("Volume Spike Multiplier", f"{tr.get('volume_spike_multiplier', 0):.1f}x"),
                ("OI Reaction Threshold",   f"{tr.get('oi_reaction_threshold', 0) * 100:.2f}%"),
                ("Rejection Wick Ratio",    f"{tr.get('rejection_wick_ratio', 0) * 100:.0f}%"),
            ])
            cfg_table("Backtest", [
                ("Default Lookback Days", str(bt.get("default_lookback_days", "—"))),
                ("Warmup Candles",        str(bt.get("warmup_candles", "—"))),
                ("Fee (taker)",           f"{bt.get('fee_pct', 0) * 100:.3f}%"),
            ])

        # ══ [تعديل] زر إرسال التقرير إلى Telegram ══════════════════════
        with ui.element("div").classes("card").style("margin-top:16px;"):
            ui.html("<div class='card-title'>📤 Telegram Report</div>")
            _status_el = ui.html(
                "<div style='font-size:13px;color:var(--text-muted);margin-bottom:12px;'>"
                "إرسال تقرير PDF شامل إلى Telegram يحتوي على ملخص الأداء وآخر الصفقات.</div>"
            )

            async def _send_report_clicked():
                _status_el.set_content(
                    "<div style='font-size:13px;color:var(--info);margin-bottom:12px;'>"
                    "⏳ جاري إنشاء التقرير وإرساله…</div>"
                )
                ok = await generate_and_send_pdf_report()
                if ok:
                    _status_el.set_content(
                        "<div style='font-size:13px;color:var(--accent);margin-bottom:12px;'>"
                        "✅ تم إرسال التقرير بنجاح إلى Telegram!</div>"
                    )
                    _safe_notify("✅ Report sent to Telegram", type="positive")
                else:
                    _status_el.set_content(
                        "<div style='font-size:13px;color:#ef4444;margin-bottom:12px;'>"
                        "❌ فشل الإرسال — تحقق من إعدادات Telegram في config.</div>"
                    )
                    _safe_notify("❌ Send failed", type="negative")

            ui.button("📤 Send Report to Telegram", on_click=_send_report_clicked).style(
                "background:var(--accent);color:#000;font-weight:700;"
                "font-size:14px;border-radius:5px;padding:6px 18px;border:none;"
            )
        # ════════════════════════════════════════════════════════════════


# ══════════════════════════════════════
#  [تعديل] PDF REPORT → TELEGRAM
# ══════════════════════════════════════

async def generate_and_send_pdf_report() -> bool:
    """Build a PDF summary of current performance and send it via Telegram."""
    import io
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors as rl_colors
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm

    try:
        equity     = await executor.get_equity()
        open_count = await executor.get_open_count()
        kill       = await executor.is_kill_switch_active()
        initial    = executor.initial_capital
        eq_pct     = (equity - initial) / initial * 100 if initial else 0

        async with AsyncSessionLocal() as s:
            res    = await s.execute(
                select(Trade)
                .where(Trade.status.in_([TradeStatus.CLOSED_TP.value, TradeStatus.CLOSED_SL.value]))
                .order_by(desc(Trade.closed_at))
                .limit(20)
            )
            closed = res.scalars().all()

        wins      = [t for t in closed if (t.pnl_usd or 0) > 0]
        losses    = [t for t in closed if (t.pnl_usd or 0) <= 0]
        win_rate  = len(wins) / len(closed) * 100 if closed else 0.0
        total_pnl = sum((t.pnl_usd or 0) for t in closed)
        now_str   = datetime.now().strftime("%Y-%m-%d %H:%M")

        buf = io.BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=A4,
                                leftMargin=2*cm, rightMargin=2*cm,
                                topMargin=2*cm, bottomMargin=2*cm)
        styles  = getSampleStyleSheet()
        title_s = ParagraphStyle("title", parent=styles["Title"], fontSize=18, spaceAfter=6)
        sub_s   = ParagraphStyle("sub",   parent=styles["Normal"], fontSize=10,
                                 textColor=rl_colors.gray, spaceAfter=12)
        hdr_s   = ParagraphStyle("hdr",   parent=styles["Heading2"], fontSize=12,
                                 spaceBefore=14, spaceAfter=6)

        story = [
            Paragraph("Liquidity Hunter — Report", title_s),
            Paragraph(f"Generated: {now_str}  |  Paper Mode", sub_s),
            Spacer(1, 0.3*cm),
            Paragraph("Portfolio Summary", hdr_s),
        ]

        eq_sign  = "+" if eq_pct >= 0 else ""
        pnl_sign = "+" if total_pnl >= 0 else ""
        kpi_data = [
            ["Metric", "Value"],
            ["Equity",         f"${equity:,.0f}  ({eq_sign}{eq_pct:.2f}%)"],
            ["Realized P/L",   f"${pnl_sign}{total_pnl:,.2f}"],
            ["Win Rate",       f"{win_rate:.1f}%  ({len(wins)}W / {len(losses)}L)"],
            ["Open Positions", str(open_count)],
            ["Kill Switch",    "ARMED" if kill else "SAFE"],
        ]
        kpi_tbl = Table(kpi_data, colWidths=[6*cm, 10*cm])
        kpi_tbl.setStyle(TableStyle([
            ("BACKGROUND",     (0, 0), (-1, 0), rl_colors.HexColor("#1a1a2e")),
            ("TEXTCOLOR",      (0, 0), (-1, 0), rl_colors.white),
            ("FONTNAME",       (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE",       (0, 0), (-1, -1), 10),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1),
             [rl_colors.HexColor("#f9f9f9"), rl_colors.white]),
            ("GRID",           (0, 0), (-1, -1), 0.4, rl_colors.HexColor("#cccccc")),
            ("LEFTPADDING",    (0, 0), (-1, -1), 8),
            ("RIGHTPADDING",   (0, 0), (-1, -1), 8),
            ("TOPPADDING",     (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING",  (0, 0), (-1, -1), 5),
        ]))
        story.append(kpi_tbl)
        story.append(Spacer(1, 0.4*cm))

        if closed:
            story.append(Paragraph("Recent Closed Trades (last 20)", hdr_s))
            trade_data = [["Symbol", "Status", "P/L ", "R", "Closed", "Dir"]]
            for t in closed:
                pnl = t.pnl_usd or 0
                trade_data.append([
                    t.symbol,
                   
                    (t.status or "").replace("CLOSED_", ""),
                    f"${pnl:+,.2f}",
                    f"{(t.pnl_r or 0):+.2f}R",
                    t.closed_at.strftime("%m/%d %H:%M") if t.closed_at else "—",
                    t.direction or "—",
                ])
            t_tbl = Table(trade_data, colWidths=[3.5*cm, 2*cm, 2*cm, 2*cm, 2.5*cm, 3.8*cm])
            t_tbl.setStyle(TableStyle([
                ("BACKGROUND",     (0, 0), (-1, 0), rl_colors.HexColor("#1a1a2e")),
                ("TEXTCOLOR",      (0, 0), (-1, 0), rl_colors.white),
                ("FONTNAME",       (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE",       (0, 0), (-1, -1), 9),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1),
                 [rl_colors.HexColor("#f9f9f9"), rl_colors.white]),
                ("GRID",           (0, 0), (-1, -1), 0.3, rl_colors.HexColor("#cccccc")),
                ("LEFTPADDING",    (0, 0), (-1, -1), 6),
                ("RIGHTPADDING",   (0, 0), (-1, -1), 6),
                ("TOPPADDING",     (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING",  (0, 0), (-1, -1), 4),
            ]))
            story.append(t_tbl)

        doc.build(story)
        buf.seek(0)

        import httpx
        from src.core.config import settings as _cfg
        token   = _cfg.env.telegram_bot_token
        chat_id = _cfg.env.telegram_chat_id
        if not token or not chat_id:
            logger.warning("Telegram not configured — cannot send report")
            return False

        fname = f"Liquidity Hunter — Report_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf"
        url   = f"https://api.telegram.org/bot{token}/sendDocument"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                url,
                data={"chat_id": chat_id,
                      "caption": f"Liquidity Hunter Report\n{now_str}"},
                files={"document": (fname, buf, "application/pdf")},
            )
            resp.raise_for_status()
        logger.info(f"PDF report sent to Telegram: {fname}")
        return True

    except Exception as e:
        logger.exception(f"generate_and_send_pdf_report failed: {e}")
        return False


# ══════════════════════════════════════
#  BACKGROUND LOOP
# ══════════════════════════════════════

async def _background_loop() -> None:
    """Run a scan cycle automatically every N seconds."""
    from src.core.config import settings as _s
    global _scan_version
    interval = int(_s.section("scanner").get("scan_interval_seconds", 300))
    await asyncio.sleep(5)
    while True:
        try:
            async with _running_lock:
                _scan_state.update({"step": 1, "counts": {}})
                result = await bot.run_cycle()
                diag   = getattr(bot.scanner, "last_diagnostics", None)
                _scan_state["step"] = 5
                _scan_state["counts"] = {
                    "total":    getattr(diag, "total_symbols",    0),
                    "excluded": getattr(diag, "excluded_quality", 0),
                    "vol":      getattr(diag, "passed_volume",    0),
                    "oi":       getattr(diag, "passed_oi",        0),
                    "final":    getattr(diag, "final_shortlist",  0),
                } if diag else {}
                _scan_version += 1
        except Exception as e:
            _scan_state["step"] = 0
            logger.exception(f"Background cycle failed: {e}")
        await asyncio.sleep(interval)


@app.on_startup
async def _startup() -> None:
    await init_db()
    logger.info("Database initialised")
    asyncio.create_task(_background_loop())


# ══════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════

def main() -> None:
    import os
    from src.core.config import settings as _s
    ui_cfg = _s.section("ui")
    port   = int(os.environ.get("LH_UI_PORT", ui_cfg.get("port", 8090)))
    host   = os.environ.get("LH_UI_HOST",    ui_cfg.get("host", "0.0.0.0"))
    print(f">>> NiceGUI binding to {host}:{port}")
    ui.run(
        title="Liquidity Hunter",
        host=host,
        port=port,
        dark=True,
        reload=False,
        favicon="🎯",
        show=False,
    )


if __name__ in ("__main__", "__mp_main__"):
    main()
