import { useCallback, useEffect, useRef, useState } from "react";
import { Icon, Logo, platformIcon } from "./icons.jsx";

const api = window.mercury;

const THEMES = {
  quicksilver: { name: "Quicksilver", note: "Graphite, liquid silver, cyan", colors: ["#0b0f17", "#9fb1c6", "#5fd0ff"] },
  caduceus: { name: "Caduceus", note: "Old gold on midnight", colors: ["#0d0b08", "#c9953a", "#e8b54d"] },
  daylight: { name: "Daylight", note: "Paper white, ink blue", colors: ["#f4f6f9", "#142033", "#2563eb"] },
  retrograde: { name: "Retrograde", note: "Violet nebula, orbits", colors: ["#0c0716", "#7dd3fc", "#c084fc"] },
  terminal: { name: "Terminal", note: "Phosphor green, monospace", colors: ["#050805", "#166534", "#4ade80"] },
};

const STATUS_TAG = {
  verified: "ok", completed: "acc", running: "acc", sent: "acc", verify_failed: "warn",
  failed: "bad", rejected: "warn", blocked: "bad", offline: "dim", timeout: "warn",
};
const FINAL_RUN = new Set(["completed", "failed", "cancelled", "interrupted"]);

const ago = (ts) => {
  if (!ts) return "never";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return new Date(ts * 1000).toLocaleDateString();
};

const store = {
  get(key, fallback) {
    try {
      return JSON.parse(localStorage.getItem(key)) ?? fallback;
    } catch {
      return fallback;
    }
  },
  set(key, value) {
    try {
      localStorage.setItem(key, JSON.stringify(value));
    } catch {
      /* storage full or unavailable: the chat still works for this session */
    }
  },
};

// Poll an admin route while `enabled`; returns { data, error, reload }.
function useAdmin(route, enabled, intervalMs = 5000) {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const reload = useCallback(async () => {
    try {
      setData(await api.adminGet(route));
      setError("");
    } catch (err) {
      setError(String(err.message || err).replace(/^Error invoking remote method '[^']+': (Error: )?/, ""));
    }
  }, [route]);
  useEffect(() => {
    if (!enabled) return undefined;
    reload();
    const timer = setInterval(reload, intervalMs);
    return () => clearInterval(timer);
  }, [enabled, reload, intervalMs]);
  return { data, error, reload };
}

const cleanError = (err) => String(err?.message || err).replace(/^Error invoking remote method '[^']+': (Error: )?/, "");

export default function App() {
  const [cfg, setCfg] = useState(null);
  const [view, setView] = useState(store.get("mercury-view", "chat"));
  const refresh = useCallback(async () => setCfg(await api.config()), []);

  useEffect(() => {
    refresh();
  }, [refresh]);
  useEffect(() => {
    if (cfg) document.body.dataset.theme = cfg.theme;
  }, [cfg]);
  useEffect(() => store.set("mercury-view", view), [view]);

  const isAdmin = Boolean(cfg?.admin);
  const devices = useAdmin("/v1/admin/devices", isAdmin);
  const tasks = useAdmin("/v1/admin/tasks?limit=50", isAdmin, 4000);
  const skills = useAdmin("/v1/admin/skills", isAdmin, 15000);

  if (!cfg) return null;
  const online = devices.data?.devices?.filter((d) => d.online).length ?? 0;
  const pending = skills.data?.pending?.length ?? 0;

  const nav = [
    ["chat", "Chat"],
    ["devices", "Devices"],
    ["tasks", "Tasks"],
    ["skills", "Skills", pending],
    ["connections", "Connections"],
  ];

  return (
    <div className={`app ${isAdmin ? "" : "no-rail"}`}>
      <nav className="side">
        <div className="brand"><Logo /><span className="name">MERCURY</span></div>
        {nav.map(([key, label, badge]) => (
          <button key={key} className={`navi ${view === key ? "on" : ""}`} onClick={() => setView(key)}>
            <Icon name={key} />
            <span>{label}</span>
            {badge ? <span className="badge">{badge}</span> : null}
          </button>
        ))}
        <div className="foot">
          {cfg.paired ? <>Signed in as <b>{cfg.deviceName}</b></> : "Not connected"}
          {isAdmin && devices.data ? <><br />{devices.data.devices.length} devices, {online} online</> : null}
        </div>
      </nav>

      <main className="main">
        {view === "chat" && <Chat cfg={cfg} goConnect={() => setView("connections")} />}
        {view === "devices" && <Devices admin={isAdmin} devices={devices} />}
        {view === "tasks" && <Tasks admin={isAdmin} tasks={tasks} />}
        {view === "skills" && <Skills admin={isAdmin} skills={skills} />}
        {view === "connections" && <Connections cfg={cfg} refresh={refresh} />}
      </main>

      {isAdmin && <Rail devices={devices.data?.devices} tasks={tasks.data?.tasks} />}
    </div>
  );
}

function Chat({ cfg, goConnect }) {
  const [items, setItems] = useState(() => store.get("mercury-chat", []));
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const bottom = useRef(null);
  // Approval prompts already answered, so their card disappears before the next poll.
  const answered = useRef(new Set());

  useEffect(() => {
    store.set("mercury-chat", items.filter((i) => i.kind !== "run").slice(-200));
    bottom.current?.scrollIntoView({ behavior: "smooth" });
  }, [items]);

  const update = (runId, patch) => setItems((all) => all.map((i) => (i.runId === runId ? { ...i, ...patch } : i)));

  async function send(newConversation = false) {
    const message = text.trim();
    if (!message || busy) return;
    setText("");
    setBusy(true);
    setItems((all) => [...all, { kind: "me", text: message, at: Date.now() }]);
    try {
      const { run_id: runId } = await api.ask({ message, newConversation });
      setItems((all) => [...all, { kind: "run", runId, status: "started", controls: [] }]);
      for (;;) {
        await new Promise((r) => setTimeout(r, 1200));
        const state = await api.askStatus(runId);
        const controls = (state.pending_controls || []).filter((c) => !answered.current.has(c.prompt_id || c.request_id));
        update(runId, { status: state.status, controls });
        if (FINAL_RUN.has(state.status)) {
          setItems((all) => all.map((i) => (i.runId === runId
            ? state.status === "completed"
              ? { kind: "ai", text: state.output || "(no reply)", at: Date.now() }
              : { kind: "err", text: `Mercury couldn't finish: ${state.error || state.status}`, at: Date.now() }
            : i)));
          break;
        }
      }
    } catch (err) {
      setItems((all) => [...all.filter((i) => i.kind !== "run"), { kind: "err", text: cleanError(err), at: Date.now() }]);
    } finally {
      setBusy(false);
    }
  }

  async function decide(runId, control, choice) {
    const id = control.prompt_id || control.request_id;
    answered.current.add(id);
    update(runId, { controls: [] });
    try {
      await api.approve({ runId, requestId: id, choice, generation: control.execution_generation });
    } catch (err) {
      setItems((all) => [...all, { kind: "err", text: cleanError(err), at: Date.now() }]);
    }
  }

  if (!cfg.paired) {
    return (
      <div className="empty">
        <h3>Connect to your Mercury hub</h3>
        <p>Ask your hub admin for a pairing code (<code>mercury pair desktop</code>), then add it in Connections.</p>
        <div className="btns"><button className="btn pri" onClick={goConnect}>Open Connections</button></div>
      </div>
    );
  }

  return (
    <>
      <div className="scroll">
        <div className="chat">
          {items.length === 0 && (
            <div className="empty" style={{ padding: 0 }}>
              <h3>What should Mercury do?</h3>
              <p>Try "What are the five largest files in Downloads on my laptop?" or "Which devices are online?"</p>
            </div>
          )}
          {items.map((item, n) => {
            if (item.kind === "me") {
              return [
                <div key={`f${n}`} className="from r">from {cfg.deviceName}</div>,
                <div key={n} className="msg me">{item.text}</div>,
              ];
            }
            if (item.kind === "ai") return <div key={n} className="msg ai">{item.text}</div>;
            if (item.kind === "err") return <div key={n} className="msg err">{item.text}</div>;
            return (
              <div key={n} className={`card ${item.controls.length ? "hl" : ""}`}>
                {item.controls.length === 0 ? (
                  <div className="working"><span className="spinner" />Mercury is working ({item.status.replace(/_/g, " ")})</div>
                ) : (
                  item.controls.map((c) => (
                    <div key={c.prompt_id || c.request_id}>
                      <h4><span style={{ color: "var(--warn)", width: 16, display: "inline-flex" }}><Icon name="shield" /></span>Approval needed</h4>
                      <pre>{c.command || c.description || c.title || JSON.stringify(c, null, 2)}</pre>
                      <div className="btns">
                        <button className="btn pri" onClick={() => decide(item.runId, c, "once")}>Approve once</button>
                        <button className="btn" onClick={() => decide(item.runId, c, "session")}>This session</button>
                        <button className="btn danger" onClick={() => decide(item.runId, c, "deny")}>Deny</button>
                      </div>
                    </div>
                  ))
                )}
              </div>
            );
          })}
          <div ref={bottom} />
        </div>
      </div>
      <div className="composer">
        <textarea
          rows={1}
          value={text}
          placeholder="Ask Mercury (Enter to send, Shift+Enter for a new line)"
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              send();
            }
          }}
        />
        <button className="btn" disabled={busy || !text.trim()} title="Start a new conversation with this message"
                onClick={() => send(true)}>New</button>
        <button className="btn pri" disabled={busy || !text.trim()} onClick={() => send()}>Send</button>
      </div>
    </>
  );
}

function AdminOnly({ title }) {
  return (
    <div className="empty">
      <h3>{title} live on the hub</h3>
      <p>Run this app on the hub machine, or add the hub admin URL and token under Connections, to manage
        devices, tasks and skills from here. Chat works from any paired device.</p>
    </div>
  );
}

function Devices({ admin, devices }) {
  const [name, setName] = useState("");
  const [code, setCode] = useState(null);
  const [error, setError] = useState("");
  if (!admin) return <AdminOnly title="Devices" />;

  async function pair() {
    setError("");
    try {
      setCode(await api.adminPost({ route: "/v1/admin/pairing", body: { name: name.trim() } }));
      setName("");
      devices.reload();
    } catch (err) {
      setError(cleanError(err));
    }
  }
  async function act(route, body) {
    try {
      await api.adminPost({ route, body });
      devices.reload();
    } catch (err) {
      setError(cleanError(err));
    }
  }

  const list = devices.data?.devices || [];
  return (
    <>
      <div className="head">
        <div><h2>Devices</h2><p>Paired devices connect out to the hub. Revoke any of them instantly.</p></div>
        <div className="row">
          <div className="field" style={{ minWidth: 180 }}>
            <input placeholder="new device name" value={name} onChange={(e) => setName(e.target.value)}
                   onKeyDown={(e) => e.key === "Enter" && name.trim() && pair()} />
          </div>
          <button className="btn pri" disabled={!name.trim()} onClick={pair}>Pair a device</button>
        </div>
      </div>
      {error && <p className="err-text" style={{ padding: "0 26px" }}>{error}</p>}
      {devices.error && <p className="err-text" style={{ padding: "0 26px" }}>{devices.error}</p>}
      <div className="scroll">
        <div className="grid">
          {code && (
            <div className="big" style={{ boxShadow: "var(--glow)" }}>
              <div className="row"><b>Code for {code.name}</b><span className="tag acc" style={{ marginLeft: "auto" }}>10 MIN</span></div>
              <div className="mono" style={{ fontSize: 22, letterSpacing: ".15em" }}>{code.code.slice(0, 4)}-{code.code.slice(4)}</div>
              <small className="note">On the device: <code>mercury-node pair --hub &lt;hub URL&gt; --code {code.code.slice(0, 4)}-{code.code.slice(4)}</code></small>
              <div className="btns"><button className="btn" onClick={() => setCode(null)}>Done</button></div>
            </div>
          )}
          {list.length === 0 && !code && <p className="note">No devices yet. Give one a name and press Pair.</p>}
          {list.map((d) => (
            <div className="big" key={d.name} style={{ opacity: d.online ? 1 : 0.8 }}>
              <div className="row">
                <span className="ic"><Icon name={platformIcon(d.platform)} /></span>
                <div><b>{d.name}</b><br /><small className="note">{d.platform || "unknown"}, {d.hostname || "-"}</small></div>
                <span className={`status ${d.online ? "on" : ""}`} style={{ marginLeft: "auto" }} />
              </div>
              <div className="kv">
                <span>Status</span><span>{d.online ? "online" : `offline, ${ago(d.last_seen)}`}</span>
                <span>Shells</span><span>{(d.capabilities?.shells || []).join(", ") || (d.capabilities?.client ? "app (chat only)" : "-")}</span>
                <span>Approval</span>
                <span>
                  <select value={d.approval} onChange={(e) => act(`/v1/admin/devices/${d.name}/policy`, { approval: e.target.value })}
                          style={{ background: "var(--bg2)", color: "var(--text)", border: "1px solid var(--line)", borderRadius: 8, padding: "2px 6px" }}>
                    <option value="all">every command</option>
                    <option value="dangerous">risky commands</option>
                    <option value="none">none (floor only)</option>
                  </select>
                </span>
              </div>
              <div className="btns">
                <button className="btn danger" onClick={() => window.confirm(`Revoke ${d.name}? It will be disconnected immediately.`)
                  && act(`/v1/admin/devices/${d.name}/revoke`)}>Revoke</button>
              </div>
            </div>
          ))}
        </div>
      </div>
    </>
  );
}

function Tasks({ admin, tasks }) {
  if (!admin) return <AdminOnly title="Tasks" />;
  const list = tasks.data?.tasks || [];
  return (
    <>
      <div className="head"><div><h2>Tasks</h2><p>Everything Mercury ran on your devices, newest first.</p></div></div>
      {tasks.error && <p className="err-text" style={{ padding: "0 26px" }}>{tasks.error}</p>}
      <div className="scroll">
        <div className="tasks">
          {list.length === 0 && <p className="note">No tasks yet.</p>}
          {list.map((t) => (
            <details className="task" key={t.id}>
              <summary>
                <span className="note">{new Date(t.created_at * 1000).toLocaleTimeString()}</span>
                <span className="cmd">{t.command}</span>
                <span className="note">{t.device}</span>
                <span className={`tag ${STATUS_TAG[t.status] || "dim"}`}>{t.status.toUpperCase().replace("_", " ")}</span>
              </summary>
              <div className="body">
                <div className="kv">
                  <span>Task</span><span className="mono">{t.id}</span>
                  <span>Exit code</span><span>{t.exit_code ?? "-"}{t.duration_ms != null ? `, ${t.duration_ms} ms` : ""}</span>
                  {t.verify_command && <><span>Verify</span><span className="mono">{t.verify_command} (exit {t.verify_exit_code ?? "-"})</span></>}
                  {t.error && <><span>Error</span><span>{t.error}</span></>}
                  {t.artifacts?.length > 0 && <><span>Files</span><span className="mono">{t.artifacts.map((a) => `${a.path} ${a.sha256 ? a.sha256.slice(0, 12) : "missing"}`).join(", ")}</span></>}
                </div>
                {t.stdout && <pre>{t.stdout}</pre>}
                {t.stderr && <pre style={{ color: "var(--warn)" }}>{t.stderr}</pre>}
              </div>
            </details>
          ))}
        </div>
      </div>
    </>
  );
}

function Skills({ admin, skills }) {
  if (!admin) return <AdminOnly title="Skills" />;
  const data = skills.data || { installed: [], pending: [], stats: [] };
  const stats = Object.fromEntries(data.stats.map((s) => [s.skill, s]));
  return (
    <>
      <div className="head">
        <div><h2>Skills</h2><p>What Mercury has learned, how often it works, and what's waiting for approval.</p></div>
        {data.pending.length > 0 && <span className="tag warn">{data.pending.length} pending approval</span>}
      </div>
      {skills.error && <p className="err-text" style={{ padding: "0 26px" }}>{skills.error}</p>}
      <div className="scroll">
        <div className="grid">
          {data.pending.map((p) => (
            <div className="big" key={p.id} style={{ boxShadow: "var(--glow)" }}>
              <div className="row"><b>{p.name}</b><span className="tag warn" style={{ marginLeft: "auto" }}>PENDING</span></div>
              <small className="note">{p.description || "New skill written by Mercury"}, staged {ago(p.staged)}</small>
              <small className="note">Approve in chat or WhatsApp: <code>/skills approve {p.id}</code></small>
            </div>
          ))}
          {data.installed.map((s) => {
            const st = stats[s.name];
            const rate = st?.success_rate;
            return (
              <div className="big" key={`${s.source}:${s.name}`}>
                <div className="row">
                  <b>{s.name}</b>
                  <span className={`tag ${s.source === "seed" ? "dim" : "ok"}`} style={{ marginLeft: "auto" }}>
                    {s.source === "seed" ? "BUILT-IN" : "LEARNED"}
                  </span>
                </div>
                <small className="note">{s.description}</small>
                {st ? (
                  <>
                    <small className="note">used {st.uses} times, {Math.round(rate * 100)}% verified</small>
                    <div className="meter"><i style={{ width: `${Math.round(rate * 100)}%`, background: rate < 0.9 ? "var(--warn)" : undefined }} /></div>
                  </>
                ) : <small className="note">not used on a device yet</small>}
              </div>
            );
          })}
        </div>
      </div>
    </>
  );
}

function Connections({ cfg, refresh }) {
  const [hubUrl, setHubUrl] = useState(cfg.hubUrl || "https://");
  const [code, setCode] = useState("");
  const [health, setHealth] = useState(null);
  const [adminUrl, setAdminUrl] = useState("");
  const [adminToken, setAdminToken] = useState("");
  const [msg, setMsg] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    api.health().then(setHealth).catch(() => setHealth({ error: true }));
  }, [cfg.hubUrl]);

  const run = (fn) => async () => {
    setError("");
    setMsg("");
    try {
      const done = await fn();
      if (done) setMsg(done);
      await refresh();
    } catch (err) {
      setError(cleanError(err));
    }
  };

  return (
    <div className="scroll">
      <div className="head"><div><h2>Connections</h2><p>Your hub, this app's pairing, and how Mercury looks.</p></div></div>
      <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(340px, 1fr))" }}>
        <div className="big">
          <div className="row"><b>Hub</b>
            {cfg.paired && <span className={`tag ${health && !health.error ? "ok" : "bad"}`} style={{ marginLeft: "auto" }}>
              {health && !health.error ? "REACHABLE" : "UNREACHABLE"}</span>}
          </div>
          {cfg.paired ? (
            <>
              <div className="kv">
                <span>URL</span><span className="mono">{cfg.hubUrl}</span>
                <span>This app</span><span>{cfg.deviceName}</span>
                {health?.version && <><span>Hub version</span><span>{health.version}</span></>}
              </div>
              <div className="btns"><button className="btn danger" onClick={run(async () => { await api.unpair(); return "Disconnected. Revoke this app on the hub too."; })}>Disconnect</button></div>
            </>
          ) : (
            <>
              <label className="field">Hub URL<input value={hubUrl} onChange={(e) => setHubUrl(e.target.value)} /></label>
              <label className="field">Pairing code<input placeholder="K7QM-2XPA" value={code} onChange={(e) => setCode(e.target.value)} /></label>
              <small className="note">Get a code from the hub admin: <code>mercury pair desktop</code></small>
              <div className="btns">
                <button className="btn pri" disabled={!code.trim()} onClick={run(async () => {
                  const r = await api.pair({ hubUrl, code });
                  return `Connected as ${r.deviceName}.`;
                })}>Connect</button>
              </div>
            </>
          )}
        </div>

        <div className="big">
          <div className="row"><b>Hub admin</b>
            <span className={`tag ${cfg.admin ? "ok" : "dim"}`} style={{ marginLeft: "auto" }}>{cfg.admin ? "ON" : "OFF"}</span>
          </div>
          {cfg.admin ? (
            <small className="note">
              {cfg.admin.fromHubEnv ? "Running on the hub: using its admin token from ~/.mercury/.env." : `Using ${cfg.admin.url}.`}
            </small>
          ) : (
            <>
              <small className="note">Optional. Lets this app show devices, tasks and skills. Only for the hub machine or an SSH tunnel to it.</small>
              <label className="field">Relay URL<input placeholder="http://127.0.0.1:8765" value={adminUrl} onChange={(e) => setAdminUrl(e.target.value)} /></label>
              <label className="field">Admin token<input type="password" value={adminToken} onChange={(e) => setAdminToken(e.target.value)} /></label>
            </>
          )}
          <div className="btns">
            {cfg.admin && !cfg.admin.fromHubEnv
              ? <button className="btn" onClick={run(() => api.setAdmin({ url: "", token: "" }))}>Remove</button>
              : !cfg.admin && <button className="btn pri" disabled={!adminUrl || !adminToken}
                                      onClick={run(() => api.setAdmin({ url: adminUrl, token: adminToken }))}>Save</button>}
          </div>
        </div>

        <div className="big" style={{ gridColumn: "1 / -1" }}>
          <b>Theme</b>
          <div className="themes">
            {Object.entries(THEMES).map(([key, t]) => (
              <button key={key} className={`theme ${cfg.theme === key ? "on" : ""}`}
                      onClick={run(() => api.setTheme(key))}>
                <span className="sw">{t.colors.map((c) => <i key={c} style={{ background: c }} />)}</span>
                {t.name}<small>{t.note}</small>
              </button>
            ))}
          </div>
        </div>
      </div>
      {msg && <p className="note" style={{ padding: "0 26px" }}>{msg}</p>}
      {error && <p className="err-text" style={{ padding: "0 26px" }}>{error}</p>}
      <p className="note" style={{ padding: "0 26px 20px" }}>Mercury Desktop {cfg.version}</p>
    </div>
  );
}

function Rail({ devices, tasks }) {
  return (
    <aside className="rail">
      <div>
        <h5>Devices</h5>
        {(devices || []).length === 0 && <p className="note">None paired.</p>}
        {(devices || []).map((d) => (
          <div className="dev" key={d.name}>
            <span className="ic"><Icon name={platformIcon(d.platform)} /></span>
            <span><b>{d.name}</b><small>{d.platform || "?"}, {d.online ? "online" : ago(d.last_seen)}</small></span>
            <span className={`status ${d.online ? "on" : ""}`} />
          </div>
        ))}
      </div>
      <div>
        <h5>Activity</h5>
        <div className="tl">
          {(tasks || []).slice(0, 8).map((t) => (
            <div key={t.id}>
              <i className={{ verified: "ok", completed: "ok", failed: "b", blocked: "b", rejected: "w", verify_failed: "w", timeout: "w" }[t.status] || ""} />
              <span>{t.device}: {t.status.replace("_", " ")}<small>{t.command.slice(0, 48)}, {ago(t.created_at)}</small></span>
            </div>
          ))}
          {(tasks || []).length === 0 && <p className="note">No tasks yet.</p>}
        </div>
      </div>
    </aside>
  );
}
