FROM python:3.14-slim
# trigger: verify GHCR push after repo recreation
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/
COPY scripts/ scripts/

# Issue #86: build-time commit SHA, surfaced read-only in the admin instance-health
# panel. Build metadata only, not a deploy-pipeline behavior change — defaults to
# empty so a plain `docker build` (e.g. pr-validate.yml's PR smoke test) still works
# unchanged. .github/workflows/build-app.yml does not currently pass --build-arg
# GIT_SHA, so this stays empty ("unknown" in the UI) on real deploys until that
# workflow is updated to pass it — see the AniDex #86 PR description for the exact
# one-line addition needed and why it wasn't made here.
ARG GIT_SHA=""
ENV GIT_SHA=$GIT_SHA

# Issue #460 — run as an unprivileged user. The app is a stateless FastAPI service
# backed by Postgres; its only runtime filesystem write is the Netflix CSV import's
# tempfile.NamedTemporaryFile (default tempdir, i.e. /tmp) — no write access under
# /app itself is needed. The deploy workflow's docker run additionally passes
# --read-only plus a --tmpfs /tmp mount to cover that one write path; PYTHONDONTWRITEBYTECODE
# avoids CPython trying (and silently failing) to write .pyc cache files under /app.
ENV PYTHONDONTWRITEBYTECODE=1
RUN useradd --no-create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8888
# Issue #465 — scoped from "*" to the one real source of legitimate traffic.
# Investigated live: both the Cloudflare Tunnel container and this app container
# sit on Docker's default bridge network, so tunneled requests arrive at uvicorn
# via the bridge gateway IP (172.17.0.1), not a container-to-container address.
# "*" previously trusted X-Forwarded-For/X-Forwarded-Proto from ANY source,
# meaning a LAN-adjacent device reaching the published port directly (bypassing
# the tunnel entirely) could also spoof those headers and dodge the per-IP login
# rate limiter (see _ip_login_attempts in app/main.py). Scoping to the real
# gateway IP keeps trusting genuine tunneled traffic (so OAuth's redirect_uri
# generation, the original reason this was set to "*" per issue #53, keeps
# working) while no longer trusting headers from anyone else.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8888", "--proxy-headers", "--forwarded-allow-ips=172.17.0.1"]
