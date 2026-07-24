import assert from "node:assert/strict";
import test from "node:test";

import {
  bearerToken,
  clampInteger,
  handleEmail,
  handleFetch,
  randomLocalPart,
  secretsEqual,
} from "../src/index.js";

test("utility functions keep bounds and parse bearer tokens", async () => {
  assert.equal(clampInteger("99", 10, 1, 50), 50);
  assert.equal(clampInteger("invalid", 10, 1, 50), 10);
  assert.equal(bearerToken(new Request("https://mail.test", {
    headers: { authorization: "Bearer mailbox-token" },
  })), "mailbox-token");
  assert.equal(bearerToken(new Request("https://mail.test")), "");
  assert.equal(await secretsEqual("same", "same"), true);
  assert.equal(await secretsEqual("same", "different"), false);
});

test("generated local parts are mailbox-safe and unique", () => {
  const values = new Set(Array.from({ length: 100 }, () => randomLocalPart()));
  assert.equal(values.size, 100);
  for (const value of values) assert.match(value, /^m[a-z0-9]{20}$/);
});

test("health endpoint is public but mailbox creation requires a secret", async () => {
  const context = { waitUntil() {} };
  const health = await handleFetch(new Request("https://mail.test/health"), {}, context);
  assert.equal(health.status, 200);
  assert.deepEqual(await health.json(), { ok: true, service: "grok-mail-kagari" });

  const denied = await handleFetch(
    new Request("https://mail.test/api/new_address", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: "{}",
    }),
    { API_TOKEN: "secret" },
    context,
  );
  assert.equal(denied.status, 401);
});

test("email handler rejects recipients that were not pre-created", async () => {
  let rejection = "";
  const message = {
    to: "missing@kagari.app",
    from: "sender@example.com",
    rawSize: 100,
    setReject(reason) {
      rejection = reason;
    },
  };
  const statement = {
    bind() {
      return this;
    },
    async first() {
      return null;
    },
  };
  await handleEmail(message, {
    MAIL_DOMAIN: "kagari.app",
    DB: { prepare: () => statement },
  });
  assert.equal(rejection, "Recipient does not exist");
});
