FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY droboweb ./droboweb
RUN pip install --no-cache-dir . && mkdir -p /data

ENV DROBOWEB_DB_PATH=/data/droboweb.sqlite3 \
    DROBOWEB_HOST=0.0.0.0 \
    DROBOWEB_PORT=8080
EXPOSE 8080
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/healthz')" || exit 1

# Talking to /dev/sgN with vendor commands needs CAP_SYS_RAWIO, so this runs as
# root inside the container; give it only the one device and that one capability.
CMD ["droboweb", "serve"]
