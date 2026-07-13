# ==========================================
# 1. Base Builder and Dependency Stage
# ==========================================
FROM python:3.12-slim AS builder

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Install system compilation packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Generate standard virtual environment to bypass non-root access limits
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install dependencies inside the virtual environment
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir -r requirements.txt


# ==========================================
# 2. Production Non-Root Runtime Stage
# ==========================================
FROM python:3.12-slim AS runner

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
    useradd -m -u 1000 -g django-group -s /bin/bash django-user

# Copy project assets and code setting ownership on import
COPY --chown=django-user:django-group . .

# Grant write permissions to the WORKDIR itself
RUN chown django-user:django-group /app

USER django-user

# Collect static files on container boot
RUN python manage.py collectstatic --noinput || true

EXPOSE 8080

# Run database initialisation and standard production server Gunicorn
CMD ["sh", "-c", "python manage.py migrate && (python init_surreal.py &) && if [ \"$DJANGO_DEBUG\" = \"True\" ]; then gunicorn --bind :8080 --workers 2 --threads 4 --timeout 120 core.wsgi:application; else gunicorn --bind :8080 --workers 2 --threads 4 --timeout 120 --log-level warning core.wsgi:application; fi"]


