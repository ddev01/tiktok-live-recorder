# VPS deployment

This deploys only this app as a Docker Swarm stack on the existing single VPS node. It does not modify the shared infrastructure stack or copy local files to the server.

## Assumptions

- The checkout has the Flask/Jinja app in `src/web`, with `web.app:app` and `web.worker` entry points.
- A root `package.json` and `bun.lock` exist. The pinned Bun assets build stage runs `bun install --frozen-lockfile` and `bun run build`, which writes compiled CSS to `src/web/static/app.css`; the image copies the generated `src/web/static/` tree into the runtime image.
- The Python project and lockfile include Flask, Gunicorn, Streamlink, and the app dependencies in the `web` extra. The Python image uses `uv sync --frozen --extra web --no-dev` and includes `ffmpeg`.
- The app stores its SQLite database under `/data` and recordings under `/data/recordings`. Both services mount one local named volume and are constrained to the same Swarm node so SQLite WAL remains on the same local filesystem. Keep one web replica and one worker.
- The existing Traefik resolver is named `letsencryptresolver` by default; override `TRAEFIK_CERTRESOLVER` if the infrastructure uses a different name. The router uses `websecure` and the existing `admin-allowlist@file` middleware.
- DNS for `tt.peltux.xyz` must be DNS-only (not proxied) so the allowlist sees client IP addresses.

The session cookie is supplied only through the existing external Swarm secret `tiktok_session_ss`; the stack maps it to `/run/secrets/tiktok_session_ss` and sets `TIKTOK_SESSION_SS_FILE`. It must be a Netscape HTTP Cookie File so Streamlink can read it without putting cookie values in process arguments. Provision that secret separately before deployment. The deploy script only checks that it exists; it never creates or reads it. The application must not expose cookie values.

Streamlink retries initial opens, but cannot resume an already-open broken FLV stream. The worker finalizes a playable partial MP4 and marks it interrupted; it deliberately does not concatenate separate live connections.

Create it interactively on the VPS so the value is not put in Git or a command argument:

```sh
read -r -s -p 'TikTok session_ss: ' SESSION_SS
printf '\n'
printf '# Netscape HTTP Cookie File\n.tiktok.com\tTRUE\t/\tTRUE\t0\tsession_ss\t%s\n' "$SESSION_SS" | docker secret create tiktok_session_ss -
unset SESSION_SS
```

## Deploy

Run from this app checkout. On first use the script clones the public `ddev01/tiktok-live-recorder` fork into `~/tt-clipper`; later runs fast-forward that checkout:

```sh
./deploy.sh
```

The script connects using `ssh server`, fast-forwards only the app checkout at `~/tt-clipper` by default, builds `Dockerfile.web` on the VPS, validates the stack, deploys only the `tt-clipper` stack, and prints its service status. Configure `REMOTE_PATH`, `STACK_NAME`, or `TRAEFIK_CERTRESOLVER` in the environment to override those defaults. It does not copy `.env` files or secrets, run broad prune commands, or restart unrelated services.

To alert on a stalled worker, export `HEALTHCHECK_URL` (the healthchecks.io ping URL) before running the script; it is passed to the worker service only. Unset, the stack sets it to empty and monitoring stays off.

To validate the current VPS checkout without pulling, building, or deploying:

```sh
./deploy.sh --validate
```

Validation requires SSH access, an active Swarm, the external `infra-public` network, and the pre-existing `tiktok_session_ss` secret.
