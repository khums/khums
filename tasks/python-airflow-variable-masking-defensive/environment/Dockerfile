# syntax=docker/dockerfile:1
ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:54c85f3c47607a77f32adec749d3c81d1348bf25833671f512b26a9b6d778cb3

FROM ${PYTHON_IMAGE} AS source
ARG BASELINE_REPO=https://github.com/apache/airflow.git
ARG BASELINE_COMMIT=0cf92e38614c87590511e6f1bcac319f9c4f50bb
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates build-essential \
    && rm -rf /var/lib/apt/lists/* \
    && git clone --filter=blob:none "${BASELINE_REPO}" /src \
    && cd /src && git checkout --detach "${BASELINE_COMMIT}" \
    && test "$(git rev-parse HEAD)" = "${BASELINE_COMMIT}" && rm -rf .git
RUN python3 -m venv /opt/task-venv \
    && /opt/task-venv/bin/pip install --no-cache-dir -e /src/shared/secrets_masker \
       'pytest==8.4.2' 'pytest-asyncio==1.4.0' 'time-machine==2.16.0' \
    && /opt/task-venv/bin/python -m compileall /src/shared/secrets_masker/src

FROM ${PYTHON_IMAGE} AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
      bash ca-certificates curl git patch procps sudo tmux util-linux \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/rewardkit-venv \
    && /opt/rewardkit-venv/bin/pip install --no-cache-dir 'pytest==8.4.2' 'harbor-rewardkit[all]==0.1.7' 'botocore==1.43.111' \
    && useradd --uid 10001 --create-home --shell /bin/bash agent \
    && useradd --uid 10002 --no-create-home --shell /usr/sbin/nologin checker \
    && mkdir -p /testbed/reproducer /app/output /opt/checker-baseline /opt/task-checker /opt/task-policy
ENV PATH="/opt/task-venv/bin:/opt/rewardkit-venv/bin:/usr/local/bin:${PATH}" PYTHONPATH="/testbed/shared/secrets_masker/src" LITELLM_LOCAL_MODEL_COST_MAP=True PYTHONDONTWRITEBYTECODE=1
COPY --from=source /opt/task-venv /opt/task-venv
COPY --from=source /src /testbed
COPY --from=source /src /opt/checker-baseline
COPY . /opt/task-assets
RUN if [ -d /opt/task-assets/reproducer ]; then cp -a /opt/task-assets/reproducer/. /testbed/reproducer/; fi \
    && if [ -d /opt/task-assets/probe ]; then cp -a /opt/task-assets/probe/. /opt/task-checker/; fi \
    && rm -rf /opt/task-assets /testbed/shared/secrets_masker/tests \
    && chown -R root:root /opt/task-venv /opt/checker-baseline /opt/task-checker /opt/task-policy /testbed/reproducer \
    && chmod -R go-rwx /opt/checker-baseline /opt/task-checker /opt/task-policy \
    && chmod -R a-w,go+rX /testbed/reproducer && chmod 0555 /testbed/reproducer \
    && if [ -f /testbed/reproducer/run-all.sh ]; then chmod 0555 /testbed/reproducer/run-all.sh; fi \
    && chmod 0500 /opt/task-checker/run-sealed-probe.py && ln -s /opt/task-checker/run-sealed-probe.py /usr/local/bin/run-sealed-probe \
    && printf '%s\n' 'agent ALL=(root) NOPASSWD: /usr/local/bin/run-sealed-probe public' >/etc/sudoers.d/public-probe && chmod 0440 /etc/sudoers.d/public-probe \
    && /opt/task-venv/bin/python -c 'import sys,pwd; from pathlib import Path; sys.path.insert(0,"/opt/task-checker"); from source_scope import configure_permissions; account=pwd.getpwnam("agent"); configure_permissions(Path("/testbed"), account.pw_uid, account.pw_gid)' \
    && chown -R agent:agent /app/output \
    && find /testbed -type d \( -name tests -o -name test -o -name __tests__ -o -path /testbed/devel-common \) -prune \
         -exec chown root:root {} + -exec chmod 0700 {} + \
    && test ! -e /testbed/.git \
    && command -v python3 && command -v pytest && command -v rewardkit && command -v tmux
RUN runuser -u agent -- /opt/task-venv/bin/python -c 'import os; from pathlib import Path; paths=[Path("/testbed/airflow-core/src/airflow/api_fastapi/common/types.py"),Path("/testbed/shared/secrets_masker/src/airflow_shared/secrets_masker/secrets_masker.py")]; assert all(p.is_file() and os.access(p,os.W_OK) for p in paths); assert not os.access("/testbed/reproducer/run-all.sh",os.W_OK); assert not os.access("/opt/task-checker",os.R_OK); assert not any(os.access(str(p),os.R_OK) for p in Path("/testbed").rglob("tests") if p.is_dir()); assert not os.access("/testbed/devel-common",os.R_OK)'
WORKDIR /testbed
CMD ["sleep", "infinity"]
