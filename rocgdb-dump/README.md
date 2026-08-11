# rocgdb-dump

rocgdb tooling for inspecting HSA/SDMA user-queue state (and HSA signals) on a hung or
running ROCm process: dump every queue automatically, save as text or as a fast binary
capture, and browse the result offline via a REPL or a small local web UI.

## 1. Background & architecture

### Origin

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
  packets via an interactive REPL (single file or a whole directory, with `list`/`use` to switch
  between queues) or a local browser UI.
- `dump_all_queues` / `dump_all_queues_txt` commands in `rocgdb_helper.py` -- automatically find
  and dump every HSA/DMA/XGMI queue (plus all-thread backtraces) instead of hand-copying
  addresses out of `info queue` one at a time.
- `dump_all_queues.sh` -- a one-shot shell wrapper around the whole
  attach/source/dump/detach sequence, pid supplied as a command-line argument.
- `rp`/`wp` jump navigation, XGMI queue-type support, and a handful of bug fixes found along
  the way (a `super()` typo in `ModifyHsaSignal` that crashed the whole script load, an
  `info queue` column-parsing regex that misread Type as a Read value on two-digit queue IDs,
  a packet index counter that never incremented in the SDMA decoder).
- Per-run `dump_summary.json`, `info queues`/`info dispatches` capture (patched in place with
  best-effort SDMA rptr/wptr values), and Target Id embedded in queue dump filenames.
- Best-effort SDMA rptr/wptr enrichment for DMA/XGMI queues, read straight out of KFD debugfs
  (no `umr` dependency), plus `rp`/`wp` jump navigation for DMA/XGMI in `queue_viewer.py`.
- Full SDMA packet decode, ported from UMR's own SDMA source -- replacing an earlier, much
  narrower decoder that only handled 5 opcodes and, worse, silently mis-sized several of them
  (every `COPY` was assumed to be the `LINEAR` sub-opcode's 28 bytes; every `POLL_REGMEM` was
  assumed to be the `MEM` sub-opcode's 24 bytes), desyncing every packet after the first
  mismatch for the rest of the ring (see "Supported features & TODO" below for what exactly
  this covers).

### Acknowledgments

Two pieces of this tool's SDMA support are directly ported from
[UMR](https://gitlab.freedesktop.org/tomstdenis/umr) ("User Mode Register Debugger for AMDGPU
Hardware", Copyright (c) 2025 AMD Inc., docs at https://umr.readthedocs.io/en/main/) -- an
existing, much more comprehensive open-source AMDGPU userspace debugging tool (MMIO register
access, wavefront analysis, ring contents, performance tracking, and more, going back to SI-era
hardware). This repo doesn't depend on the `umr` binary at all -- rather, the *logic* for two
specific things was read out of UMR's C source and reimplemented in Python here, credited at the
point of use in `queue_decode.py`/`rocgdb_helper.py`:
- **SDMA packet sizing and field decode** (`queue_decode.py`) -- ported from
  `umr/src/lib/packet/sdma/read_sdma_stream.c`'s `sized_oss1_5()` (how many bytes each SDMA
  packet occupies, every opcode/sub-opcode, generation-agnostic) and
  `umr/src/lib/packet/sdma/sdma_decode_opcodes.c`'s `decode_upto_ai()` (the field-level decode
  for the specific SDMA generation this tool has been verified against).
- **SDMA MQD (Memory Queue Descriptor) parsing** (`rocgdb_helper.py`'s SDMA rptr/wptr
  enrichment) -- the per-generation RPTR/WPTR word-offset table is transcribed from
  `umr/src/lib/lowlevel/linux/parse_clientid.c`, and the whole feature is conceptually the same
  underlying KFD debugfs data UMR's own `--list-uq`/`--print-uq` are built on.

Thanks to the UMR maintainers/contributors for building and documenting that logic in the first
place -- reading their source was materially faster and more reliable than reverse-engineering
the SDMA packet ring format and MQD layout from scratch.

### Files

- `rocgdb_helper.py` -- load into rocgdb (`source rocgdb_helper.py` after attaching). Defines all
  the `dump_*`/`modify_hsa_signal` commands. This is the **live** path: everything it decodes
  goes through gdb's memory-read channel against the actual attached process.
- `queue_decode.py` -- shared HSA/SDMA packet decoder + `.bin` dump container format. No gdb
  dependency; imported by both `rocgdb_helper.py` (live) and `queue_viewer.py` (offline), so a
  `.bin` dump always decodes identically to what the live path would have shown for the same
  queue -- one implementation, not two that can drift apart.
- `queue_viewer.py` -- standalone **offline** tool for browsing `.bin` dumps (REPL or `--web`).
  No gdb dependency at all; reads the raw ring bytes straight out of the dump file.
- `dump_all_queues.sh` -- one-shot wrapper: attach to a given pid, source `rocgdb_helper.py`,
  run `dump_all_queues`, detach -- no manual `attach`/`source`/`quit` typing, no editing a
  hardcoded pid into a file. The quickest way to get a capture (see "Usage" below).
- `save_info.gdb` -- the original, older one-shot script this repo grew out of (attach, dump
  queues/dispatches/threads/registers via plain `info` commands, quit). Superseded by
  `dump_all_queues.sh`/`dump_all_queues` for queue-focused debugging, but still useful as a
  minimal, dependency-free capture of general process state.

### Requirements & dependencies

- **rocgdb** -- required for the live path (`rocgdb_helper.py`, `dump_all_queues.sh`,
  `save_info.gdb`). `queue_viewer.py` has no gdb dependency at all once a `.bin` dump exists.
- **Python 3** -- stdlib only, no `pip install` needed anywhere in this repo (`queue_viewer.py`'s
  `--web` mode is plain `http.server`; `readline` for REPL history is stdlib and optional --
  degrades to plain `input()` where unavailable, e.g. stock Windows Python).
  `queue_decode.py`/`rocgdb_helper.py` (loaded *inside* rocgdb's own embedded Python) work the
  same way -- no extra packages inside rocgdb's Python either.
- **root/sudo** -- needed to `ptrace`-attach to another user's process (the common case for a
  hung production job). Also see the container caveat below: `--cap-add=SYS_PTRACE` at
  container-*creation* time is required in addition to running as root inside the container.
- **`/sys/kernel/debug/kfd/mqds`** (KFD debugfs) -- read for the best-effort SDMA rptr/wptr
  enrichment only (everything else works without it, just without Read/Write values for
  DMA/XGMI rows). Root-only, and **not exposed inside containers by default** -- needs an
  explicit `-v /sys/kernel/debug:/sys/kernel/debug:ro` bind mount at container-creation time,
  unlike regular sysfs (`/sys/class/*`), which containers normally see via the default `/sys`
  bind mount. See "SDMA rptr/wptr enrichment" under "Supported features & TODO" for the full
  container/multi-tenant-host discussion.
- **`/sys/class/kfd` + `/sys/class/drm/*/device/ip_discovery`** -- read to detect each GPU's
  generation (for the SDMA MQD offset table and the SDMA IP-version-specific field decoder),
  entirely root-free, no `umr` binary or ASIC database needed.

## 2. Usage -- from simplest to most advanced

### 2.1 Quickest: one-shot script

`dump_all_queues.sh` automates the whole "attach, source the helper, dump, detach" sequence
into one command -- the same idea as `save_info.gdb`, except the pid is a command-line argument
instead of something you hand-edit into a file:
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

This is the recommended first step for almost everyone -- read on for what's actually happening
under the hood, and for the manual/advanced alternatives.

### 2.2 Browsing the result: `queue_viewer.py`

With **no gdb involved at all**, open any `.bin` file the previous step produced in the
standalone `queue_viewer.py` and browse packets interactively:
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
either a plain integer or `rp`/`wp` optionally followed by `+N`/`-N`. Up/down-arrow command
history works in the REPL via Python's `readline` module when available.

Point the same command at a **directory** instead of one `.bin` file, and the REPL gains a
`list`/`use` pair for switching between every queue in it, instead of being fixed to one file for
the whole session:
```
$ python3 queue_viewer.py rocgdb_dump_bin_pid.../
6 queue dump(s) found under rocgdb_dump_bin_pid.../
   [0] dma_QID4_GPU_8_Queue_2.bin  type=DMA size=8388608 rp=21 wp=48 <-- PENDING
   [1] dma_QID5_GPU_8_Queue_1.bin  type=DMA size=8388608 rp=21 wp=48 <-- PENDING
   [2] hsa_QID0_GPU_8_Queue_6.bin  type=HSA size=4096 rp=2 wp=2
   [3] hsa_QID1_GPU_8_Queue_5.bin  type=HSA size=1048576 rp=0 wp=2 <-- PENDING
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
Rows where `rp != wp` -- submitted work the GPU hasn't consumed yet, exactly what's worth
looking at first on a hung process -- are marked `<-- PENDING` and (when stdout is a
real terminal, not piped/redirected) shown in bold red; rows where `rp`/`wp` aren't known at all
(enrichment didn't find anything for that DMA/XGMI queue) are shown plain, not flagged, since
"unknown" isn't the same as "pending". `list` (aliases `ls`/`queues`) only reads each dump's
header, not its full ring bytes, so it stays fast regardless of how many/how large the queues in
the directory are -- packet decoding only happens once a queue is actually selected via `use`.
All of the single-file commands above (`info`/`packet`/`range`/`all`/`raw`/`rp`/`wp`) apply to
whichever queue is currently selected;
running one before any `use` prints a reminder instead of an error.

Or point `--web` at the same directory for a plain-text browser UI instead of the REPL --
same commands, same output, picked from a sidebar instead of typed:
```
$ python3 queue_viewer.py rocgdb_dump_bin_pid.../ --web
Serving 33 queue dump(s) from rocgdb_dump_bin_pid.../
Open http://127.0.0.1:8765/ in a browser (Ctrl-C to stop)
```
Binds to `127.0.0.1` only by default (use an SSH tunnel/port-forward to reach it from your
laptop, e.g. `ssh -L 8765:localhost:8765 <host>`) -- pass `--host 0.0.0.0` to listen on all
interfaces if you really want that, and `--port N` to change the port. No ring visualization,
stdlib `http.server` only, no extra dependencies to install. `all` is capped at 2000 packets in
the browser view so a huge ring doesn't try to render a giant page in one go; use
`range`/`packet` for anything beyond that. Full parity with the REPL, including `rp`/`wp`
expressions in the `packet`/`range`/`raw` boxes. Queues where `rp != wp` get the same
`PENDING` highlight as the REPL's `list` (a red left border and badge on the sidebar card).

### 2.3 Manual equivalent: driving rocgdb yourself

Same result as 2.1, without the wrapper script -- useful if you're already inside a live rocgdb
session (e.g. mid-investigation) or want to run other commands in between:
```
sudo rocgdb attach <hang_pid>
source rocgdb_helper.py
dump_all_queues [output_dir]
```
```
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
`dump_all_queues` finds every HSA/DMA/XGMI queue itself (no manual copy/paste from `info queue`
needed), reads each one's raw bytes with a single bulk memory read (fast -- one round-trip per
queue, not per packet), captures `info queues`/`info dispatches`, and saves all-thread
backtraces, all in one shot. Decode happens later, offline, against an in-memory buffer instead
of over gdb. Each queue is saved as `hsa_QID<N>_GPU_<A>_Queue_<B>.bin` /
`dma_QID<N>_GPU_<A>_Queue_<B>.bin` (also `xgmi_QID<N>_GPU_<A>_Queue_<B>.bin` for XGMI-transport
DMA queues); `<N>` is the same `(QID N)` shown by `info queue`, `<A>`/`<B>` come from the queue's
Target Id (`AMDGPU Queue <A>:<B> (QID N)`). One bad/unreadable queue won't stop the rest of the
batch; failures are reported in the summary.

Every run also writes `info_queues.log`/`info_dispatches.log` (raw `info queues`/
`info dispatches -full` output, with any successfully-enriched DMA/XGMI Read/Write values
patched directly into `info_queues.log`'s columns in place) and `dump_summary.json` (a
machine/eyeball-readable manifest of what got captured -- queue counts, files written, any
per-queue failures). See "Supported features & TODO" below for the full details of both.

### 2.4 The slower alternative: live text decode

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
`info_dispatches.log` capture, and `dump_summary.json` as `dump_all_queues`. `dump_all_queues.sh
--txt <pid>` runs this instead of the default, if you want the one-shot script's convenience
with this variant.

### 2.5 Converting an existing `.bin` capture to text, offline

Already have a `dump_all_queues` (binary) capture and want the same plain-text `.log` files
`dump_all_queues_txt` would have produced, without re-attaching to the process at all? `queue_viewer.py
--to-txt` converts them **offline** -- no gdb, no live process, just the same decode pipeline the
REPL/`--web` already use, batch-run over every `.bin` file and written out in the exact same
format `dump_all_queues_txt` uses (shared via `queue_decode.write_dump_txt_header`, so a `.log`
produced this way reads identically to one produced live for the same queue):
```
$ python3 queue_viewer.py rocgdb_dump_bin_pid.../ --to-txt
dma_QID4_GPU_8_Queue_2.bin -> rocgdb_dump_bin_pid.../dma_QID4_GPU_8_Queue_2.log
hsa_QID0_GPU_8_Queue_6.bin -> rocgdb_dump_bin_pid.../hsa_QID0_GPU_8_Queue_6.log
...
Converted 6 of 6 dump(s) to text.
```
Works against a single `.bin` file or a whole directory (same `list_bin_files` used by `--web`).
Writes each `<name>.log` alongside its source `<name>.bin` by default; pass `--outdir DIR` to
write elsewhere instead. Doesn't touch `info_queues.log`/`info_dispatches.log`/
`dump_summary.json`/`backtrace_all_threads.log` -- those already exist from the original
`dump_all_queues` run this is converting; only the per-queue packet-decode text is regenerated.
One bad/corrupt `.bin` doesn't stop the rest of the batch (reported as a failure, same
"don't let one bad queue abort the whole thing" philosophy as `dump_all_queues` itself). Same
known asymmetry as browsing offline elsewhere in this doc: kernel dispatch packets show the raw
`kernel_object` address only (no live process to resolve a symbol name against).

### 2.6 Manual, one queue at a time (most advanced)

For anything not covered by the automatic paths above -- inspecting one specific queue/signal by
hand, or walking a thread's stack to find a doorbell signal address -- the original granular
commands are still available, one at a time (in rocgdb):
```
(gdb) info queue
  Id   Target Id                  Type         Read   Write  Size     Address            
  1    AMDGPU Queue 4:1 (QID 27)  DMA                        1048576  0x00007e4b57600000 
  ...
  27   AMDGPU Queue 1:27 (QID 1)  HSA          4735680 4735680 1048576  0x00007e4bc9800000 

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

Save all packet bytes into a file:
```
(gdb) dump_queue_memory binary qid3_0x00007e4bc5a00000 0x00007e4bc5a00000 1048576
success: qid3_0x00007e4bc5a00000 (size:  1048576 bytes)
```

Modify a signal:
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

Get a doorbell signal address by walking a thread's stack manually:
```
(gdb) thread 348
(gdb) bt
#0  0x00007f07556687c7 in rocr::timer::fast_clock::now () at .../timer.h:140
#1  rocr::core::InterruptSignal::WaitRelaxed (this=0x7dc1642d2ee0, ...) at .../interrupt_signal.cpp:212
#2  0x00007f075566808a in rocr::core::InterruptSignal::WaitAcquire (...) at .../interrupt_signal.cpp:265
...
#6  0x00007f0753f1e67a in amd::HostQueue::finish (this=0x7dc622f51900, ...) at .../commandqueue.cpp:164
...
(gdb) f 6
(gdb) p command->queue_
$2 = (amd::HostQueue *) 0x7dc622f51900
(gdb) p ((amd::HostQueue *) 0x7dc622f51900)->thread_.virtualDevice_
$3 = (amd::device::VirtualDevice *) 0x7dc6233a8f00
(gdb) p ((amd::roc::VirtualGPU *) 0x7dc6233a8f00)->gpu_queue_
$4 = (hsa_queue_t *) 0x7f0757fde000
(gdb) p ((hsa_queue_t *) 0x7f0757fde000)->doorbell_signal->handle
```

## 3. Supported features & TODO

### Supported features

- **`dump_all_queues.sh`** -- one-shot shell wrapper: attach, source the helper, dump, detach,
  pid as a CLI argument; auto-`sudo`-escalates; `--txt` for the text variant; full session
  transcript saved to a log file (see 2.1).
- **`dump_all_queues`** -- fast binary capture of every HSA/DMA/XGMI queue in one shot: one bulk
  memory read per queue (not per packet), `.bin` file + metadata per queue, `info_queues.log`/
  `info_dispatches.log`/`dump_summary.json` per run, Target Id embedded in filenames (see 2.3).
- **`dump_all_queues_txt`** -- same discovery/capture, but decodes every packet to text while
  attached live instead of offline; slower on a hung process with large/many rings (see 2.4).
- **Filenames** -- `TYPE_QID<n>_GPU_<A>_Queue_<B>.{bin,log}`, e.g.
  `dma_QID6_GPU_5_Queue_27.bin` for Target Id `AMDGPU Queue 5:27 (QID 6)`.
- **SDMA rptr/wptr enrichment** -- best-effort fill-in of Read/Write for DMA/XGMI rows (which
  `info queue` never reports on its own, since amd-dbgapi's packet-ID abstraction is
  HSA/AQL-specific), by reading the SDMA queue's MQD straight out of
  `/sys/kernel/debug/kfd/mqds` (root required, no `umr` binary). Patched directly into
  `info_queues.log`'s existing Read/Write columns, in place (column widths derived from the
  header line itself, not hardcoded), and into each DMA/XGMI queue's `.bin`/`.log` metadata and
  `dump_summary.json`. Silent and non-fatal when it doesn't work (no root, no debugfs,
  unrecognized GPU generation, no matching queue).
  - **Storage matches HSA; units don't.** Read/Write here are stored the same way as HSA's -- a
    raw, un-wrapped counter, wrapped to a ring position later, at use time, in `queue_viewer.py`
    -- but the *unit* differs: a ring-relative dword position, not a monotonic packet ID (SDMA
    packets are variable-length, so there's no "packet ID" concept at the hardware level).
    `rp`/`wp` show the full conversion chain for this reason: `raw=N -> dword slot M -> byte
    offset 0x... -> packet index K (of TOTAL)`.
  - **Verified against real hardware for GFX9 only** (byte-for-byte matched against UMR's own
    decoded values on this host); GFX10/11/12 offset table entries are transcribed from UMR's
    source but not independently hardware-verified (see TODO).
  - **Container caveat:** `/sys/kernel/debug` needs an explicit bind mount (not present by
    default) and root inside the container; root also needs `--cap-add=SYS_PTRACE` at
    container-*creation* time to `ptrace`-attach to the (non-root) target process at all --
    `docker exec`/`docker update` cannot retrofit that capability onto an already-running
    container. Matching is deliberately done by ring base address, not pid, because a
    containerized rocgdb sees the target's *container-local* pid, which generally has no
    relationship to the *host-level* pid KFD debugfs reports for the same process.
  - **Multi-tenant host caveat:** the same system-wide address-matching narrows to your own
    attached process's queues *before* attempting any actual memory read, so other users'
    unrelated SDMA queues on a shared host show up as silence, not "Cannot access memory" spam.
- **Full SDMA packet decode** -- ported from UMR (see Acknowledgments): sizing is a full port of
  `sized_oss1_5()` (every opcode/sub-opcode, generation-agnostic across SDMA/OSS IP versions
  1-6, checked against this host's actual SDMA IP version via `ip_discovery`) -- this matters
  because getting a packet's size wrong desyncs every packet after it for the rest of the ring.
  Field-level decode is a port of `decode_upto_ai()`, the one (of four: VI/AI/NV/OSS7)
  generation-specific decoder matching this host's confirmed SDMA IP version (major 4, minor 4).
  **The other three generations are not ported** -- an unrecognized-but-correctly-sized packet
  gets a generic `(recognized, N bytes, not decoded in detail)` line instead of a field
  breakdown; the ring walk stays correct either way (see TODO). Verified against real hardware:
  replayed against every DMA/XGMI queue already captured in this repo (tens of thousands of real
  packets) with zero decode errors. Two deliberate additions beyond straight 1:1 porting:
  `INDIRECT` packets print their descriptor and are followed one level deep automatically (works
  live, fails cleanly offline with "IB not available in this dump"); `TRAP` keeps a decoded
  `TRAP_INT_CONTEXT` field (UMR's own AI decoder does neither). A handful of legacy tiling-mode
  sub-opcodes (`COPY`'s `*_BC` variants, `WRITE`'s `TILED_BC`) are intentionally not
  field-decoded -- still sized correctly, just shown generically (see TODO).
- **HSA packet decode** -- Kernel Dispatch, Barrier And/Or, Agent Dispatch, and Invalid (AQL
  type 1) packets. An `Invalid` packet's fields are still decoded by peeking the next dword and
  guessing Kernel Dispatch or Barrier, but the packet's **title always shows `INVALID`**, never
  the guessed type -- showing the guessed type made an idle/already-processed ring (which can be
  most of a ring once it's wrapped around at least once) look like it was full of live
  `BARRIER_AND`/`KERNEL_DISPATCH` traffic, which is actively misleading for hang debugging.
- **Two-column packet display** (shared by both SDMA and HSA, `queue_decode.py`'s
  `_emit_field_groups`) -- raw hex on the left (grouped by dword; a `┌`/`┘` bracket connects the
  two dwords of a 64-bit LO/HI field), decoded `NAME = value` text on the right, separated by
  `|`; packet type shown ALL CAPS at the top-right; total packet size always shown in the title:
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
  For HSA's fixed 64-byte AQL packets, `words[i]` starts at the packet's very first byte rather
  than right after a separate header dword like SDMA, so `SETUP`/`TYPE` naturally group under
  the `HEADER` row instead of getting their own hex line:
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
  The type label itself is colored -- red for `INVALID`, green for everything else -- so a
  scroll of packet titles is easy to scan by eye. In the REPL (and rocgdb's interactive
  `dump_hsa_queue`/`dump_sdma_queue` commands) this is real ANSI, shown only when actually
  printing to a terminal (piped/redirected output, e.g. through `tee` to a log file, stays
  plain text). In `queue_viewer.py --web`, the server always sends plain text (raw ANSI would
  render as garbage `\x1b[...` in a browser) -- the same coloring is instead applied
  client-side in JS, wrapping the type label in a `<span class="pkt-invalid"|"pkt-normal">`
  before it's inserted into the page. Never colorized in `dump_all_queues_txt`'s saved `.log`
  files -- those are plain text meant to be read later with ordinary tools.
- **`queue_viewer.py` REPL** -- `info`/`packet`/`range`(`r`)/`all`/`raw`/`rp`/`wp`, up/down-arrow
  command history via `readline`, `rp`/`wp` accepted as index arguments (optionally `+N`/`-N`)
  anywhere a plain integer is accepted; directory mode adds `list`/`ls`/`queues` and
  `use <index_or_name>` (accepts an unambiguous filename prefix) to switch between every queue
  in a directory without restarting the tool (see 2.2). `list` highlights queues where
  `rp != wp` (submitted work the GPU hasn't consumed yet -- the ones worth checking first on a
  hung process) with a `<-- PENDING` marker, in bold red when stdout is a real
  terminal; queues where `rp`/`wp` aren't known at all are left unflagged.
- **`queue_viewer.py --web`** -- browser version of the same REPL, one page for a whole
  directory's queues, full parity including `rp`/`wp` expressions, JSON errors (400/404)
  instead of tracebacks, binds to localhost only by default (see 2.2). Same `PENDING`
  highlighting as the REPL's `list`, shown as a red-bordered card + badge in the sidebar.
- **`queue_viewer.py --to-txt`** -- offline `.bin` -> `.log` conversion, no gdb/live process
  involved, matching `dump_all_queues_txt`'s own text format exactly (see 2.5).
- **Manual, one-at-a-time commands** -- `dump_hsa_queue`, `dump_sdma_queue`,
  `dump_hsa_signal`, `modify_hsa_signal`, `dump_queue_memory` (see 2.6), original commands from
  the upstream `rocgdb_info` project this repo grew out of.

### TODO / known limitations

- SDMA MQD RPTR/WPTR offset table: only verified against real hardware for **GFX9**. GFX10/11/12
  rows are transcribed from UMR's source but not independently hardware-verified -- if you hit
  this on GFX10+ and it looks wrong, that's the first thing to suspect.
- SDMA field-level decode only covers the **AI-generation** decoder (`decode_upto_ai`, SDMA IP
  major 4). The VI/NV/OSS7 generation-specific decoders in UMR's source are not ported --
  packets on those generations still size correctly (so the ring walk never desyncs) but fall
  back to a generic `(recognized, N bytes, not decoded in detail)` line instead of a field
  breakdown.
- Legacy tiling-mode SDMA sub-opcodes (`COPY`'s `*_BC` variants, `WRITE`'s `TILED_BC`) are
  intentionally not field-decoded -- pre-GCN/early-GCN modes UMR's own AI decoder also defers
  on. Still sized correctly, just shown generically.
- SDMA rptr/wptr enrichment's queue-matching is by ring base virtual address, not pid (see
  "Container caveat" above) -- a *different* process's queue coincidentally sharing the same
  base address as one of yours would silently attribute its value to your queue instead of
  erroring out. Accepted as a low-probability trade-off for a best-effort diagnostic feature;
  not something that's been observed in practice.
- `queue_viewer.py --web`'s `all` is capped at 2000 packets per view (use `range`/`packet` for
  the rest of a bigger ring) -- a deliberate limit to avoid a browser tab trying to render a
  100k+-packet ring in one page, not a bug, but worth knowing about if a ring's tail seems to be
  missing from the browser view.
