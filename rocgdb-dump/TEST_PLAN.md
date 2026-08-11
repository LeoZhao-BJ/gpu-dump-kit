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
- [ ] Degrade path: run against a process with **no** GPU queues (e.g. a
      plain `sleep 60` attached via rocgdb) -- dump still completes, all
      four files are still written (summary shows zero queues), no crash.

```bash
python3 -m json.tool rocgdb_dump_bin_pid*/dump_summary.json
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
- [ ] `rptr`/`wptr` jump navigation still works for HSA after all the
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
"
```

---

## 7. `queue_viewer.py` REPL: history + wptr/rptr expressions

**Feature:** up/down-arrow command history (via `readline`); `packet`/
`range`/`raw` accept `rptr`/`wptr` (optionally `+N`/`-N`) as index arguments;
`range` also has a one-letter alias `r` (matching `packet`'s `p`).

- [ ] `import readline` succeeds without error on this host (or degrades
      silently via `except ImportError: pass` where unavailable).
- [ ] `packet wptr` == `packet <resolved wptr index>` (same output).
- [ ] `range rptr rptr+1` decodes exactly 2 packets (rptr's index and the
      next one); `r rptr rptr+1` produces identical output.
- [ ] `raw wptr-1` hex-dumps the packet immediately before the write pointer.
- [ ] Invalid token (e.g. `packet notaptr`) still produces a clean
      `error: invalid literal for int() with base 0: 'notaptr'` -- no traceback.
- [ ] **`rptr`/`wptr` alone (bare commands) must not crash when the pointer
      actually resolves to a real index** -- regression test for a real bug:
      `_resolve_pointer`'s HSA success path once returned a dict with no
      `"reason"` key, and `jump_to_pointer`'s `info["reason"]` lookup raised
      `KeyError: 'reason'` on *every successful* `wptr`/`rptr` (only the
      already-handled "missing"/"empty" paths worked) -- fixed by adding
      `"reason": None` to that return and switching all reads to
      `info.get("reason")`. Confirm bare `wptr`/`rptr` print the full
      diagnostic message and the packet, with no traceback: `raw=N -> dword
      slot M -> byte offset 0x... -> packet index K (of TOTAL)` for SDMA,
      `raw=N -> slot index K (of TOTAL)` for HSA.
- [ ] These expressions work for **both** HSA and DMA/XGMI dumps.

```bash
printf 'wptr\nrptr\npacket wptr\nrange rptr rptr+1\nr rptr rptr+1\nraw wptr-1\npacket notaptr\nquit\n' \
  | python3 queue_viewer.py <any>.bin
# expect: no "KeyError" / "Traceback" anywhere in the output
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
- [ ] `queue_viewer.py`'s `wptr`/`rptr` message shows the full conversion
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

## 9. `queue_viewer.py --web` browser UI (parity with the REPL)

**Feature:** the browser UI is a thin HTTP wrapper around the exact same
`QueueDump` methods the REPL calls -- every endpoint below must produce
output identical (line-for-line) to the equivalent REPL command, not just
"similar".

- [ ] `GET /` serves the HTML page (200, `Content-Type: text/html`); the
      page's `#src` element shows the absolute path the server was pointed
      at (`os.path.abspath(path)`), and contains no leftover
      `__ROOT_PATH_JSON__` template marker.
- [ ] `GET /api/list` returns one entry per `.bin` file with
      `name`/`qid`/`type`/`size`/`target_id`/`count` (`count` is
      `packet_count()` -- full ring walk for SDMA/XGMI, `size/64` for HSA);
      a file that fails to load (bad header, truncated) gets `{"name":...,
      "error": "..."}` instead of killing the whole listing.
- [ ] `GET /api/help` returns `{"lines": [...]}` matching `HELP_TEXT.splitlines()`
      -- single source of truth with the REPL's `help` command, not a
      hand-duplicated copy in the JS.
- [ ] `GET /api/queue/<name>/info` output == REPL `info` output for the same file.
- [ ] `GET /api/queue/<name>/packet/<n>`, `.../range/<a>/<b>`, `.../raw/<n>`,
      `.../rptr`, `.../wptr` all match the REPL's `packet`/`range`/`raw`/
      `rptr`/`wptr` output for the same arguments.
- [ ] **Parity gap this section exists to close:** `packet`/`range`/`raw`
      accept `rptr`/`wptr` (optionally `+N`/`-N`) exactly like the REPL --
      e.g. `.../packet/wptr-1`, `.../range/rptr/rptr%2B2` (the `%2B` is
      what `encodeURIComponent('rptr+2')` produces; the server
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
curl -s "http://127.0.0.1:8799/api/queue/$NAME/packet/wptr-1" | python3 -m json.tool | head -5
curl -s -o /dev/null -w "%{http_code}\n" "http://127.0.0.1:8799/api/queue/$NAME/packet/notaptr"   # expect 400
curl -s -o /dev/null -w "%{http_code}\n" "http://127.0.0.1:8799/api/queue/nope.bin/info"           # expect 404
kill $SERVER_PID
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
