# Trusted builder recipe, never built or published by contributor bin/ci.
# Immutable multiarch base, tool archives verified by ci/linux-artifacts.json.
FROM node:26.8.2-bookworm-slim@sha256:cd9f682fa2885cd1056e830424764158570061c59736a1da836bc3d73df095ae
ARG TARGETARCH
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install --no-install-recommends -y \
    bash build-essential ca-certificates clang curl git jq pkg-config \
    python3 python3-dev unzip xz-utils zsh \
    xvfb xauth fonts-liberation libgtk-3-0 libnotify4 libnss3 libxss1 libxtst6 \
    xdg-utils libatspi2.0-0 libdrm2 libgbm1 libasound2 \
    libwebkit2gtk-4.1-dev libayatana-appindicator3-dev librsvg2-dev libxdo-dev \
    libssl-dev libffi-dev patchelf \
    && rm -rf /var/lib/apt/lists/*
COPY ci/linux-artifacts.json ci/install-linux-tools.py /opt/ci/build/
COPY scripts/ci/toolchain.json /opt/ci/build/toolchain.json
RUN python3 /opt/ci/build/install-linux-tools.py "$TARGETARCH"
ENV PATH="/opt/ci/rust/bin:${PATH}"
# Install the pinned interpreter once in the immutable image. The checkout owns
# environments/caches, and uv discovers this interpreter through PATH.
RUN UV_PYTHON_INSTALL_DIR=/opt/ci/python uv python install \
    "$(python3 -c 'import json; print(json.load(open("/opt/ci/build/toolchain.json"))["python"])')" \
    && ln -s "$(find /opt/ci/python -path '*/bin/python3.11' -type f | head -1)" /usr/local/bin/python3.11 \
    && ln -s /usr/local/bin/python3.11 /usr/local/bin/python3 \
    && npm install --global "npm@$(python3 -c 'import json; print(json.load(open("/opt/ci/build/toolchain.json"))["npm"])')" \
    && npm cache clean --force \
    && rm -rf /root/.cache /opt/ci/build
# The standalone runner supplies its own unprivileged uid, writable checkout,
# isolated home and bounded scratch mounts. No Docker socket is required here.
WORKDIR /workspace
CMD ["bin/ci"]
