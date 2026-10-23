// MiLatexAI watchdog and status page. Runs on Cloudflare, a different company
// than the Azure-hosted app, so an Azure problem cannot silence it.
//
// Every minute it:
//   1. checks the site the way a customer reaches it (plus the app directly),
//   2. records the result for the public status page,
//   3. emails an alert when something is wrong or recovers,
//   4. starts the GitHub "incident" workflow (automatic repair) when something
//      of OURS is down.
//
// The decision logic lives in pure functions (classify, step, ...) that are
// unit tested in tests/test_watchdog_logic.py. Only the thin I/O layer at the
// bottom touches the network, KV storage, or email.

const DEFAULTS = {
  SITE: "https://milatexai.com",
  ORIGIN: "https://milatexai-app.graydune-9dce6624.canadaeast.azurecontainerapps.io",
  ALERT_TO: "yasaminfayyaz@gmail.com",
  ALERT_FROM_EMAIL: "alerts@milatexai.com",
  ALERT_FROM_NAME: "MiLatexAI Alerts",
  GH_REPO: "yasaminfayyaz/milatexai",
  GH_WORKFLOW: "incident.yml",
  STATUS_URL: "https://status.milatexai.com",
};

const DAILY_CRON = "0 12 * * *";   // 12:00 UTC, 8am Toronto: build and email the daily user table
const MINUTE = 60 * 1000;
const BAR_MINUTES = 1440;          // one character per minute, kept for 24 hours
const OPEN_AFTER_FAILS = 2;        // consecutive bad minutes before an incident opens
const CLOSE_AFTER_OKS = 3;         // consecutive good minutes before it closes
const REMINDER_MS = 15 * MINUTE;   // "still down" email cadence
const REDISPATCH_MS = 20 * MINUTE; // ask for another repair attempt
const MAX_DISPATCHES = 3;
const EXTERNAL_NOTIFY_MS = 10 * MINUTE;
const MAX_HISTORY = 20;
const DAYS_KEPT = 35;

// ---------------------------------------------------------------------------
// Pure logic (unit tested)
// ---------------------------------------------------------------------------

function initialState() {
  return { fails: 0, oks: 0, incident: null, ext: null, history: [], lastRun: 0,
           bar: "", days: {}, latest: null, events: [], lastError: null };
}

// Turn the /health/deep response into the shape the rest of the logic uses.
function parseDeep(status, bodyText) {
  if (status === 404) return { reachable: true, ok: true, legacy: true, failingOurs: [], failingExt: [], checks: {} };
  let j;
  try { j = JSON.parse(bodyText); } catch (e) { return { reachable: status > 0, ok: false, note: "badjson", failingOurs: ["response"], failingExt: [], checks: {} }; }
  const checks = (j && j.checks) || {};
  const failingOurs = Object.keys(checks).filter((k) => checks[k].scope === "ours" && !checks[k].ok);
  const failingExt = Object.keys(checks).filter((k) => checks[k].scope === "external" && !checks[k].ok);
  return { reachable: true, ok: j.ok === true && failingOurs.length === 0, degraded: failingExt.length > 0,
           failingOurs, failingExt, version: j.version || null, checks };
}

// Decide what kind of problem this minute shows.
//   ours      the app or something it depends on that we control is broken
//   edge      the app answers directly but customers cannot reach it (Cloudflare, DNS, certificate)
//   external  a third party (login provider, payments, Overleaf) is down; we cannot fix that
//   none      all good
function classify(r) {
  const originOk = !!(r.origin && r.origin.ok);
  const siteOk = !!(r.site && r.site.ok);
  const connectorOk = !!(r.connector && r.connector.ok);
  const app = r.app || { reachable: false, ok: false, failingOurs: [], failingExt: [] };
  const failing = [];
  let kind = "none";
  if (!originOk) {
    kind = "ours"; failing.push("origin");
  } else if (app.reachable && !app.ok) {
    kind = "ours"; for (const n of app.failingOurs) failing.push("app:" + n);
  } else if (!siteOk || !connectorOk || !app.reachable) {
    kind = "edge";
    if (!siteOk) failing.push("site");
    if (!connectorOk) failing.push("connector");
    if (!app.reachable) failing.push("app:unreachable");
  } else if ((r.signin && !r.signin.ok) || app.degraded) {
    kind = "external";
    if (r.signin && !r.signin.ok) failing.push("signin");
    for (const n of app.failingExt || []) failing.push("ext:" + n);
  }
  return { kind, failing, version: app.version || null };
}

function incidentId(now) {
  return "inc-" + new Date(now).toISOString().replace(/[-:T]/g, "").slice(0, 12);
}

// The alert/repair state machine. Takes the previous state and this minute's
// classification, returns the new state and the side effects to perform.
function step(prev, cls, now) {
  const s = JSON.parse(JSON.stringify(prev || initialState()));
  const actions = [];
  const critical = cls.kind === "ours" || cls.kind === "edge";
  s.lastRun = now;

  if (critical) {
    s.fails += 1;
    s.oks = 0;
    if (!s.incident && s.fails >= OPEN_AFTER_FAILS) {
      s.incident = { id: incidentId(now), openedAt: now, kind: cls.kind, failing: cls.failing,
                     lastNotifyAt: now, lastDispatchAt: now, dispatches: 1 };
      actions.push({ type: "email", template: "opened", incident: s.incident });
      actions.push({ type: "dispatch", incident: s.incident, retry: false });
    } else if (s.incident) {
      const inc = s.incident;
      inc.kind = cls.kind;
      inc.failing = cls.failing;
      if (now - inc.lastDispatchAt >= REDISPATCH_MS && inc.dispatches < MAX_DISPATCHES) {
        inc.lastDispatchAt = now;
        inc.dispatches += 1;
        actions.push({ type: "dispatch", incident: inc, retry: true });
      }
      if (now - inc.lastNotifyAt >= REMINDER_MS) {
        inc.lastNotifyAt = now;
        actions.push({ type: "email", template: "still_down", incident: inc, now });
      }
    }
  } else {
    s.fails = 0;
    if (s.incident) {
      s.oks += 1;
      if (s.oks >= CLOSE_AFTER_OKS) {
        const inc = s.incident;
        s.history.unshift({ id: inc.id, openedAt: inc.openedAt, closedAt: now, kind: inc.kind, failing: inc.failing, summary: inc.summary || "" });
        s.history = s.history.slice(0, MAX_HISTORY);
        actions.push({ type: "email", template: "recovered", incident: inc, closedAt: now });
        s.incident = null;
        s.oks = 0;
      }
    } else {
      s.oks = 0;
    }
  }

  // A third party is degraded but nothing of ours is broken: tell once, if it lasts.
  if (cls.kind === "external") {
    if (!s.ext) s.ext = { since: now, notified: false, failing: cls.failing };
    else s.ext.failing = cls.failing;
    if (!s.ext.notified && now - s.ext.since >= EXTERNAL_NOTIFY_MS) {
      s.ext.notified = true;
      actions.push({ type: "email", template: "external", ext: s.ext, now });
    }
  } else if (s.ext && !critical) {
    if (s.ext.notified) actions.push({ type: "email", template: "external_recovered", ext: s.ext, closedAt: now });
    s.ext = null;
  }
  return { state: s, actions };
}

function symbolFor(cls) {
  return cls.kind === "none" ? "g" : cls.kind === "external" ? "y" : "r";
}

// One character per minute; minutes the cron skipped show as ".".
function updateBar(bar, lastRun, now, symbol) {
  const gap = lastRun ? Math.max(1, Math.round((now - lastRun) / MINUTE)) : 1;
  return (bar + ".".repeat(Math.min(gap - 1, BAR_MINUTES)) + symbol).slice(-BAR_MINUTES);
}

function updateDays(days, now, critical) {
  const out = Object.assign({}, days);
  const key = new Date(now).toISOString().slice(0, 10);
  const cur = out[key] || [0, 0];
  out[key] = [cur[0] + 1, cur[1] + (critical ? 1 : 0)];
  const cutoff = new Date(now - DAYS_KEPT * 86400000).toISOString().slice(0, 10);
  for (const k of Object.keys(out)) if (k < cutoff) delete out[k];
  return out;
}

function uptimePercent(days, now, n) {
  let total = 0, bad = 0;
  for (let i = 0; i < n; i++) {
    const v = days[new Date(now - i * 86400000).toISOString().slice(0, 10)];
    if (v) { total += v[0]; bad += v[1]; }
  }
  return total ? Math.round((1 - bad / total) * 10000) / 100 : null;
}

function fmtDuration(ms) {
  const m = Math.max(1, Math.round(ms / MINUTE));
  if (m < 60) return m + (m === 1 ? " minute" : " minutes");
  const h = Math.floor(m / 60), r = m % 60;
  return h + (h === 1 ? " hour" : " hours") + (r ? " " + r + " min" : "");
}

function esc(v) {
  return String(v == null ? "" : v).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// Component statuses for the public page, from the latest probe results.
function components(latest) {
  if (!latest) return [];
  const r = latest.results || {};
  const checks = (r.app && r.app.checks) || {};
  const st = (ok, known) => (!known ? "unknown" : ok ? "operational" : "down");
  const ext = (c) => (c ? (c.skipped ? "operational" : c.ok ? "operational" : "degraded") : "unknown");
  const rows = [
    { name: "Website", status: st(r.site && r.site.ok, !!r.site) },
    { name: "Connector (MCP)", status: st(r.connector && r.connector.ok && r.origin && r.origin.ok, !!r.connector && !!r.origin) },
    { name: "Database", status: checks.storage ? (checks.storage.ok ? "operational" : "down") : "unknown" },
    { name: "LaTeX compiling", status: checks.latex ? (checks.latex.ok ? "operational" : "down") : "unknown" },
    { name: "Sign-in (WorkOS)", status: checks.signin_keys ? ext(checks.signin_keys) : st(r.signin && r.signin.ok, !!r.signin) },
    { name: "Payments (Stripe)", status: ext(checks.payments) },
    { name: "Overleaf Git", status: ext(checks.overleaf_git) },
  ];
  return rows;
}

function overall(state, comps) {
  if (state.incident) return { level: "down", text: "We are fixing a problem" };
  if (comps.some((c) => c.status === "down")) return { level: "down", text: "Some systems are down" };
  if (comps.some((c) => c.status === "degraded") || (state.ext)) return { level: "degraded", text: "A third-party service is having trouble" };
  if (!state.lastRun) return { level: "unknown", text: "Waiting for the first check" };
  return { level: "ok", text: "All systems operational" };
}

function barBuckets(bar, size) {
  const out = [];
  for (let i = 0; i < bar.length; i += size) {
    const chunk = bar.slice(i, i + size);
    out.push(chunk.includes("r") ? "r" : chunk.includes("y") ? "y" : chunk.includes("g") ? "g" : ".");
  }
  return out;
}

function renderStatus(core, now, cfg) {
  const state = core || initialState();
  const comps = components(state.latest);
  const o = overall(state, comps);
  const colors = { operational: "#16a34a", degraded: "#d97706", down: "#dc2626", unknown: "#94a3b8" };
  const labels = { operational: "Operational", degraded: "Degraded", down: "Down", unknown: "Not reported" };
  const barColors = { g: "#16a34a", y: "#d97706", r: "#dc2626", ".": "#cbd5e1" };
  const cells = barBuckets(state.bar || "", 15).map((c) => '<span class="cell" style="background:' + barColors[c] + '"></span>').join("");
  const up30 = uptimePercent(state.days || {}, now, 30);
  const rows = comps.map((c) =>
    '<tr><td>' + esc(c.name) + '</td><td class="st" style="color:' + colors[c.status] + '">' + labels[c.status] + '</td></tr>').join("");
  const incidents = [];
  if (state.incident) {
    incidents.push('<li><strong>Now:</strong> ' + esc(state.incident.headline || "We detected a problem and automatic repair is under way") +
      ' <span class="muted">(since ' + esc(new Date(state.incident.openedAt).toISOString().slice(0, 16).replace("T", " ")) + ' UTC)</span></li>');
  }
  for (const h of (state.history || []).slice(0, 8)) {
    incidents.push('<li>' + esc(new Date(h.openedAt).toISOString().slice(0, 16).replace("T", " ")) + ' UTC, lasted ' +
      esc(fmtDuration(h.closedAt - h.openedAt)) + (h.summary ? ': ' + esc(h.summary) : '') + '</li>');
  }
  const bannerColor = { ok: "#16a34a", degraded: "#d97706", down: "#dc2626", unknown: "#64748b" }[o.level];
  const version = state.latest && state.latest.version ? esc(String(state.latest.version).slice(0, 7)) : "";
  return '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">' +
    '<meta http-equiv="refresh" content="60"><title>MiLatexAI Status</title>' +
    '<style>:root{color-scheme:light dark;--bg:#fff;--fg:#0f172a;--muted:#64748b;--line:#e2e8f0}' +
    '@media(prefers-color-scheme:dark){:root{--bg:#0b1020;--fg:#e5e7eb;--muted:#94a3b8;--line:#1f2937}}' +
    'body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}' +
    'main{max-width:720px;margin:0 auto;padding:32px 20px 64px}h1{font-size:20px;margin:0 0 20px}' +
    '.banner{padding:16px 18px;border-radius:12px;color:#fff;font-weight:600;margin-bottom:28px}' +
    'table{width:100%;border-collapse:collapse;margin-bottom:28px}td{padding:12px 4px;border-bottom:1px solid var(--line)}' +
    '.st{text-align:right;font-weight:600}h2{font-size:15px;margin:24px 0 10px}' +
    '.bar{display:flex;gap:2px}.cell{flex:1;height:28px;border-radius:2px}' +
    '.muted{color:var(--muted);font-size:14px}ul{padding-left:20px}li{margin:6px 0}</style></head><body><main>' +
    '<h1>MiLatexAI Status</h1><div class="banner" style="background:' + bannerColor + '">' + esc(o.text) + '</div>' +
    '<table>' + rows + '</table>' +
    '<h2>Last 24 hours</h2><div class="bar" role="img" aria-label="Availability over the last 24 hours">' + cells + '</div>' +
    '<p class="muted">Each bar is 15 minutes. Green: healthy. Amber: a third-party service had trouble. Red: a problem on our side. Grey: no data.' +
    (up30 !== null ? ' 30-day availability: <strong>' + up30 + '%</strong>.' : '') + '</p>' +
    '<h2>Recent incidents</h2>' + (incidents.length ? '<ul>' + incidents.join("") + '</ul>' : '<p class="muted">None in the recorded history.</p>') +
    '<p class="muted">Checked every minute from outside our own servers.' + (version ? ' Running version ' + version + '.' : '') + '</p>' +
    '</main></body></html>';
}

function listify(names) {
  const map = { origin: "the app server", site: "the website", connector: "the connector endpoint",
                signin: "sign-in", "app:storage": "the database", "app:latex": "the LaTeX engine",
                "app:unreachable": "the app (unreachable through Cloudflare)", "app:response": "the health report" };
  return (names || []).map((n) => map[n] || (n.startsWith("ext:") ? "third party: " + n.slice(4).replace(/_/g, " ") : n)).join(", ") || "something";
}

function composeEmail(template, ctx, cfg) {
  const status = (cfg && cfg.STATUS_URL) || DEFAULTS.STATUS_URL;
  const inc = ctx.incident || {};
  const dur = ctx.now && inc.openedAt ? fmtDuration(ctx.now - inc.openedAt) : "";
  switch (template) {
    case "opened":
      return { subject: "[MiLatexAI] Problem detected: " + listify(inc.failing),
        text: "We detected a problem with MiLatexAI.\n\nWhat is failing: " + listify(inc.failing) +
          "\nType: " + (inc.kind === "edge" ? "customers cannot reach the app (Cloudflare, DNS or certificate)" : "a problem on our side") +
          "\nDetected: " + new Date(inc.openedAt).toISOString().replace("T", " ").slice(0, 16) + " UTC\nIncident: " + inc.id +
          "\n\nAutomatic repair has started. You will get another email when it is fixed, or if it needs you.\n\nLive status: " + status + "\n" };
    case "still_down":
      return { subject: "[MiLatexAI] Still not fixed after " + dur + ": " + listify(inc.failing),
        text: "The problem is still there after " + dur + ".\n\nWhat is failing: " + listify(inc.failing) +
          "\nIncident: " + inc.id + "\nRepair attempts so far: " + inc.dispatches +
          (ctx.now - inc.openedAt >= 30 * MINUTE ? "\n\nThis has gone past 30 minutes, so it probably needs you. The incident issue on GitHub has what was tried.\n" : "\n") +
          "\nLive status: " + status + "\n" };
    case "recovered":
      return { subject: "[MiLatexAI] Recovered after " + fmtDuration(ctx.closedAt - inc.openedAt),
        text: "Everything is healthy again.\n\nIt lasted " + fmtDuration(ctx.closedAt - inc.openedAt) +
          ".\nWhat had failed: " + listify(inc.failing) + (inc.summary ? "\nWhat was done: " + inc.summary : "") +
          "\nIncident: " + inc.id + "\n\nLive status: " + status + "\n" };
    case "external":
      return { subject: "[MiLatexAI] A third-party service is having trouble: " + listify(ctx.ext.failing),
        text: "Nothing on our side is broken, but a service we depend on has been failing for over 10 minutes:\n  " +
          listify(ctx.ext.failing) + "\n\nThere is nothing we can fix from here. Your own customers may notice. We will email when it recovers.\n\nLive status: " + status + "\n" };
    case "external_recovered":
      return { subject: "[MiLatexAI] Third-party service recovered", text: "The third-party trouble (" + listify(ctx.ext.failing) + ") is over.\n\nLive status: " + status + "\n" };
    case "report":
      return { subject: "[MiLatexAI] " + ctx.stage + ": " + ctx.headline,
        text: ctx.headline + "\n\n" + (ctx.details || []).join("\n") + (ctx.links && ctx.links.length ? "\n\nMore:\n" + ctx.links.join("\n") : "") + "\n\nLive status: " + status + "\n" };
    case "digest":
      return { subject: "[MiLatexAI] " + ctx.subject, text: ctx.text, html: ctx.html || "" };
    default:
      return { subject: "[MiLatexAI] " + template, text: JSON.stringify(ctx) };
  }
}

// Bound and clean what the repair workflow reports, since it can include text
// that originated in logs.
function cleanReport(body) {
  const clean = (v, n) => String(v == null ? "" : v).replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/g, "").slice(0, n);
  return {
    incident_id: clean(body.incident_id, 60),
    stage: clean(body.stage || "Update", 40),
    headline: clean(body.headline || "Update", 200),
    summary: clean(body.summary, 160),      // plain words for the public status page and the recovery email
    public: clean(body.public, 160),
    details: (Array.isArray(body.details) ? body.details : []).slice(0, 40).map((d) => clean(d, 300)),
    links: (Array.isArray(body.links) ? body.links : []).slice(0, 5).map((d) => clean(d, 300)).filter((u) => /^https:\/\//.test(u)),
    resolved: body.resolved === true,
  };
}

// ---------------------------------------------------------------------------
// Customer emails: payment reminders sent on behalf of the app (POST /api/customer-email)
// ---------------------------------------------------------------------------
// Only two fixed templates can be sent, to one address, with a payment link that must be a
// Stripe page. So even a leaked secret cannot turn this into a general mail relay for our domain.

const CUSTOMER_KINDS = ["payment_failed", "payment_reminder"];
const CUSTOMER_FROM = { email: "support@milatexai.com", name: "MiLatexAI Support" };
const SIGNATURE_LOGO = "https://milatexai.com/email-signature-logo.png";
const STRIPE_PAGE = /^https:\/\/(invoice|billing)\.stripe\.com\/[A-Za-z0-9_\-\/.?=&%]{1,600}$/;
const EMAIL_ADDR = /^[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,190}\.[A-Za-z]{2,24}$/;
const DATE_TEXT = /^[A-Z][a-z]{2,8} \d{1,2}$/;     // "October 26"
const AMOUNT_TEXT = /^\$\d{1,4}\.\d{2}$/;           // "$8.99"
const CUSTOMER_MAX_PER_WEEK = 4;

function cleanCustomerEmail(body) {
  const b = body && typeof body === "object" ? body : {};
  const kind = String(b.kind || "");
  const to = String(b.to || "").trim();
  const link = String(b.pay_url || "").trim();
  const ends = String(b.ends_on || "").trim();
  const amount = String(b.amount || "$8.99").trim();
  if (!CUSTOMER_KINDS.includes(kind)) return { ok: false, error: "unknown kind" };
  if (!EMAIL_ADDR.test(to)) return { ok: false, error: "bad address" };
  if (!STRIPE_PAGE.test(link)) return { ok: false, error: "bad link" };
  if (!DATE_TEXT.test(ends)) return { ok: false, error: "bad date" };
  if (!AMOUNT_TEXT.test(amount)) return { ok: false, error: "bad amount" };
  return { ok: true, value: { kind, to, link, ends, amount } };
}

function composeCustomerEmail(v) {
  const free = "the free plan (10 commits a month, one active project at a time)";
  const paras = v.kind === "payment_failed"
    ? ["Your MiLatexAI Pro renewal of " + v.amount + " didn't go through. This can happen when a card expires, a bank asks for a confirmation, or a payment method needs updating.",
       "Your Pro access stays on for now. If the payment isn't done by " + v.ends + ", your account moves to " + free + ". Your projects stay saved either way."]
    : ["A quick reminder: your MiLatexAI Pro renewal of " + v.amount + " is still unpaid.",
       "Your Pro access ends on " + v.ends + ", after which your account moves to " + free + ". Your projects stay saved, and you can come back to Pro any time."];
  const subject = v.kind === "payment_failed"
    ? "Your MiLatexAI Pro payment didn't go through"
    : "Reminder: your MiLatexAI Pro payment is still open (Pro ends " + v.ends + ")";
  const ask = "If something looks wrong, or you'd rather cancel, just reply to this email.";
  const text = "Hi,\n\n" + paras[0] + "\n\nYou can finish the payment here:\n" + v.link + "\n\n" + paras[1] + "\n\n" + ask +
    "\n\nBest,\n\nSupport\nmilatexai.com\n";
  const p = (t) => '<p style="margin:0 0 14px">' + esc(t) + "</p>";
  const html = '<div style="font:15px/1.55 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:#111827">' +
    p("Hi,") + p(paras[0]) +
    '<p style="margin:0 0 14px">You can finish the payment <a href="' + esc(v.link) + '">on this Stripe page</a>.</p>' +
    p(paras[1]) + p(ask) +
    '<p style="margin:22px 0 0">Best,</p><p style="margin:14px 0 0;font-weight:700">Support</p>' +
    '<p style="margin:6px 0 0"><img src="' + SIGNATURE_LOGO + '" width="96" height="22" alt="MiLatexAI" style="display:block;border:0"></p>' +
    '<p style="margin:6px 0 0"><a href="https://milatexai.com">milatexai.com</a></p></div>';
  return { subject, text, html };
}

// The daily digest comes from our own workflow, but it is still bounded before it is emailed or stored.
function cleanDigest(body) {
  const clean = (v, n) => String(v == null ? "" : v).replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/g, "").slice(0, n);
  const snapshot = body && typeof body.snapshot === "object" && body.snapshot ? body.snapshot : {};
  const json = JSON.stringify(snapshot);
  return {
    subject: clean(body.subject || "Daily report", 150).replace(/[\r\n]+/g, " "),
    text: clean(body.text, 30000),
    html: clean(body.html, 60000),
    snapshot: json.length <= 60000 ? snapshot : {},
  };
}

function constantTimeEqual(a, b) {
  // An empty secret must never match: a Worker with the secret unset would otherwise accept an empty token.
  if (typeof a !== "string" || typeof b !== "string" || !a || !b || a.length !== b.length) return false;
  let d = 0;
  for (let i = 0; i < a.length; i++) d |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return d === 0;
}

// ---------------------------------------------------------------------------
// I/O layer (not unit tested; exercised by the drills)
// ---------------------------------------------------------------------------

const cfgOf = (env) => Object.assign({}, DEFAULTS, ...Object.keys(DEFAULTS).filter((k) => env[k]).map((k) => ({ [k]: env[k] })));

async function timed(fn, ms) {
  const t = Date.now();
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), ms || 12000);
  try {
    return Object.assign(await fn(ac.signal), { ms: Date.now() - t });
  } catch (e) {
    return { ok: false, failed: true, ms: Date.now() - t, note: e && e.name === "AbortError" ? "timeout" : "error" };
  } finally {
    clearTimeout(timer);
  }
}

const UA = { "user-agent": "milatexai-watchdog/1.0 (+https://status.milatexai.com)" };

async function probeAll(cfg) {
  const get = (url, signal) => fetch(url, { signal, headers: UA, redirect: "manual" });
  const [site, connector, signin, app, origin] = await Promise.all([
    timed(async (signal) => { const r = await get(cfg.SITE + "/", signal); const t = await r.text(); return { ok: r.status === 200 && t.includes("MiLatexAI"), status: r.status }; }),
    timed(async (signal) => {
      const r = await fetch(cfg.SITE + "/mcp", { method: "POST", signal, redirect: "manual",
        headers: Object.assign({ "content-type": "application/json", accept: "application/json, text/event-stream" }, UA),
        body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "initialize", params: {} }) });
      await r.arrayBuffer();
      return { ok: r.status === 401 && (r.headers.get("www-authenticate") || "").includes("resource_metadata"), status: r.status };
    }),
    timed(async (signal) => { const r = await get(cfg.SITE + "/.well-known/oauth-authorization-server", signal); const t = await r.text(); return { ok: r.status === 200 && t.includes("authorization_endpoint"), status: r.status }; }),
    timed(async (signal) => { const r = await get(cfg.SITE + "/health/deep", signal); return Object.assign(parseDeep(r.status, await r.text()), { status: r.status }); }, 20000),
    timed(async (signal) => {
      let r = await get(cfg.ORIGIN + "/health/live", signal);
      if (r.status === 404) r = await get(cfg.ORIGIN + "/health/capacity", signal); // older image
      await r.arrayBuffer();
      return { ok: r.status === 200, status: r.status };
    }),
  ]);
  if (app.failed) { app.reachable = false; app.ok = false; app.failingOurs = app.failingOurs || []; app.failingExt = app.failingExt || []; }
  return { site, connector, signin, app, origin };
}

async function sendEmail(env, cfg, template, ctx) {
  const m = composeEmail(template, ctx, cfg);
  const message = { to: cfg.ALERT_TO, from: { email: cfg.ALERT_FROM_EMAIL, name: cfg.ALERT_FROM_NAME }, subject: m.subject, text: m.text };
  if (m.html) message.html = m.html;
  return await env.EMAIL.send(message);
}

async function dispatchRepair(env, cfg, inc, latest, retry, drill) {
  const r = await fetch("https://api.github.com/repos/" + cfg.GH_REPO + "/actions/workflows/" + cfg.GH_WORKFLOW + "/dispatches", {
    method: "POST",
    headers: { authorization: "Bearer " + env.GITHUB_TOKEN, accept: "application/vnd.github+json", "x-github-api-version": "2022-11-28",
               "user-agent": "milatexai-watchdog", "content-type": "application/json" },
    body: JSON.stringify({ ref: "main", inputs: {
      incident_id: inc.id, kind: drill ? "drill" : inc.kind, failing: (inc.failing || []).join(","),
      detected_at: new Date(inc.openedAt).toISOString(), version: String((latest && latest.version) || ""),
      attempt: retry ? "retry" : "first", drill_app: "" } }),
  });
  return r.status === 204;
}

// Started by the daily cron below: asks GitHub to build and email the daily user table.
async function dispatchDaily(env, cfg) {
  const r = await fetch("https://api.github.com/repos/" + cfg.GH_REPO + "/actions/workflows/daily-report.yml/dispatches", {
    method: "POST",
    headers: { authorization: "Bearer " + env.GITHUB_TOKEN, accept: "application/vnd.github+json", "x-github-api-version": "2022-11-28",
               "user-agent": "milatexai-watchdog", "content-type": "application/json" },
    body: JSON.stringify({ ref: "main" }),
  });
  if (r.status !== 204) throw new Error("daily dispatch " + r.status);
}

async function runCheck(env) {
  const cfg = cfgOf(env);
  const now = Date.now();
  const prev = (await env.STATE.get("core", "json")) || initialState();
  const results = await probeAll(cfg);
  const cls = classify(results);
  const { state, actions } = step(prev, cls, now);
  state.latest = { ts: now, kind: cls.kind, failing: cls.failing, version: cls.version, results: {
    site: { ok: results.site.ok }, connector: { ok: results.connector.ok }, signin: { ok: results.signin.ok },
    origin: { ok: results.origin.ok }, app: { ok: results.app.ok, reachable: results.app.reachable, checks: results.app.checks || {} } } };
  state.bar = updateBar(prev.bar || "", prev.lastRun || 0, now, symbolFor(cls));
  state.days = updateDays(prev.days || {}, now, cls.kind === "ours" || cls.kind === "edge");
  for (const a of actions) {
    try {
      if (a.type === "email") await sendEmail(env, cfg, a.template, Object.assign({}, a, { now }));
      else if (a.type === "dispatch") {
        const ok = await dispatchRepair(env, cfg, a.incident, state.latest, a.retry, false);
        if (!ok) state.lastError = "dispatch failed at " + new Date(now).toISOString();
      }
    } catch (e) {
      state.lastError = a.type + " " + a.template + " failed: " + String((e && (e.code || e.name)) || "error") + " at " + new Date(now).toISOString();
    }
  }
  await env.STATE.put("core", JSON.stringify(state));
}

const J = (obj, status) => new Response(JSON.stringify(obj), { status: status || 200, headers: { "content-type": "application/json", "cache-control": "no-store" } });

async function handleFetch(request, env) {
  const cfg = cfgOf(env);
  const url = new URL(request.url);
  const now = Date.now();
  const authed = () => constantTimeEqual((request.headers.get("authorization") || "").replace(/^Bearer /, ""), env.REPORT_SECRET || "");
  if (request.method === "GET" && url.pathname === "/") {
    const core = await env.STATE.get("core", "json");
    return new Response(renderStatus(core, now, cfg), { headers: { "content-type": "text/html; charset=utf-8", "cache-control": "public, max-age=30" } });
  }
  if (request.method === "GET" && url.pathname === "/api/status.json") {
    const core = (await env.STATE.get("core", "json")) || initialState();
    const comps = components(core.latest);
    return new Response(JSON.stringify({ status: overall(core, comps), components: comps, uptime30d: uptimePercent(core.days || {}, now, 30), updated: core.lastRun || null }),
      { headers: { "content-type": "application/json", "cache-control": "public, max-age=30", "access-control-allow-origin": "*" } });
  }
  if (request.method === "GET" && url.pathname === "/api/heartbeat") {
    const core = (await env.STATE.get("core", "json")) || initialState();
    return J({ lastRun: core.lastRun || 0, ageSeconds: core.lastRun ? Math.round((now - core.lastRun) / 1000) : null, error: core.lastError || null });
  }
  if (request.method === "POST" && url.pathname === "/api/report") {
    if (!authed()) return J({ error: "unauthorized" }, 401);
    const rep = cleanReport(await request.json().catch(() => ({})));
    const core = (await env.STATE.get("core", "json")) || initialState();
    let mail = true;
    if (core.incident && core.incident.id === rep.incident_id) {
      // Only the public wording reaches the status page; the owner-facing headline goes by email.
      if (rep.public) core.incident.headline = rep.public;
      // A "fixed" report rides along in the single "recovered" email once the
      // probes confirm it, so we do not send two emails for one recovery.
      if (rep.resolved) { core.incident.summary = rep.summary || rep.headline; mail = false; }
    }
    const idx = core.history.findIndex((h) => h.id === rep.incident_id);
    if (idx >= 0 && rep.resolved) core.history[idx].summary = rep.summary || rep.headline;
    await env.STATE.put("core", JSON.stringify(core));
    if (mail) await sendEmail(env, cfg, "report", rep);
    return J({ ok: true, emailed: mail });
  }
  if (request.method === "POST" && url.pathname === "/api/test-email") {
    if (!authed()) return J({ error: "unauthorized" }, 401);
    const sent = await sendEmail(env, cfg, "report", { stage: "Test", headline: "Alert email path works", details: ["Sent from the watchdog on Cloudflare at " + new Date(now).toISOString() + "."], links: [] });
    return J({ ok: true, sent: sent || null });
  }
  // The daily user table: its own secret, so the job that builds it can send nothing else.
  const digestAuthed = () => constantTimeEqual((request.headers.get("authorization") || "").replace(/^Bearer /, ""), env.DIGEST_SECRET || "");
  if (request.method === "GET" && url.pathname === "/api/digest-state") {
    if (!digestAuthed()) return J({ error: "unauthorized" }, 401);
    return J((await env.STATE.get("digest:last", "json")) || {});
  }
  if (request.method === "POST" && url.pathname === "/api/digest") {
    if (!digestAuthed()) return J({ error: "unauthorized" }, 401);
    const d = cleanDigest(await request.json().catch(() => ({})));
    await sendEmail(env, cfg, "digest", d);
    await env.STATE.put("digest:last", JSON.stringify(d.snapshot));   // only after the email was accepted
    return J({ ok: true });
  }
  // Payment reminders for customers. Its own secret, held by the app and by nothing else.
  if (request.method === "POST" && url.pathname === "/api/customer-email") {
    const ok = constantTimeEqual((request.headers.get("authorization") || "").replace(/^Bearer /, ""), env.CUSTOMER_EMAIL_SECRET || "");
    if (!ok) return J({ error: "unauthorized" }, 401);
    const c = cleanCustomerEmail(await request.json().catch(() => ({})));
    if (!c.ok) return J({ error: c.error }, 400);
    const key = "ce:" + c.value.to.toLowerCase();
    const sentBefore = parseInt((await env.STATE.get(key)) || "0", 10) || 0;
    if (sentBefore >= CUSTOMER_MAX_PER_WEEK) return J({ error: "too many emails to this address this week" }, 429);
    const m = composeCustomerEmail(c.value);
    // The owner is copied (bcc) so every customer email that goes out is also in the owner's inbox.
    const sent = await env.EMAIL.send({ to: c.value.to, bcc: cfg.ALERT_TO, from: CUSTOMER_FROM, subject: m.subject, text: m.text, html: m.html });
    await env.STATE.put(key, String(sentBefore + 1), { expirationTtl: 7 * 86400 });
    return J({ ok: true, sent: sent || null });
  }
  if (request.method === "POST" && url.pathname === "/api/test-dispatch") {
    if (!authed()) return J({ error: "unauthorized" }, 401);
    const body = await request.json().catch(() => ({}));
    const inc = { id: "drill-" + incidentId(now).slice(4), kind: "drill", failing: ["drill"], openedAt: now };
    const ok = await dispatchRepair(env, cfg, inc, { version: "" }, false, true);
    return J({ ok, incident_id: inc.id, drill_app: body.drill_app || "" }, ok ? 200 : 502);
  }
  return new Response("Not found", { status: 404, headers: { "cache-control": "no-store" } });
}

export default {
  async scheduled(event, env, ctx) {
    if (event.cron === DAILY_CRON) {
      ctx.waitUntil(dispatchDaily(env, cfgOf(env)).catch((e) => env.STATE.put("daily-error", String((e && e.message) || "error").slice(0, 100) + " at " + new Date().toISOString())));
      return;   // the every-minute cron fires at the same moment and does the health check
    }
    ctx.waitUntil(runCheck(env).catch((e) => {
      console.error("scheduled run failed:", String((e && e.name) || "error"), String((e && e.message) || "").slice(0, 200));
      return env.STATE.put("lasterror", String((e && e.name) || "error") + " at " + new Date().toISOString());
    }));
  },
  async fetch(request, env) {
    try { return await handleFetch(request, env); } catch (e) {
      console.error("request failed:", String((e && e.name) || "error"), String((e && e.message) || "").slice(0, 200));
      return new Response("Error", { status: 500 });
    }
  },
};

export const __test = { classify, step, parseDeep, updateBar, updateDays, uptimePercent, renderStatus, composeEmail, cleanReport, cleanDigest, cleanCustomerEmail, composeCustomerEmail, constantTimeEqual, initialState, fmtDuration, components, overall };
