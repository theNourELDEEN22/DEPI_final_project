FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1     PYTHONUNBUFFERED=1     TZ=Africa/Cairo     APP_ENV=staging

WORKDIR /app

COPY . /app

CMD ["python", "-m", "foundation.staging_deploy", "--verify-only"]
