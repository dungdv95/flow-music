/**
 * Flow Music Kit — background service worker.
 *
 * Connects to the local agent (ws://127.0.0.1:9300), runs its commands inside
 * a signed-in flowmusic.google tab, and posts results back over HTTP
 * (POST /api/ext/callback) so a socket blip mid-generation loses nothing.
 *
 * Commands: page_fetch, batch_rpc, page_download, set_capture, get_status.
 */

const AGENT_WS_URL = 'ws://127.0.0.1:9300';
let agentHttp = 'http://127.0.0.1:8300';
const MUSIC_URLS = ['https://flowmusic.google/*', 'https://*.flowmusic.google/*'];
const MUSIC_HOME = 'https://flowmusic.google/';
const CAPTCHA_SLOT = '__CAPTCHA__';
const MAX_TEXT = 32000000;
const MAX_DOWNLOAD = 40 * 1024 * 1024;

let ws = null;
let callbackSecret = null;
let state = 'off'; // off | idle | running
const stats = { requests: 0, ok: 0, failed: 0, captures: 0, lastError: null };
const log = [];

// ─── lifecycle ──────────────────────────────────────────────

chrome.runtime.onInstalled.addListener(start);
chrome.runtime.onStartup.addListener(start);
chrome.alarms.onAlarm.addListener((a) => {
  if (a.name === 'keepAlive') keepAlive();
  if (a.name === 'reconnect') connect();
});
start();

function start() {
  chrome.alarms.create('keepAlive', { periodInMinutes: 0.4 });
  connect();
}

function connect() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
  try {
    ws = new WebSocket(AGENT_WS_URL);
  } catch (e) {
    scheduleReconnect();
    return;
  }
  ws.onopen = async () => {
    chrome.alarms.clear('reconnect');
    setState('idle');
    const { captureEnabled } = await chrome.storage.local.get(['captureEnabled']);
    ws.send(JSON.stringify({
      type: 'extension_ready',
      version: chrome.runtime.getManifest().version,
      captureEnabled: !!captureEnabled,
    }));
  };
  ws.onmessage = ({ data }) => {
    let msg;
    try { msg = JSON.parse(data); } catch (e) { return; }
    if (msg.type === 'callback_secret') {
      callbackSecret = msg.secret;
      if (msg.apiPort) agentHttp = `http://127.0.0.1:${msg.apiPort}`;
      return;
    }
    if (msg.type === 'pong') return;
    if (msg.id && msg.method) handleCommand(msg);
  };
  ws.onclose = () => {
    setState('off');
    scheduleReconnect();
  };
  ws.onerror = () => {};
}

function scheduleReconnect() {
  chrome.alarms.create('reconnect', { delayInMinutes: 0.083 });
}

function keepAlive() {
  if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'ping' }));
  else connect();
}

async function reply(msg) {
  try {
    const r = await fetch(`${agentHttp}/api/ext/callback`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'x-callback-secret': callbackSecret || '' },
      body: JSON.stringify(msg),
    });
    if (r.ok) return;
  } catch (e) { /* fall back to the socket */ }
  if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg));
}

// ─── tabs ───────────────────────────────────────────────────

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function getMusicTab() {
  const tabs = await chrome.tabs.query({ url: MUSIC_URLS });
  let tab = tabs.find((t) => !t.discarded && t.status === 'complete') || tabs.find((t) => !t.discarded) || tabs[0];
  if (!tab) {
    tab = await chrome.tabs.create({ url: MUSIC_HOME, active: false });
    await sleep(6000);
    tab = await chrome.tabs.get(tab.id).catch(() => null);
    if (!tab) throw new Error('NO_FLOW_MUSIC_TAB');
  }
  if (tab.discarded) {
    await chrome.tabs.reload(tab.id);
    await sleep(4000);
    tab = await chrome.tabs.get(tab.id).catch(() => null);
    if (!tab) throw new Error('TAB_DISCARDED');
  }
  return tab;
}

async function inPage(tabId, func, args) {
  const [res] = await chrome.scripting.executeScript({ target: { tabId }, world: 'MAIN', func, args });
  if (!res) throw new Error('NO_INJECTION_RESULT');
  return res.result;
}

async function mintCaptcha(tabId, requestId, action, sitekey) {
  const ask = () => chrome.tabs.sendMessage(tabId, { type: 'GET_CAPTCHA', requestId, action, sitekey });
  let resp;
  try {
    resp = await ask();
  } catch (e) {
    // Tab opened before the extension loaded: inject the scripts and retry.
    await chrome.scripting.executeScript({ target: { tabId }, world: 'MAIN', files: ['page.js'] });
    await chrome.scripting.executeScript({ target: { tabId }, files: ['content.js'] });
    await sleep(300);
    resp = await ask();
  }
  if (!resp?.token) throw new Error(`CAPTCHA_FAILED: ${resp?.error || 'no token'}`);
  return resp.token;
}

function fillCaptcha(text, token) {
  return typeof text === 'string' ? text.split(CAPTCHA_SLOT).join(token) : text;
}

// ─── page-world functions (serialized into the tab) ─────────

async function pageFetch(url, method, headers, body, maxText) {
  const resp = await fetch(url, {
    method, headers: headers || {}, credentials: 'include',
    body: method === 'GET' || method === 'HEAD' ? undefined : body,
  });
  const text = await resp.text();
  return { status: resp.status, contentType: resp.headers.get('content-type'), text: text.slice(0, maxText) };
}

async function pageBatchRpc(path, rpcid, freq, maxText) {
  const wiz = globalThis.WIZ_global_data || {};
  const at = wiz.SNlM0e;
  if (!at) return { error: 'NO_AT_TOKEN — tab is signed out, still loading, or not a batchexecute app' };
  const hl = (document.documentElement.lang || navigator.language || 'en').split('-')[0];
  const url = `${path}?rpcids=${encodeURIComponent(rpcid)}`
    + `&source-path=${encodeURIComponent(location.pathname || '/')}`
    + `&bl=${encodeURIComponent(wiz.cfb2h || '')}&f.sid=${encodeURIComponent(wiz.FdrFJe || '')}`
    + `&hl=${encodeURIComponent(hl)}&_reqid=${Math.floor(Math.random() * 900000) + 100000}&rt=c`;
  const resp = await fetch(url, {
    method: 'POST', credentials: 'include',
    headers: { 'content-type': 'application/x-www-form-urlencoded;charset=UTF-8', 'x-same-domain': '1' },
    body: new URLSearchParams({ 'f.req': freq, at }),
  });
  const text = await resp.text();
  return { status: resp.status, text: text.slice(0, maxText) };
}

async function pageDownload(url, maxBytes) {
  const resp = await fetch(url, { credentials: 'include' });
  if (!resp.ok) return { status: resp.status };
  const buf = new Uint8Array(await resp.arrayBuffer());
  if (buf.byteLength > maxBytes) return { status: 413, error: `FILE_TOO_LARGE ${buf.byteLength}B` };
  let bin = '';
  for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
  return { status: resp.status, contentType: resp.headers.get('content-type'), base64: btoa(bin) };
}

// ─── commands ───────────────────────────────────────────────

async function handleCommand(msg) {
  const { id, method, params = {} } = msg;
  const visible = !!params.captchaAction || method === 'page_download';
  const entry = { id, method, label: params.rpcid || params.url || '', time: new Date().toISOString(), status: 'running' };
  if (visible) addLog(entry);
  setState('running');
  try {
    const result = await runCommand(method, params, id);
    if (result?.error) throw new Error(result.error);
    if (visible) { stats.ok++; entry.status = `ok ${result?.status ?? ''}`; }
    await reply({ id, status: result?.status, data: method === 'page_download' || method === 'get_status' || method === 'set_capture' ? result : result?.text });
  } catch (e) {
    const err = e?.message || String(e);
    if (visible) { stats.failed++; entry.status = 'failed'; entry.error = err; }
    stats.lastError = err;
    await reply({ id, error: err });
  } finally {
    if (visible) stats.requests++;
    setState(ws?.readyState === WebSocket.OPEN ? 'idle' : 'off');
    broadcast();
  }
}

async function runCommand(method, p, id) {
  if (method === 'get_status') {
    const tabs = await chrome.tabs.query({ url: MUSIC_URLS });
    const { captureEnabled } = await chrome.storage.local.get(['captureEnabled']);
    return {
      tabs: tabs.map((t) => ({ id: t.id, url: t.url, discarded: t.discarded, status: t.status })),
      captureEnabled: !!captureEnabled, stats,
    };
  }
  if (method === 'set_capture') {
    await chrome.storage.local.set({ captureEnabled: !!p.enabled });
    return { captureEnabled: !!p.enabled };
  }

  const tab = await getMusicTab();
  if (method === 'page_download') {
    return await inPage(tab.id, pageDownload, [p.url, Math.min(p.maxBytes || MAX_DOWNLOAD, MAX_DOWNLOAD)]);
  }

  let token = null;
  if (p.captchaAction) token = await mintCaptcha(tab.id, id, p.captchaAction, p.captchaSitekey || null);

  if (method === 'page_fetch') {
    const url = token ? fillCaptcha(p.url, token) : p.url;
    const body = token ? fillCaptcha(p.body, token) : p.body;
    return await inPage(tab.id, pageFetch, [url, (p.method || 'POST').toUpperCase(), p.headers || {}, body ?? null, MAX_TEXT]);
  }
  if (method === 'batch_rpc') {
    const freq = token ? fillCaptcha(p.freq, token) : p.freq;
    return await inPage(tab.id, pageBatchRpc, [p.path, p.rpcid, freq, MAX_TEXT]);
  }
  throw new Error(`UNKNOWN_METHOD ${method}`);
}

// ─── captures ───────────────────────────────────────────────

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.type === 'CAPTURE') {
    stats.captures++;
    fetch(`${agentHttp}/api/ext/capture`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'x-callback-secret': callbackSecret || '' },
      body: JSON.stringify(msg.rec),
    }).catch(() => {});
    broadcast();
    return false;
  }
  if (msg.type === 'STATUS') {
    chrome.storage.local.get(['captureEnabled']).then(({ captureEnabled }) => {
      sendResponse({ connected: ws?.readyState === WebSocket.OPEN, state, stats, log, captureEnabled: !!captureEnabled });
    });
    return true;
  }
  if (msg.type === 'SET_CAPTURE') {
    chrome.storage.local.set({ captureEnabled: !!msg.enabled }).then(() => sendResponse({ ok: true }));
    return true;
  }
  if (msg.type === 'OPEN_TAB') {
    chrome.tabs.query({ url: MUSIC_URLS }).then((tabs) => {
      if (tabs.length) chrome.tabs.update(tabs[0].id, { active: true });
      else chrome.tabs.create({ url: MUSIC_HOME });
      sendResponse({ ok: true });
    });
    return true;
  }
  if (msg.type === 'RECONNECT') { connect(); sendResponse({ ok: true }); }
  return false;
});

// ─── popup state ────────────────────────────────────────────

function addLog(entry) {
  log.unshift(entry);
  if (log.length > 50) log.pop();
}

function setState(s) {
  state = s;
  const badge = { idle: '●', running: '▶', off: '○' }[s] || '';
  const color = { idle: '#22c55e', running: '#f59e0b', off: '#6b7280' }[s] || '#000';
  chrome.action.setBadgeText({ text: badge });
  chrome.action.setBadgeBackgroundColor({ color });
  broadcast();
}

function broadcast() {
  chrome.runtime.sendMessage({ type: 'STATUS_PUSH' }).catch(() => {});
}
