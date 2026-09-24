/* =====================================================================
   Pipedream workflow 1 of 2 — POST /auth/request
   =====================================================================
   Trigger:  HTTP / Webhook, "Return a custom response from your workflow"
   Props:    a Data Store named `db`, and your Gmail account named `gmail`
   Env:      GV_FROM_EMAIL   the Green Vision Gmail address
             GV_VERIFY_URL   workflow 2's trigger URL

   Takes an email address and the onboarding answers from the desktop app,
   mints a single-use token, stores it for fifteen minutes, and sends the
   sign-in email.

   The token is 32 random bytes, not a JWT. Nothing here needs to be
   verified offline, and a JWT would put the profile into a string the
   reader can paste anywhere. An opaque handle to a row leaks nothing if
   the email is forwarded.
   ===================================================================== */

import { defineComponent } from "@pipedream/types";
import crypto from "crypto";
import { brandedEmail, sendMail } from "./_email.js";   // see pipedream/_email.js

export default defineComponent({
  props: {
    db: { type: "data_store" },
    gmail: { type: "app", app: "gmail" }
  },

  async run({ steps, $ }) {
    const body = steps.trigger.event.body || {};
    const email = String(body.email || "").trim().toLowerCase();
    const state = String(body.state || "");
    const profile = body.profile && typeof body.profile === "object" ? body.profile : {};

    if (!/^[^@\s]+@[^@\s]+\.[^@\s]{2,}$/.test(email)) {
      return $.respond({ status: 400, body: { error: "invalid email" } });
    }
    if (!state || state.length < 16) {
      return $.respond({ status: 400, body: { error: "missing state" } });
    }

    /* Rate limit per address. Without it this endpoint is a free mail
       cannon pointed at anyone whose address you can type, and it is our
       Gmail account that gets suspended for sending it. */
    const now = Date.now();
    const rlKey = "rl:" + email;
    const hits = ((await this.db.get(rlKey)) || []).filter(t => now - t < 60_000);
    if (hits.length >= 3) {
      return $.respond({ status: 429, body: { error: "Too many sign-in emails. Wait a minute." } });
    }
    await this.db.set(rlKey, [...hits, now], { ttl: 120 });

    const token = crypto.randomBytes(32).toString("base64url");
    await this.db.set("tok:" + token, { email, state, profile, created: now, used: false },
                      { ttl: 15 * 60 });

    /* The account row. Upserted here rather than after verification so a
       returning planner keeps what they gave last time; blank answers this
       time do not wipe answers from before. `isNew` decides which email
       they get — a welcome, or a sign-in notice. */
    const acctKey = "acct:" + email;
    const existing = await this.db.get(acctKey);
    const isNew = !existing;
    const merged = { ...((existing && existing.profile) || {}) };
    for (const [k, v] of Object.entries(profile)) {
      if (v !== "" && v != null) merged[k] = typeof v === "string" ? v.slice(0, 200) : v;
    }
    await this.db.set(acctKey, {
      email,
      profile: merged,
      created: (existing && existing.created) || now,
      updated: now,
      sign_ins: (existing && existing.sign_ins) || 0
    });

    const verify = process.env.GV_VERIFY_URL;
    if (!verify) return $.respond({ status: 500, body: { error: "GV_VERIFY_URL is not set" } });
    const link = verify.replace(/\/+$/, "") + "/auth/verify?token=" + encodeURIComponent(token);

    const first = merged.name ? String(merged.name).split(" ")[0] : null;
    await sendMail({
      gmail: this.gmail,
      from: process.env.GV_FROM_EMAIL,
      to: email,
      subject: isNew ? "Welcome to Green Vision — confirm your email"
                     : "Your Green Vision sign-in link",
      ...brandedEmail({ kind: isNew ? "welcome" : "signin", name: first, link, profile: merged })
    });

    return $.respond({ status: 200, body: { ok: true, isNew } });
  }
});
