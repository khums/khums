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

This task reports groups `G1` through `G4`. Each group mixes security and compatibility behavior; checks intentionally overlap between groups. Scores indicate completed validations, not distinct requirements. Group identifiers do not identify files, functions, or individual requirements.

| Status | Meaning |
|---|---|
| `PASS` | All behavioral properties in the reported group hold |
| `FAIL` | One or more behavioral properties in the reported group do not hold |
| `ERROR` | Validation infrastructure did not complete normally |

Output is intentionally coarse, for example:

```text
GROUP G1: PASS score=4/4
GROUP G2: FAIL score=2/4
SUMMARY: FAIL score=6/8
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
