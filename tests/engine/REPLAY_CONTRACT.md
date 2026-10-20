# Replay acceptance contract

Stage 7 uses `brier_replay INPUT [--threads N] [--out DIR] [--tob] [--validate]`.
Default output directory: `replay_out` relative to the working directory.
`summary.json` contains deterministic counts and final books; `markets.json` is
an array whose index is the dense market ID. `--tob` additionally writes `tob.bin`.
Thread count, paths, execution timings and wall-clock generation times do not
appear in these deterministic outputs.

Exit codes: 0 success/help; 2 invalid command-line arguments; 3 input access or
unsupported input format/pricing mode; 4 output I/O failure; 5 requested validation
is unavailable until Stage 8. `--validate` must be recognized without claiming a
validation result. Malformed individual lines are counted and skipped, not fatal.

## Binary records (to document in DATA_FORMAT.md during GREEN)

Each record is 40 bytes, little-endian, with no native alignment or header:

| Field | Offset | Type |
| --- | ---: | --- |
| t_recv_ns | 0 | int64 |
| market_id | 8 | uint32 |
| yes_bid | 12 | int32 |
| yes_ask | 16 | int32 |
| bid_qty | 20 | int64 |
| ask_qty | 28 | int64 |
| flags | 36 | uint32 |

NumPy dtype: `[('t_recv_ns','<i8'),('market_id','<u4'),('yes_bid','<i4'),
('yes_ask','<i4'),('bid_qty','<i8'),('ask_qty','<i8'),('flags','<u4')]`, `align=False`.
Flag bits: 1 valid, 2 crossed, 4 locked. Invalid records have missing prices (-1),
zero quantities and flags 0. A valid empty book has flags 1. Price zero is valid.
Only quote, quantity or flag changes produce rows. The initial invalid state is
unreported. Rows merge by (receipt time, source line index, market ID); the last
key resolves multi-market invalidations on one source line. Source line index is
an internal merge key and is omitted from the binary format.

## Hand calculation for golden_small

IDs are B=0, A=1, C=2 (first metadata occurrence). Initial YES asks are respectively
10000-5000=5000, 10000-6800=3200, 10000-8000=2000. B's first quantity update gives
200+50=250. A's lower level changes 100+100=200 without changing top of book;
its best quantity changes 155-55=100. A trade has no book effect.

The gap invalidates B and A, leaving C valid. Three A deltas are dropped (one
before the first snapshot and two after the gap, including one after a REST
sample); one B delta is dropped. C updates 100+50=150 during the other markets'
gap. Repeated resync produces no additional invalid row.

A's recovery replaces both ladders with 3100/250 and 6900/400: sum=10000, locked
(flags 1+4=5), YES ask=3100. NO bid 7000/1 gives sum=10100, crossed (1+2=3), ask
3000/1; removing that level restores the lock. B recovers at 4500/100 and 5400/200
(ask=4600), then removes both levels to become valid and empty. B's final snapshot
has YES 0/1 and NO 10000/2, hence bid=ask=0 and flags=5. A's final zero delta is
applied but produces no output. Closing the recording does not alter its books.

Totals: B/A/C successful snapshots = 3+2+1=6; applied deltas = 3+5+1=9;
dropped deltas = 1+3+0=4; invalid updates = 0. There are 15 TOB records.
The eighth number in each expected JSON TOB row is its source line index; only
the first seven fields are written to disk.
