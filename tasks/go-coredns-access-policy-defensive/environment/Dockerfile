# syntax=docker/dockerfile:1

# ---- seed: fetch baseline source and pre-populate the module cache ----
FROM golang:1.25-alpine AS seed
RUN apk add --no-cache git ca-certificates
WORKDIR /src
RUN git clone --no-checkout https://github.com/coredns/coredns.git . \
    && git checkout 3d31397fe65d44f407fa4a1529b1695f10a47c4d \
    && rm -rf .git
# Download all modules declared by the baseline; keep the cache for the runtime image.
RUN CGO_ENABLED=0 go mod download \
    && mkdir -p /go/pkg/sumdb
# Verify the baseline build command succeeds before the runtime image is committed.
RUN CGO_ENABLED=0 go build -tags grpcnotrace -o /coredns-baseline .

# Also download miekg/dns for the harness (already in the module graph; this is a no-op).
# The harness module will use the same module cache via GOMODCACHE=/go/pkg/mod.
RUN go list -m all | grep miekg/dns

# ---- harness-builder: compile the sealed reproducer harness inside the seed cache ----
FROM golang:1.25-alpine AS harness-builder
RUN apk add --no-cache git ca-certificates
COPY --from=seed /go/pkg/mod /go/pkg/mod
COPY --from=seed /go/pkg/sumdb /go/pkg/sumdb
# Harness source is provided by Agent C (environment/reproducer/harness.go).
# A stub is present here so the image builds before Agent C produces the real harness;
# the integration step will use the real one once Agent C is done.
COPY reproducer/harness.go /harness-src/harness.go
COPY reproducer/go.mod /harness-src/go.mod
COPY reproducer/go.sum /harness-src/go.sum
# Download any deps not already in the seed cache, then build.
ENV GONOSUMDB=* GOFLAGS=-mod=mod
RUN cd /harness-src && go mod download && CGO_ENABLED=0 go build -o /harness harness.go

# ---- python base for rewardkit ----
FROM python:3.12-slim-bookworm AS pybase

# ---- runtime: closed-book, no network after image build ----
FROM golang:1.25-bookworm AS runtime

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      bash tmux ca-certificates git patch procps util-linux \
    && rm -rf /var/lib/apt/lists/*

# Symlink go into a PATH-stable location that survives login-shell resets.
RUN ln -sf /usr/local/go/bin/go /usr/local/bin/go \
    && ln -sf /usr/local/go/bin/gofmt /usr/local/bin/gofmt \
    && go version

# Bring in Python 3.12 from the dedicated image.
COPY --from=pybase /usr/local /opt/python3.12

# Install harbor-rewardkit and pytest into an isolated venv.
COPY constraints.txt /tmp/constraints.txt
RUN /opt/python3.12/bin/python3.12 -m venv /opt/rewardkit-venv \
    && /opt/rewardkit-venv/bin/pip install --no-cache-dir -c /tmp/constraints.txt \
         "harbor-rewardkit[all]==0.1.7" "pytest==8.3.4" "dnspython==2.7.0" \
    && rm -f /tmp/constraints.txt \
    && /opt/rewardkit-venv/bin/rewardkit --help >/dev/null \
    && /opt/rewardkit-venv/bin/python -c "import rewardkit, pytest, dns; print('rewardkit ok')"

# Ensure rewardkit venv is first on PATH for both ENV and login-shell profiles.
ENV PATH="/opt/rewardkit-venv/bin:${PATH}"
RUN echo 'export PATH="/opt/rewardkit-venv/bin:$PATH"' > /etc/profile.d/rewardkit.sh \
    && chmod 0644 /etc/profile.d/rewardkit.sh

# Create the agent user (no USER directive: Daytona requires root entrypoint) and
# the unprivileged verifier worker that builds and runs submitted code.
RUN useradd -m -s /bin/bash agent \
    && useradd -M -d /nonexistent -s /usr/sbin/nologin checker

# Populate /testbed from the baseline source (no .git, no upstream history).
COPY --from=seed /src /testbed
# Populate the Go module cache so offline builds and tests work.
COPY --from=seed /go/pkg/mod /go/pkg/mod
COPY --from=seed /go/pkg/sumdb /go/pkg/sumdb

# Verify baseline build and focused test packages succeed inside the final image.
RUN cd /testbed && CGO_ENABLED=0 go build -tags grpcnotrace -o /tmp/coredns-check . \
    && /tmp/coredns-check --help 2>&1 | head -1 \
    && rm -f /tmp/coredns-check
RUN cd /testbed && go test -count=1 -timeout=120s \
      ./plugin/acl/... ./plugin/rewrite/... ./plugin/cache/... \
      ./plugin/autopath/... ./core/dnsserver/... 2>&1 | tail -10

# Warm the verifier worker's private build cache (checker-only, 0700) so the
# deterministic pass rebuilds submitted source quickly and offline.
RUN mkdir -p /opt/checker-gocache /tmp/checker-warm && chown checker:checker /opt/checker-gocache /tmp/checker-warm \
    && chmod 0700 /opt/checker-gocache && cp -a /testbed /tmp/checker-warm/src && chown -R checker:checker /tmp/checker-warm \
    && cd /tmp/checker-warm/src && runuser -u checker -- env -i PATH=/usr/local/go/bin:/usr/bin:/bin HOME=/tmp/checker-warm \
         GOPATH=/go GOMODCACHE=/go/pkg/mod GOCACHE=/opt/checker-gocache GOFLAGS=-mod=mod GOPROXY=off GOSUMDB=off \
         GOTOOLCHAIN=local CGO_ENABLED=0 sh -c 'go build -tags grpcnotrace -o /tmp/checker-warm/coredns . \
         && go test -count=1 -run XXX_NONE ./plugin/acl/... ./plugin/rewrite/... ./plugin/cache/... ./plugin/autopath/... ./core/dnsserver/... >/dev/null' \
    && cd / && rm -rf /tmp/checker-warm

# Sealed reproducer harness: execute-only binary so the agent cannot read fixtures.
COPY --from=harness-builder /harness /testbed/reproducer/harness
COPY reproducer/run-all.sh /testbed/reproducer/run-all.sh
RUN chown -R root:root /testbed/reproducer \
    && find /testbed/reproducer -type d -exec chmod 0555 {} \; \
    && find /testbed/reproducer -type f -exec chmod 0555 {} \; \
    && chmod 0111 /testbed/reproducer/harness

# Snapshot the pristine baseline for scope-compliance checks by the verifier.
RUN mkdir -p /opt/task-baseline \
    && cp -a /testbed/. /opt/task-baseline/ \
    && find /testbed -mindepth 1 -printf '%P\t%m\t%y\t%l\n' > /opt/task-baseline-modes.tsv \
    && chown -R root:root /opt/task-baseline \
    && chmod -R go-rwx /opt/task-baseline \
    && chmod 0600 /opt/task-baseline-modes.tsv

# Transfer /testbed ownership to the agent; keep reproducer root-owned.
RUN chown -R agent:agent /testbed \
    && chown -R root:root /testbed/reproducer \
    && chmod 0555 /testbed/reproducer /testbed/reproducer/run-all.sh \
    && chmod 0111 /testbed/reproducer/harness

# Integration check: agent can execute the harness but cannot read it.
RUN runuser -u agent -- sh -c '\
      H=/testbed/reproducer/harness; \
      "$H" 2>&1 | grep -q "^usage: harness" || { echo "ERROR: agent cannot execute harness or bad usage line"; exit 1; }; \
      if cat "$H" >/dev/null 2>&1; then echo "ERROR: agent can read harness binary"; exit 1; fi; \
      if cp "$H" /tmp/harness-copy 2>/dev/null; then echo "ERROR: agent can copy harness binary"; exit 1; fi; \
      echo "harness seal: OK"'

# Warm the agent's build cache so each public case rebuilds the server quickly.
RUN runuser -u agent -- sh -c 'cd /testbed && HOME=/home/agent CGO_ENABLED=0 GOFLAGS=-mod=mod GOPROXY=off GOSUMDB=off \
      /usr/local/go/bin/go build -tags grpcnotrace -o /home/agent/.warm-coredns . && rm -f /home/agent/.warm-coredns'

# Output and scratch directories.
RUN mkdir -p /app/output \
    && chown agent:agent /app/output
RUN mkdir -p /logs/agent /logs/verifier /home/agent/.tmp \
    && chown agent:agent /logs/agent /home/agent/.tmp \
    && chown root:root /logs/verifier

# Runtime environment: no module proxy or sum database access.
ENV GOFLAGS=-mod=mod GOPROXY=off GOSUMDB=off GONOSUMDB='*'
ENV TMPDIR=/home/agent/.tmp TMP=/home/agent/.tmp TEMP=/home/agent/.tmp

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

WORKDIR /testbed
ENTRYPOINT ["/entrypoint.sh"]
