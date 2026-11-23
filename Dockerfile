FROM python:3.12-slim
WORKDIR /app
COPY runtime/ ./runtime/
COPY memory/ ./memory/
COPY tests/ ./tests/
ENV PYTHONUNBUFFERED=1
CMD ["python", "runtime/survival_cycle.py", "--runner", "vps_cron", "--force"]
