"""Read-only private CI web monitor."""

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from app.auth import verify_api_key
from app.ci_database import get_monitor_snapshot

router = APIRouter(tags=["CI Monitor"])


@router.get("/ci/monitor", response_class=HTMLResponse)
async def ci_monitor_page():
    return HTMLResponse(CI_MONITOR_HTML)


@router.get("/api/v1/ci/monitor")
async def ci_monitor_snapshot(
    request: Request,
    active_limit: int = Query(default=50, ge=1, le=100),
    recent_limit: int = Query(default=20, ge=1, le=100),
):
    verify_api_key(request)
    return get_monitor_snapshot(active_limit=active_limit, recent_limit=recent_limit)


CI_MONITOR_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Private CI Monitor</title>
  <style>
    :root { color-scheme: dark; --bg:#0b1020; --panel:#121a2e; --muted:#8ea0bd; --text:#e6edf7; --line:#22304d; --ok:#2bd576; --warn:#ffd166; --bad:#ff6b6b; --info:#6aa7ff; }
    * { box-sizing: border-box; }
    body { margin:0; font:14px/1.45 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; background:var(--bg); color:var(--text); }
    header { padding:18px 22px; border-bottom:1px solid var(--line); display:flex; gap:14px; align-items:center; justify-content:space-between; flex-wrap:wrap; }
    h1 { margin:0; font-size:20px; }
    main { padding:18px 22px 40px; }
    input, button, select { background:#0d1426; color:var(--text); border:1px solid var(--line); border-radius:8px; padding:8px 10px; }
    button { cursor:pointer; }
    .toolbar { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
    .grid { display:grid; grid-template-columns: repeat(6, minmax(120px, 1fr)); gap:10px; margin-bottom:16px; }
    .card, .panel { background:var(--panel); border:1px solid var(--line); border-radius:12px; }
    .card { padding:14px; }
    .label { color:var(--muted); font-size:12px; }
    .value { font-size:26px; font-weight:700; margin-top:4px; }
    .panel { margin-top:16px; overflow:hidden; }
    .panel h2 { margin:0; padding:14px 16px; font-size:16px; border-bottom:1px solid var(--line); }
    table { width:100%; border-collapse:collapse; }
    th, td { padding:10px 12px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; }
    th { color:var(--muted); font-weight:600; white-space:nowrap; }
    tr:last-child td { border-bottom:0; }
    code { color:#a9c7ff; }
    .muted { color:var(--muted); }
    .pill { display:inline-block; padding:2px 8px; border-radius:999px; border:1px solid var(--line); color:var(--muted); }
    .passed { color:var(--ok); }
    .failed, .timed_out, .worker_lost, .internal_error { color:var(--bad); }
    .queued { color:var(--warn); }
    .running, .leased, .downloading, .preparing { color:var(--info); }
    .steps { min-width:260px; }
    .stepbar { display:flex; height:8px; border-radius:99px; overflow:hidden; background:#0d1426; border:1px solid var(--line); margin-top:6px; }
    .seg { flex:1; background:#263654; border-right:1px solid #0d1426; }
    .seg.done { background:var(--ok); }
    .seg.run { background:var(--info); }
    .seg.bad { background:var(--bad); }
    .error { color:var(--bad); white-space:pre-wrap; }
    @media (max-width: 900px) { .grid { grid-template-columns: repeat(2, 1fr); } table { font-size:12px; } th,td { padding:8px; } }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>Private CI Monitor</h1>
      <div class="muted">队列、Worker、当前步骤和耗时。每 5 秒自动刷新。</div>
    </div>
    <div class="toolbar">
      <input id="token" type="password" placeholder="Bearer API Key">
      <button id="save">保存</button>
      <button id="refresh">刷新</button>
      <span id="status" class="muted"></span>
    </div>
  </header>
  <main>
    <div id="error" class="error"></div>
    <section class="grid" id="summary"></section>
    <section class="panel">
      <h2>Worker</h2>
      <div id="workers"></div>
    </section>
    <section class="panel">
      <h2>当前队列 / 运行中</h2>
      <div id="active"></div>
    </section>
    <section class="panel">
      <h2>最近完成</h2>
      <div id="recent"></div>
    </section>
  </main>
  <script>
    const tokenEl = document.getElementById('token');
    const statusEl = document.getElementById('status');
    const errorEl = document.getElementById('error');
    tokenEl.value = localStorage.getItem('ciMonitorToken') || '';
    document.getElementById('save').onclick = () => { localStorage.setItem('ciMonitorToken', tokenEl.value); load(); };
    document.getElementById('refresh').onclick = () => load();

    function fmtSec(v) {
      if (v === null || v === undefined) return '-';
      v = Math.max(0, Math.round(v));
      const h = Math.floor(v / 3600), m = Math.floor((v % 3600) / 60), s = v % 60;
      return h ? `${h}h ${m}m ${s}s` : (m ? `${m}m ${s}s` : `${s}s`);
    }
    function esc(s) { return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
    function shortSha(s) { return s ? esc(s.slice(0, 12)) : '-'; }
    function cls(status) { return esc(status || '').replace(/[^a-zA-Z0-9_-]/g, '_'); }
    function when(s) { return s ? new Date(s).toLocaleString() : '-'; }

    function renderSummary(data) {
      const s = data.summary;
      const cards = [
        ['排队', s.queued], ['活跃', s.active], ['运行中', s.running],
        ['Worker 在线', `${s.workers_online}/${s.workers_total}`], ['终态总数', s.terminal], ['Job 总数', s.total],
      ];
      document.getElementById('summary').innerHTML = cards.map(([k,v]) => `<div class="card"><div class="label">${k}</div><div class="value">${v}</div></div>`).join('');
    }

    function renderWorkers(workers) {
      if (!workers.length) { document.getElementById('workers').innerHTML = '<div class="card muted">没有 Worker</div>'; return; }
      document.getElementById('workers').innerHTML = `<table><thead><tr><th>ID</th><th>状态</th><th>当前 Job</th><th>心跳</th><th>Profiles</th></tr></thead><tbody>` +
        workers.map(w => `<tr><td><code>${esc(w.worker_id)}</code></td><td>${w.online ? '<span class="passed">online</span>' : '<span class="failed">offline</span>'} / ${esc(w.status)}</td><td><code>${esc(w.current_job || '-')}</code></td><td>${when(w.last_heartbeat)}</td><td>${esc((w.supported_profiles || []).join(', '))}</td></tr>`).join('') +
        '</tbody></table>';
    }

    function stepBar(job) {
      if (!job.total_steps) return '<span class="muted">尚未开始步骤</span>';
      return `<div>${job.completed_steps}/${job.total_steps}${job.current_step_index ? `，第 ${job.current_step_index} 步：${esc(job.current_step)}` : ''}</div>` +
        `<div class="stepbar">${job.steps.map(st => `<div title="${esc(st.step_name)}: ${esc(st.status)}" class="seg ${st.status === 'running' ? 'run' : (['failed','timed_out','cancelled'].includes(st.status) ? 'bad' : (['passed','completed','skipped','autofixed'].includes(st.status) ? 'done' : ''))}"></div>`).join('')}</div>`;
    }

    function jobsTable(jobs, empty) {
      if (!jobs.length) return `<div class="card muted">${empty}</div>`;
      return `<table><thead><tr><th>Job</th><th>仓库 / 分支</th><th>状态</th><th>步骤</th><th>耗时</th><th>创建 / 开始</th></tr></thead><tbody>` +
        jobs.map(j => `<tr>
          <td><code>${esc(j.job_id)}</code><br><span class="muted">${shortSha(j.commit_sha)}</span></td>
          <td>${esc(j.repository)}<br><span class="muted">${esc(j.branch)} / ${esc(j.profile)}</span></td>
          <td><span class="${cls(j.status)}">${esc(j.status)}</span>${j.queue_position ? `<br><span class="pill">队列 #${j.queue_position}</span>` : ''}</td>
          <td class="steps">${stepBar(j)}${j.current_step_elapsed_seconds != null ? `<div class="muted">当前步骤 ${fmtSec(j.current_step_elapsed_seconds)}</div>` : ''}</td>
          <td>${fmtSec(j.elapsed_seconds)}</td>
          <td>${when(j.created_at)}<br><span class="muted">${when(j.started_at)}</span></td>
        </tr>`).join('') +
        '</tbody></table>';
    }

    async function load() {
      const token = tokenEl.value || localStorage.getItem('ciMonitorToken') || '';
      statusEl.textContent = '加载中...';
      errorEl.textContent = '';
      try {
        const res = await fetch('/api/v1/ci/monitor', { headers: { Authorization: `Bearer ${token}` } });
        if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
        const data = await res.json();
        renderSummary(data);
        renderWorkers(data.workers || []);
        document.getElementById('active').innerHTML = jobsTable(data.active_jobs || [], '当前没有排队或运行中的 Job');
        document.getElementById('recent').innerHTML = jobsTable(data.recent_jobs || [], '没有最近完成的 Job');
        statusEl.textContent = `已更新 ${new Date(data.generated_at).toLocaleTimeString()}`;
      } catch (e) {
        errorEl.textContent = `读取失败：${e.message}`;
        statusEl.textContent = '失败';
      }
    }
    load();
    setInterval(load, 5000);
  </script>
</body>
</html>"""
