const { request } = require("@playwright/test");

// API requests need the per-server session token, delivered via the
// same-origin bootstrap HTML (window.__MCT_TOKEN__). This bootstraps it the
// legitimate way — GET / like a browser, parse the injected token, then send
// it on X-MCT-Token as Shell.sessionHeaders does inside the page.
async function apiContext(baseURL) {
  const probe = await request.newContext({ baseURL });
  const res = await probe.get("/");
  const html = await res.text();
  await probe.dispose();
  const m = html.match(/__MCT_TOKEN__\s*=\s*"([^"]+)"/);
  if (!m) throw new Error("session token not found in bootstrap HTML");
  return request.newContext({
    baseURL,
    extraHTTPHeaders: { "X-MCT-Token": m[1] },
  });
}

module.exports = { apiContext };
