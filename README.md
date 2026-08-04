# Metro Agent

Metro Agent is a multi-agent assistant for metro communication operations. This public baseline contains source code, synthetic fixtures, deterministic tests, and local deployment guidance only; it does not include production credentials, operational records, conversation history, or knowledge indexes.

## Quick start

Copy `.env.example` to `.env`, fill only the provider credentials you use, then run `docker compose --profile mock up --build`. Open `http://127.0.0.1:8000`.

Run deterministic checks with `python -m pytest -m "not live and not integration"`.

Production deployments must authenticate callers server-side; the current `user_id` request field is development-only.
