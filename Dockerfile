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

EXPOSE 8888
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8888", "--proxy-headers", "--forwarded-allow-ips=*"]
