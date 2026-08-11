# rocgdb-dump Test Plan

Consolidated regression checklist for every feature added to this directory
(`rocgdb_helper.py`, `queue_decode.py`, `queue_viewer.py`) plus the related
container tooling in `ops.sh` (rocm6.3/rocm7.2.4 resource dirs). Run this
after any code change or refactor -- each section is self-contained and
lists concrete commands plus expected output.

Real `.bin`/`.log` dump directories referenced below (e.g.
`rocgdb_dump_bin_pid.../`) are examples from past test sessions and may no
longer exist by the time you read this -- if so, regenerate fresh ones with
`dump_all_queues` per "0. Baseline smoke test" below before running the
sections that need real data.

---

## 0. Baseline smoke test (run first, always)

```bash
cd /home/liangzh/umr/gpu-dump-kit/rocgdb-dump
python3 -m py_compile rocgdb_helper.py queue_decode.py queue_viewer.py && echo COMPILE_OK
rocgdb -q -batch -x rocgdb_helper.py -ex "help user-defined" 2>&1
```
**Expect:** `COMPILE_OK`, and all 8 commands listed cleanly (`dump_all_queues`,
`dump_all_queues_txt`, `dump_hsa_queue`, `dump_hsa_queue_search`,
`dump_hsa_signal`, `dump_queue_memory`, `dump_sdma_queue`,
`modify_hsa_signal`) with no traceback. `dump_all_queues` is the fast binary
capture (`.bin` files, decode later offline); `dump_all_queues_txt` is the
slower live-decode-to-text version (`.log` files) -- see README for the
distinction (this naming was swapped in an August 2026 session; older
dumps/scripts referencing `dump_all_queues_bin` predate the swap).

Generate fresh real test data (needs a live ROCm process -- `hip_deadlock`
test binary or any HIP program works):
```bash
sudo rocgdb attach <pid>
(gdb) source rocgdb_helper.py
(gdb) dump_all_queues
```

---

## 1. `dump_all_queues` / `dump_all_queues_txt` -- summary + info capture

**Feature:** every run writes `dump_summary.json`, `info_queues.log`,
`info_dispatches.log` alongside the per-queue files.

- [ ] `dump_summary.json` exists, is valid JSON (`python3 -m json.tool dump_summary.json`),
      and contains: `command`, `pid`, `comm`, `host`, `dump_time`,
      `queues.hsa`/`queues.dma_xgmi`/`queues.total`/`queues.files` (list),
      `backtrace_all_threads`, `info_queues`, `info_dispatches`, `failures` (list).
- [ ] `queues.hsa` + `queues.dma_xgmi` == number of `.bin`/`.log` files actually written.
- [ ] `info_queues.log` contains rocgdb's real `info queues` table output
      (`Id   Target Id ... Type ... Read   Write  Size     Address` header).
- [ ] `info_dispatches.log` contains rocgdb's `info dispatches -full` output
      (or `No dispatches are currently active.` if none).
- [ ] **When SDMA rptr/wptr enrichment succeeds** for a DMA/XGMI queue (see
      section 3), its Read/Write values are patched directly into
      `info_queues.log`'s existing columns, IN PLACE, in the same row and
      table rocgdb itself printed (`_capture_and_patch_info_queues` ->
      `_patch_info_queues_text`) -- NOT a separately appended section.
      Values match `dump_summary.json`/the queue's own `.bin`/`.log`
      metadata exactly (same raw, un-wrapped counter). HSA rows, and any
      DMA/XGMI row enrichment didn't resolve, are byte-for-byte unchanged
      from rocgdb's own output.
- [ ] Column alignment survives real-world value widths: a small value
      (e.g. `21`) lines up under the header the same way an HSA row's
      Read/Write already do; a value as wide as (or wider than) its column
      still renders with **at least one separating space** before the next
      field -- regression test for a real bug caught in testing where a
      7-digit enriched value glued directly onto the Size column with zero
      separator, producing one unparseable merged number
      (`20000008388608` instead of `2000000 8388608`).
- [ ] Robust to wide Target Id rows (two-digit QIDs, where `parse_info_queue`'s
      own docstring notes the column gap can collapse to a single space) --
      patching still lands in the right place because each row's patch
      position is derived from where that row's own Target Id actually
      ended, not a fixed absolute column.
- [ ] When enrichment finds nothing at all (no root, non-KFD host,
      unrecognized generation, no matching queue), `info_queues.log` is
      byte-for-byte just rocgdb's own `info queues` output.
- [ ] Degrade path: run against a process with **no** GPU queues (e.g. a
      plain `sleep 60` attached via rocgdb) -- dump still completes, all
      four files are still written (summary shows zero queues), no crash.

```bash
python3 -m json.tool rocgdb_dump_bin_pid*/dump_summary.json
grep DMA rocgdb_dump_bin_pid*/info_queues.log   # Read/Write columns filled in if enrichment found something
```

```bash
# unit test for _patch_info_queues_text: in-place patch, HSA rows untouched,
# no-op when nothing to enrich, and the width-overflow separator-space fix
python3 -c "
import sys, types
fake_gdb = types.ModuleType('gdb')
class C:
    def __init__(self, *a, **kw): pass
fake_gdb.Command = C; fake_gdb.COMMAND_USER = 0
fake_gdb.MemoryError = type('MemoryError', (Exception,), {})
fake_gdb.error = type('error', (Exception,), {})
fake_gdb.execute = lambda *a, **kw: ''
fake_gdb.string_to_argv = lambda s: s.split()
sys.modules['gdb'] = fake_gdb
sys.path.insert(0, '.')
import rocgdb_helper as rh

text = '''  Id   Target Id                Type         Read   Write  Size     Address
  1    AMDGPU Queue 8:1 (QID 5) DMA                        8388608  0x00007f49f2400000
  3    AMDGPU Queue 8:3 (QID 3) HSA          2      2      1048576  0x00007f49f8c00000
'''
rows = rh.parse_info_queue(text)
for r in rows:
    if r['type'] == 'DMA':
        r['read'], r['write'] = 21, 48
patched = rh._patch_info_queues_text(text, rows)
reparsed = rh.parse_info_queue(patched)
by_tid = {r['target_id']: r for r in reparsed}
assert by_tid['AMDGPU Queue 8:1 (QID 5)']['read'] == 21
assert by_tid['AMDGPU Queue 8:1 (QID 5)']['write'] == 48
for orig, new in zip(text.splitlines(), patched.splitlines()):
    if 'HSA' in orig:
        assert orig == new, 'HSA row must be untouched'
# no-op case
assert rh._patch_info_queues_text(text, []) == text

# width-overflow case: a 7-digit write value must not glue onto Size
rows2 = rh.parse_info_queue(text)
for r in rows2:
    if r['type'] == 'DMA':
        r['read'], r['write'] = 21, 2000000
patched2 = rh._patch_info_queues_text(text, rows2)
line = [l for l in patched2.splitlines() if 'QID 5)' in l][0]
assert '2000000' in line.split() and '8388608' in line.split(), line
print('OK')
"
```

---

## 2. Filenames -- `TYPE_QIDn_GPU_A_Queue_B.{bin,log}`

**Feature:** `hsa_QID1_GPU_1_Queue_5.bin` / `dma_QID6_GPU_5_Queue_27.bin` /
`xgmi_QID10_GPU_6_Queue_23.log` -- no literal `queue` word, target fragment
reformatted from `AMDGPU Queue A:B (QID N)` to `GPU_A_Queue_B`.

- [ ] Every generated filename matches `^(hsa|dma|xgmi)_QID\d+_GPU_\d+_Queue_\d+\.(bin|log)$`.
- [ ] `_sanitize_target_id("AMDGPU Queue 5:27 (QID 6)")` returns exactly `"GPU_5_Queue_27"`.
- [ ] Fallback for an unexpected target_id shape still sanitizes generically
      (doesn't crash) -- e.g. `_sanitize_target_id("something weird")` returns `"something_weird"`.

```bash
ls rocgdb_dump_bin_pid*/ | grep -vE '^(hsa|dma|xgmi)_QID[0-9]+_GPU_[0-9]+_Queue_[0-9]+\.(bin|log)$|^(dump_summary\.json|info_queues\.log|info_dispatches\.log|backtrace_all_threads\.log)$'
# expect: no output (nothing left unmatched)
```

---

## 3. SDMA rptr/wptr enrichment (KFD debugfs, no `umr` dependency)

**Feature:** `_enrich_sdma_pointers()` fills in `read`/`write` for DMA/XGMI
rows by parsing `/sys/kernel/debug/kfd/mqds` directly (root required, no
`umr` binary).

- [ ] With root + a real GPU process: DMA/XGMI rows in `info` /
      `dump_summary.json` show real (non-`null`) `read`/`write` values.
- [ ] Without root: prints exactly one line
      (`SDMA rptr/wptr enrichment skipped: no permission to read ...`),
      dump still completes normally, DMA/XGMI rows keep `read`/`write: null`.
- [ ] **Container case:** `/sys/kernel/debug` bind-mounted + root inside
      container -> enrichment must still find the right queues even though
      `gdb.selected_inferior().pid` is a **container-local** pid (e.g. `1`)
      that has no relationship to the real host pid in `mqds` -- this is
      why matching is done system-wide by ring base address, not by pid.
      Verify: dump inside a container, confirm DMA/XGMI rows get non-null
      read/write despite pid mismatch.
- [ ] **Multi-tenant host case:** on a shared host, other users' unrelated
      SDMA queues must be silently skipped -- **no** `SDMA rptr/wptr
      enrichment: failed for device ...` spam for queues that were never
      going to be yours. A `failed for device` message should only appear
      for a queue that actually matched one of *your* rows' addresses.
- [ ] Unsupported/undetectable GPU generation -> one line
      (`could not determine GPU generation...` or `unsupported gfx_maj=N...`)
      per distinct device, not per queue; dump still completes.

```bash
# unit test: MQD parsing + gfx9 offset math against real captured mqds text
# (see conversation history for the exact canned MQDS_SAMPLE text used --
# reconstruct via: sudo cat /sys/kernel/debug/kfd/mqds | grep -A20 "Process <pid>")
python3 -c "
import sys, types
fake_gdb = types.ModuleType('gdb')
class C:
    def __init__(self, *a, **kw): pass
fake_gdb.Command = C; fake_gdb.COMMAND_USER = 0
fake_gdb.MemoryError = type('MemoryError', (Exception,), {})
fake_gdb.error = type('error', (Exception,), {})
fake_gdb.execute = lambda *a, **kw: ''
fake_gdb.string_to_argv = lambda s: s.split()
sys.modules['gdb'] = fake_gdb
sys.path.insert(0, '.')
import rocgdb_helper as rh
print(rh._resolve_gfx_maj('eedc'))  # expect an int (9/10/11/12) or None
"
```

---

## 4. SDMA packet sizing + field decode (ported from UMR)

**Feature:** `_sdma_nwords()` (full opcode/sub-opcode sizing table, port of
`sized_oss1_5`) + `_sdma_decode_fields()` (field decode, port of
`decode_upto_ai`).

- [ ] **Sizing correctness (the actual bug this fixed):** hand-craft a ring
      with `COPY.TILED` (should NOT be sized as 28 bytes/LINEAR), `COPY.LINEAR_SUB_WINDOW`,
      `POLL_REGMEM.REG` (sub_op=1, NOT 24 bytes/MEM), `POLL_REGMEM.MEM_VERIFY`,
      `FENCE.CONDITIONAL_INTERRUPT` (sub_op=1) -- confirm every packet's
      `#N at 0x...` address lands exactly where hand-computed (no desync).
- [ ] Replay against a **real** captured DMA/XGMI `.bin` (full ring, tens
      of thousands of packets) -- **zero** `Cannot read memory`,
      `unrecognized opcode`, or `Failed to decode` lines across the whole ring.
- [ ] Legacy `_BC` sub-opcodes (`COPY` 16/17/20/21/22/36, `WRITE` 17) still
      size correctly (ring doesn't desync) but show
      `(recognized, not decoded in detail)` instead of a field breakdown.
- [ ] `INDIRECT` packet: descriptor fields (`VMID`/`PRIV`/`IB_ADDR`/`IB_SIZE`)
      always shown. Live path (`GdbReader`) follows into the IB one level;
      offline path (`BufferReader`, `.bin` dump) shows
      `IB at 0x... not available in this dump` instead of crashing.
- [ ] `TRAP` still shows a decoded `TRAP_INT_CONTEXT` field (deliberate
      addition beyond UMR's own AI-generation decoder, which shows nothing
      for TRAP).

```bash
python3 -m python3 queue_viewer.py <real_dma_or_xgmi>.bin <<'EOF'
all
quit
EOF
# then check for zero hits:
python3 - <<'PY'
import subprocess
out = subprocess.run(["python3", "queue_viewer.py", "<real_dma_or_xgmi>.bin"],
                      input="all\nquit\n", capture_output=True, text=True).stdout
bad = [l for l in out.splitlines() if any(k in l.lower() for k in
       ("cannot read", "unrecognized", "failed to decode", "traceback"))]
print("BAD LINES:", bad)  # expect: []
PY
```

---

## 5. HSA packet decode (same two-column format as SDMA)

**Feature:** `_hsa_decode_fields()` covers Kernel Dispatch, Barrier And/Or,
Agent Dispatch, Invalid/Unknown -- same rendering core as SDMA
(`_emit_field_groups`).

- [ ] **Kernel Dispatch**: `WORKGROUP_X/Y/Z` and `GRID_X/Y/Z` shown as
      separate rows (not a combined `[x,y,z]` array); `SETUP` groups under
      the same dword as `HEADER`; `KERNEL_OBJECT` shows resolved symbol
      name in quotes when `symbol_lookup` finds one; `(reserved)` shown for
      the unused 8 bytes between `KERNARG_ADDRESS` and `COMPLETION_SIGNAL`.
- [ ] **Barrier And/Or**: `DEP_SIGNAL_0`..`_4`, with `(reserved)` for the
      unused dword between `HEADER` and `DEP_SIGNAL_0`.
- [ ] **Agent Dispatch**: `TYPE` field, `(bytes 8-47 not decoded in detail)` note.
- [ ] **Invalid packet (AQL type 1) title MUST say `INVALID`**, never the
      guessed reinterpreted type (`KERNEL_DISPATCH`/`BARRIER_AND`) -- this
      was a real bug found via real data where an entire 16384-packet ring
      of already-consumed/idle slots was mislabeled as live barrier traffic.
      The reinterpreted fields are still shown below the title (silently --
      no `(invalid packet, reinterpreted as type N)` note line; that note
      was removed per user request since the `INVALID` title already says
      everything that matters).
- [ ] Replay against a real captured HSA `.bin` (16k+ packets) -- zero
      decode errors across the whole ring.
- [ ] `rp`/`wp` jump navigation still works for HSA after all the
      renderer refactoring (`queue_viewer.py`'s `_print_hsa_range`/`packet_count`
      use direct O(1) addressing, unaffected by SDMA's block-splitting changes).

```bash
python3 queue_viewer.py <real_hsa>.bin
(queue_viewer) > all
# grep the captured output for title lines and confirm label distribution
# makes sense (e.g. INVALID for idle/already-consumed slots, not
# KERNEL_DISPATCH/BARRIER_AND)
```

---

## 6. Two-column packet display format (shared HSA + SDMA)

**Feature:** hex on the left (dword-grouped, `┌`/`┘` bracket for 64-bit
LO/HI fields), left-aligned `NAME = value` on the right, `|` separator at a
**fixed column (35)**, packet type ALL CAPS at top-right, total packet size
in the title (`(N bytes)`).

- [ ] Every line containing `|` has the pipe at **exactly column 35**
      (`line.index("|") == 35`) for both SDMA and HSA packets.
- [ ] Fields sharing one dword: hex shown once (first field's row), blank
      left column for the rest.
- [ ] A field spanning two dwords (LO/HI 64-bit value): `┌` on the first
      row (no `|`, no field text), `┘` + field text on the second.
- [ ] Bracket-open rows (`┌`) have **no trailing whitespace padding** and
      **no** `|`.
- [ ] Type label is ALL CAPS (`COPY (LINEAR)`, `KERNEL_DISPATCH`, `INVALID`, etc.).
- [ ] Title line includes `(N bytes)` -- variable per-packet for SDMA,
      always `(64 bytes)` for HSA.
- [ ] `queue_viewer.py`'s block-splitting (`_ensure_sdma_walked`) finds
      the exact right number of packets -- no spurious empty blocks from
      the double-separator-per-packet format (title regex `^Packet #\d+ at 0x...`
      is what splits blocks now, not bare separator lines).
- [ ] **Packet title coloring (`use_color`, opt-in):** with `use_color=True`,
      the type label is wrapped in bold red ANSI (`\033[1;31m...\033[0m`)
      for `INVALID`, bold green (`\033[1;32m`) for everything else (SDMA
      packets, having no "invalid" concept, are always green). Column
      alignment (the pipe still at column 35 on field rows; the type label
      still right-aligned in the title) is unaffected -- padding is
      computed from the *uncolored* label length before the ANSI bytes are
      added. Default (`use_color` omitted/`False`) produces byte-identical
      output to before this feature existed.
- [ ] **Coloring must stay opt-in per call site, never leak where it
      shouldn't:**
      - `queue_viewer.py`'s REPL (`run_repl`/`run_repl_dir`) constructs
        `QueueDump(..., use_color=_USE_COLOR)` where
        `_USE_COLOR = sys.stdout.isatty()` -- colored only when actually
        connected to a real terminal, plain when piped/redirected (e.g.
        through `tee` to a log file).
      - `queue_viewer.py --web`'s `_QueueWebState.get()` constructs
        `QueueDump(...)` with **no** `use_color` kwarg (always `False`) --
        this must hold even when the server process's own stdout is a
        real terminal, since the decoded lines are sent to the browser as
        JSON, not printed locally.
      - `rocgdb_helper.py`'s interactive `dump_hsa_queue`/`dump_sdma_queue`
        commands pass `use_color=sys.stdout.isatty()`.
      - `rocgdb_helper.py`'s `dump_all_queues_txt` batch path (writing
        per-queue `.log` files) passes no `use_color` kwarg -- must never
        colorize, since these are saved files read later, not a live
        terminal session.

```bash
python3 -c "
import struct, sys
sys.path.insert(0, '.')
import queue_decode as qd
class R:
    def __init__(self, base, buf): self.base, self.buf = base, buf
    def read(self, addr, size): return self.buf[addr-self.base:addr-self.base+size]
base = 0x1000
words = [1024, 0, 0x34560000, 0x00007f12, 0x34570000, 0x00007f12]
buf = struct.pack('<I', 1) + b''.join(struct.pack('<I', w) for w in words)
lines = []
qd.decode_sdma_packets(R(base, buf), base, len(buf), emit=lines.append)
for l in lines:
    if '|' in l:
        assert l.index('|') == 35, l
print('OK: all pipes aligned at column 35')

# use_color=False (default) must be byte-identical to the lines above
lines_default = []
qd.decode_sdma_packets(R(base, buf), base, len(buf), emit=lines_default.append)
assert lines == lines_default

# use_color=True: SDMA packets are always green (no INVALID concept)
lines_colored = []
qd.decode_sdma_packets(R(base, buf), base, len(buf), emit=lines_colored.append, use_color=True)
assert any('\033[1;32m' in l for l in lines_colored), 'expected green ANSI in a colored SDMA title'
assert not any('\033[1;31m' in l for l in lines_colored), 'SDMA should never render red'
print('OK: use_color threading correct')
"
```

```bash
# real end-to-end: piped (non-tty) must be plain; a pty (real terminal) must show ANSI
cd /home/liangzh/umr/gpu-dump-kit/rocgdb-dump
DUMP_DIR=$(ls -d rocgdb_dump_bin_pid*/ | head -1)
NAME=$(ls "$DUMP_DIR"/hsa_*.bin | head -1)
printf 'rp\nquit\n' | python3 queue_viewer.py "$NAME" | cat -A | grep -q '\^\[' \
  && echo "FAIL: ANSI leaked when piped" || echo "OK: no ANSI when piped"

python3 -c "
import pty, os, subprocess, time
master, slave = pty.openpty()
p = subprocess.Popen(['python3', 'queue_viewer.py', '$NAME'], stdin=slave, stdout=slave, stderr=slave)
os.close(slave)
time.sleep(0.5); os.write(master, b'rp\n'); time.sleep(0.5); os.write(master, b'quit\n'); time.sleep(0.5)
out = b''
try:
    while True:
        chunk = os.read(master, 4096)
        if not chunk: break
        out += chunk
except OSError:
    pass
p.wait()
text = out.decode(errors='replace')
assert '\x1b[1;31m' in text or '\x1b[1;32m' in text, 'expected some ANSI color in a real-pty run'
print('OK: ANSI present via pty (real terminal)')
"
```

---

## 7. `queue_viewer.py` REPL: history + rp/wp expressions

**Feature:** up/down-arrow command history (via `readline`); `packet`/
`range`/`raw` accept `rp`/`wp` (optionally `+N`/`-N`) as index arguments;
`range` also has a one-letter alias `r` (matching `packet`'s `p`). The
commands were originally named `rptr`/`wptr` and were shortened to `rp`/`wp`
for convenience -- `rptr`/`wptr` are no longer recognized as commands (the
underlying hardware concept/feature name "SDMA rptr/wptr enrichment" in
`rocgdb_helper.py`/README is unrelated and unaffected by this rename).

- [ ] `import readline` succeeds without error on this host (or degrades
      silently via `except ImportError: pass` where unavailable).
- [ ] `packet wp` == `packet <resolved wp index>` (same output).
- [ ] `range rp rp+1` decodes exactly 2 packets (rp's index and the
      next one); `r rp rp+1` produces identical output.
- [ ] `raw wp-1` hex-dumps the packet immediately before the write pointer.
- [ ] Invalid token (e.g. `packet notaptr`) still produces a clean
      `error: invalid literal for int() with base 0: 'notaptr'` -- no traceback.
- [ ] **`rp`/`wp` alone (bare commands) must not crash when the pointer
      actually resolves to a real index** -- regression test for a real bug:
      `_resolve_pointer`'s HSA success path once returned a dict with no
      `"reason"` key, and `jump_to_pointer`'s `info["reason"]` lookup raised
      `KeyError: 'reason'` on *every successful* `wp`/`rp` (only the
      already-handled "missing"/"empty" paths worked) -- fixed by adding
      `"reason": None` to that return and switching all reads to
      `info.get("reason")`. Confirm bare `wp`/`rp` print the full
      diagnostic message and the packet, with no traceback: `raw=N -> dword
      slot M -> byte offset 0x... -> packet index K (of TOTAL)` for SDMA,
      `raw=N -> slot index K (of TOTAL)` for HSA.
- [ ] These expressions work for **both** HSA and DMA/XGMI dumps.
- [ ] The old `rptr`/`wptr` spellings are gone: typing `wptr` produces
      `unknown command: 'wptr' (try 'help')`, not a silent alias.

```bash
printf 'wp\nrp\npacket wp\nrange rp rp+1\nr rp rp+1\nraw wp-1\npacket notaptr\nwptr\nquit\n' \
  | python3 queue_viewer.py <any>.bin
# expect: no "KeyError" / "Traceback" anywhere in the output, and
# "unknown command: 'wptr'" for the old spelling
```

---

## 8. SDMA read/write storage convention (matches HSA)

**Feature:** DMA/XGMI `read`/`write` are stored as a **raw, un-wrapped**
dword counter (same convention as HSA's raw AQL packet-ID counter) --
wrapping to a ring-relative position happens at use time in
`queue_viewer.py`, not at storage time in `rocgdb_helper.py`.

- [ ] `dump_summary.json`/`.bin` metadata `read`/`write` for a DMA/XGMI
      queue can legitimately be **larger** than `size/4` (ring capacity in
      dwords) if the ring has wrapped -- this is correct, not a bug.
- [ ] `queue_viewer.py`'s `wp`/`rp` message shows the full conversion
      chain: `raw=N -> dword slot M -> byte offset 0x... -> packet index K
      (of TOTAL)` -- `dword slot M` must equal `N % (ring_size_bytes // 4)`.
- [ ] A synthetic raw value several multiples of the ring capacity still
      resolves to the correct wrapped packet index (regression test for
      the wrap-at-use-time fix).

```bash
# synthetic multi-wrap test (see section 4's harness for building a fake
# ring); set metadata['write'] = ring_size_dwords * 3 + 2 and confirm
# jump_to_pointer resolves to dword slot 2, not a huge/wrong number.
```

---

## 9. `queue_viewer.py` REPL directory browsing (`list`/`use`)

**Feature:** pointing the REPL (non-`--web`) at a directory instead of a
single `.bin` file gains `list`/`ls`/`queues` (show every dump found) and
`use <index_or_name>` (switch which queue the rest of the commands apply
to), mirroring `--web`'s sidebar without needing a browser
(`run_repl_dir`/`_dispatch_command`/`_peek_dump_metadata`).

- [ ] `python3 queue_viewer.py <dir>` (no `--web`) prints
      `N queue dump(s) found under <dir>` followed by one `[i] name  type=...
      size=... rp=... wp=...` line per `.bin` file, then a REPL prompt --
      no traceback, no full-ring decode yet (see next bullet). `qid`/
      `target_id` are deliberately NOT shown -- they're already encoded in
      the filename itself (`dma_QID11_GPU_7_Queue_22.bin`); `rp`/`wp` are
      shown instead since those aren't derivable from the filename.
- [ ] `list` is fast even for many/large queues: it only reads each dump's
      header (`_peek_dump_metadata`), never the ring bytes -- a queue's
      packets are only decoded once it's actually selected via `use`.
- [ ] `use N` (0-based index into the listing) and `use <exact filename>`
      both select that queue; `use <name>` also accepts an **unambiguous
      filename prefix** (e.g. `use dma_QID4` when the full name is
      `dma_QID4_GPU_8_Queue_2.bin`) -- if the prefix matches more than one
      file, a clear "matches N queues, be more specific: ..." error lists
      the candidates instead of guessing.
- [ ] After `use`, the prompt changes to `(queue_viewer:<name>) >`, and the
      selected queue's packet count is printed (same startup message
      `run_repl` prints for a single-file invocation).
- [ ] `use nope` (no match) prints `no such queue: 'nope' (try 'list')` --
      no traceback, selection (if any) unchanged.
- [ ] Once a queue is selected, every single-file command works exactly as
      it does for `python3 queue_viewer.py <that_file>.bin` directly --
      `info`/`packet`/`range`/`all`/`raw`/`rp`/`wp`, including `rp`/
      `wp` expressions (`packet wp-1`, etc.) -- same output either way.
- [ ] Running any of those commands **before** the first `use` prints
      `no queue selected -- try 'list' then 'use <index_or_name>'` instead
      of crashing.
- [ ] `list` while a queue is selected marks it with `*` in the listing.
- [ ] **`PENDING` highlight (`_is_pending`/`_highlight`):** a queue whose
      `rp`/`wp` are both known and differ (submitted work the GPU hasn't
      consumed yet) gets a `<-- PENDING` suffix on its `list`
      line -- e.g. `rp=21 wp=48 <-- PENDING`; a queue where
      `rp == wp`, or where either is `None` (enrichment found nothing),
      gets no suffix at all -- "unknown" is not flagged as "pending".
      When stdout is a real terminal (`sys.stdout.isatty()`), the whole
      pending line is wrapped in bold red ANSI (`\033[1;31m...\033[0m`);
      when piped/redirected (e.g. through `tee` to a log file), only the
      plain-text marker appears -- no raw escape codes leak into the log.
- [ ] `help` shows both the directory commands (`list`/`use`) and the full
      single-file `HELP_TEXT`.
- [ ] Switching `use` between an HSA and a DMA/XGMI queue in the same
      session works correctly for both (packet counts, rp/wp, decode
      format all correct for whichever is currently selected).
- [ ] Empty directory (no `.bin` files): prints `No .bin files found under
      <dir>` to stderr and exits 1 -- no traceback.
- [ ] A single `.bin` **file** passed directly (not a directory) is
      completely unaffected by this feature -- still goes straight into
      `run_repl` with its original `Loaded ... / N packet(s) .../ Type
      'help'...` startup messages, no `list`/`use` step.

```bash
cd /home/liangzh/umr/gpu-dump-kit/rocgdb-dump
DUMP_DIR=$(ls -d rocgdb_dump_bin_pid*/ | head -1)
printf 'list\nuse 0\ninfo\nuse hsa_QID\nuse nope\nlist\nquit\n' | python3 queue_viewer.py "$DUMP_DIR"
# expect: listing, then an ambiguous-prefix error for 'hsa_QID' (multiple
# HSA queues), then 'no such queue' for 'nope', then a listing with '*' on
# whichever queue 'use 0' selected -- no traceback anywhere

mkdir -p /tmp/qv_empty_test && python3 queue_viewer.py /tmp/qv_empty_test; echo "exit=$?"
# expect: "No .bin files found under /tmp/qv_empty_test", exit=1
rmdir /tmp/qv_empty_test
```

```bash
# PENDING highlight: piped (non-tty) -- plain marker only, no ANSI escapes
printf 'list\nquit\n' | python3 queue_viewer.py "$DUMP_DIR" | cat -A | grep -q '\^\[' \
  && echo "FAIL: raw ANSI escapes leaked into piped output" \
  || echo "OK: no ANSI escapes when piped"
printf 'list\nquit\n' | python3 queue_viewer.py "$DUMP_DIR" | grep -- "<-- PENDING"
# expect: at least the rows with rp != wp, each ending in the plain marker

# unit test: _is_pending / _highlight logic directly
python3 -c "
import sys; sys.path.insert(0, '.')
import queue_viewer as qv
assert qv._is_pending({'read': 1, 'write': 2}) is True
assert qv._is_pending({'read': 2, 'write': 2}) is False
assert qv._is_pending({'read': None, 'write': 2}) is False
assert qv._is_pending({'read': 1, 'write': None}) is False
qv._USE_COLOR = True
assert qv._highlight('x').startswith('\033[1;31m') and qv._highlight('x').endswith('\033[0m')
qv._USE_COLOR = False
assert qv._highlight('x') == 'x'
print('OK')
"
```

---

## 10. `queue_viewer.py --web` browser UI (parity with the REPL)

**Feature:** the browser UI is a thin HTTP wrapper around the exact same
`QueueDump` methods the REPL calls -- every endpoint below must produce
output identical (line-for-line) to the equivalent REPL command, not just
"similar".

- [ ] `GET /` serves the HTML page (200, `Content-Type: text/html`); the
      page's `#src` element shows the absolute path the server was pointed
      at (`os.path.abspath(path)`), and contains no leftover
      `__ROOT_PATH_JSON__` template marker.
- [ ] `GET /api/list` returns one entry per `.bin` file with
      `name`/`qid`/`type`/`size`/`target_id`/`count`/`read`/`write`/`pending`
      (`count` is `packet_count()` -- full ring walk for SDMA/XGMI, `size/64`
      for HSA; `pending` is `_is_pending()` on that file's metadata -- `true`
      only when `read`/`write` are both non-null and differ); a file that
      fails to load (bad header, truncated) gets `{"name":..., "error":
      "..."}` instead of killing the whole listing.
- [ ] Sidebar cards for `pending: true` entries get the `.qitem.pending` CSS
      class (red left border) and a `PENDING` badge -- same signal as the
      REPL's `list` highlight, just visual instead of ANSI/text.
- [ ] `GET /api/help` returns `{"lines": [...]}` matching `HELP_TEXT.splitlines()`
      -- single source of truth with the REPL's `help` command, not a
      hand-duplicated copy in the JS.
- [ ] `GET /api/queue/<name>/info` output == REPL `info` output for the same file.
- [ ] `GET /api/queue/<name>/packet/<n>`, `.../range/<a>/<b>`, `.../raw/<n>`,
      `.../rp`, `.../wp` all match the REPL's `packet`/`range`/`raw`/
      `rp`/`wp` output for the same arguments. The old `.../rptr`/`.../wptr`
      paths are gone -- 404, not an alias.
- [ ] **Parity gap this section exists to close:** `packet`/`range`/`raw`
      accept `rp`/`wp` (optionally `+N`/`-N`) exactly like the REPL --
      e.g. `.../packet/wp-1`, `.../range/rp/rp%2B2` (the `%2B` is
      what `encodeURIComponent('rp+2')` produces; the server
      percent-decodes every path segment, not just the queue name, then
      parses it the same way `_parse_index` does for the REPL).
- [ ] `GET /api/queue/<name>/packet/notaptr` -> HTTP 400, JSON `{"error": "..."}`
      (not 500, not an unhandled traceback in the server log).
- [ ] `GET /api/queue/does_not_exist.bin/info` -> HTTP 404, JSON `{"error": "..."}`.
- [ ] `.../all` is capped at `_WEB_ALL_CAP` (2000) packets with a trailing
      `... capped at 2000 of N packets ...` line when the ring is bigger;
      uncapped for rings <= 2000 packets.
- [ ] Server binds to `127.0.0.1` by default (not reachable from other
      hosts) unless `--host` is passed explicitly.
- [ ] Sidebar shows a type badge (HSA/DMA/XGMI, distinct colors) and the
      packet count per queue, and clicking an item auto-runs `info` for it.

```bash
cd /home/liangzh/umr/gpu-dump-kit/rocgdb-dump
python3 queue_viewer.py <dump_dir_or_file> --web --port 8799 &
SERVER_PID=$!
sleep 1
NAME=$(python3 -c "import json,urllib.request; print(json.load(urllib.request.urlopen('http://127.0.0.1:8799/api/list'))[0]['name'])")
curl -s http://127.0.0.1:8799/ | grep -q '__ROOT_PATH_JSON__' && echo "FAIL: marker not substituted" || echo "OK: marker substituted"
curl -s "http://127.0.0.1:8799/api/queue/$NAME/packet/wp-1" | python3 -m json.tool | head -5
curl -s -o /dev/null -w "%{http_code}\n" "http://127.0.0.1:8799/api/queue/$NAME/packet/notaptr"   # expect 400
curl -s -o /dev/null -w "%{http_code}\n" "http://127.0.0.1:8799/api/queue/nope.bin/info"           # expect 404
curl -s -o /dev/null -w "%{http_code}\n" "http://127.0.0.1:8799/api/queue/$NAME/wptr"              # expect 404 (old spelling gone)
kill $SERVER_PID
```

---

## 11. `dump_all_queues.sh` one-shot wrapper script

**Feature:** automates `attach <pid>` / `source rocgdb_helper.py` /
`dump_all_queues` / `detach` / `quit` into a single command, pid supplied
as a CLI argument (not hand-edited into a file like `save_info.gdb`).

- [ ] `bash -n dump_all_queues.sh` passes (syntax only, no execution).
- [ ] No args, or more than 2 positional args -> prints usage to stderr,
      exits 1, no rocgdb invocation attempted.
- [ ] Non-numeric pid (e.g. `abc`) -> `error: 'abc' is not a valid pid
      (expected a number)`, exits 1, no rocgdb invocation attempted.
- [ ] Nonexistent pid (e.g. a huge number unlikely to be a real pid) ->
      `error: no process with pid N (checked /proc/N)`, exits 1, no rocgdb
      invocation attempted.
- [ ] `--help`/`-h` prints usage and exits 1 without attempting anything.
- [ ] Unknown option (e.g. `--bogus`) -> `error: unknown option '--bogus'`
      + usage, exits 1.
- [ ] Real end-to-end run against a live process: attaches, sources
      `rocgdb_helper.py` (absolute path, resolved from the script's own
      directory -- works regardless of the caller's cwd), runs
      `dump_all_queues`, detaches cleanly (`[Inferior N (process PID)
      detached]` in the output, not a hang or a crash), and exits 0.
- [ ] The named `[output_dir]` (when given) is what `dump_all_queues`
      actually writes to -- same four files (`dump_summary.json`,
      `info_queues.log`, `info_dispatches.log`, `backtrace_all_threads.log`)
      as running the command manually inside `rocgdb`.
- [ ] Full rocgdb session transcript is saved to
      `dump_all_queues_pid<pid>_<timestamp>.log` in the current directory
      (not the output dir), and matches what was printed to the terminal.
- [ ] `--txt <pid>` runs `dump_all_queues_txt` instead (visible in the
      transcript as `dump_all_queues_txt complete: ...`, and in
      `dump_summary.json`'s `"command"` field).
- [ ] Missing `rocgdb_helper.py` (e.g. run from a copy of just this one
      script) -> clear `error: rocgdb_helper.py not found at ...`, exits 1,
      no rocgdb invocation attempted.
- [ ] Already running as root -> does NOT prefix `sudo` (`sudo` would
      otherwise prompt for a password unnecessarily / fail in a
      passwordless-sudo-less root shell).

```bash
cd /home/liangzh/umr/gpu-dump-kit/rocgdb-dump
bash -n dump_all_queues.sh && echo SYNTAX_OK
./dump_all_queues.sh; echo "exit=$?"                 # expect usage + exit 1
./dump_all_queues.sh abc; echo "exit=$?"              # expect invalid-pid error
./dump_all_queues.sh 999999999; echo "exit=$?"        # expect no-such-process error

# real end-to-end run (needs root/sudo, and something to attach to --
# a plain long-running process works fine to exercise attach/dump/detach,
# though it won't have GPU queues to actually decode):
tail -f /dev/null &
TEST_PID=$!
./dump_all_queues.sh "$TEST_PID" /tmp/dump_script_test
kill "$TEST_PID"
python3 -m json.tool /tmp/dump_script_test/dump_summary.json
sudo rm -rf /tmp/dump_script_test dump_all_queues_pid*.log   # cleanup (root-owned)
```

---

## Quick full-regression one-liner

Run after any change to `rocgdb_helper.py`/`queue_decode.py`/`queue_viewer.py`:

```bash
cd /home/liangzh/umr/gpu-dump-kit/rocgdb-dump
python3 -m py_compile rocgdb_helper.py queue_decode.py queue_viewer.py && echo COMPILE_OK
rocgdb -q -batch -x rocgdb_helper.py -ex "help user-defined" 2>&1 | tail -5
# then re-run sections 4, 5, 6 against whatever real .bin dumps are
# currently on disk (find . -maxdepth 2 -iname "*.bin"), since those give
# the strongest signal (tens of thousands of real packets, zero tolerance
# for decode errors).
```
