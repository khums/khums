# syntax=docker/dockerfile:1
ARG MAVEN_IMAGE=maven:3.9.11-eclipse-temurin-17@sha256:e4a7ace3dc0d645ed97f8d9ad0b0d3f0b14fa8d150138f27f116d7105a639b82
ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:54c85f3c47607a77f32adec749d3c81d1348bf25833671f512b26a9b6d778cb3

FROM ${MAVEN_IMAGE} AS source
ARG BASELINE_REPO=https://github.com/apache/mina.git
ARG BASELINE_COMMIT=4e50333219a5e69d0dc449740be0811602878dc2
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && git clone --filter=blob:none "${BASELINE_REPO}" /src \
    && cd /src && git checkout --detach "${BASELINE_COMMIT}" \
    && test "$(git rev-parse HEAD)" = "${BASELINE_COMMIT}" && rm -rf .git
WORKDIR /src
# The snapshot carries a stale comment describing behavior the code does not have;
# drop it so source comments do not point at the defective block. Baseline and
# testbed are both derived from this tree, so they stay identical.
RUN sed -i '/Use initialize=false to prevent static block execution/d' \
      mina-core/src/main/java/org/apache/mina/core/buffer/AbstractIoBuffer.java \
    && ! grep -q 'Use initialize=false to prevent' mina-core/src/main/java/org/apache/mina/core/buffer/AbstractIoBuffer.java
RUN mvn -B -ntp -pl mina-core -am -DskipTests package \
    && mvn -B -ntp -pl mina-core -Dtest=IoBufferTest test

FROM ${PYTHON_IMAGE} AS python-runtime

FROM ${MAVEN_IMAGE} AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
      bash ca-certificates curl git patch procps sudo tmux util-linux \
    && rm -rf /var/lib/apt/lists/*
COPY --from=python-runtime /usr/local /opt/python3.12
RUN ln -s /opt/python3.12/bin/python3.12 /usr/local/bin/python3 \
    && /opt/python3.12/bin/python3.12 -m venv /opt/rewardkit-venv \
    && /opt/rewardkit-venv/bin/pip install --no-cache-dir 'pytest==8.4.2' 'harbor-rewardkit[all]==0.1.7' botocore \
    && useradd --uid 10001 --create-home --shell /bin/bash agent \
    && useradd --uid 10002 --no-create-home --shell /usr/sbin/nologin checker \
    && mkdir -p /testbed/reproducer /app/output /opt/checker-baseline /opt/task-checker /opt/task-policy /opt/maven-cache
ENV PATH="/opt/rewardkit-venv/bin:/usr/local/bin:${PATH}" MAVEN_OPTS="-Dmaven.repo.local=/opt/maven-cache" LITELLM_LOCAL_MODEL_COST_MAP=True
COPY --from=source /root/.m2/repository /opt/maven-cache
COPY --from=source /src /testbed
COPY --from=source /src /opt/checker-baseline
COPY . /opt/task-assets
RUN if [ -d /opt/task-assets/reproducer ]; then cp -a /opt/task-assets/reproducer/. /testbed/reproducer/; fi \
    && if [ -d /opt/task-assets/probe ]; then cp -a /opt/task-assets/probe/. /opt/task-checker/; fi \
    && rm -rf /opt/task-assets \
    && chown -R root:root /opt/maven-cache /opt/checker-baseline /opt/task-checker /opt/task-policy /testbed/reproducer \
    && chmod -R a+rX /opt/maven-cache && chmod -R go-rwx /opt/checker-baseline /opt/task-checker /opt/task-policy \
    && chmod -R a-w,go+rX /testbed/reproducer && chmod 0555 /testbed/reproducer \
    && if [ -f /testbed/reproducer/run-all.sh ]; then chmod 0555 /testbed/reproducer/run-all.sh; fi \
    && chmod 0500 /opt/task-checker/run-sealed-probe.py && ln -s /opt/task-checker/run-sealed-probe.py /usr/local/bin/run-sealed-probe \
    && printf '%s\n' 'agent ALL=(root) NOPASSWD: /usr/local/bin/run-sealed-probe public' >/etc/sudoers.d/public-probe && chmod 0440 /etc/sudoers.d/public-probe \
    && chown -R agent:agent /testbed/mina-core/src/main /app/output \
    && find /testbed -type d -path '*/src/test' -prune -exec chown -R root:root {} + -exec chmod 0700 {} + \
    && find /testbed -type d -path '*/target/*' \( -name test-classes -o -name surefire-reports -o -name generated-test-sources -o -name testCompile \) -prune -exec rm -rf {} + \
    && test -z "$(find /testbed -type d -path '*/target/*' \( -name test-classes -o -name surefire-reports -o -name testCompile \))" \
    && chmod 0555 /testbed && test ! -e /testbed/.git \
    && runuser -u agent -- test ! -r /testbed/mina-core/src/test \
    && runuser -u agent -- test -w /testbed/mina-core/src/main/java/org/apache/mina/core/buffer/AbstractIoBuffer.java \
    && command -v java && command -v mvn && command -v pytest && command -v rewardkit && command -v tmux
WORKDIR /testbed
CMD ["sleep", "infinity"]
