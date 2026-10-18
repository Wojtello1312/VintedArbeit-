FROM python:3.12-slim AS builder
WORKDIR /opt/app
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
RUN --mount=type=secret,id=build_ca \
    if [ -s /run/secrets/build_ca ]; then export PIP_CERT=/run/secrets/build_ca; fi; \
    python -m pip install --no-cache-dir uv==0.12.23
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN --mount=type=secret,id=build_ca \
    if [ -s /run/secrets/build_ca ]; then export SSL_CERT_FILE=/run/secrets/build_ca; fi; \
    uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim AS runtime
WORKDIR /opt/app
ENV PATH="/opt/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    VDH_DB_PATH=/data/offers.sqlite3
RUN groupadd --gid 10001 hunter && useradd --uid 10001 --gid hunter --no-create-home hunter \
    && mkdir /data && chown hunter:hunter /data
COPY --from=builder /opt/app/.venv /opt/app/.venv
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=6s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=5).close()"
CMD ["sh", "-c", "python -m app.migrate && exec uvicorn vinted_deal_hunter.api:create_app --factory --host 0.0.0.0 --port 8000 --workers 1 --no-proxy-headers"]
