FROM node:24-bookworm-slim AS node-runtime
FROM python:3.13-slim-bookworm

COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node-runtime /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm
ARG OPENCODE_VERSION=1.19.0

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates tar \
    && npm install --global opencode-ai@${OPENCODE_VERSION} \
    && pip install --no-cache-dir 'pytest>=8,<10' 'ruff>=0.12,<1' \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
CMD ["sh", "-lc", "tail -f /dev/null"]
