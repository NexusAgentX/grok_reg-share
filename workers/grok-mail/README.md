# grok-mail-kagari Worker

Private Cloudflare Email Worker used by the browser registration path.

## Security model

- The public API creates mailboxes only with the `API_TOKEN` Worker secret.
- Each mailbox receives an independent opaque bearer token.
- The Email Routing catch-all invokes the Worker, but mail is stored only for a
  currently active mailbox created through the API.
- Unknown recipients and oversized messages are rejected.
- D1 stores parsed message bodies for at most one hour; API reads are marked
  `no-store`.
- Worker secrets, account credentials, mailbox tokens, and runtime mail are not
  committed to Git.

## API contract

- `GET /health`
- `POST /api/new_address` using the shared bearer secret
- `GET /api/mails` using a mailbox bearer token
- `GET /api/mail/:id` or `GET /api/mails/:id` using a mailbox bearer token
- `DELETE /api/mailbox` using a mailbox bearer token

The response shape matches the `cloudflare` provider in `grok_register_ttk.py`.

## Local verification

```bash
npm ci
npm run check
npm test
npm run migrate:local
npx wrangler dev
```

Wrangler simulates the email handler at `/cdn-cgi/handler/email`. Production
migration and deployment use `npm run migrate:remote` and `npm run deploy` after
loading the local Cloudflare credentials documented under `/srv/ops/domains/cloudflare`.
