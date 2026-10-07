# Superset with the PostgreSQL driver the official image does not carry, pinned by digest so a
# rebuilt tag cannot change what this runs. The quickstart's `dashboards` profile builds it, and
# `tests/emulators/superset/compose.yaml` builds this same context, so the live dashboard
# requirements exercise the image a reader starts.
FROM apache/superset@sha256:16b50bbef6648912a79e3293d418fefa743ce555d36c5af6b85d859119ed7f88

USER root
# Into Superset's own virtual environment, named explicitly, because `pip` on this image is not
# that environment's. `python` resolves to /app/.venv/bin/python -- the interpreter Superset runs --
# while `pip` resolves to /usr/local/bin/pip, the system Python 3.10's; the venv declares
# `include-system-site-packages = false` and carries no pip of its own. So a plain
# `pip install psycopg2-binary` succeeds, reports an installed driver, and puts it in
# /usr/local/lib/python3.10/site-packages, where the interpreter Superset runs cannot import it:
# the image builds, Superset starts, and a dashboard query against the warehouse fails for want of
# a driver that the build said it installed. `uv` is on the image and built that venv, so it is
# what installs into it.
RUN uv pip install --no-cache --python /app/.venv/bin/python psycopg2-binary==2.9.11 \
  && /app/.venv/bin/python -c "import psycopg2"
USER superset
