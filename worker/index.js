/**
 * Sears Island Flora Survey — contributor endpoint.
 *
 * WHAT THIS IS, AND WHAT IT DELIBERATELY IS NOT
 *
 * It is an inbox. Contributors sign in on the static site and submit three kinds
 * of thing — a field verification, a mark that a photograph is surplus, and a
 * photograph — and every one of them lands in a queue here for the survey
 * pipeline to collect on the schedule it already runs on.
 *
 * It is NOT a database of record, and it must never become one. data/observations.json
 * has exactly one writer, the pipeline, and that is the reason this project has
 * never had a merge conflict in a file three separate automated jobs touch nightly.
 * A web app that wrote to the survey directly would end that. So this Worker holds
 * no survey data, answers no question about what the survey contains, and can be
 * wiped and redeployed without losing anything that has already been collected.
 *
 * WHY THE PIPELINE RE-CHECKS EVERYTHING THIS FILE CHECKS
 *
 * The capability table below is a copy. scripts/plantdb.py holds the original and
 * `inbox-pull` applies it again to every submission before writing anything. That
 * is not redundancy for its own sake: this Worker is deployed separately from the
 * repository and can be changed without any review, while the project's central
 * claim is that a `verified` field records a real human field check. The rule
 * about who may create one therefore lives in the tested, version-controlled half
 * of the system, and no mistake out here can move it.
 *
 * tests/test_contributor_inbox.py reads this file and fails if the two drift.
 */

// --- MIRROR OF CAPABILITIES IN scripts/plantdb.py. Keep them identical. ---
const CAPABILITIES = {
  upload: ["contributor", "verifier", "admin"],
  verify: ["verifier", "admin"],
  redundant: ["admin"],
  override: ["admin"],
  drain: ["pipeline", "admin"],
};
// --- END MIRROR ---

const MAX_PHOTO_BYTES = 25 * 1024 * 1024;   // a full-resolution phone photo, with room
const MAX_PER_HOUR = 120;                   // per contributor; a day in the field is ~50
const VERIFY_STATUS = ["confirmed", "corrected", "rejected", "revisit"];

const may = (role, capability) => (CAPABILITIES[capability] || []).includes(role);

/** Hex SHA-256. Tokens are stored only as this, so the database is not a key ring. */
async function sha256(s) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function cors(env, request) {
  // An allowlist, not "*": these endpoints write to a survey, and the browser is
  // the only thing standing between a token in someone's localStorage and any
  // page that can talk to this origin.
  const allowed = (env.ALLOWED_ORIGINS || "").split(",").map((s) => s.trim()).filter(Boolean);
  const origin = request.headers.get("Origin") || "";
  const ok = allowed.includes(origin);
  return {
    "Access-Control-Allow-Origin": ok ? origin : allowed[0] || "null",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, Content-Type",
    "Access-Control-Max-Age": "86400",
    "Vary": "Origin",
  };
}

const json = (body, status, headers) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });

/**
 * Who is calling. The name and role come from the database row the token hashes
 * to — never from anything the client sent. That is the whole point: a
 * contributor cannot claim to be someone else, and a verification cannot be
 * unattributed, because the browser never gets a say in either.
 */
async function identify(request, env) {
  const auth = request.headers.get("Authorization") || "";
  const token = auth.startsWith("Bearer ") ? auth.slice(7).trim() : "";
  if (!token) return null;
  const row = await env.DB.prepare(
    "SELECT id, name, role, active FROM contributors WHERE token_sha256 = ?",
  ).bind(await sha256(token)).first();
  if (!row || !row.active) return null;
  return row;
}

/** Too many submissions too fast — a stuck retry loop, not a person in a field. */
async function overRate(env, who) {
  const row = await env.DB.prepare(
    "SELECT COUNT(*) AS n FROM submissions WHERE by_id = ? AND submitted > datetime('now', '-1 hour')",
  ).bind(who.id).first();
  return (row?.n || 0) >= MAX_PER_HOUR;
}

async function queue(env, who, kind, payload, hasPhoto) {
  const id = crypto.randomUUID();
  await env.DB.prepare(
    `INSERT INTO submissions (id, kind, by_id, by_name, role, payload, has_photo, submitted, status)
     VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), 'pending')`,
  ).bind(id, kind, who.id, who.name, who.role, JSON.stringify(payload), hasPhoto ? 1 : 0).run();
  return id;
}

/** A submission the pipeline can act on. `by` and `role` are the Worker's, not the client's. */
function submissionOut(row) {
  return {
    id: row.id,
    kind: row.kind,
    by: row.by_name,
    role: row.role,
    submitted: row.submitted.replace(" ", "T") + "Z",
    ...JSON.parse(row.payload),
  };
}

async function handle(request, env) {
  const url = new URL(request.url);
  const path = url.pathname.replace(/\/+$/, "");
  const who = await identify(request, env);

  if (!who) return json({ error: "not signed in" }, 401);

  // --- Who am I? Used by the site to decide what to even offer. -------------
  if (path === "/api/me" && request.method === "GET") {
    await env.DB.prepare("UPDATE contributors SET last_seen = datetime('now') WHERE id = ?")
      .bind(who.id).run();
    return json({
      name: who.name,
      role: who.role,
      can: Object.keys(CAPABILITIES).filter((c) => may(who.role, c)),
    }, 200);
  }

  // --- What happened to what I submitted? -----------------------------------
  // The inbox's version of the steward sheet's `recorded?` column. A refusal that
  // exists only in a CI log leaves the person who walked out there believing it
  // was recorded, which is the failure that column was added to prevent.
  if (path === "/api/receipts" && request.method === "GET") {
    const { results } = await env.DB.prepare(
      `SELECT id, kind, payload, status, detail, submitted, settled FROM submissions
       WHERE by_id = ? ORDER BY submitted DESC LIMIT 50`,
    ).bind(who.id).all();
    return json({
      receipts: (results || []).map((r) => ({
        id: r.id, kind: r.kind, status: r.status, detail: r.detail,
        submitted: r.submitted, settled: r.settled,
        file: (JSON.parse(r.payload).file) || (JSON.parse(r.payload).filename) || "",
      })),
    }, 200);
  }

  // --- The walks I have uploaded, and what became of them --------------------
  // Identification runs on the nightly Batch API submission, so a contributor who
  // uploads 80 photographs on a Saturday afternoon sees nothing on the site until
  // the next day. Without somewhere to look, that is indistinguishable from
  // having lost them.
  if (path === "/api/walks" && request.method === "GET") {
    const { results } = await env.DB.prepare(
      `SELECT payload, status, detail, submitted FROM submissions
       WHERE by_id = ? AND kind = 'upload' ORDER BY submitted DESC LIMIT 1000`,
    ).bind(who.id).all();
    const walks = new Map();
    for (const r of results || []) {
      const p = JSON.parse(r.payload);
      const key = p.walk || r.submitted.slice(0, 10);
      if (!walks.has(key)) {
        walks.set(key, { walk: key, submitted: r.submitted, queued: 0,
                         recorded: 0, refused: 0, refusals: [] });
      }
      const w = walks.get(key);
      w[r.status === "pending" ? "queued" : r.status] += 1;
      if (r.status === "refused" && w.refusals.length < 25) {
        w.refusals.push({ file: p.filename, why: r.detail });
      }
      if (r.submitted < w.submitted) w.submitted = r.submitted;
    }
    return json({ walks: [...walks.values()] }, 200);
  }

  // --- Record a field check -------------------------------------------------
  if (path === "/api/verify" && request.method === "POST") {
    if (!may(who.role, "verify")) {
      return json({ error: `a ${who.role} cannot record a field verification` }, 403);
    }
    const body = await request.json().catch(() => null);
    if (!body?.file) return json({ error: "which record?" }, 400);
    // A withdrawal is an empty status, and is allowed — someone who recorded a
    // check may take it back. Anything else has to be one of the four verdicts.
    if (body.status && !VERIFY_STATUS.includes(body.status)) {
      return json({ error: `status must be one of ${VERIFY_STATUS.join(", ")}` }, 400);
    }
    if (body.status === "corrected" && !body.species_id) {
      return json({ error: "'corrected' needs the species it actually is" }, 400);
    }
    if (await overRate(env, who)) return json({ error: "too many submissions this hour" }, 429);
    const id = await queue(env, who, "verify", {
      file: String(body.file),
      status: body.status ? String(body.status) : "",
      species_id: body.species_id ? String(body.species_id) : "",
      notes: body.notes ? String(body.notes).slice(0, 2000) : "",
      date: body.date ? String(body.date).slice(0, 10) : "",
    }, false);
    return json({ id, queued: true }, 202);
  }

  // --- Curation: edit, withdraw, restore, add a catalogue entry -------------
  // All four sit behind the `redundant` (admin) grant rather than each inventing
  // its own. The pipeline decides what any of them may actually change — notably
  // that an edit can never overwrite the machine's own identification.
  const CURATION = { "/api/edit": "edit", "/api/withdraw": "withdraw",
                     "/api/restore": "restore", "/api/species": "species" };
  if (CURATION[path] && request.method === "POST") {
    if (!may(who.role, "redundant")) {
      return json({ error: `a ${who.role} cannot curate entries` }, 403);
    }
    const body = await request.json().catch(() => null);
    const kind = CURATION[path];
    if (kind !== "species" && !body?.file) return json({ error: "which record?" }, 400);
    if (kind === "withdraw" && !(body?.reason || "").trim()) {
      return json({ error: "a withdrawal needs a reason — it is the only record "
                         + "of why this left the survey" }, 400);
    }
    if (kind === "species" && !(body?.species_id || "").trim()) {
      return json({ error: "a catalogue entry needs an id" }, 400);
    }
    if (await overRate(env, who)) return json({ error: "too many submissions this hour" }, 429);
    const payload = { ...body };
    delete payload.by;             // never client-supplied; see identify()
    delete payload.role;
    const id = await queue(env, who, kind, payload, false);
    return json({ id, queued: true }, 202);
  }

  // --- Mark a photograph surplus within one find ----------------------------
  if (path === "/api/redundant" && request.method === "POST") {
    if (!may(who.role, "redundant")) {
      return json({ error: `a ${who.role} cannot mark photographs surplus` }, 403);
    }
    const body = await request.json().catch(() => null);
    if (!body?.file) return json({ error: "which photograph?" }, 400);
    if (!body.unmark && !body.of) {
      return json({ error: "which photograph represents the find?" }, 400);
    }
    if (await overRate(env, who)) return json({ error: "too many submissions this hour" }, 429);
    const id = await queue(env, who, "redundant", {
      file: String(body.file),
      of: body.of ? String(body.of) : "",
      species_id: body.species_id ? String(body.species_id) : "",
      notes: body.notes ? String(body.notes).slice(0, 2000) : "",
      unmark: !!body.unmark,
    }, false);
    return json({ id, queued: true }, 202);
  }

  // --- Add a photograph from the field --------------------------------------
  if (path === "/api/upload" && request.method === "POST") {
    if (!may(who.role, "upload")) {
      return json({ error: `a ${who.role} cannot upload` }, 403);
    }
    const len = Number(request.headers.get("Content-Length") || 0);
    if (len > MAX_PHOTO_BYTES) {
      return json({ error: `photograph is larger than ${MAX_PHOTO_BYTES / 1e6} MB` }, 413);
    }
    if (await overRate(env, who)) return json({ error: "too many submissions this hour" }, 429);

    const form = await request.formData().catch(() => null);
    const photo = form?.get("photo");
    if (!photo || typeof photo === "string") return json({ error: "no photograph" }, 400);
    if (photo.size > MAX_PHOTO_BYTES) {
      return json({ error: `photograph is larger than ${MAX_PHOTO_BYTES / 1e6} MB` }, 413);
    }

    const id = crypto.randomUUID();
    // The bucket bound here is NOT the public one the site serves thumbnails
    // from. These are full-resolution originals that still carry their EXIF, and
    // the survey strips EXIF from everything it publishes precisely so that
    // contributor camera serials and device identifiers do not go out.
    //
    // The object is kept after collection rather than deleted: for a photograph
    // that arrived through Drive, Drive is the archive of originals, and a
    // photograph that arrived this way has no other archive at all. In a cloud
    // pipeline run, photos/ lives only as long as the runner.
    await env.INBOX.put(`originals/${id}`, photo.stream(), {
      httpMetadata: { contentType: photo.type || "image/jpeg" },
    });
    await env.DB.prepare(
      `INSERT INTO submissions (id, kind, by_id, by_name, role, payload, has_photo, submitted, status)
       VALUES (?, 'upload', ?, ?, ?, ?, 1, datetime('now'), 'pending')`,
    ).bind(id, who.id, who.name, who.role, JSON.stringify({
      filename: String(form.get("filename") || `${id}.jpg`).replace(/[^A-Za-z0-9._-]/g, "_"),
      // No coordinates are sent from the browser, deliberately. The pipeline
      // reads EXIF with Pillow, which is the authoritative reader, and a
      // photograph that has none is refused rather than given a location from
      // somewhere else. The page's own EXIF check is a courtesy that saves an
      // upload; it is not what decides.
      //
      // The single exception is `attach_to`: a photograph explicitly added to a
      // find that already exists inherits THAT find's coordinates, because the
      // person uploading it is saying this is another photograph of that patch.
      // The pipeline takes them from the stored record, never from here.
      attach_to: String(form.get("attach_to") || ""),
      // Which walk this came from, so 80 photographs from one morning stay one
      // thing a contributor can look at the state of.
      walk: String(form.get("walk") || "").slice(0, 120),
      note: String(form.get("note") || "").slice(0, 2000),
    })).run();
    return json({ id, queued: true }, 202);
  }

  // --- The pipeline's side: collect, fetch bytes, acknowledge ---------------
  if (path === "/api/inbox" && request.method === "GET") {
    if (!may(who.role, "drain")) return json({ error: "not a collector" }, 403);
    const limit = Math.min(Number(url.searchParams.get("limit")) || 200, 500);
    const { results } = await env.DB.prepare(
      `SELECT * FROM submissions WHERE status = 'pending' ORDER BY submitted ASC LIMIT ?`,
    ).bind(limit).all();
    return json({ submissions: (results || []).map(submissionOut) }, 200);
  }

  const photoMatch = path.match(/^\/api\/inbox\/([0-9a-f-]{36})\/photo$/);
  if (photoMatch && request.method === "GET") {
    if (!may(who.role, "drain")) return json({ error: "not a collector" }, 403);
    const obj = await env.INBOX.get(`originals/${photoMatch[1]}`);
    if (!obj) return json({ error: "no such photograph" }, 404);
    // Streamed, not buffered: a full-resolution photograph against a 128 MB
    // memory limit is exactly the payload that should never be read into a string.
    return new Response(obj.body, {
      headers: { "Content-Type": obj.httpMetadata?.contentType || "image/jpeg" },
    });
  }

  const receiptMatch = path.match(/^\/api\/inbox\/([0-9a-f-]{36})\/receipt$/);
  if (receiptMatch && request.method === "POST") {
    if (!may(who.role, "drain")) return json({ error: "not a collector" }, 403);
    const body = await request.json().catch(() => null);
    const status = body?.status === "recorded" ? "recorded" : "refused";
    await env.DB.prepare(
      "UPDATE submissions SET status = ?, detail = ?, settled = datetime('now') WHERE id = ?",
    ).bind(status, String(body?.detail || "").slice(0, 1000), receiptMatch[1]).run();
    return json({ ok: true }, 200);
  }

  // --- Contributors ---------------------------------------------------------
  if (path === "/api/contributors" && request.method === "GET") {
    if (who.role !== "admin") return json({ error: "admins only" }, 403);
    const { results } = await env.DB.prepare(
      "SELECT id, name, role, active, created, last_seen FROM contributors ORDER BY created",
    ).all();
    return json({ contributors: results || [] }, 200);
  }

  if (path === "/api/contributors" && request.method === "POST") {
    if (who.role !== "admin") return json({ error: "admins only" }, 403);
    const body = await request.json().catch(() => null);
    const name = String(body?.name || "").trim();
    const role = String(body?.role || "").trim();
    if (!name) return json({ error: "a token needs a name — it becomes 'verified by'" }, 400);
    if (!["contributor", "verifier", "admin", "pipeline"].includes(role)) {
      return json({ error: "unknown role" }, 400);
    }
    // 32 bytes from the platform CSPRNG. Never Math.random(): this is the only
    // thing standing between a stranger and the survey's verification fields.
    const raw = crypto.getRandomValues(new Uint8Array(32));
    const token = "sif_" + btoa(String.fromCharCode(...raw))
      .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
    const id = crypto.randomUUID().slice(0, 8);
    await env.DB.prepare(
      `INSERT INTO contributors (id, name, role, token_sha256, created, active)
       VALUES (?, ?, ?, ?, datetime('now'), 1)`,
    ).bind(id, name, role, await sha256(token)).run();
    // Returned exactly once. Only the hash is stored, so this cannot be recovered
    // — which is the property that makes the database safe to lose.
    return json({ id, name, role, token }, 201);
  }

  const revokeMatch = path.match(/^\/api\/contributors\/([0-9a-f]{8})\/revoke$/);
  if (revokeMatch && request.method === "POST") {
    if (who.role !== "admin") return json({ error: "admins only" }, 403);
    await env.DB.prepare("UPDATE contributors SET active = 0 WHERE id = ?")
      .bind(revokeMatch[1]).run();
    return json({ ok: true }, 200);
  }

  return json({ error: "no such endpoint" }, 404);
}

export default {
  async fetch(request, env, ctx) {
    const headers = cors(env, request);
    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers });
    try {
      const res = await handle(request, env);
      const out = new Response(res.body, res);
      for (const [k, v] of Object.entries(headers)) out.headers.set(k, v);
      return out;
    } catch (err) {
      // Explicit, structured, and logged. Never passThroughOnException: a survey
      // write that half-happened is worse than one that visibly failed.
      console.error(JSON.stringify({
        message: "unhandled", error: String(err?.stack || err),
        path: new URL(request.url).pathname,
      }));
      return json({ error: "the contributor endpoint failed — nothing was recorded" },
                  500, headers);
    }
  },
};
