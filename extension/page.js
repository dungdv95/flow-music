/**
 * Runs in the page's MAIN world on flowmusic.app.
 *
 * 1. reCAPTCHA: mints a token through the page's own grecaptcha when the
 *    agent asks (GET_CAPTCHA → CAPTCHA_RESULT events via content.js).
 * 2. Capture mode (off by default): records the page's own fetch/XHR request
 *    bodies and responses so a UI action can become a recipe. Headers and
 *    cookies are never read.
 */
(function () {
  'use strict';
  if (window.__fmkPageLoaded) return;
  window.__fmkPageLoaded = true;

  // ─── reCAPTCHA ────────────────────────────────────────────

  function findSitekey(node, depth) {
    if (!node || typeof node !== 'object' || depth > 5) return null;
    if (typeof node.sitekey === 'string') return node.sitekey;
    for (const key of Object.keys(node)) {
      let found = null;
      try { found = findSitekey(node[key], depth + 1); } catch (e) { /* cross-origin */ }
      if (found) return found;
    }
    return null;
  }

  function waitFor(check, timeout) {
    return new Promise((resolve, reject) => {
      const start = Date.now();
      (function loop() {
        const v = check();
        if (v) return resolve(v);
        if (Date.now() - start > timeout) return reject(new Error('timeout'));
        setTimeout(loop, 200);
      })();
    });
  }

  let mintTail = Promise.resolve();

  async function mintCaptcha(action, sitekeyHint) {
    const api = await waitFor(() => {
      const g = window.grecaptcha;
      if (g?.enterprise?.execute) return g.enterprise;
      if (g?.execute) return g;
      return null;
    }, 20000).catch(() => { throw new Error('NO_GRECAPTCHA on page'); });
    const sitekey = sitekeyHint || findSitekey(window.___grecaptcha_cfg?.clients, 0);
    if (!sitekey) throw new Error('NO_SITEKEY — set captcha_sitekey in the recipe');
    await new Promise((r) => { try { api.ready(r); } catch (e) { r(); } setTimeout(r, 3000); });
    const token = await Promise.race([
      api.execute(sitekey, { action }),
      new Promise((_, rej) => setTimeout(() => rej(new Error('CAPTCHA_TIMEOUT')), 15000)),
    ]);
    if (!token) throw new Error('CAPTCHA_EMPTY');
    return String(token);
  }

  window.addEventListener('FMK_GET_CAPTCHA', (e) => {
    const { requestId, action, sitekey } = JSON.parse(e.detail || '{}');
    const prev = mintTail.catch(() => {});
    mintTail = prev.then(async () => {
      let out;
      try {
        out = { requestId, token: await mintCaptcha(action, sitekey) };
      } catch (err) {
        out = { requestId, error: err.message || String(err) };
      }
      window.dispatchEvent(new CustomEvent('FMK_CAPTCHA_RESULT', { detail: JSON.stringify(out) }));
    });
  });

  // ─── Capture mode ─────────────────────────────────────────

  let captureOn = false;
  const MAX_TEXT = 2000000;
  const SKIP = /recaptcha|google-analytics|googletagmanager|\/gen_204|play\.google\.com\/log|\/log\?|\/jserror|\/csp|\.(js|css|woff2?|png|jpe?g|svg|webp|ico)(\?|$)/i;

  window.addEventListener('FMK_CAPTURE_STATE', (e) => {
    try { captureOn = !!JSON.parse(e.detail || '{}').enabled; } catch (err) { captureOn = false; }
  });
  window.dispatchEvent(new CustomEvent('FMK_CAPTURE_STATE_REQ'));

  function wanted(url, method) {
    if (!captureOn || !url) return false;
    if (SKIP.test(url)) return false;
    return method !== 'GET' || url.includes('batchexecute') || url.includes('/api/');
  }

  function absUrl(u) {
    try { return new URL(u, location.href).href; } catch (e) { return String(u); }
  }

  async function bodyToText(body) {
    if (body == null) return null;
    if (typeof body === 'string') return body;
    if (body instanceof URLSearchParams) return body.toString();
    if (body instanceof FormData) {
      const parts = [];
      for (const [k, v] of body.entries()) {
        parts.push(`${k}=${typeof v === 'string' ? v : `<file ${v.name} ${v.size}B ${v.type}>`}`);
      }
      return `<FormData> ${parts.join(' & ')}`;
    }
    if (body instanceof Blob) {
      if (body.size < 200000 && /json|text/.test(body.type)) return await body.text();
      return `<binary Blob ${body.size}B ${body.type}>`;
    }
    if (body instanceof ArrayBuffer || ArrayBuffer.isView(body)) {
      return `<binary ${body.byteLength}B>`;
    }
    return `<${Object.prototype.toString.call(body)}>`;
  }

  function emit(rec) {
    rec.pageUrl = location.href;
    window.dispatchEvent(new CustomEvent('FMK_CAPTURE', { detail: JSON.stringify(rec) }));
  }

  const origFetch = window.fetch;
  window.fetch = async function (input, init) {
    const url = absUrl(typeof input === 'string' ? input : input?.url || String(input));
    const method = String(init?.method || (input instanceof Request ? input.method : 'GET')).toUpperCase();
    const record = wanted(url, method);
    let reqText = null;
    let reqType = null;
    if (record) {
      try {
        if (init && 'body' in init) reqText = await bodyToText(init.body);
        else if (input instanceof Request) reqText = await input.clone().text();
        const h = new Headers(init?.headers || (input instanceof Request ? input.headers : undefined));
        reqType = h.get('content-type');
      } catch (e) { reqText = `<unreadable: ${e.message}>`; }
    }
    const resp = await origFetch.apply(this, arguments);
    if (record) {
      resp.clone().text().then((text) => emit({
        url, method, status: resp.status, requestBody: reqText, requestContentType: reqType,
        responseContentType: resp.headers.get('content-type'), responseText: text.slice(0, MAX_TEXT),
      })).catch(() => {});
    }
    return resp;
  };

  const XO = XMLHttpRequest.prototype.open;
  const XS = XMLHttpRequest.prototype.send;
  const XH = XMLHttpRequest.prototype.setRequestHeader;
  XMLHttpRequest.prototype.open = function (method, url) {
    this.__fmk = { method: String(method).toUpperCase(), url: absUrl(url), ctype: null };
    return XO.apply(this, arguments);
  };
  XMLHttpRequest.prototype.setRequestHeader = function (name, value) {
    if (this.__fmk && String(name).toLowerCase() === 'content-type') this.__fmk.ctype = value;
    return XH.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function (body) {
    const meta = this.__fmk;
    if (meta && wanted(meta.url, meta.method)) {
      const xhr = this;
      bodyToText(body).then((reqText) => {
        xhr.addEventListener('loadend', () => {
          let text = '';
          try {
            text = (xhr.responseType === '' || xhr.responseType === 'text')
              ? xhr.responseText : `<responseType ${xhr.responseType}>`;
          } catch (e) { text = '<unreadable>'; }
          emit({
            url: meta.url, method: meta.method, status: xhr.status, requestBody: reqText,
            requestContentType: meta.ctype, responseContentType: xhr.getResponseHeader('content-type'),
            responseText: String(text).slice(0, MAX_TEXT),
          });
        });
      }).catch(() => {});
    }
    return XS.apply(this, arguments);
  };
})();
