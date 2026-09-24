/* =====================================================================
   Pipedream workflow 2 of 2 — everything after the email is sent
   =====================================================================
   Trigger:  HTTP / Webhook, "Return a custom response from your workflow"
   Props:    the SAME Data Store as workflow 1, named `db`
             your Gmail account, named `gmail`   (for the sign-in notice)
   Env:      none — the sender is the connected Gmail account, and the
             verify link is built from the request's own host

   One workflow, path-routed, because Pipedream gives a workflow a single
   trigger URL and a second one would mean a second URL to configure.

     POST /auth/request            the app, asking for a link
     GET  /auth/verify?token=      the link in the email
     POST /auth/exchange           the app, redeeming it for a session

     GET  /me                      profile + counts
     PUT  /me                      update profile / onboarding answers
     GET  /projects                this user's saved designs
     PUT  /projects                save or update one
     POST /projects/delete         remove one
     GET  /library                 designs other people published
     POST /library                 publish one of yours
     GET  /chats                   assistant history, newest last
     POST /chats                   append a turn
     POST /chats/clear             forget the conversation
     GET  /history                 places searched
     POST /history                 append a search

   Everything except /auth/* and /library requires the session as
   `Authorization: Bearer <session>`.

   Why the emailed GET does not spend the token: Outlook Safe Links,
   corporate mail filters and antivirus all follow links before a human
   does. If the GET consumed it, the scanner would burn it and the reader
   would click a dead link. The GET only hands the token onward; nothing
   is spent until the app POSTs, which no scanner does.
   ===================================================================== */

import { defineComponent } from "@pipedream/types";
import crypto from "crypto";
import { brandedEmail, sendMail, escapeHtml } from "./_email.js";  // paste _email.js above this

/* The connected Gmail account. Gmail sends as the authenticated user
   regardless of what the From header claims, so this is the display
   address and nothing more — hard-coded rather than held in an
   environment variable, which was one more thing to set and one more
   way to deploy a workflow that looks fine and cannot send. */
const FROM_EMAIL = "greenvision.support@gmail.com";

const LIMITS = {
  projects: 60,        // per user
  chats: 120,          // turns kept; the assistant's memory window
  history: 100,        // searches
  library: 200,        // published designs listed
  bodyBytes: 400_000   // a design with a few hundred items is ~50 KB
};

export default defineComponent({
  props: { db: { type: "data_store" }, gmail: { type: "app", app: "gmail" } },

  async run({ steps, $ }) {
    const ev = steps.trigger.event;
    const method = (ev.method || "GET").toUpperCase();
    const path = (ev.path || "").replace(/\/+$/, "");
    const body = ev.body && typeof ev.body === "object" ? ev.body : {};
    const db = this.db;

    const json = (status, obj) => $.respond({ status, body: obj });
    const html = (status, s) => $.respond({ status, headers: { "content-type": "text/html; charset=utf-8" }, body: s });


    /* ---------- ask for a link ----------
       Lives here rather than in its own workflow because the app has ONE
       base URL. Splitting request and exchange across two Pipedream
       workflows means two hosts, and the client would need to know which
       call goes where — a configuration mistake waiting to happen for no
       benefit. */
    if (method === "POST" && path.endsWith("/auth/request")) {
      const email = String(body.email || "").trim().toLowerCase();
      const state = String(body.state || "");
      const profile = body.profile && typeof body.profile === "object" ? body.profile : {};

      if (!/^[^@\s]+@[^@\s]+\.[^@\s]{2,}$/.test(email)) {
        return json(400, { error: "invalid email" });
      }
      if (!state || state.length < 16) return json(400, { error: "missing state" });

      /* Rate limit per address, or this endpoint is a free mail cannon
         pointed at anyone whose address you can type — and it is our Gmail
         account that gets suspended for sending it. */
      const now = Date.now();
      const rlKey = "rl:" + email;
      const hits = ((await db.get(rlKey)) || []).filter(t => now - t < 60000);
      if (hits.length >= 3) {
        return json(429, { error: "Too many sign-in emails. Wait a minute." });
      }
      await db.set(rlKey, [...hits, now], { ttl: 120 });

      const token = crypto.randomBytes(32).toString("base64url");
      await db.set("tok:" + token, { email, state, profile, created: now, used: false },
                   { ttl: 15 * 60 });

      const acctKey2 = "acct:" + email;
      const existing = await db.get(acctKey2);
      const isNew = !existing;
      const merged = { ...((existing && existing.profile) || {}) };
      for (const [k, v] of Object.entries(profile)) {
        if (v !== "" && v != null) merged[k] = typeof v === "string" ? v.slice(0, 200) : v;
      }
      await db.set(acctKey2, {
        email, profile: merged,
        created: (existing && existing.created) || now,
        updated: now, sign_ins: (existing && existing.sign_ins) || 0
      });

      /* The verify link points back at THIS workflow. Built from the
         request's own host so there is no URL to keep in sync — one fewer
         environment variable, and one fewer way to deploy a broken link. */
      const host = (ev.headers && (ev.headers.host || ev.headers.Host)) || "";
      const base = process.env.GV_VERIFY_URL || (host ? "https://" + host : "");
      if (!base) return json(500, { error: "cannot determine my own URL" });
      const link = base.replace(/\/+$/, "") + "/auth/verify?token=" + encodeURIComponent(token);

      const first = merged.name ? String(merged.name).split(" ")[0] : null;
      await sendMail({
        gmail: this.gmail, from: FROM_EMAIL, to: email,
        subject: isNew ? "Welcome to Green Vision — confirm your email"
                       : "Your Green Vision sign-in link",
        ...brandedEmail({ kind: isNew ? "welcome" : "signin", name: first, link, profile: merged })
      });

      return json(200, { ok: true, isNew });
    }

    /* ---------- the click ---------- */
    if (method === "GET" && path.endsWith("/auth/verify")) {
      const token = (ev.query && ev.query.token) || "";
      const rec = token ? await db.get("tok:" + token) : null;
      if (!rec)      return html(200, page("This link has expired",
        "Sign-in links last fifteen minutes and work once. Open Green Vision and ask for a new one."));
      if (rec.used)  return html(200, page("This link has already been used",
        "For your security each link works once. Open Green Vision and ask for a new one."));

      const deep = "greenvision://auth?token=" + encodeURIComponent(token) +
                   "&state=" + encodeURIComponent(rec.state);
      return html(200, page("Opening Green Vision…",
        "If nothing happens, use the button below. You can close this tab once the app has signed you in.",
        deep));
    }

    /* ---------- the exchange ---------- */
    if (method === "POST" && path.endsWith("/auth/exchange")) {
      const token = String(body.token || ""), state = String(body.state || "");
      const rec = token ? await db.get("tok:" + token) : null;
      if (!rec)     return json(400, { error: "expired or unknown token" });
      if (rec.used) return json(400, { error: "token already used" });

      const a = Buffer.from(state), b = Buffer.from(rec.state || "");
      if (a.length !== b.length || !crypto.timingSafeEqual(a, b)) {
        return json(400, { error: "state mismatch" });
      }
      // Spent, but kept briefly so a double-submit reads "already used"
      // rather than "unknown token" — the same event, described accurately.
      await db.set("tok:" + token, { ...rec, used: true }, { ttl: 300 });

      const acctKey = "acct:" + rec.email;
      const acct = (await db.get(acctKey)) || { email: rec.email, created: Date.now() };
      const session = crypto.randomBytes(32).toString("base64url");
      const isFirst = !acct.sign_ins;
      const updated = {
        ...acct, email: rec.email,
        profile: acct.profile || rec.profile || {},
        sign_ins: (acct.sign_ins || 0) + 1,
        last_sign_in: Date.now()
      };
      await db.set(acctKey, updated);
      await db.set("sess:" + session, { email: rec.email, issued: Date.now() },
                   { ttl: 60 * 60 * 24 * 90 });

      /* Tell them it happened. An account that never mentions its own
         sign-ins cannot show you one you did not make. Skipped on the very
         first, because the welcome email two minutes ago already said it. */
      if (!isFirst) {
        try {
          const p = updated.profile || {};
          await sendMail({
            gmail: this.gmail, from: FROM_EMAIL, to: rec.email,
            subject: "You signed in to Green Vision",
            ...brandedEmail({ kind: "notice", name: p.name ? String(p.name).split(" ")[0] : null,
                              when: new Date().toUTCString(), device: "the Windows app" })
          });
        } catch (e) { /* a notice that fails must not fail the sign-in */ }
      }

      return json(200, { ok: true, email: rec.email, session,
                         profile: updated.profile, sign_ins: updated.sign_ins,
                         isNew: isFirst });
    }

    /* ---------- the public library ---------- */
    if (method === "GET" && path.endsWith("/library")) {
      const index = (await db.get("lib:index")) || [];
      return json(200, { ok: true, designs: index.slice(0, LIMITS.library) });
    }

    /* ---------- everything below needs a session ---------- */
    const auth = (ev.headers && (ev.headers.authorization || ev.headers.Authorization)) || "";
    const session = auth.replace(/^Bearer\s+/i, "").trim();
    const sess = session ? await db.get("sess:" + session) : null;
    if (!sess) return json(401, { error: "sign in first" });
    const email = sess.email;
    const key = suffix => "u:" + email + ":" + suffix;

    if (JSON.stringify(body).length > LIMITS.bodyBytes) {
      return json(413, { error: "too large" });
    }

    const acct = (await db.get("acct:" + email)) || { email, profile: {} };

    /* ---------- profile ---------- */
    if (path.endsWith("/me")) {
      if (method === "GET") {
        const [projects, chats, history] = await Promise.all([
          db.get(key("projects")), db.get(key("chats")), db.get(key("history"))
        ]);
        return json(200, { ok: true, email, profile: acct.profile || {},
          created: acct.created, sign_ins: acct.sign_ins || 0,
          counts: { projects: (projects || []).length, chats: (chats || []).length,
                    history: (history || []).length } });
      }
      if (method === "PUT") {
        const p = { ...(acct.profile || {}) };
        for (const [k, v] of Object.entries(body.profile || {})) {
          if (v === null) delete p[k];
          else if (v !== "") p[k] = typeof v === "string" ? v.slice(0, 2000) : v;
        }
        await db.set("acct:" + email, { ...acct, profile: p, updated: Date.now() });
        return json(200, { ok: true, profile: p });
      }
    }

    /* ---------- projects ---------- */
    if (path.endsWith("/projects")) {
      const list = (await db.get(key("projects"))) || [];
      if (method === "GET") return json(200, { ok: true, projects: list });
      if (method === "PUT") {
        const d = body.design;
        if (!d || !d.id) return json(400, { error: "no design" });
        const row = { id: d.id, name: d.name || "Untitled", place: d.place || "",
                      city: d.city || "", goal: d.goal || "park",
                      n_items: (d.items || []).length,
                      n_trees: (d.items || []).filter(i => i.k === "tree").length,
                      area_m2: d.plot ? Math.round(d.plot.area_m2 || 0) : 0,
                      updated: Date.now(), design: d };
        const next = [row, ...list.filter(x => x.id !== d.id)].slice(0, LIMITS.projects);
        await db.set(key("projects"), next);
        return json(200, { ok: true, saved: row.id, count: next.length });
      }
    }
    if (method === "POST" && path.endsWith("/projects/delete")) {
      const list = (await db.get(key("projects"))) || [];
      const next = list.filter(x => x.id !== body.id);
      await db.set(key("projects"), next);
      return json(200, { ok: true, count: next.length });
    }

    /* ---------- publishing to the shared library ---------- */
    if (method === "POST" && path.endsWith("/library")) {
      const d = body.design;
      if (!d || !d.id) return json(400, { error: "no design" });
      const p = acct.profile || {};
      const entry = {
        id: d.id, name: d.name || "Untitled",
        by: p.name || email.split("@")[0],
        org: p.org || "", role: p.role || "",
        city: d.city || p.city || "", place: d.place || "",
        goal: d.goal || "park",
        n_trees: (d.items || []).filter(i => i.k === "tree").length,
        area_m2: d.plot ? Math.round(d.plot.area_m2 || 0) : 0,
        published: Date.now(), design: d
      };
      const index = (await db.get("lib:index")) || [];
      const next = [entry, ...index.filter(x => x.id !== d.id)].slice(0, LIMITS.library);
      await db.set("lib:index", next);
      return json(200, { ok: true, published: entry.id, count: next.length });
    }

    /* ---------- the assistant's memory ---------- */
    if (path.endsWith("/chats")) {
      const log = (await db.get(key("chats"))) || [];
      if (method === "GET") return json(200, { ok: true, chats: log });
      if (method === "POST") {
        const turn = { role: body.role === "bot" ? "bot" : "me",
                       text: String(body.text || "").slice(0, 4000),
                       place: body.place || "", intent: body.intent || "",
                       at: Date.now() };
        const next = [...log, turn].slice(-LIMITS.chats);
        await db.set(key("chats"), next);
        return json(200, { ok: true, count: next.length });
      }
    }
    if (method === "POST" && path.endsWith("/chats/clear")) {
      await db.set(key("chats"), []);
      return json(200, { ok: true, count: 0 });
    }

    /* ---------- search history ---------- */
    if (path.endsWith("/history")) {
      const list = (await db.get(key("history"))) || [];
      if (method === "GET") return json(200, { ok: true, history: list });
      if (method === "POST") {
        const row = { place: String(body.place || "").slice(0, 200),
                      lat: body.lat, lon: body.lon, at: Date.now() };
        if (!row.place) return json(400, { error: "no place" });
        const next = [row, ...list.filter(x => x.place !== row.place)].slice(0, LIMITS.history);
        await db.set(key("history"), next);
        return json(200, { ok: true, count: next.length });
      }
    }

    return json(404, { error: "not found" });
  }
});

/* The two pages a reader can land on. Same brand as the email, no external
   assets, and it says what is happening rather than spinning. */
function page(title, body, deep) {
  const btn = deep ? `<a class="b" href="${deep}">Open Green Vision</a>` : "";
  const go = deep ? `<script>setTimeout(function(){location.href=${JSON.stringify(deep)};},400);<\/script>` : "";
  return `<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>${escapeHtml(title)} · Green Vision</title>
<style>
 body{margin:0;background:#f2f5f3;font:15px/1.6 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
      color:#33473f;display:grid;place-items:center;min-height:100vh;padding:24px;}
 .c{max-width:460px;background:#fff;border:1px solid #dfe6e1;border-radius:16px;padding:28px;text-align:center;}
 .l{width:48px;height:48px;border-radius:14px;display:block;margin:0 auto 16px;}
 h1{margin:0 0 10px;font-size:20px;color:#0f2a22;font-weight:800;}
 p{margin:0 0 18px;font-size:14px;}
 .b{display:block;background:#0e9f6e;color:#fff;text-decoration:none;padding:13px;border-radius:12px;font-weight:700;}
 .f{margin-top:18px;font-size:11.5px;color:#8a9a92;}
</style></head><body>
 <div class="c"><img class="l" src="https://green-vision-india.netlify.app/brand/green-vision-mark-128.png" alt="Green Vision" width="48" height="48">
  <h1>${escapeHtml(title)}</h1><p>${escapeHtml(body)}</p>${btn}
  <div class="f">Green Vision — tree-planting prioritisation for Indian cities.</div>
 </div>${go}</body></html>`;
}
