# 📬 Email digest

A small, self-contained tool that reads your recent **Gmail inbox**, groups the
mail, **flags anything that looks like it needs a personal reply**, and emails
you the summary. It runs unattended once a day via GitHub Actions — no server,
no always-on session.

## What it does

Every run it pulls inbox mail from the last `LOOKBACK_HOURS` (default 24h) and
sorts it into four buckets:

| Bucket | What lands here |
|--------|-----------------|
| ✋ **Needs a reply** | Mail that looks like it's from a real human (not a no-reply / bulk / marketing sender) |
| 📌 **Worth acting on** | Bills, invoices, orders, shipments, account/security notices |
| 💼 **Job alerts** | LinkedIn / Indeed / Glassdoor etc. and "we're hiring" subjects |
| 🗑️ **Ignorable / marketing** | Promotions, newsletters, anything with an unsubscribe + promo language |

Classification is heuristic (sender patterns + Gmail's own category labels), so
it works with **zero AI cost**. If you add an `ANTHROPIC_API_KEY` it also writes
a 2-3 sentence natural-language **TL;DR** at the top.

## One-time setup

### 1. Get Gmail OAuth credentials

The job acts on your personal Gmail using an OAuth2 **refresh token** (a service
account can't read a normal `@gmail.com` mailbox).

1. In [Google Cloud Console](https://console.cloud.google.com/) → create/select a
   project → **APIs & Services → Enable APIs** → enable **Gmail API**.
2. **Credentials → Create credentials → OAuth client ID → Desktop app.** Save the
   **Client ID** and **Client secret**.
3. Generate a refresh token. Easiest path is the
   [OAuth 2.0 Playground](https://developers.google.com/oauthplayground/):
   - Gear icon → "Use your own OAuth credentials" → paste Client ID + secret.
   - Authorize scope `https://www.googleapis.com/auth/gmail.modify`
     (read inbox + send the digest).
   - "Exchange authorization code for tokens" → copy the **refresh token**.

### 2. Add repo secrets & variables

In the GitHub repo → **Settings → Secrets and variables → Actions**:

**Secrets** (required):
- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- `GOOGLE_REFRESH_TOKEN`

**Secrets** (optional):
- `ANTHROPIC_API_KEY` — enables the AI TL;DR.

**Variables** (optional):
- `DIGEST_TO` — where to send the digest (defaults to your own mailbox).
- `LOOKBACK_HOURS` — defaults to `24`.

### 3. Schedule

`.github/workflows/email-digest.yml` runs daily at **13:00 UTC** (≈ 7 AM US
Central). Edit the `cron:` line for your timezone. You can also trigger it
manually from the **Actions** tab ("Run workflow"), with an optional **dry run**
that logs the digest instead of emailing it.

## Run it locally

```bash
cd email-digest
npm install
export GOOGLE_CLIENT_ID=... GOOGLE_CLIENT_SECRET=... GOOGLE_REFRESH_TOKEN=...
npm run dry-run   # prints the digest, sends nothing
npm run digest    # actually emails it
```

## Config reference

| Env var | Default | Purpose |
|---------|---------|---------|
| `GOOGLE_CLIENT_ID` | — | OAuth client ID (required) |
| `GOOGLE_CLIENT_SECRET` | — | OAuth client secret (required) |
| `GOOGLE_REFRESH_TOKEN` | — | OAuth refresh token (required) |
| `ANTHROPIC_API_KEY` | — | Optional; adds AI TL;DR |
| `ANTHROPIC_MODEL` | `claude-sonnet-4-6` | Optional model override |
| `DIGEST_TO` | mailbox owner | Recipient address |
| `LOOKBACK_HOURS` | `24` | How far back to scan |
| `MAX_EMAILS` | `60` | Cap on messages scanned per run |
| `DRY_RUN` | — | `1` = print instead of send |
