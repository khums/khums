# syntax=docker/dockerfile:1
ARG NODE_IMAGE=node:24-bookworm-slim@sha256:d6aa754f16b3197301076f047b5def2f02ea1dbbc2ca920407d46d7ec7f87b20
ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:54c85f3c47607a77f32adec749d3c81d1348bf25833671f512b26a9b6d778cb3

FROM ${NODE_IMAGE} AS source
ARG BASELINE_REPO=https://github.com/lxsmnsyc/seroval.git
ARG BASELINE_COMMIT=56c02841986116148dfbd3428be89d1f84f3e483
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && git clone --filter=blob:none "${BASELINE_REPO}" /src \
    && cd /src && git checkout --detach "${BASELINE_COMMIT}" \
    && test "$(git rev-parse HEAD)" = "${BASELINE_COMMIT}" && rm -rf .git \
    && corepack enable && corepack prepare pnpm@10.6.5 --activate
WORKDIR /src
RUN pnpm install --no-frozen-lockfile --ignore-scripts && pnpm --filter seroval build

FROM ${PYTHON_IMAGE} AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
      bash ca-certificates curl git patch procps sudo tmux util-linux \
    && rm -rf /var/lib/apt/lists/*
COPY --from=source /usr/local/bin/node /usr/local/bin/node
COPY --from=source /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN node /usr/local/lib/node_modules/npm/bin/npm-cli.js install -g --force pnpm@10.6.5
RUN python3 -m venv /opt/rewardkit-venv \
    && /opt/rewardkit-venv/bin/pip install --no-cache-dir 'pytest==8.4.2' 'harbor-rewardkit[all]==0.1.7' 'botocore==1.40.46' \
    && useradd --uid 10001 --create-home --shell /bin/bash agent \
    && useradd --uid 10002 --no-create-home --shell /usr/sbin/nologin checker \
    && mkdir -p /testbed/reproducer /app/output /opt/checker-baseline /opt/task-checker /opt/task-policy
ENV PATH="/opt/rewardkit-venv/bin:/usr/local/bin:${PATH}" LITELLM_LOCAL_MODEL_COST_MAP=True
COPY --from=source /src /testbed
COPY --from=source /src /opt/checker-baseline
COPY . /opt/task-assets
RUN python3 /opt/task-assets/checker-runtime.py /opt/checker-runtime \
    && mkdir -p /opt/task-policy/private \
    && chown root:checker /opt/task-policy /opt/task-policy/private \
    && chmod 0750 /opt/task-policy /opt/task-policy/private \
    && if [ -d /opt/task-assets/reproducer ]; then cp -a /opt/task-assets/reproducer/. /testbed/reproducer/; fi \
    && if [ -d /opt/task-assets/probe ]; then cp -a /opt/task-assets/probe/. /opt/task-checker/; fi \
    && rm -rf /opt/task-assets \
    && chown -R root:root /opt/checker-baseline /opt/task-checker /opt/checker-runtime /testbed/reproducer \
    && chmod -R go-rwx /opt/checker-baseline /opt/task-checker /opt/checker-runtime \
    && chmod -R a-w,go+rX /testbed/reproducer && chmod 0555 /testbed/reproducer \
    && if [ -f /testbed/reproducer/run-all.sh ]; then chmod 0555 /testbed/reproducer/run-all.sh; fi \
    && chmod 0500 /opt/task-checker/run-sealed-probe.py && ln -s /opt/task-checker/run-sealed-probe.py /usr/local/bin/run-sealed-probe \
    && printf '%s\n' 'agent ALL=(root) NOPASSWD: /usr/local/bin/run-sealed-probe public' >/etc/sudoers.d/public-probe && chmod 0440 /etc/sudoers.d/public-probe \
    && chown -R agent:agent /testbed/packages/seroval/src /testbed/packages/seroval/dist /app/output \
    && chmod 0555 /testbed && test ! -e /testbed/.git \
    && command -v node && command -v pnpm && command -v pytest && command -v rewardkit && command -v tmux
RUN runuser -u agent -- node -e 'const fs=require("fs");const w=p=>{try{fs.accessSync(p,fs.constants.W_OK);return true}catch{return false}};const r=p=>{try{fs.accessSync(p,fs.constants.R_OK);return true}catch{return false}};if(!w("/testbed/packages/seroval/src/core/context/deserializer.ts")||!w("/testbed/packages/seroval/src/core/base-primitives.ts")||w("/testbed/reproducer/run-all.sh")||r("/opt/task-checker/groups.json")||r("/opt/checker-baseline"))process.exit(1)'
WORKDIR /testbed
CMD ["sleep", "infinity"]
