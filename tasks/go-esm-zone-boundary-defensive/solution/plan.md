# Remediation Plan

## Root causes identified

1. **The `X-Zone-Id` request header is trusted verbatim as a filesystem path segment (RC1).** `server/router.go` reads `ctx.R.Header.Get("X-Zone-Id")` at two independent sites — the `POST /transform` build-and-cache write path and the `GET /+<hash>.<ext>` cached-transform read path — and passes it straight into `normalizeSavePath` (`server/build_resolver.go`), which prepends it (plus `"/"`) to the storage key with no validation. `internal/storage/storage_fs.go`'s `fsStorage.Put`/`Get` then resolve that key with `filepath.Join(fs.root, key)`. Go's `filepath.Clean` semantics mean a sufficient run of `../` segments inside the header collapses the joined path outside `fs.root` entirely, so the header — fully attacker-controlled, unauthenticated — determines where on the filesystem the server writes or reads.

2. **Tarball entry names are trusted after only cosmetic normalization (RC2).** `server/npmrc.go`'s `extractPackageTarball` strips just the first path segment of each tar entry name and runs `path.Clean` on the remainder before joining it into the package's extraction directory. `path.Clean` does not confine a relative path to a base directory; a crafted entry name with enough `../` segments still escapes the intended `node_modules/<pkg>` directory when joined. The tarball is fetched from whatever registry serves the package, so its entry names are not trusted input.

3. **The raw HTTP request path reaches the filesystem unvalidated in the CLI's web-serving handler (RC3).** `web/handler.go`'s `ServeHTTP` — which backs the root module's own `serve`/`dev` commands, a separate built program from `server/esmd` — joins the incoming request's raw URL path directly into the configured app directory for the default route, with no traversal or NUL-byte check before the `Lstat`/serve. Building this program at all requires a one-line, unrelated compile-compatibility fix in `web/utils.go` (the pinned `esbuild-internal` dependency renamed a struct field this file still uses under its old name); that fix carries no security implication of its own and is applied purely so the program compiles.

All three share the same underlying pattern: a value that is not trusted input (an HTTP header, a tarball entry name, or a raw URL path) is joined into a filesystem path with only cosmetic normalization (or none), instead of being confined to its intended base directory before the join is trusted.

## Production changes

- `server/router.go` (RC1): at both the `POST /transform` write site and the `GET /+<hash>.<ext>` read site, `X-Zone-Id` is now validated with `valid.IsDomain(zoneId)` immediately after being read, and discarded if invalid, before it reaches `normalizeSavePath`. A third call site (the proxy/x-module branch) is refactored to reuse its own already-validated zone id instead of re-reading the raw header.
- `server/npmrc.go` (RC2): tar entry names are now normalized with `utils.NormalizePathname` (absolute-rooted, so a leading run of `../` collapses at the root instead of escaping) before the first-segment strip and join into the package directory.
- `web/handler.go` (RC3): the request path is validated (`filepath.IsLocal` after stripping the leading slash, plus a NUL-byte check) before being joined into the app directory; invalid paths are rejected with 400 instead of reaching the filesystem.
- `web/utils.go` (unrelated compile-compatibility fix, prerequisite for RC3): the `logger.Source` struct literal in `validateModule` is updated from the removed `PrettyPath string` field to the current `PrettyPaths logger.PrettyPaths{Abs, Rel}` field, matching the pinned `esbuild-internal` dependency version. This file has no security-relevant content; the change is required only so the binary containing RC3's fix compiles at all.

## Preserved legitimate behavior

- Requests with no `X-Zone-Id` header, or with a syntactically valid domain `X-Zone-Id`, continue to build/cache/retrieve transforms exactly as before (RC1).
- Well-formed package tarballs continue to extract normally to their expected locations, including nested legitimate paths (RC2).
- Requests for files that genuinely exist inside the configured app directory continue to be served exactly as before (RC3).
- No public API signature or documented error convention changed in any of the three fixes.

## Validation

Applying, in order, `solution/patches/01-FIX-1-833a29f4.1-upstream.patch`, `solution/patches/02-FIX-2-c62ab83c.1-upstream.patch` (plus its documented adjustment, `02-FIX-2-c62ab83c.2-adjust.patch`), the compile-compatibility adjustment (`solution/adjustments/web-utils-compile-fix.patch`), and `solution/patches/03-FIX-5-468b6b2a.1-upstream.patch` (verbatim upstream diffs, test hunks excluded), then rebuilding both `go build -o esmd ./server/esmd` and `go build -o esmcli .` (both verified to succeed against a real `golang:1.25-alpine` toolchain) and running `/testbed/reproducer/run-all.sh`, is the validation path exercised by this Oracle.

Four additional upstream fixes originally in scope were investigated and dropped, each for a distinct, empirically verified reason: two (targeting `internal/storage/storage_fs.go` and `internal/deno/deno.go`) target APIs/subsystems that do not exist yet at this task's resolved baseline; two others (module-resolution import-specifier traversal and JSON-metadata-resolution traversal) compose and apply cleanly against the baseline and are real, verified fixes, but no executable public/hidden reproducer case could be built for either — one requires narrow esbuild-internal conditions that black-box probing could not reliably trigger, and the other's vulnerable branch was traced and confirmed architecturally unreachable via HTTP given an unrelated pre-existing router guard.
