#!/bin/sh
# Public case runner — cases are organised into named groups.
# Each line reports:  <group>: PASS (n/m)  or  <group>: FAIL (n/m)
# where n is the number of cases that passed and m is the group size.
# Exit 0 only when every case in every group passes.
HARNESS="$(dirname "$0")/harness"
TOTAL_FAIL=0
TOTAL_ERR=0

run_group() {
    name=$1; shift
    pass=0; fail=0; err=0
    for id in "$@"; do
        line=$("$HARNESS" "$id" 2>/dev/null || true)
        case "$line" in
            *": PASS") pass=$((pass + 1)) ;;
            *": FAIL") fail=$((fail + 1)) ;;
            *)          err=$((err  + 1)) ;;
        esac
    done
    total=$((pass + fail + err))
    if [ "$fail" -gt 0 ] || [ "$err" -gt 0 ]; then
        echo "$name: FAIL ($pass/$total)"
        TOTAL_FAIL=$((TOTAL_FAIL + 1))
    else
        echo "$name: PASS ($pass/$total)"
    fi
}

run_group "access-enforcement"  A B C D F G H
run_group "configuration-sync"  E I

echo ""
if [ "$TOTAL_FAIL" -gt 0 ] || [ "$TOTAL_ERR" -gt 0 ]; then
    echo "Results: FAIL"
    exit 1
fi
echo "Results: PASS"
exit 0