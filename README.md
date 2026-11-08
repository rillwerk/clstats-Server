# ClStats Server

Self-hosted companion server for the [ClStats](https://rillwerk.app/en/clstats/) iOS app.
The app alone shows your Claude plan limits and builds a history from its own updates.
With your own server you additionally get:

- polling around the clock (every 5 minutes), so the history has no gaps,
- the history kept permanently in a small SQLite database,
- insights: sessions, usage per day, a heatmap of when you use Claude,
- token totals per project and model from your Claude Code logs (optional script),
- a small web dashboard and a CSV export.

It is a small Python service (FastAPI, SQLite) in a Docker container.
"Own Server" mode in the app requires ClStats Pro (or the free trial).

> **Not affiliated with Anthropic.** ClStats reads the same usage data the claude.ai
> settings page shows, with your own session. This is not an official API and may stop
> working at any time.

> **No support, no warranty.** This is offered as is (MIT license). Run it only if you are
> comfortable operating a small server yourself.

## What you need

- A machine with Docker (a NAS, a home server, a small VPS).
- An HTTPS address for it if you want to use the app away from home, e.g. through a
  reverse proxy such as Caddy, nginx, Traefik or SWAG, or a VPN like Tailscale.

## Install

```sh
mkdir clstats && cd clstats
curl -fsSLO https://raw.githubusercontent.com/rillwerk/clstats-server/main/docker-compose.yml
curl -fsSL https://raw.githubusercontent.com/rillwerk/clstats-server/main/.env.example -o .env
# set API_TOKEN in .env to a long random string:
sed -i.bak "s/^API_TOKEN=.*/API_TOKEN=$(openssl rand -hex 32)/" .env && rm .env.bak
mkdir data && sudo chown 99:100 data   # the container runs as uid 99 / gid 100
docker compose up -d
curl http://127.0.0.1:8080/api/health   # {"ok":true}
```

Settings in `.env`:

| Variable | |
|---|---|
| `API_TOKEN` | **Required.** At least 24 characters. The app sends it as "Access Token". |
| `SECRET_KEY` | Optional. Encrypts the stored claude.ai cookie. Generated on first start and kept in `data/secret.key` if empty. |
| `TZ` | Time zone for daily statistics, default `UTC`. |
| `POLL_INTERVAL` | Seconds between polls, default 300, minimum 60. |

The compose file publishes the port only on `127.0.0.1`. Put your reverse proxy in front
of it, for example with Caddy (`examples/caddy/Caddyfile`):

```
clstats.example.com {
	reverse_proxy 127.0.0.1:8080
}
```

More examples: `examples/swag` (nginx/SWAG), `examples/unraid` (Unraid with SWAG).
If you put the dashboard behind an extra login (e.g. Authelia), keep `/api/` and
`/privacy` reachable without it: the app cannot do such a login and uses its token instead.

## Connect the app

1. In ClStats, open **Settings**, choose **Own Server**.
2. Enter the server address (`https://clstats.example.com`) and the `API_TOKEN` as
   access token, tap **Save and Test**.
3. Tap **Sign In to claude.ai**. The app hands the session cookie to your server, which
   stores it encrypted and polls from then on.

If claude.ai blocks requests from your server, the app fetches the data itself whenever
it is opened and passes it on.

## Token totals per project (optional)

claude.ai only reports percentages. Real token counts are in the logs Claude Code writes on
your computer (`~/.claude/projects/…/*.jsonl`). The script in `scripts/` reads only the
counters (no content) and sends totals per day, project and model to your server every
5 minutes:

```sh
sh scripts/install.sh            # macOS: asks for server address and API_TOKEN, sets up a LaunchAgent
sh scripts/install-linux.sh      # Linux: cron entry
python3 scripts/claude_tokens.py --dry-run   # shows what would be sent
sh scripts/install.sh uninstall
```

Costs shown in the app are estimates at API list prices, not what your plan costs.

## Push notifications

Push notifications to the App Store version of ClStats need the app developer's Apple
push key, which cannot be shared. **Self-hosted servers therefore run without push**;
leave the `APNS_*` variables empty. The app shows everything else as usual.

## Data and privacy

The server stores the encrypted claude.ai session cookie, usage samples, optional token
totals and nothing else. It talks only to claude.ai. You operate it, so you are responsible
for it. `GET /privacy` shows a short public description. "Sign Out" in the app deletes the
cookie, deleting the `data` folder deletes everything.

Security notes: all endpoints except `/api/health` and `/privacy` require the token;
repeated wrong tokens are answered with `429` for a while; manual refreshes are limited to
one per minute.

## API

All endpoints except `/api/health` and `/privacy` need `Authorization: Bearer <API_TOKEN>`.

| Endpoint | |
|---|---|
| `GET /api/current` | Current limits with pace, rate, forecast, status |
| `POST /api/refresh` | Poll now (at most once per minute) |
| `POST /api/credentials` / `DELETE` | Set or remove the claude.ai cookie |
| `POST /api/ingest` | Raw `/usage` payload from the app (fallback) |
| `GET /api/history?key=five_hour&hours=24` | History |
| `GET /api/daily`, `/api/heatmap`, `/api/windows`, `/api/summary` | Insights |
| `POST /api/tokens`, `GET /api/tokens?days=30` | Token totals |
| `GET /api/export.csv` | All samples as CSV |

## Development

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest
API_TOKEN=$(openssl rand -hex 32) DATA_DIR=./data uvicorn app.main:app_factory --factory --reload --port 8080
```

## License

MIT, see `LICENSE`.
