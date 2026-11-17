// HTTP calls and the live event stream. Every error carries a message meant for the operator.

export class ApiError extends Error {
  constructor(message, status, data) { super(message); this.status = status; this.data = data || {}; }
}

let onUnauthorized = () => {};
export function setUnauthorizedHandler(fn) { onUnauthorized = fn; }

export async function api(path, { method = 'GET', body, raw, headers = {} } = {}) {
  const opts = { method, headers: { ...headers }, credentials: 'same-origin' };
  if (raw !== undefined) opts.body = raw;
  else if (body !== undefined) { opts.body = JSON.stringify(body); opts.headers['Content-Type'] = 'application/json'; }
  let r;
  try { r = await fetch(path, opts); }
  catch (e) { throw new ApiError('The server cannot be reached. Check the network or whether the plotter PC is on.', 0); }
  let data = {};
  const text = await r.text();
  try { data = text ? JSON.parse(text) : {}; } catch (e) { data = { error: text.slice(0, 300) }; }
  if (r.status === 401 && path !== '/api/login') onUnauthorized();
  if (!r.ok || (data.ok === false && data.error)) throw new ApiError(data.error || `Request failed (${r.status})`, r.status, data);
  return data;
}

export const post = (path, body) => api(path, { method: 'POST', body });

// Server-Sent Events with resume. The server sends {type:"reset"} when it cannot replay (restart, fell behind);
// the caller then reloads the snapshot. A ping arrives every 15 s, so silence means the link is gone.
export function connectEvents({ epoch, seq, onEvent, onLink }) {
  let es = null, closed = false, cursor = seq, ep = epoch, retryTimer = null;
  const open = () => {
    if (closed) return;
    es = new EventSource(`/api/events?since=${cursor}&epoch=${encodeURIComponent(ep)}`);
    es.onopen = () => onLink('live');
    es.onmessage = (m) => {
      let ev; try { ev = JSON.parse(m.data); } catch (e) { return; }
      if (ev.type === 'reset') { ep = ev.epoch; cursor = ev.seq; }
      else if (ev.seq) cursor = Math.max(cursor, ev.seq);
      onEvent(ev);
    };
    es.onerror = () => {
      onLink('reconnecting');
      // EventSource retries by itself with Last-Event-ID; if it gave up (closed), reopen with our cursor.
      if (es.readyState === EventSource.CLOSED) { clearTimeout(retryTimer); retryTimer = setTimeout(open, 3000); }
    };
  };
  open();
  return { close() { closed = true; if (es) es.close(); }, reopen(newEpoch, newSeq) { ep = newEpoch; cursor = newSeq; if (es) es.close(); open(); } };
}
