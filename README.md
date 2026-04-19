# Sticky

A Telegram sticker manager for macOS power users. See your real lifetime
sticker usage and build dynamic "Top N" packs that appear in your native
Telegram sticker panel on every device.

Sticky reads your local Telegram-macOS sticker cache directly. It never
authenticates to Telegram as you — no MTProto user session, no ban risk.

---

## Requirements

- macOS with Telegram-macOS installed and signed in.
- **No passcode set** on Telegram-macOS (the passcode blocks the decryption
  key Sticky needs to read the local cache).
- Python 3.13 and [`uv`](https://docs.astral.sh/uv/).

## Install

One line:

```sh
curl -fsSL https://raw.githubusercontent.com/adrianleb/sticky/main/install.sh | bash
```

This installs [`uv`](https://docs.astral.sh/uv/) if you don't already
have it, then puts `sticky` on your PATH. Then:

```sh
sticky init
```

Walks you through picking a mode (below) and pairing a bot. Done.

<details>
<summary>Manual install</summary>

```sh
git clone https://github.com/adrianleb/sticky.git
cd sticky
uv sync --all-packages
uv run sticky init
```
</details>

---

## Two modes

Sticky needs a Telegram bot to own the sticker sets it creates. You pick
one of two ways to provide one:

### Proxy mode (default — zero setup)

You use the hosted bot `@sticky_sticky_sticky_bot`. When `sticky init`
asks, DM that bot `/pair` in Telegram, paste the 6-digit code it replies
with, and you're done.

### Local mode (bring your own bot)

Create your own bot with [@BotFather](https://t.me/BotFather):

1. `/newbot` → pick a name and username → copy the token.
2. DM your new bot `/start` so Sticky can find your chat id.
3. `sticky init` → choose `local` → paste the token.

Nothing touches a third-party server; Sticky talks straight to
`api.telegram.org`.

---

## Everyday use

```sh
sticky sync                         # re-scan Postbox (incremental, fast)
sticky status                       # top stickers + pack heat
sticky report --open                # HTML report of your sticker usage
sticky packs create "All-time Top 30"
sticky packs list
sticky packs refresh <short_name>   # pull latest top-N into the set
sticky daemon install               # run sync every 12 hours via launchd
```

`sticky packs create` DMs you a `t.me/addstickers/…` link. Tap it to
install the pack — it will then appear in your native Telegram sticker
panel on every device.

Run `sticky --help` for the full command list.

---

## What Sticky reads

When you run `sticky sync`, the agent:

1. Opens `~/Library/Group Containers/…/account-*/postbox/db/db_sqlite`
   (Telegram-macOS's local SQLCipher database) read-only.
2. Walks outgoing messages and counts sticker sends by `file_id`.
3. Writes aggregates to `~/.sticky/sticky.db` (local SQLite).
4. No network calls.

It never writes to Telegram's database and never opens a Telegram session.

Peer ids are one-way hashed with a random per-install salt before any
aggregation, so no raw chat or user ids are retained.

## What the proxy sees

Only applies if you chose proxy mode. The hosted proxy is **stateless**:

- It knows your Telegram user id (so the bot can DM you install links).
- It forwards the Bot API calls you make: `uploadStickerFile`,
  `createNewStickerSet`, `addStickerToSet`, `deleteStickerFromSet`,
  `setStickerPositionInSet`, `getStickerSet`, `sendMessage`.
- It does **not** see your sticker usage counts, histograms, timestamps,
  other installed packs, your messages, raw peer ids, or your Telegram
  session. All of that stays on your Mac.
- It does **not** persist requests. The only state it holds is a
  5-minute-TTL pairing code and the JWT it issues you.

In local mode there is no proxy at all.

## What never leaves your Mac

- Message text, captions, or reply chains.
- Raw peer ids, contact names, or usernames.
- Your Telegram auth key or phone number.
- Sticker media files themselves (only the Bot API uploads in proxy /
  local mode send PNGs, and those go to Telegram, not the proxy).

The source is short — read `apps/sticky/src/sticky/` to audit.

---

## Repo layout

- `apps/sticky/` — the CLI and Postbox reader. This is what you run.
- `apps/proxy/` — the optional stateless proxy (FastAPI + aiogram).
  You only need this if you want to host your own proxy.

## License

MIT. See [LICENSE](LICENSE).
