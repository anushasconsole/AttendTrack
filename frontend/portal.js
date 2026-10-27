(() => {
     const apiBase = (window.ATTENDTRACK_API || window.location.origin).replace(/\/$/, '');
  const portalRole = (window.ATTENDTRACK_PORTAL_ROLE || 'manager').toLowerCase();
  const OT_CAP = 105;
  const WORK_START = 8 * 60 + 30;
  const WORK_END = 17 * 60;

  const roleConfig = {
    manager: {
      label: 'Manager',
      title: 'Manager Dashboard',
      subtitle: 'Site Manager Portal · AttendTrack',
      badge: 'Manager',
      pageTitle: 'Site Overview',
      loginRole: 'manager',
      summaryEndpoint: (user) => `/api/manager/stats/${encodeURIComponent(user.employee_id)}`,
      pendingEndpoint: (user) => `/api/manager/pending/${encodeURIComponent(user.employee_id)}`,
      allEndpoint: (user, filters = {}) => {
        const params = new URLSearchParams();
        if (filters.date) params.set('date', filters.date);
        const query = params.toString();
        return `/api/manager/all/${encodeURIComponent(user.employee_id)}${query ? `?${query}` : ''}`;
      },
      verifyField: 'manager_id',
      verifyEndpoint: '/api/manager/verify',
      tabs: [
        ['overview', 'Overview'],
        ['pending', 'Pending approval'],
        ['live', 'Live today'],
        ['overtime', 'Live overtime'],
        ['predict', 'Predictions'],
        ['behaviour', 'Behaviour intelligence'],
        ['history', 'History'],
      ],
    },
    hr: {
      label: 'HR',
      title: 'HR Dashboard',
      subtitle: 'Human Resources Portal · AttendTrack',
      badge: 'HR',
      pageTitle: 'HR Command Centre',
      loginRole: 'hr',
      summaryEndpoint: () => '/api/hr/summary',
      pendingEndpoint: (user) => `/api/hr/pending/${encodeURIComponent(user.employee_id)}`,
      allEndpoint: (user, filters = {}) => {
        const params = new URLSearchParams();
        if (filters.date) params.set('date', filters.date);
        if (filters.employee_id) params.set('employee_id', filters.employee_id);
        const query = params.toString();
        return `/api/hr/all/${encodeURIComponent(user.employee_id)}${query ? `?${query}` : ''}`;
      },
      verifyField: 'hr_id',
      verifyEndpoint: '/api/hr/verify',
      tabs: [
        ['overview', 'Overview'],
        ['pending', 'Pending HR approval'],
        ['live', 'Live today'],
        ['all', 'All records'],
        ['employees', 'Employees'],
        ['overtime', 'Live overtime'],
        ['predict', 'Predictions'],
        ['behaviour', 'Behaviour intelligence'],
        ['schedreports', 'Scheduled reports'],
      ],
    },
  };

  const cfg = roleConfig[portalRole] || roleConfig.manager;

  const state = {
    currentUser: null,
    accessToken: null,    // JWT stored in memory
    currentTab: 'overview',
    currentSummary: null,
    employees: [],
    siteList: [],
    allRecords: [],
    pendingRecords: [],
    refreshTimer: null,
    aiBusy: false,
  };

  const qs = (sel) => document.querySelector(sel);
  const qsa = (sel) => Array.from(document.querySelectorAll(sel));
  const el = (id) => document.getElementById(id);

  function escapeHtml(value) {
    return String(value ?? '')
      .replaceAll('&', '&amp;')
      .replaceAll('<', '&lt;')
      .replaceAll('>', '&gt;')
      .replaceAll('"', '&quot;')
      .replaceAll("'", '&#39;');
  }

  function monthMeta() {
    const now = new Date();
    const start = new Date(now.getFullYear(), now.getMonth(), 1);
    const next = new Date(now.getFullYear(), now.getMonth() + 1, 1);
    const daysElapsed = Math.max(1, Math.floor((now - start) / 86400000) + 1);
    const daysInMonth = Math.round((next - start) / 86400000);
    return { daysElapsed, daysInMonth, daysRemaining: Math.max(0, daysInMonth - daysElapsed) };
  }

  function toMinutes(timeLike) {
    if (!timeLike) return null;
    const value = String(timeLike);
    const match = value.match(/(\d{2}):(\d{2})/);
    if (!match) return null;
    return Number(match[1]) * 60 + Number(match[2]);
  }

  function timeOnly(value) {
    if (!value) return '–';
    const str = String(value);
    return str.length >= 16 ? str.slice(11, 16) : str;
  }

  function sameMonth(dateValue) {
    if (!dateValue) return false;
    const d = new Date(dateValue);
    const now = new Date();
    return d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth();
  }

  function sameDay(dateValue) {
    if (!dateValue) return false;
    const d = new Date(dateValue);
    const now = new Date();
    return d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth() && d.getDate() === now.getDate();
  }

  function currentHM() {
    return new Date().toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
  }

  function authHeaders(extra) {
    const h = { 'Content-Type': 'application/json', ...extra };
    if (state.accessToken) h['Authorization'] = `Bearer ${state.accessToken}`;
    return h;
  }

  async function apiJson(path, options = {}) {
    // Always inject auth header
    const opts = {
      ...options,
      headers: authHeaders(options.headers || {}),
    };
    const response = await fetch(`${apiBase}${path}`, opts);
    if (response.status === 401) {
      // Token expired or revoked — force logout
      doLogout();
      throw new Error('Session expired. Please log in again.');
    }
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(payload.detail || payload.message || payload.error || 'Request failed');
    }
    return payload;
  }

  function setText(id, value) {
    const node = el(id);
    if (node) node.textContent = value;
  }

  function setHtml(id, value) {
    const node = el(id);
    if (node) node.innerHTML = value;
  }

  function ensureShell() {
    document.title = `AttendTrack — ${cfg.title}`;
    const logo = qs('.logo');
    if (logo) logo.innerHTML = `Attend<em style="font-style:italic">Track</em> · ${cfg.label}`;
    const navBadge = qs('.nav-badge');
    if (navBadge) navBadge.textContent = cfg.badge;
    setText('navName', '');

    const loginTitle = qs('.card-title');
    if (loginTitle) loginTitle.textContent = cfg.title;
    const loginSub = qs('.card-sub');
    if (loginSub) loginSub.textContent = cfg.subtitle;
    const pageTitle = qs('.page-title');
    if (pageTitle) pageTitle.innerHTML = `${cfg.pageTitle.split(' ')[0]} <em>${cfg.pageTitle.split(' ').slice(1).join(' ')}</em>`;

    const tabs = qs('.tabs');
    if (tabs) {
      tabs.innerHTML = cfg.tabs.map(([key, label]) => {
        const badgeId = key === 'pending' ? (portalRole === 'hr' ? 'hrBadge' : 'pendingBadge') : null;
        const badge   = badgeId ? `<span class="tab-badge" id="${badgeId}" style="display:none">0</span>` : '';
        return `<div class="tab${state.currentTab === key ? ' active' : ''}" data-tab="${key}">${escapeHtml(label)}${badge}</div>`;
      }).join('');
      qsa('.tab').forEach((tab) => {
        tab.addEventListener('click', () => switchTab(tab.dataset.tab));
      });
    }

    const aiSelect = el('aiSiteSelect');
    if (aiSelect) {
      aiSelect.style.display = portalRole === 'hr' ? 'block' : 'none';
    }
  }

  function applyRoleVisibility(showDashboard) {
    const loginView = el('loginView');
    const nav = el('nav');
    const app = el('app');
    const fab = el('aiFab');
    if (loginView) loginView.style.display = showDashboard ? 'none' : 'flex';
    if (nav) nav.style.display = showDashboard ? 'flex' : 'none';
    if (app) app.style.display = showDashboard ? 'block' : 'none';
    if (fab) fab.style.display = showDashboard ? 'flex' : 'none';
  }

  function startRefresh() {
    clearInterval(state.refreshTimer);
    state.refreshTimer = setInterval(() => {
      loadSummary().catch(() => {});
      loadTab().catch(() => {});
    }, 30000);
  }

  function loadStats() {
    return loadSummary();
  }

  function stopRefresh() {
    clearInterval(state.refreshTimer);
    state.refreshTimer = null;
  }

  function formatStatSummary(summary) {
    function animateStat(id, value) {
      const node = el(id);
      if (!node) return;
      node.classList.remove('skel');
      node.textContent = value ?? '–';
      node.classList.remove('animating');
      // force reflow then add class for animation
      void node.offsetWidth;
      node.classList.add('animating');
      setTimeout(() => node.classList.remove('animating'), 500);
    }
    if (portalRole === 'manager') {
      animateStat('statToday',   summary.today_unique_checkins ?? '–');
      animateStat('statPending', summary.pending_count ?? '–');
      animateStat('statApproved',summary.approved_count ?? '–');
      animateStat('statTotal',   summary.total_labours_site ?? '–');
      setText('pendingBadge', summary.pending_count ?? '0');
      const pendingBadgeEl = el('pendingBadge');
      if (pendingBadgeEl) pendingBadgeEl.style.display = (summary.pending_count > 0) ? 'inline-block' : 'none';
      const pageSub = el('pageSub');
      if (pageSub) pageSub.textContent = `${state.currentUser?.site_name || 'Your Site'} · ${state.currentUser?.name || ''}`;
    } else {
      animateStat('sToday',      summary.today_checkins ?? '–');
      animateStat('sPendingMgr', summary.pending_manager ?? '–');
      animateStat('sPendingHR',  summary.pending_hr ?? '–');
      animateStat('sApproved',   summary.approved_today ?? '–');
      animateStat('sTotal',      summary.total_employees ?? '–');
      setText('hrBadge', summary.pending_hr ?? '0');
      const hrBadgeEl = el('hrBadge');
      if (hrBadgeEl) hrBadgeEl.style.display = (summary.pending_hr > 0) ? 'inline-block' : 'none';
      const pageSub = el('pageSub');
      if (pageSub) pageSub.textContent = 'All sites · All employees · Real-time attendance';
    }
  }

  function buildMonthRecordMap(records) {
    const map = new Map();
    records.forEach((record) => {
      if (!record.employee_id) return;
      if (!sameMonth(record.date)) return;
      if (!map.has(record.employee_id)) map.set(record.employee_id, []);
      map.get(record.employee_id).push(record);
    });
    return map;
  }

  function employeeDirectory() {
    return new Map(state.employees.map((entry) => [entry.employee_id, entry]));
  }

  function projectHours(hoursWorked) {
    const { daysElapsed, daysInMonth } = monthMeta();
    return Math.round((Number(hoursWorked || 0) / daysElapsed) * daysInMonth);
  }

  function analyzeEmployee(recordGroup, employeeMeta) {
    const monthRecords = recordGroup.filter((record) => sameMonth(record.date));
    const totalHours = monthRecords.reduce((sum, record) => sum + Number(record.hours_worked || 0), 0);
    const lateCount = monthRecords.filter((record) => {
      const mins = toMinutes(record.login_time);
      return mins !== null && mins > WORK_START;
    }).length;
    const earlyCount = monthRecords.filter((record) => {
      const mins = toMinutes(record.logout_time);
      return mins !== null && mins < WORK_END;
    }).length;
    const rejectedCount = monthRecords.filter((record) => record.status === 'rejected').length;
    const offsiteCount = monthRecords.filter((record) => employeeMeta?.site_name && record.site_name && record.site_name !== employeeMeta.site_name).length;
    const projected = projectHours(totalHours);
    const findings = [];

    if (totalHours >= 90 || projected > OT_CAP) {
      findings.push({
        severity: 'high',
        title: 'Possible burnout or leave misuse',
        pattern: `${totalHours.toFixed(1)} hours logged this month with a projected pace of ${projected}h.`,
        signals: [`${totalHours.toFixed(1)}h this month`, `${projected}h projected`, `${monthRecords.length} records`],
        reco: 'Review workload balance and confirm whether extra hours are operationally required.',
      });
    }

    if (lateCount + earlyCount >= 4) {
      findings.push({
        severity: 'medium',
        title: 'Repeated late arrivals and early exits',
        pattern: `${lateCount} late arrivals and ${earlyCount} early exits in the current month.`,
        signals: [`${lateCount} late arrivals`, `${earlyCount} early exits`],
        reco: 'Raise a timekeeping conversation and check the shift pattern against the roster.',
      });
    }

    if (rejectedCount >= 2 && (lateCount + rejectedCount) >= 4) {
      findings.push({
        severity: 'high',
        title: 'Disciplinary issues alongside poor attendance',
        pattern: `${rejectedCount} rejected attendance items with attendance irregularities.`,
        signals: [`${rejectedCount} rejected records`, `${lateCount} late arrivals`],
        reco: 'Escalate to HR for a documented review and close the loop on repeated exceptions.',
      });
    }

    if (offsiteCount > 0) {
      findings.push({
        severity: 'medium',
        title: 'Off relocation and manpower plan',
        pattern: `${offsiteCount} attendance record(s) were logged at a site different from the employee assignment.`,
        signals: [`${offsiteCount} off-site records`, `Assigned: ${employeeMeta?.site_name || 'unknown'}`],
        reco: 'Confirm transfer approval or correct the site assignment in the manpower plan.',
      });
    }

    return {
      employee_id: employeeMeta?.employee_id || recordGroup[0]?.employee_id,
      name: employeeMeta?.name || recordGroup[0]?.labour_name || recordGroup[0]?.employee_id,
      trade: employeeMeta?.department || recordGroup[0]?.department || 'Labour',
      site_name: employeeMeta?.site_name || recordGroup[0]?.site_name || '—',
      totalHours,
      projected,
      lateCount,
      earlyCount,
      rejectedCount,
      offsiteCount,
      findings,
      monthRecords,
      lastSeen: recordGroup[0]?.date || '',
      present: Boolean(recordGroup.find((record) => record.status === 'active' || (record.logout_time === null && record.login_time))),
    };
  }

  function summarizeScope(records) {
    const dir = employeeDirectory();
    const grouped = buildMonthRecordMap(records);
    const summaries = [];
    for (const [employeeId, group] of grouped.entries()) {
      summaries.push(analyzeEmployee(group, dir.get(employeeId)));
    }
    return summaries.sort((a, b) => b.projected - a.projected);
  }

  function statusLabel(status) {
    const map = {
      active: ['b-active', 'Active'],
      pending_manager: ['b-pending', 'Pending manager'],
      pending_hr: ['b-pending', 'Pending HR'],
      approved: ['b-approved', 'Approved'],
      rejected: ['b-rejected', 'Rejected'],
    };
    return map[status] || ['b-pending', String(status || 'pending')];
  }

  function severityPill(severity) {
    if (severity === 'high') return '<span class="badge b-rejected">High priority</span>';
    if (severity === 'medium') return '<span class="badge b-pending">Watch</span>';
    return '<span class="badge b-active">Low</span>';
  }

  function renderBanner(title, text) {
    return `<div class="rec-card" style="margin-bottom:.9rem;border-radius:16px"><div class="rec-name" style="margin-bottom:.35rem">${escapeHtml(title)}</div><div class="rec-meta" style="font-size:.85rem;line-height:1.6">${escapeHtml(text)}</div></div>`;
  }

  function recordTitle(record) {
    return record.labour_name || record.name || record.employee_id;
  }

  function renderOverview() {
    const summary = state.currentSummary || {};
    const analyses = summarizeScope(state.allRecords);
    const topAlerts = analyses.filter((entry) => entry.findings.length > 0).slice(0, 4);
    const present = analyses.filter((entry) => entry.present).length;
    const total = analyses.length;

    // Build per-site summary cards (HR only)
    let siteSummaryHtml = '';
    if (portalRole === 'hr' && state.siteList.length) {
      const siteCards = state.siteList.map((site) => {
        const siteRecords = state.allRecords.filter((r) => r.site_name === site.name);
        const siteEmployees = state.employees.filter((e) => e.site_name === site.name && e.role === 'labour');
        const presentToday = siteRecords.filter((r) => sameDay(r.date)).length;
        const pending = siteRecords.filter((r) => r.status === 'pending_manager' || r.status === 'pending_hr').length;
        const otRisk = analyses.filter((a) => a.site_name === site.name && a.projected > OT_CAP).length;
        const totalSite = siteEmployees.length;
        const attendPct = totalSite > 0 ? Math.round((presentToday / totalSite) * 100) : 0;
        const barColor = attendPct >= 80 ? 'var(--green)' : attendPct >= 50 ? 'var(--amber)' : 'var(--red)';
        return `<div style="background:var(--bg2);border:1px solid var(--border);border-radius:16px;padding:1rem;border-left:4px solid var(--amber)">
          <div style="font-family:'Syne',sans-serif;font-weight:700;font-size:.95rem;margin-bottom:.5rem">🏗️ ${escapeHtml(site.name)}</div>
          <div style="display:flex;gap:1rem;font-size:.78rem;color:var(--white-dim);margin-bottom:.55rem">
            <span>👷 <strong style="color:var(--white)">${presentToday}</strong> present</span>
            <span>⏳ <strong style="color:${pending > 0 ? 'var(--amber)' : 'var(--white)'}">${pending}</strong> pending</span>
            <span>⚠️ <strong style="color:${otRisk > 0 ? 'var(--red)' : 'var(--white)'}">${otRisk}</strong> OT risk</span>
          </div>
          <div style="height:6px;background:var(--white-faint);border-radius:999px;overflow:hidden">
            <div style="width:${attendPct}%;height:100%;background:${barColor};border-radius:999px;transition:width .6s ease"></div>
          </div>
          <div style="font-size:.68rem;color:var(--white-dim);margin-top:.3rem">${attendPct}% attendance today · ${totalSite} total</div>
        </div>`;
      });
      siteSummaryHtml = `<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:.7rem;margin-bottom:1.2rem">${siteCards.join('')}</div>`;
    }

    const html = [
      siteSummaryHtml,
      `<div class="stats-grid" style="margin-bottom:1rem">`
        + `<div class="stat-card sc-green"><span class="stat-icon">👷</span><div class="stat-val green">${present}</div><div class="stat-lbl">Present in scope</div></div>`
        + `<div class="stat-card sc-amber"><span class="stat-icon">⏳</span><div class="stat-val amber">${summary.pending_count ?? summary.pending_hr ?? summary.pending_manager ?? 0}</div><div class="stat-lbl">Pending approvals</div></div>`
        + `<div class="stat-card sc-red"><span class="stat-icon">⚠️</span><div class="stat-val" style="color:var(--red)">${analyses.filter((entry) => entry.projected > OT_CAP).length}</div><div class="stat-lbl">Predicted breaches</div></div>`
        + `<div class="stat-card sc-purple"><span class="stat-icon">🏢</span><div class="stat-val" style="color:var(--purple-light)">${total}</div><div class="stat-lbl">Employees in scope</div></div>`
      + `</div>`,
      `<div class="rec-card"><div class="rec-head"><div><div class="rec-name">Top alerts right now</div><div class="rec-meta">Raised automatically from live attendance data</div></div></div>`,
      topAlerts.length ? topAlerts.map((entry) => {
        const finding = entry.findings[0];
        return `<div class="rec-card" style="margin:.7rem 0 0;padding:.9rem 1rem"><div class="rec-head"><div><div class="rec-name">${escapeHtml(entry.name)}</div><div class="rec-meta">${escapeHtml(entry.site_name)} · ${escapeHtml(entry.trade)}</div></div>${severityPill(finding.severity)}</div><div class="rec-times" style="margin:.45rem 0">${escapeHtml(finding.title)}</div><div class="rec-meta">${escapeHtml(finding.pattern)}</div></div>`;
      }).join('') : '<div class="empty" style="border:0;background:transparent;padding:1rem 0">No active flags in this scope.</div>',
      `</div>`,
    ].join('');

    setHtml('recList', html);
  }

  function renderPending(records) {
    if (!records.length) {
      setHtml('recList', '<div class="empty">No pending approvals 🎉</div>');
      return;
    }
    const html = records.map((record) => {
      const statusClass = record.status === 'pending_hr' ? 'b-pending' : 'b-pending';
      return `
        <div class="rec-card" id="rc${record.id}">
          <div class="rec-head">
            <div>
              <div class="rec-name">${escapeHtml(recordTitle(record))}</div>
              <div class="rec-meta">${escapeHtml(record.employee_id)} · ${escapeHtml(record.phone || '–')} · ${escapeHtml(record.site_name || '–')}</div>
            </div>
            <span class="badge ${statusClass}">${escapeHtml(record.status === 'pending_hr' ? 'Pending HR' : 'Pending')}</span>
          </div>
          <div class="rec-times">📅 ${escapeHtml(record.date)} · In: ${escapeHtml(timeOnly(record.login_time))} → Out: ${escapeHtml(timeOnly(record.logout_time))} · <strong>${Number(record.hours_worked || 0).toFixed(1)}h</strong></div>
          <div class="rec-actions">
            <input placeholder="Note (optional)" id="note${record.id}">
            <button class="btn-approve" onclick="this.classList.add('flash');setTimeout(()=>this.classList.remove('flash'),600);verify(${record.id}, true)">✓ Approve</button>
            <button class="btn-reject" onclick="this.classList.add('shake');setTimeout(()=>this.classList.remove('shake'),500);verify(${record.id}, false)">✗ Reject</button>
          </div>
        </div>`;
    }).join('');
    setHtml('recList', html);
  }

  function renderReadOnly(records, title) {
    if (!records.length) {
      setHtml('recList', '<div class="empty">No records found</div>');
      return;
    }
    const html = [
      renderBanner(title, title === 'Live today'
        ? 'The list is limited to records captured today so managers can scan the current shift status at a glance.'
        : 'The history view keeps the latest attendance entries visible for audit and follow-up.'),
      records.map((record) => {
        const [statusClass, label] = statusLabel(record.status);
        return `
          <div class="rec-card">
            <div class="rec-head">
              <div>
                <div class="rec-name">${escapeHtml(recordTitle(record))}</div>
                <div class="rec-meta">${escapeHtml(record.employee_id)} · ${escapeHtml(record.site_name || '–')}</div>
              </div>
              <span class="badge ${statusClass}">${escapeHtml(label)}</span>
            </div>
            <div class="rec-times">📅 ${escapeHtml(record.date)} · In: ${escapeHtml(timeOnly(record.login_time))} → Out: ${escapeHtml(timeOnly(record.logout_time))} · <strong>${Number(record.hours_worked || 0).toFixed(1)}h</strong></div>
          </div>`;
      }).join(''),
    ].join('');
    setHtml('recList', html);
  }

  function renderEmployees() {
    if (!state.employees.length) {
      setHtml('recList', '<div class="empty">No employees found</div>');
      return;
    }
    // Build a quick lookup: employee_id → today's status
    const todayStatus = new Map();
    state.allRecords.forEach((record) => {
      if (sameDay(record.date)) {
        todayStatus.set(record.employee_id, record.status);
      }
    });

    const ringColor = (empId) => {
      const s = todayStatus.get(empId);
      if (!s) return '#444';
      if (s === 'active') return 'var(--green)';
      if (s === 'approved') return 'var(--blue)';
      if (s === 'rejected') return 'var(--red)';
      return 'var(--amber)'; // pending_manager / pending_hr
    };

    const html = [`
      <div class="emp-grid">
        ${state.employees.map((employee) => {
          const faceClass = employee.face_registered ? 'face-yes' : 'face-no';
          const faceLabel = employee.face_registered ? 'Face enrolled' : 'Face not registered';
          const ring = ringColor(employee.employee_id);
          const initial = (employee.name || '?')[0].toUpperCase();
          return `
            <div class="emp-card" style="position:relative">
              <div style="display:flex;align-items:center;gap:.6rem;margin-bottom:.55rem">
                <div style="width:36px;height:36px;border-radius:50%;background:var(--bg);border:2.5px solid ${ring};display:flex;align-items:center;justify-content:center;font-family:'Syne',sans-serif;font-weight:700;font-size:.95rem;flex-shrink:0;color:${ring}">${escapeHtml(initial)}</div>
                <div>
                  <div class="emp-name">${escapeHtml(employee.name)}</div>
                  <div class="emp-id">${escapeHtml(employee.employee_id)}</div>
                </div>
              </div>
              <div class="emp-meta">${escapeHtml(employee.role || 'labour')} · ${escapeHtml(employee.phone || '–')}</div>
              <div class="emp-meta">${escapeHtml(employee.department || '–')}</div>
              <div class="emp-meta">${escapeHtml(employee.site_name || 'Unassigned site')}</div>
              <span class="face-tag ${faceClass}">${escapeHtml(faceLabel)}</span>
            </div>`;
        }).join('')}
      </div>`
    ].join('');
    setHtml('recList', html);
  }

  function renderAggregates(records, mode) {
    const analyses = summarizeScope(records);
    if (!analyses.length) {
      setHtml('recList', '<div class="empty">No records found</div>');
      return;
    }
    const title = mode === 'overtime'
      ? `Monthly cap ${OT_CAP}h · ${monthMeta().daysElapsed}/${monthMeta().daysInMonth} days elapsed`
      : `Projection = current hours scaled across the full month at today's pace`;
    const bannerTitle = mode === 'overtime'
      ? `${analyses.filter((entry) => entry.totalHours >= 90).length} employee(s) nearing the ${OT_CAP}h cap`
      : `${analyses.filter((entry) => entry.projected > OT_CAP).length} employee(s) on pace to exceed the cap`;

    const rows = analyses.map((entry) => {
      const exceeds = entry.projected > OT_CAP;
      const alertPill = mode === 'overtime'
        ? (entry.totalHours >= 90 ? '<span class="badge b-rejected">Alert sent to manager</span>' : '<span class="badge b-active">Within limit</span>')
        : (exceeds ? '<span class="badge b-rejected">Will exceed</span>' : '<span class="badge b-active">On track</span>');
      return `
        <div class="rec-card">
          <div class="rec-head">
            <div>
              <div class="rec-name">${escapeHtml(entry.name)}</div>
              <div class="rec-meta">${escapeHtml(entry.trade)} · ${escapeHtml(entry.site_name)}</div>
            </div>
            ${alertPill}
          </div>
          <div class="rec-times">${mode === 'overtime' ? 'Monthly hours' : 'Projected hours'} · <strong>${mode === 'overtime' ? entry.totalHours.toFixed(1) : `${entry.projected}h`}</strong></div>
          <div style="height:8px;background:var(--white-faint);border-radius:999px;overflow:hidden;margin:.65rem 0">
            <div style="width:${Math.min(100, (entry.totalHours / OT_CAP) * 100)}%;height:100%;background:${mode === 'overtime' ? 'var(--amber)' : (exceeds ? 'var(--red)' : 'var(--green)')};border-radius:999px"></div>
          </div>
          <div class="rec-meta">Now: ${entry.totalHours.toFixed(1)}h · Projected: ${entry.projected}h · ${entry.lateCount} late / ${entry.earlyCount} early</div>
        </div>`;
    }).join('');

    setHtml('recList', [
      renderBanner(mode === 'overtime' ? 'Live overtime' : 'Predictions', title),
      `<div class="rec-card"><div class="rec-name" style="margin-bottom:.25rem">${escapeHtml(bannerTitle)}</div><div class="rec-meta">${escapeHtml(title)}</div></div>`,
      rows,
    ].join(''));
  }

  function renderBehaviour(records) {
    const analyses = summarizeScope(records).filter((entry) => entry.findings.length > 0);
    if (!analyses.length) {
      setHtml('recList', '<div class="empty">No behaviour patterns flagged in this scope.</div>');
      return;
    }
    const html = [renderBanner(
      'Behaviour intelligence',
      'Patterns are surfaced from overtime, punctuality, repeated exceptions and site mismatches so managers can review the right cases first.'
    )];

    analyses.forEach((entry) => {
      entry.findings.forEach((finding) => {
        html.push(`
          <div class="rec-card">
            <div class="rec-head">
              <div>
                <div class="rec-name">${escapeHtml(finding.title)}</div>
                <div class="rec-meta">${escapeHtml(entry.name)} · ${escapeHtml(entry.employee_id)} · ${escapeHtml(entry.site_name)}</div>
              </div>
              ${severityPill(finding.severity)}
            </div>
            <div class="rec-times"><strong>Pattern detected.</strong> ${escapeHtml(finding.pattern)}</div>
            <div class="rec-meta" style="display:flex;flex-wrap:wrap;gap:.4rem;margin-top:.6rem">${finding.signals.map((signal) => `<span class="badge b-pending">${escapeHtml(signal)}</span>`).join('')}</div>
            <div class="rec-card" style="margin-top:.75rem;padding:.8rem 1rem;background:var(--bg2)">
              <div class="rec-meta"><strong>Recommendation.</strong> ${escapeHtml(finding.reco)}</div>
            </div>
          </div>`);
      });
    });

    setHtml('recList', html.join(''));
  }

  async function loadEmployees() {
    if (state.employees.length) return state.employees;
    const payload = await apiJson('/api/employees');
    state.employees = payload.employees || [];
    return state.employees;
  }

  async function loadSites() {
    if (state.siteList.length) return state.siteList;
    try {
      const payload = await apiJson('/api/sites');
      state.siteList = payload.sites || [];
      const select = el('aiSiteSelect');
      if (select && portalRole === 'hr') {
        select.innerHTML = state.siteList.map((site) => `<option value="${site.id}">${escapeHtml(site.name)}</option>`).join('');
        if (state.currentUser?.site_id) select.value = String(state.currentUser.site_id);
      }
    } catch {
      state.siteList = [];
    }
    return state.siteList;
  }

  function currentFilters() {
    if (portalRole === 'hr') {
      return {
        date: el('filterDate')?.value || '',
        employee_id: el('filterEmp')?.value.trim().toUpperCase() || '',
      };
    }
    return { date: el('filterDate')?.value || '' };
  }

  async function loadSummary() {
    state.currentSummary = await apiJson(cfg.summaryEndpoint(state.currentUser));
    formatStatSummary(state.currentSummary);
    return state.currentSummary;
  }

  async function loadPending() {
    const payload = await apiJson(cfg.pendingEndpoint(state.currentUser));
    state.pendingRecords = payload.records || [];
    return state.pendingRecords;
  }

  async function loadAllRecords() {
    const filters = currentFilters();
    const payload = await apiJson(cfg.allEndpoint(state.currentUser, filters));
    state.allRecords = payload.records || [];
    return state.allRecords;
  }

  async function loadTab() {
    if (!state.currentUser) return;

    if (state.currentTab === 'overview') {
      if (!state.allRecords.length) await loadAllRecords();
      renderOverview();
      return;
    }

    if (state.currentTab === 'pending') {
      if (!state.pendingRecords.length) await loadPending();
      renderPending(state.pendingRecords);
      return;
    }

    if (state.currentTab === 'live') {
      if (!state.allRecords.length) await loadAllRecords();
      renderReadOnly(state.allRecords.filter((record) => sameDay(record.date)), 'Live today');
      return;
    }

    if (state.currentTab === 'history' || state.currentTab === 'all') {
      await loadAllRecords();
      renderReadOnly(state.allRecords, state.currentTab === 'all' ? 'All records' : 'History');
      return;
    }

    if (state.currentTab === 'employees') {
      await loadEmployees();
      renderEmployees();
      return;
    }

    if (state.currentTab === 'overtime') {
      if (!state.allRecords.length) await loadAllRecords();
      renderAggregates(state.allRecords, 'overtime');
      return;
    }

    if (state.currentTab === 'predict') {
      if (!state.allRecords.length) await loadAllRecords();
      renderAggregates(state.allRecords, 'predict');
      return;
    }

    if (state.currentTab === 'behaviour') {
      if (!state.allRecords.length) await loadAllRecords();
      await loadEmployees();
      renderBehaviour(state.allRecords);
      return;
    }

    if (state.currentTab === 'schedreports') {
      // The actual panel is rendered by hr.html's inline script via MutationObserver.
      // Just clear the recList so there's no stale content behind the overlay.
      setHtml('recList', '');
    }
  }

  async function loadAll() {
    await loadSummary();
    await Promise.allSettled([loadEmployees(), loadSites(), loadAllRecords(), loadPending()]);
    await loadTab();
    updateRefreshStamp();
  }

  function updateRefreshStamp() {
    setText('lastUpdate', new Date().toLocaleTimeString());
  }

  function openAiSheet() {
    const backdrop = el('aiBackdrop');
    const sheet = el('aiSheet');
    if (backdrop) backdrop.classList.add('open');
    if (sheet) sheet.classList.add('open');
  }

  function closeAiSheet() {
    const backdrop = el('aiBackdrop');
    const sheet = el('aiSheet');
    if (backdrop) backdrop.classList.remove('open');
    if (sheet) sheet.classList.remove('open');
  }

  async function generateAISummary() {
    if (!state.currentUser || state.aiBusy) return;
    state.aiBusy = true;
    const btn = el('aiGenBtn');
    const box = el('aiResult');
    if (btn) {
      btn.disabled = true;
      btn.textContent = 'Generating…';
    }
    if (box) {
      box.innerHTML = '<div class="ai-result loading"><span class="ai-spinner"></span>Asking the AI to analyze attendance data — this calls a real LLM and can take a few seconds…</div>';
    }
    try {
      const siteSelect = el('aiSiteSelect');
      const siteId = portalRole === 'hr' && siteSelect ? siteSelect.value : state.currentUser.site_id;
      const payload = await apiJson(`/api/ai/site-summary/${siteId}`);
      if (box) {
        if (!payload.ai_used) {
          box.innerHTML = `<div class="ai-result error">⚠️ AI not available: ${escapeHtml(payload.error || payload.message || 'Unknown error')}<br><br>This usually means AI_API_KEY is not set in the server environment yet.</div>`;
        } else {
          box.innerHTML = `<div class="ai-result">${escapeHtml(payload.summary)}</div><div class="ai-meta">Generated by ${escapeHtml(payload.provider)} (${escapeHtml(payload.model)}) · ${escapeHtml(payload.site_name)} · ${currentHM()}</div>`;
        }
      }
    } catch (error) {
      if (box) box.innerHTML = `<div class="ai-result error">⚠️ Request failed: ${escapeHtml(error.message)}</div>`;
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = '✨ Generate report';
      }
      state.aiBusy = false;
    }
  }

  async function doLogin() {
    const id = el('empId')?.value.trim().toUpperCase();
    const pw = el('pwd')?.value || '';
    if (!id || !pw) return;
    try {
      // Login doesn't need an auth header — it IS the auth
      const response = await fetch(`${apiBase}/api/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ employee_id: id, password: pw }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.detail || payload.message || 'Login failed');

      if (payload.user.role !== cfg.loginRole) {
        throw new Error(cfg.loginRole === 'manager' ? 'This portal is for Site Managers only.' : 'This portal is for HR only.');
      }
      // Store JWT in memory
      state.accessToken = payload.access_token;
      state.currentUser = payload.user;
      applyRoleVisibility(true);
      setText('navName', state.currentUser.name);
      const navBadge = qs('.nav-badge');
      if (navBadge) navBadge.textContent = cfg.badge;
      const title = qs('.page-title');
      if (title) title.innerHTML = `${escapeHtml(cfg.pageTitle.split(' ')[0])} <em>${escapeHtml(cfg.pageTitle.split(' ').slice(1).join(' '))}</em>`;
      const sub = el('pageSub');
      if (sub && portalRole === 'manager') sub.textContent = `${state.currentUser.site_name || 'Your Site'} · ${state.currentUser.name}`;
      const aiFab = el('aiFab');
      if (aiFab) aiFab.style.display = 'flex';
      await loadAll();
      startRefresh();
    } catch (error) {
      const msg = el('loginMsg');
      if (msg) {
        msg.className = 'msg err';
        msg.style.display = 'block';
        msg.textContent = error.message;
      }
    }
  }

  async function doLogout() {
    stopRefresh();
    closeAiSheet();
    // Blacklist token server-side
    if (state.accessToken) {
      try {
        await fetch(`${apiBase}/api/logout`, {
          method: 'POST',
          headers: authHeaders(),
        });
      } catch(e) { /* ignore network errors on logout */ }
    }
    state.currentUser = null;
    state.accessToken = null;
    state.currentSummary = null;
    state.allRecords = [];
    state.pendingRecords = [];
    applyRoleVisibility(false);
    if (el('aiFab')) el('aiFab').style.display = 'none';
    if (el('loginMsg')) {
      el('loginMsg').className = 'msg';
      el('loginMsg').style.display = 'none';
      el('loginMsg').textContent = '';
    }
  }

  // Keep sync name for onclick handlers
  function logout() { doLogout(); }

  function fillDemo(id, pw) {
    const empId = el('empId');
    const pwd = el('pwd');
    if (empId) empId.value = id;
    if (pwd) pwd.value = pw;
  }

  function switchTab(tab) {
    state.currentTab = tab;
    qsa('.tab').forEach((node) => node.classList.toggle('active', node.dataset.tab === tab));
    const dateFilter = el('filterDate');
    const empFilter = el('filterEmp');
    const filterArea = el('filterArea');
    const histFilter = el('histFilter');
    if (filterArea) filterArea.style.display = portalRole === 'hr' && tab === 'all' ? 'flex' : 'none';
    if (histFilter) histFilter.style.display = portalRole === 'manager' && tab === 'history' ? 'block' : 'none';
    if (dateFilter && tab === 'all') dateFilter.onchange = loadAll;
    if (empFilter && tab === 'all') empFilter.onchange = loadAll;
    loadTab().catch((error) => {
      setHtml('recList', `<div class="empty">Error loading content: ${escapeHtml(error.message)}</div>`);
    });
  }

  async function verify(attendanceId, approved) {
    const note = el(`note${attendanceId}`)?.value || '';
    if (!state.currentUser) return;
    try {
      const payload = await apiJson(cfg.verifyEndpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          attendance_id: attendanceId,
          [cfg.verifyField]: state.currentUser.employee_id,
          approved,
          note,
        }),
      });
      const card = el(`rc${attendanceId}`);
      if (card) card.style.opacity = '0.35';
      if (state.currentTab === 'pending') {
        await loadAll();
      } else {
        state.pendingRecords = state.pendingRecords.filter((record) => record.id !== attendanceId);
        await loadSummary();
      }
      if (payload?.message) {
        // Keep the UI responsive by relying on the refreshed state.
      }
    } catch (error) {
      window.alert(error.message);
    }
  }

  function wireGlobals() {
    window.fillDemo = fillDemo;
    window.doLogin = doLogin;
    window.logout = logout;
    window.switchTab = switchTab;
    window.verify = verify;
    window.openAiSheet = openAiSheet;
    window.closeAiSheet = closeAiSheet;
    window.generateAISummary = generateAISummary;
    window.loadAll = loadAll;
    // Expose current user id for inline scripts (HR scheduled reports panel)
    window._getCurrentUserId = () => state.currentUser?.employee_id || null;
  }

  function wireUi() {
    qsa('.btn-sm').forEach((button) => {
      if (button.textContent.trim() === 'Logout') {
        button.addEventListener('click', logout);
      }
    });
    if (el('aiFab')) el('aiFab').style.display = 'none';
    if (el('aiSiteSelect') && portalRole === 'hr') {
      el('aiSiteSelect').addEventListener('change', () => {
        if (state.aiBusy) return;
      });
    }

    const loginBtn = qsa('.btn-primary').find((button) => button.closest('#loginView'));
    if (loginBtn) loginBtn.addEventListener('click', doLogin);

    const empId = el('empId');
    const pwd = el('pwd');
    [empId, pwd].forEach((field) => {
      if (!field) return;
      field.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') doLogin();
      });
    });

    const loginMsg = el('loginMsg');
    if (loginMsg) loginMsg.style.display = 'none';
  }

  function init() {
    wireGlobals();
    ensureShell();
    wireUi();
    applyRoleVisibility(false);
    if (portalRole === 'hr') {
      const filterArea = el('filterArea');
      if (filterArea) {
        filterArea.style.display = 'none';
      }
    }
  }

  document.addEventListener('DOMContentLoaded', init);
})();
