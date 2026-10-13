# syntax=docker/dockerfile:1

# ---- builder: fetch pinned baseline source and Go module cache, offline afterwards ----
FROM golang:1.25-alpine AS builder
RUN apk update && apk add --no-cache git ca-certificates
WORKDIR /src
RUN git clone --no-checkout https://github.com/esm-dev/esm.sh.git . \
    && git checkout e5296da490fdc779affb5016557c755e0e217c1c \
    && rm -rf .git
RUN go mod download
RUN go build -o esmd ./server/esmd

# Sealed reproducer harness: compiled here from source that is NEVER copied
# into the runtime image, so a solver cannot recover the exact per-case
# fixture construction (payload shapes) by reading it -- it behaves like
# /testbed/esmd itself under `grep`/`cat`: a binary-file match, no content.
# This source only reaches the distributable task package as a build input
# (needed to rebuild the image), never the running container's /testbed.
COPY reproducer/harness.go /harness-src/harness.go
COPY reproducer/launcher.go /launcher-src/launcher.go
# One random launch token, generated and baked into both binaries in a single
# step (never written to a file or layer). The harness refuses to run unless
# the launcher passes it this token, so run-all.sh is the only way to start it.
RUN TOKEN="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')" \
    && cd /harness-src && go build -ldflags "-X main.launchToken=$TOKEN" -o /harness harness.go \
    && cd /launcher-src && go build -ldflags "-X main.launchToken=$TOKEN" -o /launcher launcher.go

# harbor-rewardkit requires Python >=3.12; Debian bookworm's system python3 is
# 3.11, so borrow a real Python 3.12 toolchain from its own image rather than
# building one from source.
FROM python:3.12-slim-bookworm AS pybase

# ---- runtime: no repository history, no upstream/authoring material, offline ----
# Debian base (not Alpine/musl): harbor-rewardkit's dependency graph is large enough
# that pip's resolver needs manylinux prebuilt wheels to stay tractable; Alpine's
# musl wheels force source builds and pip resolution-too-deep failures.
FROM golang:1.25-bookworm AS runtime

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      bash tmux ca-certificates curl git patch \
    && rm -rf /var/lib/apt/lists/*

# The orchestrator/verifier invoke this image via `bash -lc "..."` (a login
# shell), which sources /etc/profile and resets PATH to Debian's default
# (/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin), dropping the
# golang image's own /usr/local/go/bin and $GOPATH/bin ENV entries. Symlink go
# into a directory that survives that reset instead of relying on PATH ENV.
RUN ln -s /usr/local/go/bin/go /usr/local/bin/go \
    && ln -s /usr/local/go/bin/gofmt /usr/local/bin/gofmt

COPY --from=pybase /usr/local /opt/python3.12

COPY constraints.txt /tmp/constraints.txt
RUN /opt/python3.12/bin/python3.12 -m venv /opt/rewardkit-venv \
    && /opt/rewardkit-venv/bin/pip install --no-cache-dir -c /tmp/constraints.txt \
         "harbor-rewardkit[all]==0.1.7" "pytest==8.3.4" \
    && rm -f /tmp/constraints.txt \
    && /opt/rewardkit-venv/bin/rewardkit --help >/dev/null

RUN useradd -m -s /bin/bash agent

COPY --from=builder /src /testbed
COPY --from=builder /go/pkg/mod /go/pkg/mod

# Public, read-only reproducer, all root-owned and unmodifiable by the agent:
# - run-all.sh (0555): the documented entry point; it execs the launcher.
# - launcher (0111): builds /testbed and starts the harness with the launch token.
# - harness (0111): refuses to run without the token.
# Both binaries are execute-only, so neither the token nor the harness
# internals can be read; their .go sources are builder-stage-only inputs and
# are never copied into this image.
COPY --from=builder /harness /testbed/reproducer/harness
COPY --from=builder /launcher /testbed/reproducer/launcher
COPY reproducer/run-all.sh /testbed/reproducer/run-all.sh
RUN chown -R root:root /testbed/reproducer \
    && find /testbed/reproducer -type d -exec chmod 0555 {} \; \
    && find /testbed/reproducer -type f -exec chmod 0555 {} \; \
    && chmod 0111 /testbed/reproducer/harness /testbed/reproducer/launcher

# Baseline snapshot (taken after the reproducer is in place, matching what the agent
# actually starts from) for the verifier's scope-compliance check. Root-owned,
# unreadable by the agent, so the diff target itself cannot be tampered with.
RUN mkdir -p /opt/task-baseline && \
    cp -a /testbed/. /opt/task-baseline/ && \
    find /testbed -mindepth 1 -printf '%P\t%m\t%y\t%l\n' > /opt/task-baseline-modes.tsv && \
    chown -R root:root /opt/task-baseline && chmod -R go-rwx /opt/task-baseline && \
    chmod 0600 /opt/task-baseline-modes.tsv

RUN chown -R agent:agent /testbed && \
    chown -R root:root /testbed/reproducer && \
    chmod 0555 /testbed/reproducer /testbed/reproducer/run-all.sh && \
    chmod 0111 /testbed/reproducer/harness /testbed/reproducer/launcher

# Final-image isolation check, run as the declared agent user. Fails the build
# if the agent can read/copy either sealed binary, or can start the harness
# directly (with no token or a forged one) instead of through run-all.sh.
RUN runuser -u agent -- sh -c '\
      for B in /testbed/reproducer/harness /testbed/reproducer/launcher; do \
        if cat "$B" >/dev/null 2>&1; then echo "agent can cat $B"; exit 1; fi; \
        if strings "$B" >/dev/null 2>&1; then echo "agent can run strings on $B"; exit 1; fi; \
        if cp "$B" /tmp/sealed-copy 2>/dev/null; then echo "agent can copy $B"; exit 1; fi; \
      done; \
      H=/testbed/reproducer/harness; \
      out=$("$H" /bin/true "" 2>&1) && { echo "agent can start harness directly"; exit 1; }; \
      echo "$out" | grep -q "run-all.sh; run that instead" || { echo "harness did not refuse direct start: $out"; exit 1; }; \
      out=$(ESM_REPRO_LAUNCH=forged "$H" /bin/true "" 2>&1) && { echo "harness accepted a forged token"; exit 1; }; \
      echo "$out" | grep -q "run-all.sh; run that instead" || { echo "harness did not refuse forged token: $out"; exit 1; }'

RUN mkdir -p /app/output && chown agent:agent /app/output
RUN mkdir -p /logs/agent /logs/verifier /home/agent/.tmp && \
    chown agent:agent /logs/agent /home/agent/.tmp && \
    chown root:root /logs/verifier
ENV TMPDIR=/home/agent/.tmp TMP=/home/agent/.tmp TEMP=/home/agent/.tmp

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV GOFLAGS=-mod=mod GOPROXY=off GOSUMDB=off
WORKDIR /testbed
ENTRYPOINT ["/entrypoint.sh"]
