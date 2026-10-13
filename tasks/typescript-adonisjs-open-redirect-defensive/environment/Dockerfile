# syntax=docker/dockerfile:1
ARG NODE_IMAGE=node:24-bookworm-slim@sha256:d6aa754f16b3197301076f047b5def2f02ea1dbbc2ca920407d46d7ec7f87b20
ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:54c85f3c47607a77f32adec749d3c81d1348bf25833671f512b26a9b6d778cb3

FROM ${NODE_IMAGE} AS source
ARG BASELINE_REPO=https://github.com/adonisjs/http-server.git
ARG BASELINE_COMMIT=f1be0306beacd73f450ab97ed815f50d54299f58
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && git clone --filter=blob:none "${BASELINE_REPO}" /src \
    && cd /src && git checkout --detach "${BASELINE_COMMIT}" \
    && test "$(git rev-parse HEAD)" = "${BASELINE_COMMIT}" \
    && rm -rf .git
WORKDIR /src
RUN npm install --ignore-scripts --no-audit --no-fund && npm run build

FROM ${PYTHON_IMAGE} AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
      bash ca-certificates curl git patch procps sudo tmux util-linux \
    && rm -rf /var/lib/apt/lists/*
COPY --from=source /usr/local/bin/node /usr/local/bin/node
COPY --from=source /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && python3 -m venv /opt/rewardkit-venv \
    && /opt/rewardkit-venv/bin/pip install --no-cache-dir 'pytest==8.4.2' 'harbor-rewardkit[all]==0.1.7' 'botocore==1.43.111' \
    && useradd --uid 10001 --create-home --shell /bin/bash agent \
    && useradd --uid 10002 --no-create-home --shell /usr/sbin/nologin checker \
    && mkdir -p /testbed/reproducer /app/output /opt/checker-baseline /opt/task-checker /opt/task-policy
ENV PATH="/opt/rewardkit-venv/bin:/usr/local/bin:${PATH}" LITELLM_LOCAL_MODEL_COST_MAP=True
ENV NODE_OPTIONS="--dns-result-order=ipv4first"
COPY --from=source /src /testbed
COPY --from=source /src /opt/checker-baseline
COPY . /opt/task-assets
RUN if [ -d /opt/task-assets/reproducer ]; then cp -a /opt/task-assets/reproducer/. /testbed/reproducer/; fi \
    && if [ -d /opt/task-assets/probe ]; then cp -a /opt/task-assets/probe/. /opt/task-checker/; fi \
    && rm -rf /opt/task-assets \
    && chown -R root:root /opt/checker-baseline /opt/task-checker /opt/task-policy /testbed/reproducer \
    && chmod -R go-rwx /opt/checker-baseline /opt/task-checker /opt/task-policy \
    && chmod -R a-w,go+rX /testbed/reproducer && chmod 0555 /testbed/reproducer \
    && if [ -f /testbed/reproducer/run-all.sh ]; then chmod 0555 /testbed/reproducer/run-all.sh; fi \
    && chmod 0555 /testbed/reproducer/run-project-tests.sh \
    && chmod 0500 /opt/task-checker/run-sealed-probe.py \
    && ln -s /opt/task-checker/run-sealed-probe.py /usr/local/bin/run-sealed-probe \
    && printf '%s\n' 'agent ALL=(root) NOPASSWD: /usr/local/bin/run-sealed-probe public' >/etc/sudoers.d/public-probe \
    && printf '%s\n' 'agent ALL=(root) NOPASSWD: /usr/local/bin/run-sealed-probe project-tests, /usr/local/bin/run-sealed-probe project-tests *' >>/etc/sudoers.d/public-probe \
    && chmod 0440 /etc/sudoers.d/public-probe \
    && visudo -cf /etc/sudoers.d/public-probe \
    && chown -R agent:agent /testbed/src /testbed/build /app/output \
    && chmod 0555 /testbed && test ! -e /testbed/.git \
    && command -v node && command -v npm && command -v pytest && command -v rewardkit && command -v tmux
WORKDIR /testbed
CMD ["sleep", "infinity"]
