# Stage 8 validation acceptance contract

Validation compares REST samples to replayed stream states; REST never changes
or restores a stream book. Reuse Stage 7 invalidation and metadata semantics.
Input events are in source-line order with nondecreasing receipt times. Reject
regressing receipt times, reversed REST windows, bad market IDs/pool ranges,
invalid REST ladders (duplicates, off-grid levels, negative quantities), and
non-ok parse status with `invalid_input`, returning no partial counts or diffs.
Zero workers gives `invalid_thread_count`. Resource failures may throw.

For a REST sample, consider only earlier source lines for its market. A state
persists from its update until the next update. Include the state active at
`t_send_ns` and every state reached within the inclusive request/receipt window.
At the lower boundary, a state replaced exactly at that time is no longer active;
at the upper boundary, include updates at that time preceding the REST line.
Multiple stream updates with the same timestamp are ordered by source line and
all states reached inside the window are candidates. Later source lines never
supply a candidate, even with the same timestamp as the REST sample.

Ignore invalid candidate states. If there are no valid candidates, count the
sample as `excluded_invalid` and omit it from match-rate denominators. Otherwise
count it as `compared`, even when part of the window was invalid. A successful
snapshot inside such a window can supply a match. Never compare to another market.

Full depth matches if ANY one valid candidate has both identical bid ladders;
never combine sides or levels from different candidates. Canonical ladders are
descending by price, omit zero quantities, and compare exact integer quantities.
Top of book independently matches if ANY candidate has the same YES best bid and
implied YES best ask, including both quantities. Empty books are valid states;
missing prices differ from price zero. Rates are matches / compared, or null when
compared is zero. `mismatches = compared - full_depth_matches`.

`max_diffs` (API default 10) limits stored mismatch samples, never counts. Each diff
contains market_id, rest_line_index, state_line_index and canonical rest_yes,
rest_no, engine_yes, engine_no. The diagnostic reference is the LAST valid
candidate in source order, not a synthetic or nearest state. Diffs are ordered by
REST source line, independent of worker count. All numbers use ticks/hundredths.

CLI: `--validate` now succeeds with exit 0 and additionally writes validation.json;
ordinary replay artifacts are identical with and without validation. Mismatches
are data, not process errors. Invalid validation input exits 3; report write errors
exit 4. Existing usage/output contracts remain unchanged. validation.json contains
schema_version=1 and all result fields except status; diffs serialize levels as
[price, quantity] pairs. No threads, paths, timings, or nondeterministic metadata.
No validation.json is created without --validate. Default diff limit is 10.

## Verified REST pricing convention

Checked 2026-10-21 against the official documentation:
https://docs.kalshi.com/getting_started/orderbook_responses
https://docs.kalshi.com/api-reference/market/get-market-orderbook

orderbook_fp.yes_dollars and no_dollars are YES and NO bid ladders respectively.
NO bid 0.6500 means implied YES ask 1.0000 - 0.6500 = 0.3500; do not complement
NO prices before comparing REST and stream NO ladders. Compare returned full-depth
samples; truncated REST depth cannot establish a full-depth match.

## Hand-computed validation_windows fixture

All data belongs to A on a 0.01 grid. Lines are zero-based.
- Line 1: no stream snapshot yet, excluded.
- Line 2 snapshot: YES 4000/100 and 2000/200; NO 6500/300 (YES ask 3500).
- Line 3: unchanged state persists across [15,20], full and top match.
- Line 4 delta: best YES quantity 100+50=150.
- Line 5: [25,40] includes the pre-delta state, so REST quantity 100 matches.
- Line 6: REST lower YES quantity 999 differs from 200; top 4000/150 and
  3500/300 still matches. Full-depth mismatch; diagnostic uses state line 4.
- Line 7: REST YES best 5000/150 differs from 4000/150: full and top mismatch.
- Line 8 gap invalidates A. Line 9 is excluded; REST does not restore it.
- Line 10 snapshot restores the original ladders. Line 11's [85,100] includes
  that valid recovery state, so both comparisons match.

7 samples = 5 compared + 2 excluded. Full matches 3/5=0.6; top matches 4/5=0.8;
2 full-depth mismatches. Diff order is REST lines 6 then 7, both referencing line 4.
