# rocgdb-dump

rocgdb tooling for inspecting HSA/SDMA user-queue state (and HSA signals) on a hung or
running ROCm process: dump every queue automatically, save as text or as a fast binary
capture, and browse the result offline via a REPL or a small local web UI.

## Origin

This started from `rocgdb_info/` in
[Yuechguo/debug_tools](https://github.com/Yuechguo/debug_tools) (original author: yuechaguo,
yuechao.guo@amd.com) -- specifically `queue_script.py` (the `dump_hsa_queue`, `dump_sdma_queue`,
`dump_hsa_signal`, `modify_hsa_signal`, `dump_queue_memory` commands) and `save_info.gdb`, as of
upstream commit `081e79e` ("add find doorbell signal guide in readme", 2025-09-29). That file has
since been renamed `rocgdb_helper.py` in this repo (content unchanged at the rename).

Everything else in this directory was added on top of that base in a Claude Code session with
Leo Zhao (2026-08):
- `queue_decode.py` -- the packet-decoding logic extracted out into its own module with no gdb
  dependency, so it can be shared between the live rocgdb path and an offline tool.
- `queue_viewer.py` -- new standalone tool (no gdb) that reads a `.bin` dump and lets you browse
  packets via an interactive REPL or a local browser UI.
- `dump_all_queues` / `dump_all_queues_txt` commands in `rocgdb_helper.py` -- automatically find
  and dump every HSA/DMA/XGMI queue (plus all-thread backtraces) instead of hand-copying
  addresses out of `info queue` one at a time.
- `rp`/`wp` jump navigation, XGMI queue-type support, and a handful of bug fixes found along
  the way (a `super()` typo in `ModifyHsaSignal` that crashed the whole script load, an
  `info queue` column-parsing regex that misread Type as a Read value on two-digit queue IDs,
  a packet index counter that never incremented in the SDMA decoder).
- Per-run `dump_summary.json`, `info queues`/`info dispatches` capture, and Target Id embedded in
  queue dump filenames (see "Auto-dump everything" below).
- Best-effort SDMA rptr/wptr enrichment for DMA/XGMI queues, read straight out of KFD debugfs
  (no `umr` dependency), plus `rp`/`wp` jump navigation for DMA/XGMI in `queue_viewer.py`
  (see "SDMA rptr/wptr enrichment" below).
- Full SDMA packet decode, ported from UMR's own SDMA source
  (`umr/src/lib/packet/sdma/read_sdma_stream.c` + `sdma_decode_opcodes.c`) -- replacing an
  earlier, much narrower decoder that only handled 5 opcodes and, worse, silently mis-sized
  several of them (every `COPY` was assumed to be the `LINEAR` sub-opcode's 28 bytes; every
  `POLL_REGMEM` was assumed to be the `MEM` sub-opcode's 24 bytes), desyncing every packet
  after the first mismatch for the rest of the ring (see "SDMA packet decode" below).

## Files

- `rocgdb_helper.py` -- load into rocgdb (`source rocgdb_helper.py` after attaching). Defines all
  the `dump_*`/`modify_hsa_signal` commands.
- `queue_decode.py` -- shared HSA/SDMA packet decoder + `.bin` dump container format. No gdb
  dependency; imported by both `rocgdb_helper.py` and `queue_viewer.py`.
- `queue_viewer.py` -- standalone offline tool for browsing `.bin` dumps (REPL or `--web`).
- `save_info.gdb` -- one-shot script for the older `sudo rocgdb -x save_info.gdb` workflow
  (attach, dump queues/dispatches/threads/registers, quit).
- `dump_all_queues.sh` -- one-shot wrapper: attach to a given pid, source `rocgdb_helper.py`,
  run `dump_all_queues`, detach -- no manual `attach`/`source`/`quit` typing, no editing a
  hardcoded pid into a file (see "One-shot script" below).

## Use rocgdb to save info
modify the `<hang_pid>` in `save_info.gdb` and run command:
```
Command: sudo rocgdb -x save_info.gdb 2>&1 | tee hang_gdb.log
```

## Use rocgdb_helper to collect data
rocgdb attach the <hang_pid> and enable script:
```
sudo rocgdb attach <hang_pid>
source rocgdb_helper.py
```

## One-shot script: attach + dump_all_queues in a single command
`dump_all_queues.sh` automates the whole "attach, source the helper, dump, detach" sequence
above into one command -- the same idea as `save_info.gdb`, except the pid is a command-line
argument instead of something you hand-edit into a file:
```
$ ./dump_all_queues.sh <pid> [output_dir]
Attaching rocgdb to pid <pid>, running 'dump_all_queues' ...
...
dump_all_queues complete: rocgdb_dump_bin_pid<pid>_<timestamp>
  ...
[Inferior 1 (process <pid>) detached]

Full rocgdb session log saved to: dump_all_queues_pid<pid>_<timestamp>.log
```
Re-invokes itself via `sudo` automatically if not already root (so `./dump_all_queues.sh <pid>`
works the same whether or not you prefix `sudo` yourself). `[output_dir]` is optional, passed
straight through to `dump_all_queues` (its own timestamped default is used if omitted). Pass
`--txt` before the pid to run the slower live-text-decode `dump_all_queues_txt` instead of the
default fast binary capture: `./dump_all_queues.sh --txt <pid>`. The full rocgdb session
transcript (everything the script's rocgdb invocation printed) is also saved to
`dump_all_queues_pid<pid>_<timestamp>.log` in the current directory, in case something needs
double-checking after the fact (e.g. whether `attach` itself failed).

## Auto-dump everything, fast (recommended first step)
No manual copy/paste from `info queue` needed -- this finds every HSA/DMA/XGMI queue itself,
reads each one's raw bytes with a single bulk memory read (fast -- one round-trip per queue,
not per packet), captures `info queues`/`info dispatches`, and saves all-thread backtraces, all
in one shot. Each queue's metadata (qid, type, address, rptr/wptr, size, ...) is saved alongside
its raw bytes in one `.bin` file per queue; decode happens later, offline, against an in-memory
buffer instead of over gdb:
```
(gdb) dump_all_queues
------------------------------
dump_all_queues complete: rocgdb_dump_bin_pid<pid>_<timestamp>
  HSA queues captured: 20
  DMA/XGMI queues captured: 8
  backtraces: rocgdb_dump_bin_pid<pid>_<timestamp>/backtrace_all_threads.log
  info queues: rocgdb_dump_bin_pid<pid>_<timestamp>/info_queues.log
  info dispatches: rocgdb_dump_bin_pid<pid>_<timestamp>/info_dispatches.log
  summary: rocgdb_dump_bin_pid<pid>_<timestamp>/dump_summary.json
view with: python3 queue_viewer.py <output_dir>/<file>.bin
```
Each queue is saved as `hsa_QID<N>_GPU_<A>_Queue_<B>.bin` / `dma_QID<N>_GPU_<A>_Queue_<B>.bin`
(also `xgmi_QID<N>_GPU_<A>_Queue_<B>.bin` for XGMI-transport DMA queues) in that directory. `<N>`
is the same `(QID N)` shown by `info queue` (use that to cross-reference against CLR-side logs);
`<A>`/`<B>` come from the queue's Target Id in `info queue` (`AMDGPU Queue <A>:<B> (QID N)`),
e.g. Target Id `AMDGPU Queue 5:27 (QID 6)` -> `hsa_QID6_GPU_5_Queue_27.bin`. Pass a directory
name to control where it's written: `dump_all_queues /tmp/my_capture`. One bad/unreadable queue
won't stop the rest of the batch; failures are reported in the summary.

Every run also writes:
- `info_queues.log` / `info_dispatches.log` -- raw output of rocgdb's own `info queues` and
  `info dispatches -full` commands, captured alongside the per-queue ring dumps. `info queues`
  itself never reports Read/Write for DMA/XGMI rows (amd-dbgapi's packet-ID abstraction is
  HSA/AQL-specific -- see "SDMA rptr/wptr enrichment" below), so whenever that best-effort
  enrichment succeeds for one or more DMA/XGMI queues, their raw rptr/wptr values are patched
  directly into `info_queues.log`'s existing Read/Write columns, in place, in the same table
  rocgdb itself printed -- not a separate section, and not visible only in
  `dump_summary.json`/each queue's `.bin`/`.log` metadata:
  ```
    Id   Target Id                Type         Read   Write  Size     Address
    1    AMDGPU Queue 8:1 (QID 5) DMA          21     48     8388608  0x00007f49f2400000
    2    AMDGPU Queue 8:2 (QID 4) DMA          21     48     8388608  0x00007f49f3600000
    3    AMDGPU Queue 8:3 (QID 3) HSA          2      2      1048576  0x00007f49f8c00000
  ```
  Column widths are derived from the header line itself at patch time (not hardcoded), so this
  stays correct even if rocgdb's own formatting shifts. Nothing is patched if enrichment finds
  nothing for a given row (no root, non-KFD host, unrecognized GPU generation, or no matching
  queue) -- that row, and the whole file if nothing was found at all, is left exactly as rocgdb
  wrote it.
- `dump_summary.json` -- what this run actually captured: pid/comm/host/timestamp, HSA/DMA+XGMI
  queue counts and the list of files written for each, the backtrace/info-command file paths, and
  any per-queue failures. Meant as a quick machine- or eyeball-readable manifest of the dump
  directory's contents, e.g. to confirm a batch capture actually got everything before archiving
  or sharing it.

Then, with **no gdb involved at all**, open any of those `.bin` files in the standalone
`queue_viewer.py` and browse packets interactively:
```
$ python3 queue_viewer.py rocgdb_dump_bin_pid.../hsa_QID27_GPU_1_Queue_27.bin
Loaded .../hsa_QID27_GPU_1_Queue_27.bin (HSA, qid=27)
16384 packet(s) decoded/available (indices 0..16383)
Type 'help' for commands.
(queue_viewer) > info
qid:       27
type:      HSA
addr:      0x7e4b57600000
size:      1048576 bytes
read:      438427
write:     438427
...
(queue_viewer) > packet 5
------------------------------------------------------------------------------------
Packet #5 at 0x7e4b57600140 (64 bytes)                              KERNEL_DISPATCH
------------------------------------------------------------------------------------
+0x00  02 0b 00 00                 | HEADER type=2 barrier=0 acquire=1 release=1
                                   | SETUP = 0x0
...
------------------------------------------------------------------------------------
(queue_viewer) > range 0 3
(queue_viewer) > all
(queue_viewer) > raw 5
(queue_viewer) > rp             # jump straight to the packet at the read pointer
(queue_viewer) > wp             # jump straight to the packet at the write pointer
(queue_viewer) > packet wp      # same as above, as a plain index expression
(queue_viewer) > range rp rp+5        # decode from the read pointer through 5 packets later
(queue_viewer) > raw wp-1       # hex-dump the packet just before the write pointer
(queue_viewer) > quit
```
The packet title always shows the total packet size (`(N bytes)`), and `N`/`A`/`B` above accept
either a plain integer or `rp`/`wp` optionally followed by `+N`/`-N`, resolved the same way
the `rp`/`wp` commands themselves resolve (see the previous section for what that resolution
means for DMA/XGMI specifically). Up/down-arrow command history works in the REPL via Python's
`readline` module (imported automatically when available; degrades to plain `input()` if not).

Point the same command at a **directory** instead of one `.bin` file, and the REPL gains a
`list`/`use` pair for switching between every queue in it, instead of being fixed to one file for
the whole session:
```
$ python3 queue_viewer.py rocgdb_dump_bin_pid.../
6 queue dump(s) found under rocgdb_dump_bin_pid.../
   [0] dma_QID4_GPU_8_Queue_2.bin  type=DMA size=8388608 rp=21 wp=48
   [1] dma_QID5_GPU_8_Queue_1.bin  type=DMA size=8388608 rp=21 wp=48
   [2] hsa_QID0_GPU_8_Queue_6.bin  type=HSA size=4096 rp=2 wp=2
   [3] hsa_QID1_GPU_8_Queue_5.bin  type=HSA size=1048576 rp=0 wp=2
   ...
Type 'use <index_or_name>' to select one, 'list' to see this again, 'help' for commands.
(queue_viewer) > use 3                    # by 0-based index...
switched to hsa_QID1_GPU_8_Queue_5.bin (HSA, qid=1)
16384 packet(s) decoded/available (indices 0..16383)
(queue_viewer:hsa_QID1_GPU_8_Queue_5.bin) > wp
...
(queue_viewer:hsa_QID1_GPU_8_Queue_5.bin) > use dma_QID4      # ...or an unambiguous filename prefix
switched to dma_QID4_GPU_8_Queue_2.bin (DMA, qid=4)
(queue_viewer:dma_QID4_GPU_8_Queue_2.bin) > list              # '*' marks the currently selected queue
(queue_viewer:dma_QID4_GPU_8_Queue_2.bin) > quit
```
`list` (aliases `ls`/`queues`) only reads each dump's header, not its full ring bytes, so it stays
fast regardless of how many/how large the queues in the directory are -- packet decoding only
happens once a queue is actually selected via `use`, same as `--web`'s lazy per-queue loading. All
of the single-file commands above (`info`/`packet`/`range`/`all`/`raw`/`rp`/`wp`) apply to
whichever queue is currently selected; running one before any `use` prints a reminder instead of
an error. A directory with only one `.bin` file still goes through this same `list`/`use` flow
(matching `--web`'s behavior for consistency) rather than auto-selecting it.

`queue_viewer.py` and `rocgdb_helper.py` share the exact same packet-decoding logic
(`queue_decode.py`), so a `.bin` dump decodes identically to what `dump_all_queues_txt`'s live
text path would have shown for the same queue. Kernel dispatch packets show the raw
`kernel_object` address only offline (no live process to resolve a symbol name against).

For **HSA** queues, `rptr`/`wptr` are the queue's read/write **packet IDs** as reported by
rocgdb/amd-dbgapi's `amd_dbgapi_queue_packet_list()` -- a monotonically increasing count of
packets ever submitted to the queue, not a byte offset -- so the actual ring slot is
`packet_id % (size / 64)`.

For **DMA/XGMI** (SDMA-engine) queues, `info queue` never reports Read/Write in the first
place -- amd-dbgapi's packet-ID abstraction is HSA/AQL-specific and returns "not supported"
for SDMA. `dump_all_queues`/`dump_all_queues_txt` instead make a **best-effort** attempt,
every run, to fill these in by reading the queue's raw rptr/wptr straight out of KFD debugfs
(see "SDMA rptr/wptr enrichment" below); when that succeeds, `rp`/`wp` navigation works
for DMA/XGMI too, resolving to whichever decoded packet contains that ring position (SDMA
packets are variable-length, so there's no "packet ID" the way HSA has one -- see below for
the exact units). When it doesn't (no root, non-KFD host, unrecognized GPU generation, or the
queue just wasn't found), `rp`/`wp` print a one-line explanation instead of a value.

## Auto-dump everything, text (live decode, slower)
`dump_all_queues` reads each queue's raw bytes with a single bulk memory read and decodes them
later, offline. `dump_all_queues_txt` instead decodes every packet to text *while attached
live* -- on a hung process with large/many rings that's slow, since every packet is a separate
round-trip over gdb's memory channel. Prefer `dump_all_queues` unless you specifically want
plain-text `.log` files with no separate viewer step:
```
(gdb) dump_all_queues_txt
------------------------------
dump_all_queues_txt complete: rocgdb_dump_pid<pid>_<timestamp>
  HSA queues captured: 20
  DMA queues captured: 8
  backtraces: rocgdb_dump_pid<pid>_<timestamp>/backtrace_all_threads.log
  info queues: rocgdb_dump_pid<pid>_<timestamp>/info_queues.log
  info dispatches: rocgdb_dump_pid<pid>_<timestamp>/info_dispatches.log
  summary: rocgdb_dump_pid<pid>_<timestamp>/dump_summary.json
```
Same `<N>_<TargetId>` filename convention (`.log` instead of `.bin`), `info_queues.log`/
`info_dispatches.log` capture, and `dump_summary.json` as `dump_all_queues` above.

### SDMA rptr/wptr enrichment

Every `dump_all_queues`/`dump_all_queues_txt` run also tries, best-effort, to fill in
Read/Write for DMA/XGMI rows by reading the SDMA queue's MQD (Memory Queue Descriptor)
straight out of `/sys/kernel/debug/kfd/mqds` (root required, no `umr` binary needed) -- the
same underlying data UMR's `--list-uq`/`--print-uq` are built on. The MQD carries two plain
memory addresses the GPU DMA-writes live pointer values into (an RPTR "report" address and a
WPTR "poll" address); `rocgdb_helper.py` decodes their location from the MQD's raw dwords
(generation-specific word offsets, ported from UMR's `parse_clientid.c`, covering GFX9/10/11/12),
reads the two addresses the same way it already reads the ring itself, and matches the result
back to the right queue by ring base address (the one piece of information both `info queue`
and the MQD reliably agree on).

This is silent and non-fatal when it doesn't work -- no root, no `/sys/kernel/debug/kfd/mqds`,
an unrecognized GPU generation, or simply no matching queue -- exactly like a failed
`info_dispatches` capture: the rest of the dump still completes, and affected rows just keep
their Read/Write columns blank as before. **Verified against real hardware for GFX9 only**
(byte-for-byte matched against UMR's own decoded values on this host); the GFX10/11/12 offset
table entries are transcribed from UMR's source but not independently hardware-verified.

**Running inside a container:** `/sys/kernel/debug` needs to be bind-mounted into the
container (it isn't by default) and read as root -- `--user`-restricted containers need
`docker exec -u root` (or equivalent) for the rocgdb session doing the dump. Root inside the
container also needs `--cap-add=SYS_PTRACE` at container-*creation* time to `ptrace`-attach to
the (non-root) target process in the first place -- `docker exec`/`docker update` cannot add
capabilities to an already-running container, so this only takes effect after the container is
recreated. Separately: this enrichment deliberately does **not** filter `mqds` by pid, because
a containerized rocgdb sees the target's *container-local* pid (e.g. `1`), which generally has
no relationship to the *host-level* pid KFD debugfs reports for the same process (there's no
unprivileged way to recover the host pid from inside the container's own pid namespace) --
matching is done purely by ring base address across every SDMA queue on the system instead,
which sidesteps the mismatch (and is safe: these ring addresses are effectively unique across
processes).

**Running on a shared, multi-tenant host** (bare metal, no container involved): the same
system-wide scan applies -- `/sys/kernel/debug/kfd/mqds` lists *every* process's SDMA queues,
most of which belong to other users' jobs that this rocgdb session has no ptrace access to.
The scan narrows to ring-base-address matches against your own attached process's queues
*before* attempting any actual memory read, so this shows up as silence (no "Cannot access
memory" spam for queues that were never going to be yours), not failures -- if you do see a
"Cannot access memory" enrichment failure for one of *your own* queues, that's a real problem
worth investigating (wrong offset table for this generation, a truncated/stale MQD snapshot,
etc.), not the expected multi-tenant noise.

**Storage matches HSA; units don't.** Read/Write here are stored the same way as HSA's --
a **raw, un-wrapped counter** (e.g. `read: 21` could just as easily have been `2097173` on a
larger/longer-running queue; wrapping to a ring position happens later, at use time, in
`queue_viewer.py`, exactly like HSA's raw AQL packet-ID counter does) -- but the *unit* is still
different: a ring-relative **dword position** (byte offset = value * 4), not a monotonic packet
ID, since SDMA packets are variable-length and there's no equivalent "packet index" concept at
the hardware level. Don't compare DMA/XGMI Read/Write numbers directly against HSA ones; they're
stored the same way but mean different things. `rp`/`wp` in `queue_viewer.py` show the full
conversion chain for this reason: `raw=N -> dword slot M -> byte offset 0x... -> packet index K
(of TOTAL)`.

### SDMA packet decode

`queue_decode.py`'s SDMA decoder (`decode_sdma_packets`, shared by the live `dump_all_queues_txt`/
`dump_sdma_queue` and `queue_viewer.py`) is a port of UMR's own SDMA parsing source:

- **Sizing** -- how many bytes each packet occupies, needed to find the next packet -- is a
  full port of `read_sdma_stream.c`'s `sized_oss1_5()`: every opcode and sub-opcode, generation-
  agnostic across SDMA/OSS IP versions 1-6 (checked against this host's actual SDMA IP version
  via `ip_discovery`). This matters because SDMA packets are variable-length: getting a size
  wrong desyncs every packet after it for the rest of the ring. (The previous decoder didn't do
  this -- it assumed every `COPY` was the `LINEAR` sub-opcode's fixed 28 bytes and every
  `POLL_REGMEM` was the `MEM` sub-opcode's fixed 24 bytes, silently corrupting the rest of the
  ring's decode whenever a different sub-opcode showed up.)
- **Field-level decode** -- broken down into the two-column view described below -- is a port
  of `sdma_decode_opcodes.c`'s `decode_upto_ai()`, the one (of four:
  VI/AI/NV/OSS7) generation-specific decoder that matches this host's confirmed SDMA IP version
  (major 4, minor 4 -- cross-checked against `sdma_decode_opcodes.c`'s own version-gating logic).
  **The other three generations are not ported** -- an unrecognized-but-correctly-sized packet
  just gets a generic `(recognized, N bytes, not decoded in detail)` line instead of a field
  breakdown; the ring walk stays correct either way. Verified against real hardware: replayed
  against every DMA/XGMI queue already captured in this repo (tens of thousands of real packets
  across several rings) with zero decode errors and zero "not decoded in detail" fallbacks --
  real traffic on this host is entirely `COPY`/`TIMESTAMP`/`POLL_REGMEM`/`ATOMIC`, all fully
  covered.
- Two deliberate additions beyond straight 1:1 porting: `INDIRECT` packets print their
  descriptor (address/vmid/size) and are followed one level deep automatically -- this works
  live (rocgdb can read anywhere in the process) and fails cleanly with "IB not available in
  this dump" offline (a `.bin` dump only contains its own ring's bytes); `TRAP` keeps a decoded
  `TRAP_INT_CONTEXT` field. UMR's own AI-generation decoder does neither (it falls through to
  the older, generation-agnostic decoder for these two opcodes, which itself skips `INDIRECT`'s
  fields entirely) -- these are small, code-comment-flagged deviations, not accuracy issues.
- A handful of legacy tiling-mode sub-opcodes (`COPY`'s `*_BC` variants, `WRITE`'s `TILED_BC`) are
  intentionally not field-decoded -- they're pre-GCN/early-GCN modes that don't occur on this
  hardware in practice, and are exactly the opcodes UMR's own AI decoder defers on too. They're
  still sized correctly (so the ring walk never desyncs), just shown generically.

Each packet is displayed as a two-column view -- raw hex on the left (grouped by dword; a
`┌`/`┘` bracket connects the two dwords of a 64-bit LO/HI field; fields sharing one dword show
the hex only on their first row), decoded `NAME = value` text on the right, separated by `|`.
The packet type is shown ALL CAPS at the top-right of each packet's header. This same layout
(shared rendering code, `queue_decode.py`'s `_emit_field_groups`) is used for **both** SDMA and
HSA packets:
```
------------------------------------------------------------------------------------
Packet #2 at 0x7e7b51a00024                                          COPY (LINEAR)
------------------------------------------------------------------------------------
+0x00  2a 10 03 06                 | HEADER op=0x1 sub_op=0x0
+0x04  00 04 00 00                 | COPY_COUNT = 1024
+0x08  00 00 00 00                 | DST_SW = 0
                                   | DST_CACHE_POLICY = 0
                                   | SRC_SW = 0
                                   | SRC_CACHE_POLICY = 0
+0x0c  00 00 34 56 ┌
+0x10  12 f1 7f 00 ┘               | SRC_ADDR = 0x7f1234560000
+0x14  00 00 70 45 ┌
+0x18  12 f1 7f 00 ┘               | DST_ADDR = 0x7f1234570000
------------------------------------------------------------------------------------
```
For HSA's fixed 64-byte AQL packets, `words[i]` starts at the packet's very first byte (offset
`4*i`) rather than right after a separate header dword like SDMA -- HSA packs `SETUP` (Kernel
Dispatch) or `TYPE` (Agent Dispatch) into the same dword as the header bits, so those fields
naturally group under the `HEADER` row instead of getting their own hex line:
```
------------------------------------------------------------------------------------
Packet #5 at 0x7e47efe23d40                                        KERNEL_DISPATCH
------------------------------------------------------------------------------------
+0x00  02 00 01 00                 | HEADER type=2 barrier=0 acquire=0 release=0
                                   | SETUP = 0x1
+0x04  00 01 01 00                 | WORKGROUP_X = 256
                                   | WORKGROUP_Y = 1
+0x08  01 00 00 00                 | WORKGROUP_Z = 1
+0x0c  00 04 00 00                 | GRID_X = 1024
+0x10  01 00 00 00                 | GRID_Y = 1
+0x14  01 00 00 00                 | GRID_Z = 1
+0x18  00 00 00 00                 | PRIVATE_SEGMENT_SIZE = 0
+0x1c  00 20 00 00                 | GROUP_SEGMENT_SIZE = 8192
+0x20  00 00 56 34 ┌
+0x24  12 7f 00 00 ┘               | KERNEL_OBJECT = 0x7f1234560000
+0x28  00 00 57 34 ┌
+0x2c  12 7f 00 00 ┘               | KERNARG_ADDRESS = 0x7f1234570000
+0x30  00 00 00 00 ┌
+0x34  00 00 00 00 ┘               | (reserved)
+0x38  00 00 58 34 ┌
+0x3c  12 7f 00 00 ┘               | COMPLETION_SIGNAL = 0x7f1234580000
------------------------------------------------------------------------------------
```
`Barrier And`/`Barrier Or` (`DEP_SIGNAL_0`..`_4`) and `Agent Dispatch` (`TYPE`, with bytes 8-47
still undecoded as before -- the original decoder never broke those down either) follow the
same convention.

An `Invalid` packet (AQL type 1 -- the spec-defined state for a slot the hardware has already
consumed and reset, which in practice can be most of a ring once it's wrapped around at least
once) still has its fields decoded by peeking the next dword and guessing Kernel Dispatch or
Barrier, same heuristic as the original decoder, with a `(invalid packet, reinterpreted as type
N)` note -- but the packet's **title always shows `INVALID`**, never the guessed type. Showing
the guessed type in the title made an idle/already-processed ring look like it was full of live
`BARRIER_AND`/`KERNEL_DISPATCH` packets, which is actively misleading for hang debugging (you'd
suspect a barrier storm that isn't real). The reinterpreted fields are still shown below the
title -- only the title changed.

### Browser UI instead of the REPL
Same tool, `--web` instead of nothing, and point it at a whole `dump_all_queues` output
directory instead of one file to browse every queue from one page:
```
$ python3 queue_viewer.py rocgdb_dump_bin_pid.../ --web
Serving 33 queue dump(s) from rocgdb_dump_bin_pid.../
Open http://127.0.0.1:8765/ in a browser (Ctrl-C to stop)
```
Binds to `127.0.0.1` only by default (use an SSH tunnel/port-forward to reach it from your
laptop, e.g. `ssh -L 8765:localhost:8765 <host>`) -- pass `--host 0.0.0.0` to listen on all
interfaces if you really want that, and `--port N` to change the port. It's a plain-text
browser version of the REPL (pick a queue in the sidebar, click info/all/rp/wp, use the
`help` button for the full command reference, or type a packet/range/raw index) -- no ring
visualization, stdlib `http.server` only, no extra dependencies to install. `all` is capped at
2000 packets in the browser view (says so in the output when it truncates) so a huge ring
doesn't try to render a giant page in one go; use `range`/`packet` for anything beyond that.

Full parity with the REPL: the `packet`/`range`/`raw` boxes accept `rp`/`wp` (optionally
`+N`/`-N`) exactly like the REPL does, e.g. `wp-1` or a range of `rp` to `rp+5`; an
invalid index or unknown queue name returns a clean JSON error (400/404) instead of a
traceback. The sidebar shows each queue's type (color-coded HSA/DMA/XGMI badge), qid, decoded
packet count, and size so you can tell queues apart without opening each one.

## Manual, one queue at a time
get queue info and dump queue packet and signal info manually, one at a time (in rocgdb):
```
(gdb) info queue
  Id   Target Id                  Type         Read   Write  Size     Address            
  1    AMDGPU Queue 4:1 (QID 27)  DMA                        1048576  0x00007e4b57600000 
  2    AMDGPU Queue 4:2 (QID 26)  DMA                        1048576  0x00007e4b57800000 
  3    AMDGPU Queue 3:3 (QID 25)  DMA                        1048576  0x00007e4b58200000 
  4    AMDGPU Queue 3:4 (QID 24)  DMA                        1048576  0x00007e4b58400000 
  5    AMDGPU Queue 2:5 (QID 23)  DMA                        1048576  0x00007e4b58e00000 
  6    AMDGPU Queue 2:6 (QID 22)  DMA                        1048576  0x00007e4b59000000 
  7    AMDGPU Queue 1:7 (QID 21)  DMA                        1048576  0x00007e4b59a00000 
  8    AMDGPU Queue 1:8 (QID 20)  DMA                        1048576  0x00007e4b5c400000 
  9    AMDGPU Queue 4:9 (QID 19)  HSA          438427 438427 1048576  0x00007e4bb1a00000 
  10   AMDGPU Queue 4:10 (QID 18) HSA          481374 481374 1048576  0x00007e4bb2c00000 
  11   AMDGPU Queue 4:11 (QID 17) HSA          479194 479194 1048576  0x00007e4bb3a00000 
  12   AMDGPU Queue 4:12 (QID 16) HSA          423666 423666 1048576  0x00007e4bb5c00000 
  13   AMDGPU Queue 4:13 (QID 15) HSA          1588   1588   4096     0x00007f91cc714000 
  14   AMDGPU Queue 3:14 (QID 14) HSA          444298 444298 1048576  0x00007e4bb7600000 
  15   AMDGPU Queue 3:15 (QID 13) HSA          453365 453365 1048576  0x00007e4bb8800000 
  16   AMDGPU Queue 3:16 (QID 12) HSA          425246 425246 1048576  0x00007e4bbc000000 
  17   AMDGPU Queue 3:17 (QID 11) HSA          418775 418775 1048576  0x00007e4bbd200000 
  18   AMDGPU Queue 3:18 (QID 10) HSA          1592   1592   4096     0x00007f91cce98000 
  19   AMDGPU Queue 2:19 (QID 9)  HSA          444064 444064 1048576  0x00007e4bbec00000 
  20   AMDGPU Queue 2:20 (QID 8)  HSA          455545 455545 1048576  0x00007e4bbfe00000 
  21   AMDGPU Queue 2:21 (QID 7)  HSA          419029 419029 1048576  0x00007e4bc0c00000 
  22   AMDGPU Queue 2:22 (QID 6)  HSA          419071 419071 1048576  0x00007e4bc2e00000 
  23   AMDGPU Queue 2:23 (QID 5)  HSA          1580   1580   4096     0x00007f91cceda000 
  24   AMDGPU Queue 1:24 (QID 4)  HSA          438252 438252 1048576  0x00007e4bc4800000 
  25   AMDGPU Queue 1:25 (QID 3)  HSA          2569549 2569550 1048576  0x00007e4bc5a00000 
  26   AMDGPU Queue 1:26 (QID 2)  HSA          480617 480617 1048576  0x00007e4bc8600000 
  27   AMDGPU Queue 1:27 (QID 1)  HSA          4735680 4735680 1048576  0x00007e4bc9800000 
  28   AMDGPU Queue 1:28 (QID 0)  HSA          1660   1660   4096     0x00007f91ccf1c000 
  
(gdb) dump_hsa_queue 0x00007e4bc9800000  2569549 2569550 1048576
------------------------------
Packet #2293 at 0x7e47efe23d40: header=0x1503 (type=3, barrier=1, acquire=2, release=2)
Barrier Packet Fields:
  dep_signal[0]=0x0
  dep_signal[1]=0x0
  dep_signal[2]=0x0
  dep_signal[3]=0x0
  dep_signal[4]=0x0
  completion_signal=0x7e4cb57d3100

(gdb) dump_hsa_signal 0x7e4cb57d3100
Signal at 0x7e4cb57d3100:
Signal Fields:
  kind=user(1)
  value=1
  mailbox_ptr=0x7f8df84c37d8
  event_id=1787
  start_ts=257063712923241, end_ts=257063712923413
  queue_ptr=0x0
```

save all packet into file:
```
(gdb) dump_queue_memory binary qid3_0x00007e4bc5a00000 0x00007e4bc5a00000 1048576
success: qid3_0x00007e4bc5a00000 (size:  1048576 bytes)
```

modify signal:
```
(gdb) modify_hsa_signal 0x7e4cb57d3100 0
Signal Fields:
  kind=user(1)
  value=1
  mailbox_ptr=0x7f8df84c37d8
  event_id=1787
  start_ts=257063712923241, end_ts=257063712923413
  queue_ptr=0x0
  Modified signal at 0x7e4cb57d3100 - value changed from 1 to 0
```

get doorbell signal from the rocgdb:
```
(gdb) thread 348
(gdb) bt
#0  0x00007f07556687c7 in rocr::timer::fast_clock::now () at /data/testhome/mainline-rocm-runtime/amd_new_base/ROCR-Runtime/runtime/hsa-runtime/core/util/timer.h:140
#1  rocr::core::InterruptSignal::WaitRelaxed (this=0x7dc1642d2ee0, condition=HSA_SIGNAL_CONDITION_LT, compare_value=1, timeout=<optimized out>, wait_hint=HSA_WAIT_STATE_ACTIVE)
    at /data/testhome/mainline-rocm-runtime/amd_new_base/ROCR-Runtime/runtime/hsa-runtime/core/runtime/interrupt_signal.cpp:212
#2  0x00007f075566808a in rocr::core::InterruptSignal::WaitAcquire (this=<optimized out>, condition=<optimized out>, compare_value=<optimized out>, timeout=<optimized out>, wait_hint=<optimized out>)
    at /data/testhome/mainline-rocm-runtime/amd_new_base/ROCR-Runtime/runtime/hsa-runtime/core/runtime/interrupt_signal.cpp:265
#3  0x00007f075565cff9 in rocr::HSA::hsa_signal_wait_scacquire (hsa_signal=..., condition=HSA_SIGNAL_CONDITION_LT, compare_value=1, timeout_hint=18446744073709551615, 
    wait_state_hint=HSA_WAIT_STATE_ACTIVE) at /data/testhome/mainline-rocm-runtime/amd_new_base/ROCR-Runtime/runtime/hsa-runtime/core/runtime/hsa.cpp:1239
#4  0x00007f0753f393fb in amd::roc::WaitForSignal<false> (forced_wait=false, active_wait=<optimized out>, signal=...)
    at /data/testhome/mainline-rocm-runtime/amd_new_base/clr/rocclr/device/rocm/rocvirtual.hpp:70
#5  amd::roc::Device::IsHwEventReady (this=<optimized out>, event=..., wait=<optimized out>, hip_event_flags=<optimized out>)
    at /data/testhome/mainline-rocm-runtime/amd_new_base/clr/rocclr/device/rocm/rocdevice.cpp:3007
#6  0x00007f0753f1e67a in amd::HostQueue::finish (this=0x7dc622f51900, cpu_wait=<optimized out>) at /data/testhome/mainline-rocm-runtime/amd_new_base/clr/rocclr/platform/commandqueue.cpp:164
#7  0x00007f0753cba6ce in hip::Device::SyncAllStreams (this=0x7dc624314300, cpu_wait=<optimized out>, wait_blocking_streams_only=<optimized out>)
    at /data/testhome/mainline-rocm-runtime/amd_new_base/clr/hipamd/src/hip_device.cpp:281
#8  0x00007f0753ca5799 in hip::hipDeviceSynchronize () at /data/testhome/mainline-rocm-runtime/amd_new_base/clr/hipamd/src/hip_device_runtime.cpp:621
#9  0x00007f075b9b04ab in stream_executor::gpu::GpuDriver::SynchronizeContext () at external/org_tensorflow/tensorflow/stream_executor/rocm/rocm_driver.cc:886
#10 0x00007f075b83805c in stream_executor::StreamExecutor::SynchronizeAllActivity () at external/org_tensorflow/tensorflow/stream_executor/stream_executor_pimpl.cc:554
#11 0x00007f075f18e10a in tensorflow::XlaCompilationCache::~XlaCompilationCache () at external/org_tensorflow/tensorflow/compiler/jit/xla_compilation_cache.cc:78
#12 0x00007f075f18e512 in tensorflow::XlaCompilationCache::~XlaCompilationCache () at external/org_tensorflow/tensorflow/compiler/jit/xla_compilation_cache.cc:90
#13 0x00007f075b25c4d7 in tensorflow::core::RefCounted::Unref () at external/org_tensorflow/tensorflow/core/lib/core/refcount.h:104
#14 tensorflow::core::RefCounted::Unref () at external/org_tensorflow/tensorflow/core/lib/core/refcount.h:97
#15 tensorflow::ResourceMgr::Clear () at external/org_tensorflow/tensorflow/core/framework/resource_mgr.cc:119
#16 0x00007f07664a68a4 in tensorflow::DirectSession::~DirectSession () at external/org_tensorflow/tensorflow/core/common_runtime/direct_session.cc:474
#17 0x00007f07664a72b2 in tensorflow::DirectSession::~DirectSession () at external/org_tensorflow/tensorflow/core/common_runtime/direct_session.cc:478
#18 0x00000000059de862 in std::_Sp_counted_base<(__gnu_cxx::_Lock_policy)2>::_M_release () at /usr/lib/gcc/x86_64-redhat-linux/10/../../../../include/c++/10/bits/shared_ptr_base.h:158
#19 std::__shared_count<(__gnu_cxx::_Lock_policy)2>::~__shared_count () at /usr/lib/gcc/x86_64-redhat-linux/10/../../../../include/c++/10/bits/shared_ptr_base.h:733
#20 std::__shared_ptr<tensorflow::Session, (__gnu_cxx::_Lock_policy)2>::~__shared_ptr () at /usr/lib/gcc/x86_64-redhat-linux/10/../../../../include/c++/10/bits/shared_ptr_base.h:1183
#21 std::shared_ptr<tensorflow::Session>::~shared_ptr () at /usr/lib/gcc/x86_64-redhat-linux/10/../../../../include/c++/10/bits/shared_ptr.h:121
#22 std::_Destroy<std::shared_ptr<tensorflow::Session> > () at /usr/lib/gcc/x86_64-redhat-linux/10/../../../../include/c++/10/bits/stl_construct.h:140
#23 std::_Destroy_aux<false>::__destroy<std::shared_ptr<tensorflow::Session>*> () at /usr/lib/gcc/x86_64-redhat-linux/10/../../../../include/c++/10/bits/stl_construct.h:152
#24 std::_Destroy<std::shared_ptr<tensorflow::Session>*> () at /usr/lib/gcc/x86_64-redhat-linux/10/../../../../include/c++/10/bits/stl_construct.h:185
#25 std::_Destroy<std::shared_ptr<tensorflow::Session>*, std::shared_ptr<tensorflow::Session> > () at /usr/lib/gcc/x86_64-redhat-linux/10/../../../../include/c++/10/bits/alloc_traits.h:738
#26 std::vector<std::shared_ptr<tensorflow::Session>, std::allocator<std::shared_ptr<tensorflow::Session> > >::~vector ()
    at /usr/lib/gcc/x86_64-redhat-linux/10/../../../../include/c++/10/bits/stl_vector.h:680
#27 suez::turing::TfSession::~TfSession () at bazel-out/k8-opt/bin/aios/suez_turing/_virtual_includes/query_resource/suez/turing/common/TfSession.h:15
#28 __gnu_cxx::new_allocator<suez::turing::TfSession>::destroy<suez::turing::TfSession> () at /usr/lib/gcc/x86_64-redhat-linux/10/../../../../include/c++/10/ext/new_allocator.h:156
#29 std::allocator_traits<std::allocator<suez::turing::TfSession> >::destroy<suez::turing::TfSession> () at /usr/lib/gcc/x86_64-redhat-linux/10/../../../../include/c++/10/bits/alloc_traits.h:531
#30 std::_Sp_counted_ptr_inplace<suez::turing::TfSession, std::allocator<suez::turing::TfSession>, (__gnu_cxx::_Lock_policy)2>::_M_dispose ()
    at /usr/lib/gcc/x86_64-redhat-linux/10/../../../../include/c++/10/bits/shared_ptr_base.h:560
#31 0x000000000631ab72 in std::_Sp_counted_base<(__gnu_cxx::_Lock_policy)2>::_M_release () at /usr/lib/gcc/x86_64-redhat-linux/10/../../../../include/c++/10/bits/shared_ptr_base.h:158
#32 std::__shared_count<(__gnu_cxx::_Lock_policy)2>::~__shared_count () at /usr/lib/gcc/x86_64-redhat-linux/10/../../../../include/c++/10/bits/shared_ptr_base.h:733
#33 std::__shared_ptr<suez::turing::TfSession, (__gnu_cxx::_Lock_policy)2>::~__shared_ptr () at /usr/lib/gcc/x86_64-redhat-linux/10/../../../../include/c++/10/bits/shared_ptr_base.h:1183
#34 std::shared_ptr<suez::turing::TfSession>::~shared_ptr () at /usr/lib/gcc/x86_64-redhat-linux/10/../../../../include/c++/10/bits/shared_ptr.h:121
#35 std::pair<std::basic_string<char, std::char_traits<char>, std::allocator<char> > const, std::shared_ptr<suez::turing::TfSession> >::~pair ()
    at /usr/lib/gcc/x86_64-redhat-linux/10/../../../../include/c++/10/bits/stl_pair.h:211
#36 __gnu_cxx::new_allocator<std::_Rb_tree_node<std::pair<std::basic_string<char, std::char_traits<char>, std::allocator<char> > const, std::shared_ptr<suez::turing::TfSession> > > >::destroy<std::pair<std::basic_string<char, std::char_traits<char>, std::allocator<char> > const, std::shared_ptr<suez::turing::TfSession> > > ()
    at /usr/lib/gcc/x86_64-redhat-linux/10/../../../../include/c++/10/ext/new_allocator.h:156
#37 std::allocator_traits<std::allocator<std::_Rb_tree_node<std::pair<std::basic_string<char, std::char_traits<char>, std::allocator<char> > const, std::shared_ptr<suez::turing::TfSession> > > > >::destroy<std::pair<std::basic_string<char, std::char_traits<char>, std::allocator<char> > const, std::shared_ptr<suez::turing::TfSession> > > ()
    at /usr/lib/gcc/x86_64-redhat-linux/10/../../../../include/c++/10/bits/alloc_traits.h:531
#38 std::_Rb_tree<std::basic_string<char, std::char_traits<char>, std::allocator<char> >, std::pair<std::basic_string<char, std::char_traits<char>, std::allocator<char> > const, std::shared_ptr<suez::turing::TfSession> >, std::_Select1st<std::pair<std::basic_string<char, std::char_traits<char>, std::allocator<char> > const, std::shared_ptr<suez::turing::TfSession> > >, std::less<std::basic_string<char, std:--Type <RET> for more, q to quit, c to continue without paging--Quit
(gdb) f 6
(gdb) p command->queue_
$2 = (amd::HostQueue *) 0x7dc622f51900
(gdb) p ((amd::HostQueue *) 0x7dc622f51900)->thread_.virtualDevice_
$3 = (amd::device::VirtualDevice *) 0x7dc6233a8f00
(gdb) p ((amd::roc::VirtualGPU *) 0x7dc6233a8f00)->gpu_queue_
$4 = (hsa_queue_t *) 0x7f0757fde000
(gdb) p ((hsa_queue_t *) 0x7f0757fde000)->doorbell_signal->handle
```
