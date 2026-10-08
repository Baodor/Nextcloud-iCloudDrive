FROM golang:1.26-alpine AS rclone
RUN apk add --no-cache python3
WORKDIR /build
# Go verifies the pinned upstream source against its public module checksum.
RUN go mod init icloud-bridge-build && go mod download github.com/rclone/rclone@v1.75.1 \
    && cp -R /go/pkg/mod/github.com/rclone/rclone@v1.75.1 /build/rclone \
    && chmod -R u+w /build/rclone
COPY rclone /build/bridge-fix
RUN python3 /build/bridge-fix/apply.py /build/rclone
WORKDIR /build/rclone
RUN gofmt -w bridge_main.go backend/iclouddrive/bridge_iwork*.go \
    backend/iclouddrive/iclouddrive.go backend/iclouddrive/api/drive.go \
    && CGO_ENABLED=0 go test -v ./backend/iclouddrive -run '^TestBridgeIWork' \
    && CGO_ENABLED=0 go build -trimpath \
    -ldflags='-s -w -X github.com/rclone/rclone/fs.Version=v1.75.1-icloud-bridge.1' \
    -o /usr/local/bin/rclone bridge_main.go \
    && chmod 755 /usr/local/bin/rclone
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
