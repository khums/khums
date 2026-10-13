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

The public reproducer reports grouped behavioral scores. Groups aggregate several representative checks; they are intentionally not a direct list of underlying cases.

| Status | Meaning |
|---|---|
| `PASS` | All behavioral properties in the reported group hold |
| `FAIL` | One or more behavioral properties in the reported group do not hold |
| `ERROR` | Validation infrastructure did not complete normally |

Output is intentionally coarse, for example:

```text
GROUP G1: PASS score=3/3
GROUP G2: FAIL score=1/3
SUMMARY: FAIL score=4/6
```

Group names and scores are feedback for investigation, not a complete specification. A passing public summary does not guarantee every security or regression invariant is satisfied; thorough source review and focused project tests remain necessary.

## Scope

In-scope materials are permitted production source under `/testbed` and the read-only public reproducer at `/testbed/reproducer/run-all.sh`.

Out-of-scope materials include test trees, task tests, solutions, verifier files and fixtures, scoring files, generated output, repository history, and sealed runtime internals (including whatever `/testbed/reproducer/run-all.sh` calls out to). You may run `/testbed/reproducer/run-all.sh` as documented above, but do not modify it, and do not attempt to read, modify, or otherwise inspect any of the other out-of-scope materials. Tests created for investigation must stay outside the submitted source tree. Preserve public APIs, established error conventions, and existing supported inputs where directly implicated by the hardening work.

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
