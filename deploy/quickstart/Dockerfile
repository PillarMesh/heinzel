# The local demonstration image decided in ADR-0009. It packages the demonstration backend,
# not a deployment: it has no authentication, so anyone who reaches the published port acts
# as the architect. Publish it on loopback only (`-p 127.0.0.1:8000:8000`).
#
# Build it from the repository root:
#   docker build -f deploy/quickstart/Dockerfile -t quickstart-console .
#
# That tag is the one `compose.yaml` beside this file builds under, derived from its project
# and service names, so a build either way leaves one image rather than two under two names.
#
# Every base image is pinned by digest as well as by tag, so a rebuilt tag cannot change what
# this installs. The Node version matches apps/console/.node-version and the Python version
# matches .python-version; change them together.

FROM node:24.20.0-bookworm-slim@sha256:ba849c60be29959425b8734d57b8b4b7d56f98edd9504c9af091d5281095a71e AS web
WORKDIR /src
# The manifests alone first, so an edit to the console's sources does not reinstall npm.
COPY apps/console/package.json apps/console/package-lock.json ./apps/console/
RUN cd apps/console && npm ci
COPY apps/console ./apps/console
RUN cd apps/console && npm run build

FROM python:3.13.5-slim-bookworm@sha256:4c2cf9917bd1cbacc5e9b07320025bdb7cdf2df7b0ceaccb55e9dd7e30987419 AS runtime
# `copy` because uv installs a package by hard-linking it out of its own cache, and a hard
# link cannot cross a filesystem boundary. Whether the cache and `/src/.venv` sit on one
# filesystem depends on how the build is run rather than on anything written here — a cache
# mount, a bind, another storage driver — and where they do not, uv warns and falls back to
# copying anyway. Naming the mode makes it the same either way. Bytecode up front so the
# first request does not pay for it. Downloads are off so the interpreter is the one this
# image pins above, rather than whichever build a download happens to fetch.
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never
WORKDIR /src
COPY pyproject.toml uv.lock ./
# Every workspace member, because `uv sync` resolves the whole workspace against the lockfile
# even when it installs one package, and the members it does install are installed editable
# from these directories. `.dockerignore` keeps local `node_modules`, `dist` and `.venv` out.
COPY apps ./apps
COPY packages ./packages
COPY providers ./providers
COPY services ./services
# uv is a build tool, not a runtime one: mounted for the length of this instruction rather
# than copied in, so the 43 MB binary that installs the environment is not carried by every
# container that runs it.
RUN --mount=from=ghcr.io/astral-sh/uv:0.9.7@sha256:ba4857bf2a068e9bc0e64eed8563b065908a4cd6bfb66b531a9c424c8e25e142,source=/uv,target=/usr/local/bin/uv \
  uv sync --locked --no-dev --package heinzel-console \
  && rm -rf /root/.cache/uv
# The bundle comes only from the web stage above; the host's own `dist` is excluded from the
# build context, so nothing here can serve a stale local build.
COPY --from=web /src/apps/console/dist /srv/console/dist
RUN useradd --system --create-home --uid 10001 heinzel \
  && mkdir -p /var/lib/heinzel && chown heinzel:heinzel /var/lib/heinzel
USER heinzel
# The origin is configuration, not something the server can derive from the `0.0.0.0` it binds
# under `--container`: a command whose `Origin` header differs is refused `same_origin_required`,
# which reads as a console where every page loads and every button fails. This default matches
# `-p 127.0.0.1:8000:8000`. Publishing on any other host or port means overriding it with the
# origin the browser sends, spelled exactly as the browser spells it: no surrounding space,
# no trailing slash or path, no uppercase, no default port (a console published on port 80 is
# `http://127.0.0.1`, not `http://127.0.0.1:80`), and an address written the one way a browser
# writes it (`http://127.0.0.1`, not `http://127.1`; `http://[2001:db8::1]`, not
# `http://[2001:db8:0:0:0:0:0:1]`). Any other spelling is refused at startup rather than
# adjusted, and the refusal names the spelling to use instead.
ENV PATH=/src/.venv/bin:$PATH \
    HEINZEL_CONSOLE_ALLOWED_ORIGIN=http://127.0.0.1:8000 \
    HEINZEL_CONSOLE_DIST=/srv/console/dist
EXPOSE 8000
# Loopback inside the container, which `--container` binds along with every other interface.
HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --retries=6 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz').read()"
# The state directory holds the demonstration's SQLite stores. Mount a volume on it to keep
# the demonstration between runs.
ENTRYPOINT ["heinzel-console", "serve", "--state-dir", "/var/lib/heinzel", "--container"]
