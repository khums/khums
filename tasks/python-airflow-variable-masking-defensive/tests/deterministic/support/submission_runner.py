#!/usr/bin/env python3
"""Narrow child-process adapter: accepts inputs and returns observations only."""
import json
import sys


def main():
    from airflow_shared.secrets_masker.secrets_masker import SecretsMasker

    request = json.load(sys.stdin)
    if request.get("operation") != "redact":
        raise ValueError("unsupported operation")
    masker = SecretsMasker()
    masker.sensitive_variables_fields = request.get("sensitive_fields", [])
    observed = masker._redact(
        request["payload"], request.get("name"), request.get("depth", 0), request.get("max_depth", 5)
    )
    json.dump(observed, sys.stdout, sort_keys=True, separators=(",", ":"))


if __name__ == "__main__":
    main()
