"""Professional dark theme — Liquidity Hunter Dashboard v2.0 (Sniper Radar Style)."""

COLORS = {
    "bg":           "#0a0b0d",
    "surface":      "#13151a",
    "surface_alt":  "#181b21",
    "border":       "rgba(255,255,255,0.07)",
    "text":         "#e2e4e9",
    "text_muted":   "#6b7280",
    "text_faint":   "#374151",
    "accent":       "#00d084",
    "accent_dim":   "rgba(0,208,132,0.12)",
    "success":      "#00d084",
    "danger":       "#ff4d6d",
    "warning":      "#fbbf24",
    "info":         "#22d3ee",
    "long_bg":      "rgba(0,208,132,0.08)",
    "short_bg":     "rgba(255,77,109,0.08)",
}

FONTS = {
    "sans": "'Inter', system-ui, -apple-system, sans-serif",
    "mono": "'JetBrains Mono', 'SF Mono', Consolas, monospace",
}

GLOBAL_CSS = """
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600;700&display=swap');

:root {
  --bg:          #0a0b0d;
  --bg2:         #0f1012;
  --surface:     #13151a;
  --surface-alt: #181b21;
  --border:      rgba(255,255,255,0.07);
  --text:        #e2e4e9;
  --text-muted:  #6b7280;
  --text-faint:  #374151;
  --accent:      #00d084;
  --accent-dim:  rgba(0,208,132,0.12);
  --success:     #00d084;
  --danger:      #ff4d6d;
  --warning:     #fbbf24;
  --info:        #22d3ee;
  --long-bg:     rgba(0,208,132,0.08);
  --short-bg:    rgba(255,77,109,0.08);
  --r:           6px;
  --font-mono:   'JetBrains Mono', monospace;
  --font-body:   'Inter', sans-serif;
}

html, body, .nicegui-content {
  background: var(--bg) !important;
  color: var(--text);
  font-family: var(--font-body);
  font-size: 13px;
  -webkit-font-smoothing: antialiased;
}
.q-page { background: var(--bg) !important; }

/* ── LAYOUT ── */
.app-shell { display:flex; min-height:100vh; background:var(--bg); }

/* SIDEBAR */
.sidebar {
  width: 200px; min-width:200px;
  background: var(--bg2);
  border-right: 1px solid var(--border);
  display: flex; flex-direction:column;
  position: fixed; top:0; bottom:0; left:0; z-index:100;
  overflow-y: auto;
}
.main-content {
  margin-left: 200px;
  flex: 1;
  display: flex; flex-direction:column;
  min-height: 100vh;
}

/* LOGO */
.logo-block {
  padding: 16px 14px 12px;
  border-bottom: 1px solid var(--border);
}
.logo-title {
  font-family: var(--font-mono);
  font-size: 13px; font-weight:700;
  color: var(--accent); letter-spacing:.04em;
}
.logo-sub {
  font-size: 9px; color: var(--text-muted);
  margin-top: 3px; letter-spacing:.07em; text-transform:uppercase;
}

/* NAV */
.sidebar-section { padding: 10px 0 4px; border-bottom: 1px solid var(--border); }
.sidebar-label {
  font-size: 9px; font-weight:700; letter-spacing:.1em;
  color: var(--text-faint); padding: 0 14px 6px;
  text-transform:uppercase;
}
.nav-item {
  display:flex; align-items:center; gap:8px;
  padding: 7px 14px; cursor:pointer;
  font-size: 12px; color: var(--text-muted);
  border-left: 2px solid transparent;
  transition: all .15s; text-decoration:none;
}
.nav-item:hover { color:var(--text); background:rgba(255,255,255,.03); }
.nav-item.active { color:var(--accent); background:var(--accent-dim); border-left-color:var(--accent); }
.nav-dot { width:6px; height:6px; border-radius:50%; background:currentColor; flex-shrink:0; }

/* TOPBAR */
.topbar {
  background: var(--bg2);
  border-bottom: 1px solid var(--border);
  padding: 10px 20px;
  display: flex; align-items:center; justify-content:space-between;
  flex-shrink:0; gap:12px;
  position: sticky; top:0; z-index:90;
}
.topbar-title { font-family:var(--font-mono); font-size:15px; font-weight:700; color:var(--accent); letter-spacing:.04em; }
.topbar-sub { font-size:10px; color:var(--text-muted); letter-spacing:.06em; text-transform:uppercase; margin-top:1px; }
.status-pill {
  display:flex; align-items:center; gap:5px;
  padding: 4px 10px;
  background: var(--accent-dim); border:1px solid rgba(0,208,132,.2);
  border-radius: 20px; font-size:11px; font-weight:600; color:var(--accent);
  font-family: var(--font-mono);
}
.status-dot {
  width:6px; height:6px; border-radius:50%; background:var(--accent);
  animation: livepulse 1.5s infinite;
}
@keyframes livepulse { 0%,100%{opacity:1;transform:scale(1);} 50%{opacity:.4;transform:scale(.7);} }
.uptime { font-family:var(--font-mono); font-size:11px; color:var(--text-muted); }

/* PAGE CONTAINER */
.page-body { flex:1; overflow-y:auto; padding:14px 18px; display:flex; flex-direction:column; gap:10px; }

/* CARDS */
.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--r);
  padding: 12px 14px;
}
.card-title {
  font-size: 10px; font-weight:700; letter-spacing:.07em;
  color: var(--text-muted); text-transform:uppercase;
  margin-bottom: 10px;
  display:flex; align-items:center; justify-content:space-between;
}

/* KPI ROW */
.kpi-row { display:grid; grid-template-columns:repeat(5,1fr); gap:10px; }
.kpi { background:var(--surface); border:1px solid var(--border); border-radius:var(--r); padding:12px 14px; }
.kpi-label { font-size:9px; font-weight:700; letter-spacing:.08em; color:var(--text-muted); text-transform:uppercase; margin-bottom:4px; }
.kpi-val { font-family:var(--font-mono); font-size:22px; font-weight:700; line-height:1; }
.kpi-val.green  { color:var(--accent); }
.kpi-val.red    { color:var(--danger); }
.kpi-val.cyan   { color:var(--info); }
.kpi-val.yellow { color:var(--warning); }
.kpi-change { font-family:var(--font-mono); font-size:10px; margin-top:3px; }
.kpi-change.up   { color:var(--accent); }
.kpi-change.down { color:var(--danger); }

/* TABLE */
.table-wrap { overflow-x:auto; }
.lh-table { width:100%; border-collapse:collapse; }
.lh-table thead tr { border-bottom:1px solid var(--border); }
.lh-table th {
  font-size:9px; font-weight:700; letter-spacing:.08em;
  color:var(--text-faint); text-align:left; padding:6px 8px;
  text-transform:uppercase; white-space:nowrap;
  background:var(--surface); position:sticky; top:0;
}
.lh-table td {
  padding:7px 8px; font-family:var(--font-mono); font-size:11px;
  text-align:left; border-bottom:1px solid rgba(255,255,255,.03);
  white-space:nowrap;
}
.lh-table tr:hover td { background:rgba(255,255,255,.02); }
.sym-cell { font-weight:600; color:var(--text); min-width:90px; }
.col-dir { min-width:90px; }
.col-pnl { min-width:90px; }
.col-r   { min-width:60px; }
.mono { font-family:var(--font-mono) !important; }
.text-success { color:var(--accent) !important; }
.text-danger  { color:var(--danger) !important; }
.text-muted   { color:var(--text-muted); }
.text-faint   { color:var(--text-faint); }
.tabular-nums { font-variant-numeric:tabular-nums; }

/* PILLS */
.pill {
  display:inline-flex; align-items:center;
  padding:2px 8px; border-radius:20px;
  font-size:10px; font-weight:700; letter-spacing:.04em;
  font-family:var(--font-mono);
}
.pill-long  { background:var(--long-bg); color:var(--accent); }
.pill-short { background:var(--short-bg); color:var(--danger); }
.pill-muted { background:rgba(255,255,255,.06); color:var(--text-muted); }
.pill-warn  { background:rgba(251,191,36,.12); color:var(--warning); }

/* SCORE BAR */
.score-bar { height:4px; border-radius:2px; background:var(--text-faint); margin-top:4px; overflow:hidden; }
.score-bar-fill { height:100%; border-radius:2px; background:var(--accent); transition:width .4s; }

/* DIVIDER */
.divider { border:none; border-top:1px solid var(--border); margin:10px 0; }

/* SECTION HEADER */
.section-header {
  font-size:11px; font-weight:700; color:var(--text);
  letter-spacing:-.01em; margin-bottom:10px;
  display:flex; align-items:center; gap:6px;
}

/* TABS */
.tab-row { display:flex; gap:0; border-bottom:1px solid var(--border); margin-bottom:10px; }
.tab-item {
  padding:6px 14px; font-size:11px; font-weight:600;
  color:var(--text-muted); cursor:pointer;
  border-bottom:2px solid transparent; transition:.15s;
}
.tab-item.active { color:var(--accent); border-bottom-color:var(--accent); }
.tab-badge {
  display:inline-block; border-radius:10px;
  padding:1px 6px; font-size:9px; margin-right:3px;
  background:var(--accent-dim); color:var(--accent);
}

/* HEALTH ROW */
.health-row {
  display:flex; align-items:center; justify-content:space-between;
  padding:5px 0; border-bottom:1px solid rgba(255,255,255,.04);
  font-size:11px;
}
.health-row:last-child { border:none; }
.health-ok   { color:var(--accent); font-weight:700; font-family:var(--font-mono); }
.health-warn { color:var(--warning); font-weight:700; font-family:var(--font-mono); }

/* CTRL */
.ctrl-row { padding:6px 14px; display:flex; align-items:center; justify-content:space-between; }
.ctrl-label { font-size:11px; color:var(--text-muted); }
.ctrl-num { font-family:var(--font-mono); font-size:11px; color:var(--info); min-width:28px; text-align:right; }

/* SCROLLBAR */
::-webkit-scrollbar { width:4px; height:4px; }
::-webkit-scrollbar-track { background:var(--bg); }
::-webkit-scrollbar-thumb { background:#374151; border-radius:2px; }
"""
