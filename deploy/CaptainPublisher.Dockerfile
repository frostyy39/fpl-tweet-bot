FROM python:3.13.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
ENV PYTHONUNBUFFERED=1
CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${PORT:-8080} --workers 1 --threads 2 --timeout 90 --access-logfile - --error-logfile - 'fpl_bot.captain_publisher_runtime:create_app()'"]
