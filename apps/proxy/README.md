# sticky-proxy

Stateless Bot API proxy for Sticky. Lets users pair with the shared `@sticky_bot`
Telegram bot and proxy Bot API calls scoped to their own `user_id`, without
forcing each user to run their own bot via BotFather.

No user data is stored. Pairing codes live in memory with a short TTL. Long-lived
auth is a signed JWT that encodes only the paired Telegram user id.
