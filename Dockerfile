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
# hadolint ignore=DL3013
RUN pip install --no-cache-dir --upgrade --only-binary :all: pip==26.0.1 && \
    pip install --no-cache-dir --only-binary :all: \
      bleach==6.4.0 \
      django==6.0.8 \
      django-storages[google]==1.14.6 \
      google-cloud-tasks==2.24.0 \
      google-genai==2.17.0 \
      gunicorn==26.0.0 \
      httpx==0.28.1 \
      markdown==3.10.3 \
      protobuf==7.35.1 \
      psycopg[binary]==3.3.4 \
      python-dotenv==1.2.2 \
      pyyaml==6.0.3 \
      ruff==0.16.2 \
      sentry-sdk[django]==2.22.0 \
      sqlparse==0.5.5 \
      surrealdb==2.0.0 \
      whitenoise==6.12.0


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
