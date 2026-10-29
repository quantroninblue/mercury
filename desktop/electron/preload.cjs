// The only bridge between the page and the main process: a fixed list of calls.
const { contextBridge, ipcRenderer } = require("electron");

const invoke = (channel) => (arg) => ipcRenderer.invoke(channel, arg);

contextBridge.exposeInMainWorld("mercury", {
  config: invoke("config:get"),
  setTheme: invoke("config:theme"),
  setAdmin: invoke("config:admin"),
  pair: invoke("pair"),
  unpair: invoke("unpair"),
  health: invoke("health"),
  ask: invoke("ask"),
  askStatus: invoke("ask:status"),
  approve: invoke("ask:approve"),
  adminGet: invoke("admin:get"),
  adminPost: invoke("admin:post"),
});
