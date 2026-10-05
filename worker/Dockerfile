FROM rclone/rclone:1.75.1 AS rclone
FROM python:3.12-slim
COPY --from=rclone /usr/local/bin/rclone /usr/local/bin/rclone
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home bridge \
    && mkdir /data && chown bridge:bridge /data
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bridge ./bridge
COPY config_password.py access-check.txt ./
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=3)"
CMD ["python3", "-m", "bridge.server"]
