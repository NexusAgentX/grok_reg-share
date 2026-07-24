import PostalMime from "postal-mime";

const JSON_HEADERS = {
  "content-type": "application/json; charset=utf-8",
  "cache-control": "no-store",
  "x-content-type-options": "nosniff",
};

function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: JSON_HEADERS,
  });
}

function clampInteger(value, fallback, minimum, maximum) {
  const parsed = Number.parseInt(String(value ?? ""), 10);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.max(minimum, Math.min(maximum, parsed));
}

function bearerToken(request) {
  const value = request.headers.get("authorization") || "";
  const match = /^Bearer\s+(.+)$/i.exec(value.trim());
  return match ? match[1].trim() : "";
}

function randomToken(byteLength = 32) {
  const bytes = new Uint8Array(byteLength);
  crypto.getRandomValues(bytes);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/g, "");
}

function randomLocalPart() {
  return `m${randomToken(15).toLowerCase().replaceAll("_", "a").replaceAll("-", "b")}`;
}

async function sha256(value) {
  const bytes = new TextEncoder().encode(String(value));
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function secretsEqual(left, right) {
  if (!left || !right) return false;
  const [a, b] = await Promise.all([sha256(left), sha256(right)]);
  let difference = a.length ^ b.length;
  const length = Math.max(a.length, b.length);
  for (let index = 0; index < length; index += 1) {
    difference |= (a.charCodeAt(index) || 0) ^ (b.charCodeAt(index) || 0);
  }
  return difference === 0;
}

function nowSeconds() {
  return Math.floor(Date.now() / 1000);
}

function boundedText(value, limit = 262144) {
  return String(value || "").slice(0, limit);
}

async function cleanupExpired(env) {
  const now = nowSeconds();
  await env.DB.batch([
    env.DB.prepare(
      "DELETE FROM messages WHERE mailbox_id IN (SELECT id FROM mailboxes WHERE expires_at <= ?1)",
    ).bind(now),
    env.DB.prepare("DELETE FROM mailboxes WHERE expires_at <= ?1").bind(now),
  ]);
}

async function authorizeMailbox(request, env) {
  const token = bearerToken(request);
  if (!token) return null;
  const tokenHash = await sha256(token);
  return env.DB.prepare(
    "SELECT id, address, expires_at FROM mailboxes WHERE token_hash = ?1 AND expires_at > ?2",
  )
    .bind(tokenHash, nowSeconds())
    .first();
}

async function createMailbox(request, env) {
  if (!(await secretsEqual(bearerToken(request), env.API_TOKEN))) {
    return jsonResponse({ error: "unauthorized" }, 401);
  }
  if (!env.API_TOKEN) {
    return jsonResponse({ error: "service is not configured" }, 503);
  }

  let payload = {};
  try {
    payload = await request.json();
  } catch {
    payload = {};
  }
  const domain = String(payload.domain || env.MAIL_DOMAIN || "").trim().toLowerCase();
  if (!domain || domain !== String(env.MAIL_DOMAIN || "").trim().toLowerCase()) {
    return jsonResponse({ error: "unsupported domain" }, 400);
  }

  await cleanupExpired(env);
  const maximum = clampInteger(env.MAX_ACTIVE_MAILBOXES, 25, 1, 100);
  const count = await env.DB.prepare("SELECT COUNT(*) AS count FROM mailboxes WHERE expires_at > ?1")
    .bind(nowSeconds())
    .first();
  if (Number(count?.count || 0) >= maximum) {
    return jsonResponse({ error: "mailbox capacity reached" }, 429);
  }

  const ttl = clampInteger(env.MAILBOX_TTL_SECONDS, 3600, 300, 86400);
  for (let attempt = 0; attempt < 5; attempt += 1) {
    const id = randomLocalPart();
    const address = `${id}@${domain}`;
    const token = randomToken(32);
    const tokenHash = await sha256(token);
    const createdAt = nowSeconds();
    try {
      await env.DB.prepare(
        "INSERT INTO mailboxes (id, address, token_hash, created_at, expires_at) VALUES (?1, ?2, ?3, ?4, ?5)",
      )
        .bind(id, address, tokenHash, createdAt, createdAt + ttl)
        .run();
      return jsonResponse({ address, jwt: token, expiresIn: ttl }, 201);
    } catch (error) {
      if (attempt === 4) throw error;
    }
  }
  return jsonResponse({ error: "could not allocate mailbox" }, 503);
}

function messagePayload(row, includeBody = true) {
  const payload = {
    id: row.id,
    address: row.recipient,
    from: { address: row.sender, name: "" },
    to: [{ address: row.recipient, name: "" }],
    subject: row.subject,
    createdAt: new Date(row.created_at * 1000).toISOString(),
    size: row.raw_size,
  };
  if (includeBody) {
    payload.text = row.text_body;
    payload.html = row.html_body ? [row.html_body] : [];
  }
  return payload;
}

async function listMessages(request, env, mailbox) {
  const url = new URL(request.url);
  const limit = clampInteger(url.searchParams.get("limit"), 20, 1, 50);
  const offset = clampInteger(url.searchParams.get("offset"), 0, 0, 1000);
  const result = await env.DB.prepare(
    "SELECT id, sender, recipient, subject, text_body, html_body, raw_size, created_at FROM messages WHERE mailbox_id = ?1 ORDER BY created_at DESC LIMIT ?2 OFFSET ?3",
  )
    .bind(mailbox.id, limit, offset)
    .all();
  return jsonResponse({ messages: (result.results || []).map((row) => messagePayload(row, true)) });
}

async function getMessage(env, mailbox, messageId) {
  const row = await env.DB.prepare(
    "SELECT id, sender, recipient, subject, text_body, html_body, raw_size, created_at FROM messages WHERE mailbox_id = ?1 AND id = ?2",
  )
    .bind(mailbox.id, messageId)
    .first();
  if (!row) return jsonResponse({ error: "message not found" }, 404);
  return jsonResponse({ data: messagePayload(row, true) });
}

async function deleteMailbox(env, mailbox) {
  await env.DB.batch([
    env.DB.prepare("DELETE FROM messages WHERE mailbox_id = ?1").bind(mailbox.id),
    env.DB.prepare("DELETE FROM mailboxes WHERE id = ?1").bind(mailbox.id),
  ]);
  return new Response(null, { status: 204, headers: { "cache-control": "no-store" } });
}

async function handleFetch(request, env, ctx) {
  const url = new URL(request.url);
  if (request.method === "GET" && url.pathname === "/health") {
    return jsonResponse({ ok: true, service: "grok-mail-kagari" });
  }
  if (request.method === "POST" && url.pathname === "/api/new_address") {
    try {
      return await createMailbox(request, env);
    } catch {
      return jsonResponse({ error: "mailbox creation failed" }, 500);
    }
  }

  const mailbox = await authorizeMailbox(request, env);
  if (!mailbox) return jsonResponse({ error: "unauthorized" }, 401);
  ctx.waitUntil(cleanupExpired(env));

  if (request.method === "GET" && url.pathname === "/api/mails") {
    return listMessages(request, env, mailbox);
  }
  const detailPrefix = url.pathname.startsWith("/api/mails/")
    ? "/api/mails/"
    : "/api/mail/";
  if (request.method === "GET" && url.pathname.startsWith(detailPrefix)) {
    const messageId = decodeURIComponent(url.pathname.slice(detailPrefix.length));
    return getMessage(env, mailbox, messageId);
  }
  if (request.method === "DELETE" && url.pathname === "/api/mailbox") {
    return deleteMailbox(env, mailbox);
  }
  return jsonResponse({ error: "not found" }, 404);
}

async function handleEmail(message, env) {
  const recipient = String(message.to || "").trim().toLowerCase();
  const domain = String(env.MAIL_DOMAIN || "").trim().toLowerCase();
  if (!recipient.endsWith(`@${domain}`)) {
    message.setReject("Unsupported recipient");
    return;
  }

  const mailbox = await env.DB.prepare(
    "SELECT id, address, expires_at FROM mailboxes WHERE address = ?1 AND expires_at > ?2",
  )
    .bind(recipient, nowSeconds())
    .first();
  if (!mailbox) {
    message.setReject("Recipient does not exist");
    return;
  }

  const maximumSize = clampInteger(env.MAX_MESSAGE_BYTES, 1048576, 65536, 2097152);
  if (Number(message.rawSize || 0) > maximumSize) {
    message.setReject("Message is too large");
    return;
  }

  const raw = await new Response(message.raw).arrayBuffer();
  const parsed = await PostalMime.parse(raw);
  const id = randomToken(18);
  const sender = String(parsed.from?.address || message.from || "").slice(0, 320);
  const subject = boundedText(parsed.subject, 500);
  const text = boundedText(parsed.text, 262144);
  const html = boundedText(parsed.html, 262144);
  await env.DB.prepare(
    "INSERT INTO messages (id, mailbox_id, sender, recipient, subject, text_body, html_body, raw_size, created_at) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)",
  )
    .bind(id, mailbox.id, sender, recipient, subject, text, html, Number(message.rawSize || raw.byteLength), nowSeconds())
    .run();
}

export {
  bearerToken,
  clampInteger,
  handleEmail,
  handleFetch,
  randomLocalPart,
  secretsEqual,
};

export default {
  fetch: handleFetch,
  email: handleEmail,
  async scheduled(_event, env, ctx) {
    ctx.waitUntil(cleanupExpired(env));
  },
};
