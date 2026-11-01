# Build the same browser UI used by the optional desktop shell.
FROM node:22-alpine AS web
WORKDIR /web
COPY web/package*.json ./
RUN npm ci --ignore-scripts
COPY web/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir uv==0.10.9
COPY pyproject.toml uv.lock ./
COPY control_plane/ ./control_plane/
RUN uv sync --frozen --no-dev --no-editable
COPY alembic.ini ./
COPY migrations/ ./migrations/
COPY --from=web /web/dist ./web/dist
RUN useradd --uid 10001 --create-home platform
ENV PLATFORM_STATIC_DIR=/app/web/dist
ENV PATH="/app/.venv/bin:$PATH"
USER 10001:10001
EXPOSE 8088
# Migrations run as an explicit deployment Job before this service starts.
CMD ["python", "-m", "control_plane", "--host", "0.0.0.0", "--port", "8088"]
