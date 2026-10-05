FROM rclone/rclone:1.75.1 AS rclone
FROM python:3.12-slim
COPY --from=rclone /usr/local/bin/rclone /usr/local/bin/rclone
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home bridge \
    && mkdir /data && chown bridge:bridge /data
WORKDIR /app
ENV PYTHONPATH=/app PYTHONDONTWRITEBYTECODE=1
COPY requirements.txt .
# Installed packages must also be readable when the builder inherits umask 077.
RUN umask 022 && python3 -m pip install --no-cache-dir -r requirements.txt \
    && chmod -R a+rX /usr/local/lib/python3.12/site-packages
COPY bridge ./bridge
COPY config_password.py access-check.txt ./
# COPY preserves local permissions, including a checkout made with umask 077.
# Keep code root-owned while making it readable by the unprivileged worker.
RUN chmod -R a+rX /app
USER 10001:10001
# Fail during the build if the runtime user cannot import the server.
RUN python3 -c "import bridge.server"
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=3)"
CMD ["python3", "-m", "bridge.server"]
