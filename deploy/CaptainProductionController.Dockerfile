FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN groupadd --system app && useradd --system --gid app --home-dir /app --no-create-home app
RUN mkdir /app/.gunicorn && chown app:app /app/.gunicorn
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install .
USER app
EXPOSE 8080
CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${PORT:-8080} --workers 1 --threads 2 --timeout 90 --access-logfile - --error-logfile - 'fpl_bot.captain_production_controller_runtime:create_app()'"]
