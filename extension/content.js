/**
 * ISOLATED-world relay between background.js and page.js (MAIN world).
 */

// Captcha: background → page → background
chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  if (msg.type !== 'GET_CAPTCHA') return;
  const { requestId, action, sitekey } = msg;
  const timer = setTimeout(() => {
    window.removeEventListener('FMK_CAPTCHA_RESULT', onResult);
    reply({ error: 'CAPTCHA_TIMEOUT (content)' });
  }, 25000);
  function onResult(e) {
    let d = {};
    try { d = JSON.parse(e.detail || '{}'); } catch (err) { return; }
    if (d.requestId !== requestId) return;
    clearTimeout(timer);
    window.removeEventListener('FMK_CAPTCHA_RESULT', onResult);
    reply({ token: d.token, error: d.error });
  }
  window.addEventListener('FMK_CAPTCHA_RESULT', onResult);
  window.dispatchEvent(new CustomEvent('FMK_GET_CAPTCHA', {
    detail: JSON.stringify({ requestId, action, sitekey }),
  }));
  return true;
});

// Capture state: storage → page
function pushCaptureState() {
  chrome.storage.local.get(['captureEnabled']).then(({ captureEnabled }) => {
    window.dispatchEvent(new CustomEvent('FMK_CAPTURE_STATE', {
      detail: JSON.stringify({ enabled: !!captureEnabled }),
    }));
  }).catch(() => {});
}
window.addEventListener('FMK_CAPTURE_STATE_REQ', pushCaptureState);
chrome.storage.onChanged.addListener((changes) => {
  if ('captureEnabled' in changes) pushCaptureState();
});
pushCaptureState();

// Captured requests: page → background
window.addEventListener('FMK_CAPTURE', (e) => {
  let rec;
  try { rec = JSON.parse(e.detail || '{}'); } catch (err) { return; }
  chrome.runtime.sendMessage({ type: 'CAPTURE', rec }).catch(() => {});
});
