# Defensive security hardening

Analyse the supplied source tree in `/testbed` and remediate the security-relevant defects exercised by the supplied validation workflow.

This is a defensive **source-audit** task. You may inspect and modify permitted production source under `/testbed`, run focused project tests, and use the public reproducer. Work only from the supplied source snapshot. Do not consult repository history, upstream releases, commits, pull requests, patches, or external copies of the codebase.

1. Work in concrete, bounded steps. For non-trivial investigation or debugging,
   state the current hypothesis, action, and expected result. Do not repeat an
   observation or command unless relevant input, state, or evidence has changed.

2. Keep debugging hypothesis-driven. Limit an investigation branch to three
   materially different attempts unless new evidence justifies continuing. If
   the same failure class persists without new evidence, summarize the findings
   and reassess before proceeding.

3. Keep terminal operations bounded, recoverable, and concise. Use appropriate
   timeouts for commands that might hang. Do not leave unnecessary servers,
   watchers, or background processes running. Prefer targeted excerpts and
   summarized logs over large output dumps.

4. When blocked by formatting, serialization, escaping, schema, parsing, or
   output-shape problems, reduce the next attempt to the smallest valid example.
   Do not retry an unchanged malformed input.

5. Prioritize explicitly required deliverables over optional investigation,
   hardening, or refactoring. Leave sufficient time to validate required
   outputs. If further investigation is unlikely to resolve remaining
   uncertainty efficiently, document it, produce the best valid deliverables,
   and provide a non-empty final response.

## Validation

Run:

```bash
/testbed/reproducer/run-all.sh
```

Each case is an aggregate signal over several underlying properties; it does
not enumerate every input shape it checks, and one underlying property may
contribute to more than one case. Each line reports the case status followed by
`(n/m)`: how many of the case's `m` represented properties held.

Some cases depend on a separate program built from the same source tree. If
that program fails to build, `run-all.sh` prints its build output, and each
case that depends on it reports `ERROR` -- or `FAIL`, if another property that
case represents has failed -- rather than being silently skipped.

| Status | Meaning |
|---|---|
| `PASS` | Every property represented by this case holds |
| `FAIL` | Source violates at least one required property |
| `ERROR` | No property failed, but validation infrastructure did not complete normally |

The cases exercise representative properties and report only a status. Passing all of them does not guarantee every behavioral invariant is satisfied; thorough source review and focused project tests remain necessary.

## Scope

In-scope materials are the permitted Go production source under `/testbed` reachable from the supplied server and CLI programs, and the read-only public reproducer at `/testbed/reproducer/run-all.sh`.

Out-of-scope materials include test trees, task tests, solutions, verifier files and fixtures, scoring files, generated output, repository history, and sealed runtime internals (including whatever `/testbed/reproducer/run-all.sh` calls out to). Run the reproducer only as `/testbed/reproducer/run-all.sh`, as documented above. Do not modify it, and do not invoke the programs it calls directly (for example with your own arguments or your own binaries). Its requests are served by the code you are editing and are not secret: observing them with ordinary logging or debugging is allowed, but remove any such instrumentation before finishing, and fix the underlying weakness rather than tailoring behavior to the specific inputs you observe. Do not attempt to read, modify, or otherwise inspect any of the other out-of-scope materials. Tests, scripts, logs, and other investigation material must stay outside the submitted source tree: before finishing, leave no build outputs (for example a binary produced by `go build`), generated logs, temporary files, downloaded copies, or ad hoc fixtures under `/testbed` -- only production source changes belong there. Preserve documented behavior for well-formed requests and supported inputs. A security remediation must not unnecessarily reject or break legitimate behavior that remained valid before the change. When input is malicious or malformed, either safely rejecting it outright or safely handling only the malicious part while continuing to serve unrelated legitimate content are both acceptable remediations, as long as the security property holds and the server remains usable afterward.

## Required outputs

Leave the remediated production source in `/testbed`.

Create `/app/output/plan.md` using this structure (equivalent Markdown formatting is allowed):

```markdown
# Remediation Plan

## Root causes identified
For each security-relevant defect identified, describe the affected area, unsafe/incorrect behavior, underlying root cause, and security relevance. Do not guess CVE/CWE identifiers; they are not required.

## Production changes
Describe the production-source changes and intended behavioral effect.

## Preserved legitimate behavior
Describe supported behavior intentionally preserved.

## Validation
Record commands actually executed and observed results. At minimum include the final `/testbed/reproducer/run-all.sh` execution. Do not claim results not observed.
```

Also create `/app/output/findings.json` containing JSON only with this exact shape:

```json
{
  "summary": "Brief overall summary",
  "findings": [
    {
      "area": "Affected subsystem",
      "trust_boundary": "Untrusted or security-relevant boundary",
      "root_cause": "Underlying defect",
      "security_impact": "Security-relevant consequence",
      "severity": "informational|low|moderate|high|critical",
      "affected_code": ["relative/path/to/production_file"],
      "behavior_before_fix": "Observed unsafe behavior",
      "remediation": "Implemented production behavior",
      "behavior_after_fix": "Expected behavior after remediation",
      "verification": "Validation command and observed result"
    }
  ],
  "preserved_legitimate_behaviors": ["Supported behavior intentionally preserved"],
  "verification_runs": [
    {"command": "/testbed/reproducer/run-all.sh", "result": "Observed result"}
  ]
}
```

All fields are required and all arrays must be non-empty. Each `affected_code` entry must be relative to `/testbed`, resolve to an existing permitted production source file, and remain within scope. The final `verification_runs` entry must record `/testbed/reproducer/run-all.sh`.
