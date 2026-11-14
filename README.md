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
| `INGEST_TOKEN` | Optional, at least 24 characters, different from `API_TOKEN`. May only send token totals (`POST /api/tokens`); use it in the token script on other computers, see below. |
| `FORWARDED_ALLOW_IPS` | Optional. Addresses or networks of your reverse proxy whose `X-Forwarded-For` is trusted, e.g. `172.18.0.0/16` for its Docker network. Default `127.0.0.1`. Only affects which client address appears in logs and in the lockout after wrong tokens. |

The compose file publishes the port only on `127.0.0.1`. Put your reverse proxy in front
of it, for example with Caddy (`examples/caddy/Caddyfile`):

```
clstats.example.com {
	reverse_proxy 127.0.0.1:8080
}
```

More examples: `examples/swag` (nginx/SWAG), `examples/unraid` (Unraid with SWAG),
`examples/kubernetes` (see below).
If you put the dashboard behind an extra login (e.g. Authelia), keep `/api/` and
`/privacy` reachable without it: the app cannot do such a login and uses its token instead.

## Kubernetes

`examples/kubernetes` has plain manifests (no Helm) with a `kustomization.yaml`:
namespace, secret, PersistentVolumeClaim, Deployment, Service and an Ingress with TLS.

```sh
git clone https://github.com/rillwerk/clstats-server.git && cd clstats-server/examples/kubernetes
kubectl create namespace clstats
kubectl -n clstats create secret generic clstats --from-literal=API_TOKEN="$(openssl rand -hex 32)"
# remove "- secret.yaml" from kustomization.yaml (or put the token into secret.yaml instead),
# set your host name in ingress.yaml, then:
kubectl apply -k .
kubectl -n clstats rollout status deploy/clstats
kubectl -n clstats port-forward svc/clstats 8080:80   # curl http://127.0.0.1:8080/api/health
```

- **One replica only.** The data lives in SQLite and the server polls claude.ai itself;
  the Deployment uses `replicas: 1` and `strategy: Recreate`. Do not scale it up.
- **A PersistentVolumeClaim is required** for `/data` (database and generated key).
  Use a local or block volume (ReadWriteOnce); SQLite does not work reliably on NFS.
- **HTTPS through the Ingress.** Set the host name and a TLS secret, e.g. with cert-manager
  (annotation prepared as a comment). The app needs an `https://` address on the go.
- The pod runs as uid 99 / gid 100 with a read-only root filesystem, no capabilities and
  a writable `emptyDir` for `/tmp`. `TZ` and `POLL_INTERVAL` are set in `deployment.yaml`.
- **k3s:** works as is with the built-in Traefik ingress and the `local-path` storage
  class; uncomment `ingressClassName: traefik` if your cluster has several ingress classes.

## Connect the app

1. In ClStats, open **Settings**, choose **Own Server**.
2. Enter the server address (`https://clstats.example.com`) and the `API_TOKEN` as
   access token, tap **Save and Test**.
3. Tap **Sign In to claude.ai**. The app hands the session cookie to your server, which
   stores it encrypted and polls from then on.

If claude.ai blocks requests from your server, the app fetches the data itself whenever
it is opened and passes it on.

## Cloudflare Access (optional)

If the server is published through Cloudflare (e.g. a Cloudflare Tunnel), you can put it
behind [Cloudflare Access](https://developers.cloudflare.com/cloudflare-one/policies/access/).
The app and the token script cannot complete a browser login, so `/api` needs a
**service token**. Cloudflare checks it at the edge; the server itself needs no changes and
still requires its `API_TOKEN` as well.

1. **Create a service token.** Zero Trust dashboard → *Access* → *Service Auth* →
   *Service Tokens* → *Create Service Token*. Copy the **Client ID** and the
   **Client Secret** (the secret is shown only once).
2. **Protect `/api` with it.** *Access* → *Applications* → *Add an application* →
   *Self-hosted*. Domain `clstats.example.com`, path `api`. Add a policy with the action
   **Service Auth** (not *Allow*: an *Allow* policy answers with a login page) and the
   rule *Include → Service Token →* your token.
3. **Web dashboard (optional).** The dashboard at `/` calls `/api` from the browser. To use
   it, add a second policy with the action *Allow* (e.g. your e-mail address) to the same
   application, and protect `/` with another application without a path. Requests with a
   valid service token pass the *Service Auth* policy; browsers get the Access login.
4. **App:** Settings → *Own Server* → *Cloudflare Access (optional)*: enter Client ID and
   Client Secret, tap **Save and Test**. Both are kept in the keychain and sent as
   `CF-Access-Client-Id` / `CF-Access-Client-Secret` with every request to your server.
5. **Token script (if used):** add to `~/.config/claude-usage/config`
   ```
   CF_ACCESS_CLIENT_ID=<client id>
   CF_ACCESS_CLIENT_SECRET=<client secret>
   ```

Test from a shell:

```sh
curl -H "CF-Access-Client-Id: <client id>" -H "CF-Access-Client-Secret: <client secret>" \
     -H "Authorization: Bearer <API_TOKEN>" https://clstats.example.com/api/current
```

Without the two headers Cloudflare answers `403` (or redirects to its login page); the app
then reports that Cloudflare Access blocked the request. Make sure the server is reachable
only through Cloudflare (a tunnel, or a firewall that admits only Cloudflare), otherwise
Access can be bypassed.

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

The script's config (`~/.config/claude-usage/config`) holds a token in plain text. Rather than
the `API_TOKEN`, give it an `INGEST_TOKEN`: set `INGEST_TOKEN=$(openssl rand -hex 32)` in the
server's `.env`, restart the server, and put that value into the script config as
`API_TOKEN=…`. A leaked script config can then only send token totals, not read your data or
change the claude.ai login. Scripts that still use the `API_TOKEN` keep working.

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
repeated wrong tokens from one address (or more than 100 overall) are answered with `429`
and a `Retry-After` header for up to 10 minutes, while the correct token keeps working;
manual refreshes are limited to one per minute. When claude.ai itself answers `429`, the
server pauses polling (Retry-After, at least the poll interval, doubling up to an hour)
and reports `status.error = "rate_limited"` with `retry_at`.

## API

All endpoints except `/api/health` and `/privacy` need `Authorization: Bearer <API_TOKEN>`
(the token is only accepted in this header, never in the URL). `POST /api/tokens` also
accepts the `INGEST_TOKEN`.

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
