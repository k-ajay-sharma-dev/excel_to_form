# Microsoft's Playwright image already contains Chromium and all its system libraries.
# Keep the tag in step with the playwright version in requirements.txt.
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DJANGO_DEBUG=0

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render passes the port in $PORT. One worker: background fills run in threads of this process
# and share one lock, so a single process keeps only one browser open at a time (fits 512 MB).
CMD python manage.py migrate --noinput && \
    gunicorn kobo_web.wsgi:application --bind 0.0.0.0:${PORT:-8000} --workers 1 --threads 4 --timeout 120
