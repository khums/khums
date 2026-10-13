#!/usr/bin/env python3
"""Root-owned aggregate scorer for the public probe.

Case inputs and expected observations live only in this root-owned process. Each
case sends one behavioral input (no expectation) to a fixture-free worker that
runs as the unprivileged agent account and returns raw observations, which are
judged here. Only coarse group results are printed.
"""
import json
import os
import re
import signal
import subprocess
import sys
from urllib.parse import urljoin, urlsplit

WORKER = "/opt/task-checker/probe/worker.js"
HOST = "site.example"
LONG = " " * 32000
COST_CASES = {"c01", "c09", "c13"}
GROUPS = {
    "G1": ("c01", "c02", "c03", "c04", "c05"),
    "G2": ("c06", "c07", "c08", "c09", "c10"),
    "G3": ("c11", "c12", "c13", "c14", "c15"),
    "G4": ("c16", "c17", "c18", "c19", "c20"),
}


def request(field, headers, proxy=False, version=1):
    return {"mode": "request", "field": field, "headers": headers, "proxy": proxy,
            "httpVersionMajor": version}


def redirect(target, headers=None, accept="text/html", alt=None):
    return {"mode": "back" if target == "back" else "redirect", "target": target, "alt": alt,
            "headers": {"host": HOST, "accept": accept, **(headers or {})}}


def cost(field, header):
    return request(field, {"host": HOST, header: f"a{LONG}b,c"}, proxy=True)


def bounded(o):
    return o.get("defined") is True and isinstance(o.get("elapsed"), (int, float)) and o["elapsed"] < 250


def confined_local(location, fallback):
    """The fallback, or a same-origin path that cannot leave the host."""
    if location == fallback:
        return True
    if not isinstance(location, str) or not location.startswith("/") or location.startswith("//"):
        return False
    if "\\" in location or re.search(r"[\x00-\x20\x7f]", location):
        return False
    resolved = urlsplit(urljoin(f"http://{HOST}/", location))
    return resolved.scheme == "http" and resolved.netloc == HOST


def single_anchor_to(body, location):
    anchors = re.findall(r"<a\s[^>]*href\s*=\s*\"([^\"]*)\"[^>]*>", body or "", re.I)
    if len(anchors) != 1:
        return False
    href = (anchors[0].replace("&quot;", '"').replace("&#39;", "'").replace("&lt;", "<")
            .replace("&gt;", ">").replace("&amp;", "&"))
    base = f"http://{HOST}/"
    return urljoin(base, href) == urljoin(base, location or "")


CASES = {
    "c01": (cost("host", "x-forwarded-host"), bounded),
    "c02": (request("hostname", {"host": "site.example:8080"}), lambda o: o.get("value") == "site.example"),
    "c03": (redirect("javascript:alert(1)"), lambda o: not re.search(r"<a\s", o.get("body") or "", re.I)),
    "c04": (redirect("/safe"), lambda o: o.get("location") == "/safe" and single_anchor_to(o.get("body"), "/safe")),
    "c05": (redirect("back", {"referrer": "https://elsewhere.example/a"}, "text/plain", "/safe"),
            lambda o: o.get("location") == "/safe"),
    "c06": (redirect("back", {"referrer": "/account"}, "text/plain", "/safe"), lambda o: o.get("location") == "/account"),
    "c07": (request("hostname", {"host": "legit.example", "x-forwarded-host": "evil.example@legit.example"}, proxy=True),
            lambda o: o.get("value") in ("legit.example", "")),
    "c08": (request("host", {"host": HOST, "x-forwarded-host": "proxy.example, backup.example"}, proxy=True),
            lambda o: o.get("value") == "proxy.example"),
    "c09": (cost("protocol", "x-forwarded-proto"), bounded),
    "c10": (redirect("https://site.example/next"), lambda o: o.get("status") == 302),
    "c11": (redirect("back", {"referrer": "//elsewhere.example/a"}, "text/plain", "/safe"),
            lambda o: confined_local(o.get("location"), "/safe")),
    "c12": (request("hostname", {"host": "[::1]:8080"}), lambda o: o.get("value") == "[::1]"),
    "c13": (cost("ips", "x-forwarded-for"), bounded),
    "c14": (redirect("javascript:alert(1)", accept="text/plain"), lambda o: o.get("type") == "text/plain"),
    "c15": (redirect("back", {"referrer": "http://site.example/a"}, "text/plain", "/safe"),
            lambda o: o.get("location") == "http://site.example/a"),
    "c16": (request("hostname", {"host": "fallback.example", ":authority": "h2.example:8443"}, version=2),
            lambda o: o.get("value") == "h2.example"),
    "c17": (request("ips", {"host": HOST, "x-forwarded-for": "client.example, relay.example"}, proxy=True),
            lambda o: o.get("value") == ["client.example", "relay.example"]),
    "c18": (redirect("/safe", accept="text/plain"), lambda o: o.get("body") == "Redirecting to /safe."),
    "c19": (redirect("back", accept="text/plain", alt="/safe"), lambda o: o.get("location") == "/safe"),
    "c20": (request("hostname", {"host": "evil.example@legit.example"}), lambda o: o.get("value") in ("legit.example", "")),
}


def observe(worker_source, payload, timeout):
    """Run the candidate as agent; return (observation | None, timed_out)."""
    command = ["/usr/sbin/runuser", "-u", "agent", "--", "node", "-e", worker_source]
    try:
        child = subprocess.Popen(
            command, text=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, cwd="/testbed",
            env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/home/agent"},
            start_new_session=True,
        )
    except OSError:
        return None, False
    try:
        output, _ = child.communicate(input=json.dumps({**payload, "root": "/testbed"}), timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        child.communicate()
        return None, True
    finally:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        data = json.loads(output)
    except (TypeError, ValueError):
        return None, False
    if not isinstance(data, dict):
        return None, False
    if "error" in data:
        return {"__candidate_error__": data["error"]}, False  # a behavioral failure, not infrastructure
    observed = data.get("observed")
    return (observed if isinstance(observed, dict) else None), False


def run_case(worker_source, case_id):
    payload, predicate = CASES[case_id]
    observed, timed_out = observe(worker_source, payload, timeout=4)
    if timed_out:
        # An unbounded parse is the defect under test for cost cases.
        return False if case_id in COST_CASES else None
    if observed is None:
        return None
    if "__candidate_error__" in observed:
        return False
    try:
        return bool(predicate(observed))
    except Exception:
        return False


def main():
    if len(sys.argv) != 2 or sys.argv[1] != "public" or os.geteuid() != 0:
        print("SUMMARY: ERROR", flush=True)
        return 2
    try:
        with open(WORKER, "r", encoding="utf-8") as handle:
            worker_source = handle.read()
    except OSError:
        print("SUMMARY: ERROR", flush=True)
        return 2
    totals = []
    for group, case_ids in GROUPS.items():
        results = [run_case(worker_source, case_id) for case_id in case_ids]
        if any(result is None for result in results):
            print(f"GROUP {group}: ERROR", flush=True)
            totals.append(None)
        else:
            passed = sum(results)
            status = "PASS" if passed == len(results) else "FAIL"
            print(f"GROUP {group}: {status} score={passed}/{len(results)}", flush=True)
            totals.append((passed, len(results)))
    if any(total is None for total in totals):
        print("SUMMARY: ERROR", flush=True)
        return 2
    passed = sum(total[0] for total in totals)
    possible = sum(total[1] for total in totals)
    status = "PASS" if passed == possible else "FAIL"
    print(f"SUMMARY: {status} score={passed}/{possible}", flush=True)
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
