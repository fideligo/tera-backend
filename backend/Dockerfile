FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml ./
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
COPY samples ./samples

RUN pip install --no-cache-dir -e .

EXPOSE 8000

# Migrations run before the server, and the port comes from the environment.
#
# **Both halves were missing and both bite in production.** The image's CMD started uvicorn alone;
# only `docker-compose.yml` ran `alembic upgrade head`, as a `command:` override. Locally that is
# invisible — Compose supplies it — but any other host builds this image and runs *this* line, so a
# deploy would start an API against a schema nobody had migrated. The first request touching a new
# column is where you would find out.
#
# `$PORT` is what a platform host injects to tell the process where to listen (Koyeb, Fly, Render,
# Heroku all do). Hardcoding 8000 works only where the host happens to agree; defaulting to it
# keeps Compose and a bare `docker run` behaving exactly as before.
#
# `alembic upgrade head` is idempotent, so a restart is safe. It is not safe to run concurrently
# from several instances — if this is ever scaled past one, migrations move to a release step that
# runs once, ahead of the rollout.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
