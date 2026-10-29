// Mercury Desktop, main process.
//
// All network calls happen here, never in the page: the device token (and the hub admin
// token, when this app runs on the hub itself) stay in this process and are stored
// encrypted with the OS keychain through Electron's safeStorage.

const { app, BrowserWindow, ipcMain, safeStorage, shell } = require("electron");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const VERSION = "0.1.0";
const configFile = () => path.join(app.getPath("userData"), "config.json");

function readConfig() {
  try {
    return JSON.parse(fs.readFileSync(configFile(), "utf8"));
  } catch {
    return {};
  }
}

function writeConfig(cfg) {
  fs.mkdirSync(path.dirname(configFile()), { recursive: true });
  fs.writeFileSync(configFile(), JSON.stringify(cfg, null, 2), { mode: 0o600 });
}

function seal(secret) {
  if (!secret) return "";
  return safeStorage.isEncryptionAvailable()
    ? "enc:" + safeStorage.encryptString(secret).toString("base64")
    : "raw:" + secret;
}

function unseal(value) {
  if (!value) return "";
  if (value.startsWith("enc:")) return safeStorage.decryptString(Buffer.from(value.slice(4), "base64"));
  return value.startsWith("raw:") ? value.slice(4) : "";
}

// On the hub machine, pick up the relay admin token so the Devices, Tasks and Skills
// screens work without extra setup.
function hubLocalAdmin() {
  const home = process.env.MERCURY_HOME || path.join(os.homedir(), ".mercury");
  try {
    const env = Object.fromEntries(
      fs.readFileSync(path.join(home, ".env"), "utf8")
        .split(/\r?\n/)
        .filter((l) => l && !l.startsWith("#") && l.includes("="))
        .map((l) => [l.slice(0, l.indexOf("=")).trim(), l.slice(l.indexOf("=") + 1).trim()]),
    );
    if (!env.MERCURY_ADMIN_TOKEN) return null;
    return { url: `http://127.0.0.1:${env.MERCURY_RELAY_PORT || 8765}`, token: env.MERCURY_ADMIN_TOKEN };
  } catch {
    return null;
  }
}

function admin() {
  const cfg = readConfig();
  if (cfg.adminUrl && cfg.adminToken) return { url: cfg.adminUrl, token: unseal(cfg.adminToken) };
  return hubLocalAdmin();
}

async function call(method, url, token, body) {
  let response;
  try {
    response = await fetch(url, {
      method,
      headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}) },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(60000),
    });
  } catch (err) {
    throw new Error(`Can't reach ${new URL(url).host} (${err.cause?.code || err.name})`);
  }
  const text = await response.text();
  let data;
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { detail: text.slice(0, 200) };
  }
  if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
  return data;
}

function device() {
  const cfg = readConfig();
  const token = unseal(cfg.deviceToken);
  if (!cfg.hubUrl || !token) throw new Error("Not connected to a hub yet. Pair this app in Connections.");
  return { hub: cfg.hubUrl, token };
}

const platformName = () => ({ win32: "windows", darwin: "darwin" })[process.platform] || "linux";

const handlers = {
  "config:get": () => {
    const cfg = readConfig();
    const adm = admin();
    return {
      hubUrl: cfg.hubUrl || "",
      deviceName: cfg.deviceName || "",
      paired: Boolean(cfg.deviceToken),
      admin: adm ? { url: adm.url, fromHubEnv: !cfg.adminToken } : null,
      theme: cfg.theme || "quicksilver",
      version: VERSION,
    };
  },
  "config:theme": (_e, theme) => writeConfig({ ...readConfig(), theme }),
  "config:admin": (_e, { url, token }) => {
    const cfg = readConfig();
    writeConfig({ ...cfg, adminUrl: url ? url.replace(/\/$/, "") : "", adminToken: url ? seal(token) : "" });
  },
  pair: async (_e, { hubUrl, code }) => {
    const hub = hubUrl.trim().replace(/\/$/, "");
    if (!/^https?:\/\//.test(hub)) throw new Error("Hub URL must start with https:// (or http:// on a local network)");
    const reply = await call("POST", `${hub}/v1/pair`, "", {
      code: code.trim(),
      platform: platformName(),
      hostname: os.hostname(),
      node_version: `desktop-${VERSION}`,
      capabilities: { client: "desktop" },
    });
    writeConfig({ ...readConfig(), hubUrl: hub, deviceName: reply.device_name, deviceToken: seal(reply.device_token) });
    return { deviceName: reply.device_name };
  },
  unpair: () => {
    const { deviceToken, deviceName, ...rest } = readConfig();
    writeConfig(rest);
  },
  health: async () => {
    const cfg = readConfig();
    if (!cfg.hubUrl) return null;
    return call("GET", `${cfg.hubUrl}/health`);
  },
  ask: async (_e, { message, newConversation }) => {
    const { hub, token } = device();
    return call("POST", `${hub}/v1/ask`, token, { message, new_conversation: Boolean(newConversation) });
  },
  "ask:status": async (_e, runId) => {
    const { hub, token } = device();
    return call("GET", `${hub}/v1/ask/${encodeURIComponent(runId)}`, token);
  },
  "ask:approve": async (_e, { runId, requestId, choice, generation }) => {
    const { hub, token } = device();
    return call("POST", `${hub}/v1/ask/${encodeURIComponent(runId)}/approval`, token, {
      request_id: requestId,
      choice,
      ...(generation === undefined || generation === null ? {} : { execution_generation: generation }),
    });
  },
  "admin:get": async (_e, route) => {
    const adm = admin();
    if (!adm) throw new Error("admin-unavailable");
    return call("GET", adm.url + route, adm.token);
  },
  "admin:post": async (_e, { route, body }) => {
    const adm = admin();
    if (!adm) throw new Error("admin-unavailable");
    return call("POST", adm.url + route, adm.token, body ?? {});
  },
};

function createWindow() {
  const win = new BrowserWindow({
    width: 1320,
    height: 840,
    minWidth: 900,
    minHeight: 600,
    title: "Mercury",
    backgroundColor: "#0b0f17",
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
    },
  });
  // Links open in the system browser, never inside the app.
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:\/\//.test(url)) shell.openExternal(url);
    return { action: "deny" };
  });
  win.webContents.on("will-navigate", (event) => event.preventDefault());
  if (process.env.MERCURY_DESKTOP_DEV) win.loadURL(process.env.MERCURY_DESKTOP_DEV);
  else win.loadFile(path.join(__dirname, "..", "dist", "index.html"));
}

app.whenReady().then(() => {
  for (const [channel, fn] of Object.entries(handlers)) ipcMain.handle(channel, fn);
  createWindow();
  app.on("activate", () => BrowserWindow.getAllWindows().length === 0 && createWindow());
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
