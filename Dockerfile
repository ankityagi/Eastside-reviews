FROM python:3.12-slim

RUN useradd --create-home --uid 10001 app \
 && mkdir /data && chown app:app /data
WORKDIR /app
COPY pyproject.toml README.md ./
COPY recs ./recs
RUN pip install --no-cache-dir ".[server]"

USER app
ENV RECS_HOME=/data \
    RECS_TRUST_PROXY=1 \
    PYTHONUNBUFFERED=1
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
CMD ["gunicorn", "--workers", "2", "--threads", "4", "--bind", "0.0.0.0:8000", "--forwarded-allow-ips", "*", "recs.web:application"]
