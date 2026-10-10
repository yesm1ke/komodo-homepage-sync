FROM python:3.13-alpine
RUN adduser -D -H -u 10001 app
COPY komodo_homepage_sync.py /app/komodo_homepage_sync.py
USER app
EXPOSE 2375
HEALTHCHECK --interval=30s --timeout=3s CMD wget -q -O /dev/null http://127.0.0.1:2375/_ping || exit 1
ENTRYPOINT ["python3", "-u", "/app/komodo_homepage_sync.py"]
