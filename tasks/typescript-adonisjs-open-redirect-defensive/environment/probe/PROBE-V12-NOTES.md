# v13 public probe

- Four groups, 15 executions, 15 distinct commands, no repeats. Each group has a
  distinct broad area label; labels name behavior areas, not files or functions.
- G2 is a compatibility control group (passes on the vulnerable baseline).
- G4 includes the explicit `redirect(path, false)` case, which is what
  distinguishes the query-forwarding defect; the inherit/enable cases are controls.
- Overlay and private specs are Prettier/ESLint/tsc clean because `npm run build`
  lints and type-checks the tests tree.
- Measured locally (image emulation): no-op 7/15 public, 39/56 verifier rows;
  FIX-1 only 11/15, 49/56; oracle 15/15, 56/56, reward 1.0.
