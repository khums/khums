# Remediation Plan

## Root causes identified

- Request metadata accessors used a delimiter expression whose work could grow disproportionately on adversarial, whitespace-heavy values. This creates a CPU exhaustion path.
- The HTML body of a redirect made every destination clickable, including script-capable destinations. Escaping HTML characters alone does not make a link target safe.
- Back navigation accepted an untrusted history value as a redirect destination. Even a simple relative-path exception could include a scheme-relative external URL.
- Host derivation could misinterpret malformed authority with userinfo-like syntax, allowing an attacker-controlled apparent hostname to affect downstream origin decisions.

## Production changes

- Parse comma-separated request metadata with bounded-cost splitting and trim the resulting values.
- Render script-capable or malformed redirect destinations as non-clickable text while keeping safe web destinations clickable in HTML responses.
- Use back-navigation history only when it is an ordinary relative path or resolves to the current origin; otherwise use the supplied alternative or root fallback.
- Parse malformed authority defensively before deriving the host, and avoid assigning a spoofed hostname on parse failure.

## Preserved legitimate behavior

The changes retain valid proxy lists, ordinary host and authority forms, normal redirect status and Location, HTML and plain-text responses, safe clickable redirect destinations, and supported same-origin and relative back navigation.

## Validation

The ordered production changes and compatibility adjustment applied cleanly to the baseline. A local Node.js 24 run of `npm run build` succeeded. `npm test -- --runInBand --silent` reported 364 passed and 4 skipped tests. The final `/testbed/reproducer/run-all.sh` execution reported PASS for G1–G4, each 5/5, and summary 20/20. The revised deterministic suite passed 30/30 checks. RewardKit reported deterministic, process, and binary reward of 1.0; the semantic judge was skipped for this Oracle run.
