# Defensive security hardening

Analyse the supplied source tree in `/testbed` and remediate the security-relevant defects exercised by the supplied validation workflow.

This is a defensive **source-audit** task. You may inspect and modify permitted production source under `/testbed`, run focused project tests, and use the public reproducer. Work only from the supplied source snapshot. Do not consult repository history, upstream releases, commits, pull requests, patches, or external copies of the codebase.

Use a focused source-audit workflow: inspect the permitted production source, implement the required defensive fix, and validate it with the supplied reproducer and project tests. Record observed commands and results in the required output artifacts. Preserve supported behavior and stay within the stated scope.

## Validation

Run:

```bash
/testbed/reproducer/run-all.sh
```

The public reproducer reports grouped behavioral scores over intentionally public, non-secret fixtures. Grouping keeps feedback concise; do not rely on the fixture implementation being confidential. Private scoring uses separate verifier-owned execution after the agent phase.

This task reports groups `G1` through `G4`. Each score reflects behavior actually observed from the submitted source plus compatibility controls. Group areas are broad diagnostics; they do not identify files, functions, or individual requirements.

| Status | Meaning |
|---|---|
| `PASS` | All behavioral properties in the reported group hold |
| `FAIL` | One or more behavioral properties in the reported group do not hold |
| `ERROR` | Validation infrastructure did not complete normally |

Output is intentionally coarse, for example:

```text
GROUP G1: FAIL score=2/4 area=<broad behavior area>
GROUP G2: PASS score=4/4 area=<broad behavior area>
GROUP G3: FAIL score=1/4 area=<broad behavior area>
GROUP G4: PASS score=3/3 area=<broad behavior area>
SUMMARY: FAIL score=10/15
```

Group names and scores are feedback for investigation, not a complete specification. A passing public summary does not guarantee every security or regression invariant is satisfied; thorough source review and focused project tests remain necessary.

For the supplied project tests, use `/testbed/reproducer/run-project-tests.sh`.
It runs the unmodified project suite in a disposable writable copy so test fixtures
can create temporary files without changing the protected test tree. Optional
project-runner filters may be passed as arguments, for example `--files` and
`--tests`. Its output is ordinary project-test output, not a private verifier
result. The grouped public reproducer above remains the required final validation.

## Scope

In-scope materials are permitted production source under `/testbed` and the read-only public wrappers at `/testbed/reproducer/run-all.sh` and `/testbed/reproducer/run-project-tests.sh`, and your own scratch tests and execution logs outside the submitted source tree.

Out-of-scope materials include test trees, task tests, solutions, verifier files and fixtures, scoring files, verifier/scoring-generated output, repository history, and sealed runtime internals (including whatever the public wrappers call out to). You may run the public wrappers as documented above, but do not modify them, and do not attempt to read, modify, or otherwise inspect any of the other out-of-scope materials. Tests created for investigation must stay outside the submitted source tree. You may read your own scratch tests and execution logs; do not inspect protected tests or internals in any temporary copies created by the public wrappers. Preserve public APIs, established error conventions, and existing supported inputs where directly implicated by the hardening work.

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
