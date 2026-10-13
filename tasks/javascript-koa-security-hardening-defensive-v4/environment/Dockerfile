ARG NODE_IMAGE=node:24-bookworm-slim@sha256:d6aa754f16b3197301076f047b5def2f02ea1dbbc2ca920407d46d7ec7f87b20
ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:54c85f3c47607a77f32adec749d3c81d1348bf25833671f512b26a9b6d778cb3

FROM ${NODE_IMAGE} AS baseline
ARG BASELINE_REPO=https://github.com/koajs/koa.git
ARG BASELINE_COMMIT=77cbf2e47ea7067c937b0f21ea0aa4fb73bac115
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --filter=blob:none "${BASELINE_REPO}" /testbed \
    && cd /testbed \
    && git checkout --detach "${BASELINE_COMMIT}" \
    && test "$(git rev-parse HEAD)" = "${BASELINE_COMMIT}" \
    && rm -rf .git \
    && test ! -e .git

FROM ${NODE_IMAGE} AS node-runtime

FROM ${PYTHON_IMAGE} AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
      bash ca-certificates curl git patch procps tmux util-linux sudo \
    && rm -rf /var/lib/apt/lists/*
COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node-runtime /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && node --version \
    && npm --version

RUN python3 -m venv /opt/rewardkit-venv \
    && /opt/rewardkit-venv/bin/pip install --no-cache-dir \
         'harbor-rewardkit[all]==0.1.7' 'pytest==8.4.2' \
    && /opt/rewardkit-venv/bin/python -c 'import rewardkit, pytest' \
    && /opt/rewardkit-venv/bin/rewardkit --help >/dev/null
ENV PATH="/opt/rewardkit-venv/bin:/usr/local/bin:${PATH}"
ENV LITELLM_LOCAL_MODEL_COST_MAP=True
RUN printf '%s\n' 'export PATH="/opt/rewardkit-venv/bin:$PATH"' >/etc/profile.d/rewardkit.sh \
    && chmod 0644 /etc/profile.d/rewardkit.sh \
    && useradd --uid 10001 --create-home --shell /bin/bash agent \
    && useradd --uid 10002 --no-create-home --shell /usr/sbin/nologin checker \
    && mkdir -p /app/output /testbed/reproducer /opt/task-checker/probe

COPY --from=baseline /testbed /testbed
COPY --from=baseline /testbed /opt/task-baseline
RUN find /opt/task-baseline -type d \( -name node_modules -o -name .git -o -name reproducer -o -name testdata -o -name dist \) -prune -exec rm -rf {} + \
    && chown -R root:root /opt/task-baseline \
    && find /opt/task-baseline -type d -exec chmod 0700 {} + \
    && find /opt/task-baseline -type f -exec chmod 0400 {} + \
    && test "$(stat -c '%U:%a' /opt/task-baseline)" = 'root:700' \
    && test ! -e /opt/task-baseline/.git \
    && test ! -e /opt/task-baseline/node_modules
WORKDIR /testbed
RUN npm ci --ignore-scripts --no-audit --no-fund \
    && npm run build \
    && test -d node_modules \
    && test ! -e .git \
    && chown -R root:root /testbed \
    && chmod -R a-w,go+rX /testbed \
    && chown -R agent:agent /testbed/lib /testbed/dist /app/output \
    && chmod -R u+w /testbed/lib /testbed/dist \
    && chmod 0555 /testbed /testbed/reproducer \
    && chown -R root:root /opt/task-checker \
    && chmod -R go-rwx /opt/task-checker

# Optional public launcher and sealed probe enter through the environment build context.
COPY . /opt/task-assets
RUN if [ -d /opt/task-assets/reproducer ]; then \
      cp -a /opt/task-assets/reproducer/. /testbed/reproducer/; \
    fi \
    && if [ -d /opt/task-assets/probe ]; then \
      cp -a /opt/task-assets/probe/. /opt/task-checker/probe/; \
    fi \
    && chown -R root:root /testbed/reproducer /opt/task-checker \
    && chmod -R a-w,go+rX /testbed/reproducer \
    && chmod 0555 /testbed/reproducer \
    && chmod -R go-rwx /opt/task-checker \
    && if [ -f /opt/task-checker/probe/run-sealed-probe.py ]; then \
         chmod 0500 /opt/task-checker/probe/run-sealed-probe.py; \
         ln -s /opt/task-checker/probe/run-sealed-probe.py /usr/local/bin/run-sealed-probe; \
         printf '%s\n' 'agent ALL=(root) NOPASSWD: /usr/local/bin/run-sealed-probe public' >/etc/sudoers.d/koa-probe; \
         chmod 0440 /etc/sudoers.d/koa-probe; \
       fi \
    && if [ -f /testbed/reproducer/run-all.sh ]; then \
         chmod 0555 /testbed/reproducer/run-all.sh; \
         test -x /testbed/reproducer/run-all.sh; \
       fi \
    && rm -rf /opt/task-assets \
    && test ! -e /testbed/.git \
    && test "$(stat -c '%U:%a' /testbed)" = 'root:555' \
    && test "$(stat -c '%U' /testbed/node_modules)" = root \
    && test "$(stat -c '%U' /testbed/lib)" = agent \
    && test "$(stat -c '%U:%a' /testbed/reproducer)" = 'root:555'

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chown root:root /usr/local/bin/entrypoint.sh \
    && chmod 0555 /usr/local/bin/entrypoint.sh \
    && command -v python3 >/dev/null \
    && command -v python >/dev/null \
    && command -v pytest >/dev/null \
    && command -v rewardkit >/dev/null \
    && command -v node >/dev/null \
    && command -v npm >/dev/null \
    && command -v git >/dev/null \
    && command -v patch >/dev/null \
    && command -v tmux >/dev/null \
    && command -v runuser >/dev/null \
    && python3 -c 'import rewardkit, pytest' \
    && rewardkit --help >/dev/null \
    && test -x /usr/local/bin/entrypoint.sh

WORKDIR /testbed
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["sleep", "infinity"]
