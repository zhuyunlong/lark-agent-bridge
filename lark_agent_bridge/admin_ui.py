"""Single-page admin console for the local report server."""

from __future__ import annotations


def render_admin_page() -> str:
    return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Lark Agent Bridge 后台</title>
  <style>
    :root {
      --bg: #f5f7fb;
      --panel: #ffffff;
      --panel-soft: #f8fafc;
      --text: #172033;
      --muted: #667085;
      --line: #dbe3ef;
      --blue: #2563eb;
      --green: #16a34a;
      --amber: #d97706;
      --red: #dc2626;
      --violet: #6d28d9;
      --cyan: #0891b2;
      --shadow: 0 8px 26px rgba(15, 23, 42, 0.08);
    }
    * { box-sizing: border-box; }
    body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--bg); color: var(--text); }
    header { height: 60px; padding: 0 20px; background: #111827; color: #fff; display: flex; align-items: center; justify-content: space-between; gap: 14px; }
    h1 { font-size: 18px; margin: 0; letter-spacing: 0; }
    h2 { font-size: 17px; margin: 0 0 12px; }
    h3 { font-size: 14px; margin: 18px 0 10px; }
    button, input, textarea, select { font: inherit; }
    button { border: 1px solid var(--line); border-radius: 6px; padding: 7px 10px; background: #fff; color: var(--text); cursor: pointer; }
    button:hover { border-color: #94a3b8; }
    button.primary { background: var(--blue); border-color: var(--blue); color: #fff; }
    button.danger { background: #fff; border-color: #fecaca; color: var(--red); }
    button.ghost { background: transparent; color: #dbeafe; border-color: #334155; }
    input, textarea, select { width: 100%; border: 1px solid var(--line); border-radius: 6px; padding: 8px 10px; color: var(--text); background: #fff; }
    textarea { min-height: 340px; resize: vertical; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; line-height: 1.55; }
    a { color: var(--blue); text-decoration: none; font-weight: 600; }
    a:hover { text-decoration: underline; }
    .toolbar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
    .tabs { display: flex; gap: 8px; padding: 14px 20px 0; }
    .tab { border: 0; background: transparent; padding: 9px 12px; color: var(--muted); }
    .tab.active { color: var(--blue); border-bottom: 2px solid var(--blue); border-radius: 0; font-weight: 700; }
    main { padding: 14px 20px 24px; }
    .view { display: none; }
    .view.active { display: block; }
    .grid { display: grid; gap: 14px; }
    .metrics { grid-template-columns: repeat(6, minmax(130px, 1fr)); margin-bottom: 14px; }
    .metric, .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; box-shadow: var(--shadow); }
    .metric { padding: 12px; min-height: 76px; }
    .metric .label { color: var(--muted); font-size: 12px; margin-bottom: 8px; }
    .metric .value { font-size: 24px; font-weight: 800; }
    .split { grid-template-columns: minmax(320px, 430px) minmax(0, 1fr); align-items: start; }
    .panel { overflow: hidden; }
    .panel-head { padding: 13px 14px; border-bottom: 1px solid var(--line); display: flex; align-items: center; justify-content: space-between; gap: 12px; }
    .panel-body { padding: 14px; }
    .scroll { max-height: calc(100vh - 218px); overflow: auto; }
    .item { display: block; width: 100%; padding: 12px 14px; border: 0; border-bottom: 1px solid var(--line); border-radius: 0; background: #fff; text-align: left; }
    .item:hover, .item.active { background: #eff6ff; }
    .item.failed { border-left: 4px solid var(--red); }
    .item.running { border-left: 4px solid var(--blue); }
    .item.pending { border-left: 4px solid var(--amber); }
    .item.succeeded { border-left: 4px solid var(--green); }
    .row { display: flex; justify-content: space-between; gap: 12px; align-items: center; }
    .title { font-weight: 750; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .meta, .muted { color: var(--muted); font-size: 12px; }
    .badge { display: inline-flex; align-items: center; border-radius: 999px; padding: 2px 8px; font-size: 12px; font-weight: 700; background: #e2e8f0; color: #334155; white-space: nowrap; }
    .badge.succeeded { background: #dcfce7; color: #166534; }
    .badge.failed { background: #fee2e2; color: #991b1b; }
    .badge.running { background: #dbeafe; color: #1d4ed8; }
    .badge.pending { background: #fef3c7; color: #92400e; }
    .badge.skipped { background: #f1f5f9; color: #475569; }
    .badge.primary { background: #dbeafe; color: #1d4ed8; }
    .badge.auxiliary { background: #ede9fe; color: #5b21b6; }
    .badge.custom { background: #cffafe; color: #0e7490; }
    .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 10px; margin-bottom: 14px; }
    .card { border: 1px solid var(--line); border-radius: 8px; padding: 10px; background: var(--panel-soft); min-height: 68px; }
    .card .name { color: var(--muted); font-size: 12px; margin-bottom: 5px; }
    .card .body { font-weight: 700; word-break: break-word; }
    .progress-bar { height: 8px; background: #e5e7eb; border-radius: 999px; overflow: hidden; margin: 10px 0 2px; }
    .progress-bar span { display: block; height: 100%; width: 0; background: var(--blue); transition: width .25s ease; }
    .timeline { border-left: 2px solid #dbeafe; margin-left: 9px; padding-left: 16px; }
    .step { position: relative; padding: 0 0 14px; }
    .step::before { content: ""; position: absolute; left: -22px; top: 4px; width: 10px; height: 10px; border-radius: 999px; background: var(--blue); }
    .step.failed::before { background: var(--red); }
    .step.succeeded::before { background: var(--green); }
    pre { white-space: pre-wrap; word-break: break-word; background: #111827; color: #e5e7eb; border-radius: 8px; padding: 12px; overflow: auto; font-size: 12px; line-height: 1.5; }
    table { width: 100%; border-collapse: collapse; }
    th, td { border-bottom: 1px solid var(--line); padding: 10px; text-align: left; vertical-align: top; font-size: 13px; }
    th { color: var(--muted); background: #f8fafc; font-size: 12px; }
    .case-actions, .skill-actions { display: flex; gap: 8px; flex-wrap: wrap; }
    .filters { display: grid; grid-template-columns: minmax(180px, 1fr) 150px 150px auto; gap: 8px; align-items: center; }
    .skill-layout { grid-template-columns: 330px minmax(0, 1fr); align-items: start; }
    .form-grid { display: grid; grid-template-columns: 170px 220px minmax(220px, 1fr) auto; gap: 8px; align-items: end; }
    .checks { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 8px; }
    .check { border: 1px solid var(--line); border-radius: 8px; padding: 9px; background: var(--panel-soft); }
    .check.ok { border-color: #bbf7d0; }
    .check.bad { border-color: #fecaca; }
    .empty { padding: 18px; color: var(--muted); }
    @media (max-width: 1040px) { .metrics { grid-template-columns: repeat(3, 1fr); } .split, .skill-layout, .filters, .form-grid { grid-template-columns: 1fr; } .scroll { max-height: none; } }
    @media (max-width: 640px) { header { height: auto; padding: 14px; align-items: flex-start; flex-direction: column; } main, .tabs { padding-left: 12px; padding-right: 12px; } .metrics { grid-template-columns: repeat(2, 1fr); } }
  </style>
</head>
<body>
  <header>
    <h1>Lark Agent Bridge 会话控制台 / 后台管理</h1>
    <div class="toolbar">
      <span id="clock" class="muted"></span>
      <button id="refresh" class="ghost">刷新</button>
    </div>
  </header>
  <nav class="tabs">
    <button class="tab active" data-view="sessions-view">多会话进度</button>
    <button class="tab" data-view="cases-view">历史案件</button>
    <button class="tab" data-view="skills-view">Skill 管理</button>
    <button class="tab" data-view="daemon-view">运行状态</button>
  </nav>
  <main>
    <section class="grid metrics">
      <div class="metric"><div class="label">运行中</div><div id="metric-running" class="value">0</div></div>
      <div class="metric"><div class="label">待确认</div><div id="metric-pending" class="value">0</div></div>
      <div class="metric"><div class="label">失败</div><div id="metric-failed" class="value">0</div></div>
      <div class="metric"><div class="label">今日会话</div><div id="metric-sessions" class="value">0</div></div>
      <div class="metric"><div class="label">历史 Bug</div><div id="metric-cases" class="value">0</div></div>
      <div class="metric"><div class="label">Skill</div><div id="metric-skills" class="value">0</div></div>
    </section>

    <section id="sessions-view" class="view active">
      <div class="grid split">
        <section class="panel">
          <div class="panel-head"><h2>会话列表</h2><span id="session-count" class="muted"></span></div>
          <div class="scroll" id="sessions"></div>
        </section>
        <section class="panel">
          <div class="panel-head"><h2>会话详情</h2><span id="selected-status"></span></div>
          <div class="panel-body scroll" id="detail"><p class="muted">选择左侧会话查看后台 agent 过程。</p></div>
        </section>
      </div>
    </section>

    <section id="cases-view" class="view">
      <section class="panel">
        <div class="panel-head"><h2>历史案件</h2><span class="muted">默认每个 Bug 只展示最后一次报告</span></div>
        <div class="panel-body">
          <div class="filters">
            <input id="case-keyword" placeholder="搜索结论、请求、标签" />
            <select id="case-mode"><option value="">全部类型</option><option value="bug_analysis">Bug分析</option><option value="signal_lifecycle">信号链路</option><option value="direct_analysis">日志分析</option><option value="perception_summary">感知总结</option></select>
            <select id="case-scope"><option value="latest">每个 Bug 最后报告</option><option value="all">全部记录</option></select>
            <button id="case-search" class="primary">查询</button>
          </div>
        </div>
        <div class="scroll"><table><thead><tr><th>Bug / 请求</th><th>结论</th><th>类型</th><th>报告</th><th>确认</th></tr></thead><tbody id="cases"></tbody></table></div>
      </section>
    </section>

    <section id="skills-view" class="view">
      <div class="grid skill-layout">
        <section class="panel">
          <div class="panel-head"><h2>Skill 列表</h2><span id="skill-count" class="muted"></span></div>
          <div class="scroll" id="skills"></div>
        </section>
        <section class="panel">
          <div class="panel-head"><h2>编辑与调试</h2><span id="skill-status" class="muted"></span></div>
          <div class="panel-body">
            <div class="form-grid">
              <input id="new-skill-name" placeholder="new-skill-name" />
              <input id="new-skill-label" placeholder="显示名称" />
              <input id="new-skill-desc" placeholder="description" />
              <button id="create-skill" class="primary">新增</button>
            </div>
            <h3 id="editor-title">未选择 Skill</h3>
            <textarea id="skill-editor" spellcheck="false" placeholder="选择 skill 后编辑 SKILL.md"></textarea>
            <div class="skill-actions">
              <button id="save-skill" class="primary">保存</button>
              <button id="debug-skill">调试</button>
              <button id="delete-skill" class="danger">删除</button>
            </div>
            <h3>调试输入</h3>
            <input id="debug-sample" placeholder="粘贴一段用户描述，检查这个 skill 是否容易被命中" />
            <h3>调试结果</h3>
            <div id="debug-result" class="checks"></div>
          </div>
        </section>
      </div>
    </section>

    <section id="daemon-view" class="view">
      <section class="panel">
        <div class="panel-head"><h2>Listener 与健康状态</h2><span id="daemon-stage"></span></div>
        <div class="panel-body">
          <div class="cards" id="daemon-cards"></div>
          <h3>原始状态</h3>
          <pre id="daemon-json">正在读取...</pre>
        </div>
      </section>
    </section>
  </main>
  <script>
    let selectedSession = "";
    let selectedSkill = "";
    let sessionsData = [];
    let casesData = [];
    let skillsData = [];
    let daemonData = {};

    const $ = (id) => document.getElementById(id);
    const text = (value) => value === undefined || value === null || value === "" ? "-" : String(value);

    function el(tag, props, ...children) {
      const node = document.createElement(tag);
      props = props || {};
      for (const [key, value] of Object.entries(props)) {
        if (key === "class") node.className = value;
        else if (key === "text") node.textContent = value;
        else if (key === "dataset") Object.assign(node.dataset, value);
        else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
        else if (value !== undefined && value !== null) node.setAttribute(key, value);
      }
      for (const child of children) {
        if (child === undefined || child === null) continue;
        node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
      }
      return node;
    }

    function badge(status, label) {
      return el("span", { class: "badge " + (status || ""), text: label || status || "unknown" });
    }

    function card(name, value) {
      return el("div", { class: "card" }, el("div", { class: "name", text: name }), el("div", { class: "body", text: text(value) }));
    }

    function statusProgress(session) {
      if (session.status === "succeeded") return 100;
      if (session.status === "failed" || session.status === "skipped") return 100;
      const count = Number(session.progress_count || (session.progress || []).length || 0);
      return Math.max(10, Math.min(92, 12 + count * 12));
    }

    function lastStage(session) {
      const progress = session.progress || [];
      if (progress.length) return progress[progress.length - 1].stage || session.status || "running";
      return session.status || "unknown";
    }

    function renderMetrics() {
      const running = sessionsData.filter(x => x.status === "running").length;
      const pending = sessionsData.filter(x => x.status === "pending").length;
      const failed = sessionsData.filter(x => x.status === "failed").length;
      $("metric-running").textContent = running;
      $("metric-pending").textContent = pending;
      $("metric-failed").textContent = failed;
      $("metric-sessions").textContent = sessionsData.length;
      $("metric-cases").textContent = casesData.length;
      $("metric-skills").textContent = skillsData.length;
      $("clock").textContent = new Date().toLocaleTimeString();
    }

    function renderSessions() {
      const root = $("sessions");
      root.textContent = "";
      $("session-count").textContent = sessionsData.length + " 条";
      if (!sessionsData.length) {
        root.appendChild(el("div", { class: "empty", text: "暂无会话。" }));
        return;
      }
      for (const item of sessionsData) {
        const button = el("button", { class: "item " + (item.status || "") + (item.session_id === selectedSession ? " active" : ""), dataset: { sessionId: item.session_id || "" }, onclick: () => loadDetail(item.session_id) });
        button.appendChild(el("div", { class: "row" }, el("div", { class: "title", text: item.content || item.message || item.session_id }), badge(item.status)));
        button.appendChild(el("div", { class: "meta", text: [item.mode || "unknown", lastStage(item), item.updated_at || item.started_at || ""].join(" · ") }));
        const bar = el("div", { class: "progress-bar" }, el("span"));
        bar.firstChild.style.width = statusProgress(item) + "%";
        button.appendChild(bar);
        root.appendChild(button);
      }
    }

    function renderDetail(session) {
      selectedSession = session.session_id || "";
      $("selected-status").replaceChildren(badge(session.status));
      const root = $("detail");
      root.textContent = "";
      const cards = el("div", { class: "cards" },
        card("session", session.session_id),
        card("event", session.event_id),
        card("chat", session.chat_id),
        card("job", session.job_id),
        card("耗时", session.duration_seconds ? session.duration_seconds + "s" : "-"),
        card("更新", session.updated_at)
      );
      root.appendChild(cards);
      const actions = el("div", { class: "case-actions" });
      if (session.report_url) actions.appendChild(el("a", { href: session.report_url, target: "_blank", rel: "noreferrer", text: "打开报告" }));
      if (session.job_dir) actions.appendChild(el("span", { class: "muted", text: "job_dir: " + session.job_dir }));
      root.appendChild(actions);
      root.appendChild(el("h3", { text: "用户请求" }));
      root.appendChild(el("pre", { text: text(session.content) }));
      root.appendChild(el("h3", { text: "回复结果" }));
      root.appendChild(el("pre", { text: text(session.message) }));
      root.appendChild(el("h3", { text: "后台过程" }));
      const timeline = el("div", { class: "timeline" });
      for (const step of session.progress || []) {
        const cls = /fail|error|失败/.test(step.stage || step.message || "") ? "step failed" : /done|完成|succeed/.test(step.stage || step.message || "") ? "step succeeded" : "step";
        const row = el("div", { class: "row" }, el("strong", { text: step.stage || "progress" }), el("span", { class: "meta", text: step.timestamp || "" }));
        const node = el("div", { class: cls }, row, el("div", { text: step.message || "" }));
        if (step.details && Object.keys(step.details).length) node.appendChild(el("pre", { text: JSON.stringify(step.details, null, 2) }));
        timeline.appendChild(node);
      }
      if (!(session.progress || []).length) timeline.appendChild(el("p", { class: "muted", text: "暂无进度事件。" }));
      root.appendChild(timeline);
      renderSessions();
    }

    function renderDaemon() {
      $("daemon-stage").replaceChildren(badge(daemonData.stage || "unknown"));
      const cards = $("daemon-cards");
      cards.textContent = "";
      cards.appendChild(card("event_key", daemonData.event_key));
      cards.appendChild(card("pid", daemonData.process_id));
      cards.appendChild(card("ready", daemonData.ready));
      cards.appendChild(card("restarts", daemonData.restart_count));
      cards.appendChild(card("updated", daemonData.updated_at));
      $("daemon-json").textContent = JSON.stringify(daemonData, null, 2);
    }

    function renderCases() {
      const body = $("cases");
      body.textContent = "";
      if (!casesData.length) {
        body.appendChild(el("tr", {}, el("td", { colspan: "5", class: "empty", text: "暂无历史案件。" })));
        return;
      }
      for (const item of casesData) {
        const title = item.bug_url || item.request_text || item.case_id;
        const reportCell = el("td");
        if (item.report_url) reportCell.appendChild(el("a", { href: item.report_url, target: "_blank", rel: "noreferrer", text: "打开最后报告" }));
        else reportCell.textContent = "-";
        const confirm = el("button", { text: item.human_confirmed ? "取消确认" : "人工确认", onclick: () => confirmCase(item.case_id, !item.human_confirmed) });
        body.appendChild(el("tr", {},
          el("td", {}, el("div", { class: "title", text: title }), el("div", { class: "meta", text: item.updated_at || item.created_at || "" })),
          el("td", { text: clip(item.conclusion, 180) }),
          el("td", {}, badge("", item.problem_type || item.analysis_mode || "未分类")),
          reportCell,
          el("td", {}, confirm)
        ));
      }
    }

    function renderSkills() {
      const root = $("skills");
      root.textContent = "";
      $("skill-count").textContent = skillsData.length + " 个";
      if (!skillsData.length) {
        root.appendChild(el("div", { class: "empty", text: "暂无 skill。" }));
        return;
      }
      for (const item of skillsData) {
        const button = el("button", { class: "item" + (item.name === selectedSkill ? " active" : ""), onclick: () => loadSkill(item.name) });
        button.appendChild(el("div", { class: "row" }, el("div", { class: "title", text: item.label || item.name }), badge(item.role, item.role)));
        button.appendChild(el("div", { class: "meta", text: [item.name, item.kind || "custom", item.status].join(" · ") }));
        root.appendChild(button);
      }
    }

    function renderSkillEditor(skill) {
      selectedSkill = skill.name || "";
      $("editor-title").textContent = (skill.label || skill.name) + " / " + skill.name;
      $("skill-status").textContent = [skill.role, skill.kind || "custom", skill.status].join(" · ");
      $("skill-editor").value = skill.content || "";
      $("debug-result").textContent = "";
      renderSkills();
    }

    function renderDebug(result) {
      const root = $("debug-result");
      root.textContent = "";
      root.appendChild(el("div", { class: "check ok" }, el("strong", { text: "总结" }), el("div", { text: result.summary || "-" })));
      for (const check of result.checks || []) {
        root.appendChild(el("div", { class: "check " + (check.ok ? "ok" : "bad") }, el("strong", { text: check.label || check.key }), el("div", { class: "meta", text: check.message || (check.ok ? "通过" : "未通过") })));
      }
      if (result.sample && result.sample.provided) {
        root.appendChild(el("div", { class: "check" }, el("strong", { text: "示例命中" }), el("div", { text: "score=" + result.sample.score + " / terms=" + (result.sample.matched_terms || []).join(", ") })));
      }
    }

    function clip(value, limit) {
      const raw = text(value);
      return raw.length > limit ? raw.slice(0, limit - 1) + "…" : raw;
    }

    async function loadAll() {
      await Promise.all([loadSessions(), loadCases(), loadSkills(), loadDaemon()]);
      renderMetrics();
    }

    async function loadSessions() {
      const response = await fetch("/api/sessions", { cache: "no-store" });
      const data = await response.json();
      sessionsData = data.sessions || [];
      renderSessions();
      if (!selectedSession && sessionsData.length) await loadDetail(sessionsData[0].session_id);
    }

    async function loadDetail(id) {
      if (!id) return;
      selectedSession = id;
      const response = await fetch("/api/sessions/" + encodeURIComponent(id), { cache: "no-store" });
      if (!response.ok) {
        $("detail").textContent = "会话不存在或已过期。";
        return;
      }
      const data = await response.json();
      renderDetail(data.session || {});
    }

    async function loadCases() {
      const params = new URLSearchParams();
      const keyword = $("case-keyword").value.trim();
      const mode = $("case-mode").value;
      const scope = $("case-scope").value;
      if (keyword) params.set("keyword", keyword);
      if (mode) params.set("analysis_mode", mode);
      if (scope === "all") params.set("all", "1");
      const response = await fetch("/api/cases?" + params.toString(), { cache: "no-store" });
      const data = await response.json();
      casesData = data.cases || [];
      renderCases();
    }

    async function confirmCase(caseId, confirmed) {
      await fetch("/api/cases/" + encodeURIComponent(caseId) + "/confirm", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ confirmed })
      });
      await loadCases();
      renderMetrics();
    }

    async function loadSkills() {
      const response = await fetch("/api/skills", { cache: "no-store" });
      const data = await response.json();
      skillsData = data.skills || [];
      renderSkills();
    }

    async function loadSkill(name) {
      selectedSkill = name;
      const response = await fetch("/api/skills/" + encodeURIComponent(name), { cache: "no-store" });
      const data = await response.json();
      if (!response.ok) {
        $("skill-status").textContent = data.error || "读取失败";
        return;
      }
      renderSkillEditor(data.skill || {});
    }

    async function createSkill() {
      const name = $("new-skill-name").value.trim();
      if (!name) return;
      const response = await fetch("/api/skills", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, label: $("new-skill-label").value, description: $("new-skill-desc").value })
      });
      const data = await response.json();
      if (!response.ok) {
        $("skill-status").textContent = data.error || "新增失败";
        return;
      }
      $("new-skill-name").value = "";
      $("new-skill-label").value = "";
      $("new-skill-desc").value = "";
      await loadSkills();
      renderMetrics();
      renderSkillEditor(data.skill);
    }

    async function saveSkill() {
      if (!selectedSkill) return;
      const response = await fetch("/api/skills/" + encodeURIComponent(selectedSkill), {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: $("skill-editor").value })
      });
      const data = await response.json();
      $("skill-status").textContent = response.ok ? "已保存" : (data.error || "保存失败");
      if (response.ok) {
        await loadSkills();
        renderSkillEditor(data.skill);
      }
    }

    async function deleteSkill() {
      if (!selectedSkill) return;
      if (!window.confirm("确认删除 " + selectedSkill + "？")) return;
      const response = await fetch("/api/skills/" + encodeURIComponent(selectedSkill), { method: "DELETE" });
      const data = await response.json();
      $("skill-status").textContent = response.ok ? "已删除 " + selectedSkill : (data.error || "删除失败");
      if (response.ok) {
        selectedSkill = "";
        $("skill-editor").value = "";
        $("editor-title").textContent = "未选择 Skill";
        await loadSkills();
        renderMetrics();
      }
    }

    async function debugSkill() {
      if (!selectedSkill) return;
      const response = await fetch("/api/skills/" + encodeURIComponent(selectedSkill) + "/debug", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sample_text: $("debug-sample").value })
      });
      const data = await response.json();
      if (!response.ok) {
        $("debug-result").textContent = data.error || "调试失败";
        return;
      }
      renderDebug(data);
    }

    async function loadDaemon() {
      const response = await fetch("/api/daemon", { cache: "no-store" });
      const data = await response.json();
      daemonData = data.daemon || {};
      renderDaemon();
    }

    for (const tab of document.querySelectorAll(".tab")) {
      tab.addEventListener("click", () => {
        for (const item of document.querySelectorAll(".tab")) item.classList.remove("active");
        for (const view of document.querySelectorAll(".view")) view.classList.remove("active");
        tab.classList.add("active");
        $(tab.dataset.view).classList.add("active");
      });
    }
    $("refresh").onclick = loadAll;
    $("case-search").onclick = async () => { await loadCases(); renderMetrics(); };
    $("create-skill").onclick = createSkill;
    $("save-skill").onclick = saveSkill;
    $("delete-skill").onclick = deleteSkill;
    $("debug-skill").onclick = debugSkill;

    loadAll();
    setInterval(async () => {
      await loadSessions();
      await loadDaemon();
      renderMetrics();
    }, 3000);
  </script>
</body>
</html>
"""
