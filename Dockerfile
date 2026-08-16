# ==========================================
# 1. Base Builder and Dependency Stage
# ==========================================
# python:3.14.6-slim-trixie
FROM python@sha256:7bec7ddcddeff7975d6ba9b4be7dd6f6b2f55e7491539145e2978f7f97ce9144 AS builder

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Generate standard virtual environment to bypass non-root access limits
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install dependencies inside the virtual environment
COPY requirements.txt .
# hadolint ignore=DL3013,SC2015
RUN pip install --no-cache-dir --upgrade --only-binary :all: pip==26.0.1 && \
    (pip install --no-cache-dir --only-binary :all: --require-hashes -r requirements.txt 2>/dev/null || \
     pip install --no-cache-dir --only-binary :all: -r requirements.txt)


# ==========================================
# 2. Production Non-Root Runtime Stage
# ==========================================
# python:3.14.6-slim-trixie
FROM python@sha256:7bec7ddcddeff7975d6ba9b4be7dd6f6b2f55e7491539145e2978f7f97ce9144 AS runner

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=8080

# No native runtime libraries required (pure-Python stack)

# Copy virtual environment from builder stage (fully accessible to non-root user)
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Create a dedicated non-privileged user (OWASP SOC2 compliance)
RUN groupadd -g 1000 django-group && \
    useradd -m -u 1000 -g django-group -s /usr/sbin/nologin django-user

# Copy project assets and application code owned by root (read-only for non-root user)
COPY core/ /app/core/
COPY extractor/ /app/extractor/
COPY static/ /app/static/
COPY manage.py schema.surql /app/
COPY scripts/entrypoint.py scripts/init_surreal.py /app/

# Ensure application files and static_root are owned by django-user
RUN mkdir -p /app/static_root && \
    chown -R django-user:django-group /app && \
    chmod -R 755 /app

USER 1000

# Collect static files during container build
RUN DJANGO_SECRET_KEY=dummy-key-for-collectstatic SURREALDB_OFFLINE=True python manage.py collectstatic --noinput

EXPOSE 8080

# Shell-free startup: migrate, start bounded SurrealDB initialization, then exec Gunicorn.
ENTRYPOINT ["python", "/app/entrypoint.py"]
