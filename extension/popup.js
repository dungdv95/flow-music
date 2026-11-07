const $ = (id) => document.getElementById(id);
let captureEnabled = false;

function esc(s) {
  return String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

async function refresh() {
  const s = await chrome.runtime.sendMessage({ type: 'STATUS' }).catch(() => null);
  if (!s) return;
  $('dot').className = `dot ${s.state === 'running' ? 'run' : s.connected ? 'on' : ''}`;
  $('state').textContent = s.state;
  $('agent').textContent = s.connected ? 'connected' : 'not running (port 9300)';
  $('counts').textContent = `${s.stats.ok} / ${s.stats.failed}`;
  $('err').textContent = s.stats.lastError ? s.stats.lastError.slice(0, 60) : '—';
  $('cap-count').textContent = `${s.stats.captures} captured this session`;
  captureEnabled = s.captureEnabled;
  $('capture').textContent = captureEnabled ? 'Stop' : 'Start';
  $('rec').textContent = captureEnabled ? '● REC' : '';
  $('capture-box').className = `capture ${captureEnabled ? 'on' : ''}`;
  $('log').innerHTML = s.log.slice(0, 20).map((e) =>
    `<li><b>${esc(e.status)}</b> ${esc(e.method)} <span class="muted">${esc(e.label).slice(0, 80)}</span>${e.error ? `<br><span style="color:#ef4444">${esc(e.error)}</span>` : ''}</li>`
  ).join('') || '<li class="muted">nothing yet</li>';
}

$('capture').onclick = async () => {
  await chrome.runtime.sendMessage({ type: 'SET_CAPTURE', enabled: !captureEnabled });
  refresh();
};
$('open').onclick = () => chrome.runtime.sendMessage({ type: 'OPEN_TAB' });
$('reconnect').onclick = () => chrome.runtime.sendMessage({ type: 'RECONNECT' }).then(refresh);
chrome.runtime.onMessage.addListener((m) => { if (m.type === 'STATUS_PUSH') refresh(); });
refresh();
