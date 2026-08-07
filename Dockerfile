# =============================================================================
# sol-futures-trading-bot
#
# Design notes:
#
# * No pip install at build or run time. The application runs on the Python
#   standard library in DISABLED and MOCK modes, so the image contains the
#   interpreter and our source and nothing else. Fewer moving parts, smaller
#   attack surface, and no dependency resolution to go wrong during a deploy.
#   (The optional `ibkr` extra is installed deliberately in a later phase; see
#   docs/IBKR_API_NOTES.md.)
#
# * Runs as a non-root user with a fixed uid, so bind-mounted host directories
#   have a stable owner to be chowned to.
#
# * No `.env` is copied in. Configuration is injected at run time from a file
#   that exists only on the server. See .dockerignore and SECURITY.md.
# =============================================================================

FROM python:3.12-slim-bookworm

# Build metadata, surfaced by /status and `make status`.
ARG GIT_COMMIT=unknown
ARG BUILD_TIMESTAMP=unknown

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    PYTHONPATH=/app \
    GIT_COMMIT=${GIT_COMMIT} \
    BUILD_TIMESTAMP=${BUILD_TIMESTAMP}

# Fixed uid/gid so a bind-mounted ./data and ./logs on the host can be chowned
# to a known owner (see scripts/bootstrap_dirs.sh).
RUN groupadd --gid 10001 trader \
 && useradd --uid 10001 --gid 10001 --home-dir /app --no-create-home --shell /usr/sbin/nologin trader

WORKDIR /app

COPY --chown=trader:trader app/ /app/app/
COPY --chown=trader:trader scripts/healthcheck.py /app/scripts/healthcheck.py
COPY --chown=trader:trader pyproject.toml README.md /app/

# Created here so the container still starts if a volume is not mounted;
# a mount simply shadows these.
RUN mkdir -p /app/data /app/logs && chown -R trader:trader /app/data /app/logs

USER trader

# Loopback only. Deliberately NOT an EXPOSE of a trading port, and nothing in
# docker-compose.yml publishes anything.
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD ["python", "/app/scripts/healthcheck.py"]

STOPSIGNAL SIGTERM

ENTRYPOINT ["python", "-m", "app.main"]
