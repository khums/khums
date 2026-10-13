# Remediation Plan

## Root causes identified

- **Rewrite evaluated after the access policy (name and type bypass).** The handler chain is built from the `Directives` order generated from `plugin.cfg`. `acl` ran before `rewrite`, so the policy evaluated the client's original question, and `rewrite` then changed the name or type that was actually resolved. A query for an allowed name or type could be rewritten into a blocked zone or blocked type and still be answered.
- **Cache consulted before the access policy.** Once `acl` follows `rewrite`, `cache` (ordered before `rewrite`) would answer cache hits without calling the rest of the chain. An answer cached for an allowed client would then be served to a client that the policy blocks.
- **Search-path expansion after the access policy.** `autopath` ran after `acl`. When the original name was NXDOMAIN, autopath retried the search-path expansions downstream of the policy, so an allowed short name could be answered from an ACL-protected zone.
- **Generator input out of sync with generated files.** `plugin.cfg` lacked the `proxyproto` entry that the committed `zdirectives.go`/`zplugin.go` contained, so `go generate coredns.go` changed the generated files and would silently drop the plugin.

## Production changes

- `plugin.cfg`: added `proxyproto:proxyproto`, and ordered the request pipeline as `rewrite`, `autopath`, `acl`, `cache`, so the policy evaluates the final question (after rewriting and search-path expansion) and runs before any cached answer is returned.
- Regenerated `core/dnsserver/zdirectives.go` and `core/plugin/zplugin.go` with `go generate coredns.go`; regenerating again produces no changes.

## Preserved legitimate behavior

- Name and type rewrites into allowed zones and types still answer.
- Allowed clients still receive cached answers; caching is unchanged for permitted queries.
- Search-path expansion into allowed zones still answers.
- Direct ACL allow/block decisions and ordinary resolution are unchanged; every previously available plugin, including `proxyproto`, remains compiled in.

## Validation

- `CGO_ENABLED=0 go build -tags grpcnotrace -o coredns .` succeeded.
- `go generate coredns.go` produced no changes after the fix.
- `/testbed/reproducer/run-all.sh`: `access-enforcement: PASS`, `configuration-sync: PASS`, `Results: PASS`.
