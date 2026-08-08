# =============================================================================
# sol-futures-trading-bot
#
# Design notes:
#
# * Exactly one dependency: the IBKR TWS API, vendored from Interactive
#   Brokers' own distribution in a build stage, pinned to a version and
#   verified against a SHA-256 checksum. Not the PyPI redistribution -- for the
#   one component that can place orders, an uploader who is not the vendor is a
#   party this project does not need in the chain. See docs/IBKR_API_NOTES.md
#   for the options that were weighed.
#
#   The final image is otherwise the interpreter and our source. Nothing is
#   resolved from an index at build time, so a deploy cannot be changed by
#   something republished upstream.
#
#   Its presence enables nothing on its own: DISABLED and MOCK never import it,
#   and TRADING_MODE alone decides whether a broker is used at all.
#
# * Runs as a non-root user with a fixed uid, so bind-mounted host directories
#   have a stable owner to be chowned to.
#
# * No `.env` is copied in. Configuration is injected at run time from a file
#   that exists only on the server. See .dockerignore and SECURITY.md.
# =============================================================================

# -----------------------------------------------------------------------------
# Stage 1 -- vendor the TWS API from Interactive Brokers
#
# The checksum is the point of this stage. Without it, "downloaded from IBKR"
# means "downloaded from whatever answered that hostname at build time".
# Update TWSAPI_VERSION and TWSAPI_SHA256 together, deliberately, and verify the
# hash against a copy you fetched yourself.
# -----------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS ibapi-build

ARG TWSAPI_VERSION=1030.01
ARG TWSAPI_SHA256=ea79fa5b4c7b30359458424085e55f918115a2889efc248a2b79319da71a139a

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl unzip ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && curl -fsSL -o /tmp/twsapi.zip \
      "https://interactivebrokers.github.io/downloads/twsapi_macunix.${TWSAPI_VERSION}.zip" \
 && echo "${TWSAPI_SHA256}  /tmp/twsapi.zip" | sha256sum -c - \
 && unzip -q /tmp/twsapi.zip -d /tmp/twsapi \
 && pip install --no-cache-dir --target=/vendor /tmp/twsapi/IBJts/source/pythonclient \
 && python -c "import sys; sys.path.insert(0, '/vendor'); import ibapi; print('vendored ibapi', ibapi.get_version_string())"

# -----------------------------------------------------------------------------
# Stage 2 -- the runtime image
# -----------------------------------------------------------------------------
FROM python:3.12-slim-bookworm

# Build metadata, surfaced by /status and `make status`.
ARG GIT_COMMIT=unknown
ARG BUILD_TIMESTAMP=unknown

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    PYTHONPATH=/app:/vendor \
    GIT_COMMIT=${GIT_COMMIT} \
    BUILD_TIMESTAMP=${BUILD_TIMESTAMP}

# Fixed uid/gid so a bind-mounted ./data and ./logs on the host can be chowned
# to a known owner (see scripts/bootstrap_dirs.sh).
RUN groupadd --gid 10001 trader \
 && useradd --uid 10001 --gid 10001 --home-dir /app --no-create-home --shell /usr/sbin/nologin trader

WORKDIR /app

# Vendored TWS API. Read-only to the application user: nothing at run time has
# any business modifying the library that talks to the broker.
COPY --from=ibapi-build --chown=root:root /vendor /vendor

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
