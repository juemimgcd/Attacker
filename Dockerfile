FROM python:3.12.13-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11.11 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY app ./app
COPY conf ./conf
COPY contracts ./contracts
COPY equipment ./equipment
COPY samples ./samples
COPY alembic ./alembic
COPY alembic.ini main.py README.md LICENSE ./
COPY scripts ./scripts
RUN uv sync --locked --no-dev

FROM python:3.12.13-slim-bookworm AS runtime

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUTF8=1

RUN groupadd --gid 10001 attacker \
    && useradd --uid 10001 --gid attacker --home-dir /app --shell /usr/sbin/nologin attacker

WORKDIR /app
COPY --from=builder --chown=attacker:attacker /app /app
RUN mkdir -p /var/lib/attacker /var/lib/attacker-prometheus /var/log/attacker /run/attacker-secrets \
    && chown -R attacker:attacker /var/lib/attacker /var/lib/attacker-prometheus /var/log/attacker /run/attacker-secrets \
    && chmod 0755 /app/scripts/*.sh

USER 10001:10001
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=3)"]

ENTRYPOINT ["/app/scripts/entrypoint.sh"]
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
