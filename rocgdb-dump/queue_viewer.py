#!/usr/bin/env python3
"""Standalone offline viewer for the .bin queue dumps produced by
rocgdb_helper.py's `dump_all_queues` rocgdb command.

No gdb dependency at all -- this reads the raw ring bytes straight out of
the dump file and decodes packets against an in-memory buffer, using the
exact same packet-format logic as the live path (see queue_decode.py).

Usage:
    python3 queue_viewer.py <dump.bin>              # interactive REPL, one file
    python3 queue_viewer.py <dir_of_dumps>           # REPL with 'list'/'use' to switch queues
    python3 queue_viewer.py <dir_or_file> --web      # browser UI (localhost only by default)

REPL prompt commands (up/down-arrow command history via readline, when
available):
    info                 show the queue's metadata (qid/type/addr/rp/wp/size/...)
    packet N  (p N)       decode and show packet index N
    range A B  (r A B)    decode and show packets A..B (inclusive)
    all                   decode the whole ring
    raw N                 hex-dump the raw bytes for packet/slot N
    rp / wp               jump to the packet at the read/write pointer
                          (HSA: always available; DMA/XGMI: only if the dump
                          carries SDMA rptr/wptr enrichment -- see README)
    help                  show this list
    quit / exit           leave

When pointed at a directory instead of a single file, two more commands
are available (see run_repl_dir) to switch which queue the above commands
apply to, without restarting the tool:
    list / ls / queues    show every .bin dump found in the directory
    use N_or_name          switch to that queue (0-based index, exact
                          filename, or an unambiguous filename prefix)

N/A/B above accept a plain integer, or 'rp'/'wp' optionally followed by
+N/-N (e.g. "range rp rp+5", "raw wp-1"), resolved the same way the
rp/wp commands themselves resolve.
"""

import json
import os
import re
import sys

try:
    # Importing readline is enough to make input() gain up/down-arrow
    # command history and basic line editing for free -- no other code
    # needed. Not available on some platforms (e.g. stock Windows Python),
    # so this degrades to plain input() there instead of failing outright.
    import readline  # noqa: F401
except ImportError:
    pass

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import queue_decode as qd

_SDMA_LIKE_TYPES = {"DMA", "XGMI"}
_SEPARATOR = "-" * 84  # must match queue_decode.py's _PKT_SEPARATOR_WIDTH
_PACKET_TITLE_RE = re.compile(r"^Packet #\d+ at 0x([0-9a-fA-F]+)")
_WEB_ALL_CAP = 2000  # don't let a browser tab try to render a 100k+-packet ring in one go


class BufferReader:
    """queue_decode reader interface backed by an in-memory byte buffer
    loaded from a dump file, instead of a live gdb.Inferior."""

    def __init__(self, base_addr, buf):
        self.base_addr = base_addr
        self.buf = buf

    def read(self, addr, size):
        off = addr - self.base_addr
        if off < 0 or off + size > len(self.buf):
            raise qd.MemoryReadError(
                f"0x{addr:x} (+{size}) is outside the dumped range "
                f"[0x{self.base_addr:x}, 0x{self.base_addr + len(self.buf):x})"
            )
        return self.buf[off : off + size]


class QueueDump:
    """One loaded .bin dump: metadata + raw bytes + decode-on-demand."""

    def __init__(self, path):
        self.path = path
        with open(path, "rb") as f:
            self.metadata = qd.read_dump_header(f)
            self.buf = f.read()

        declared_size = self.metadata.get("size", len(self.buf))
        if len(self.buf) != declared_size:
            print(
                f"warning: dump declares size={declared_size} bytes but file has "
                f"{len(self.buf)} bytes of ring data -- dump may be truncated"
            )

        self.reader = BufferReader(self.metadata["addr"], self.buf)
        self.qtype = self.metadata["type"]
        self.is_hsa = self.qtype == "HSA"

        # SDMA/XGMI packets are variable-length -- no random access by
        # construction. Walked and cached lazily on first use.
        self._sdma_blocks = None  # list[list[str]], one block of lines per packet
        self._sdma_addrs = None  # list[int], start address of each packet

    # -- metadata -----------------------------------------------------
    def print_info(self, emit=print):
        m = self.metadata
        emit(f"file:      {self.path}")
        emit(f"qid:       {m.get('qid')}")
        emit(f"type:      {m.get('type')}")
        emit(f"target_id: {m.get('target_id')}")
        emit(f"addr:      0x{m.get('addr', 0):x}")
        emit(f"size:      {m.get('size')} bytes")
        emit(f"read:      {m.get('read')}")
        emit(f"write:     {m.get('write')}")
        emit(f"pid:       {m.get('pid')}")
        emit(f"comm:      {m.get('comm')}")
        emit(f"host:      {m.get('host')}")
        emit(f"dump_time: {m.get('dump_time')}")

    # -- HSA: O(1) random access ---------------------------------------
    def _hsa_count(self):
        return self.metadata["size"] // 64

    def _print_hsa_range(self, start, end, emit=print):
        qd.decode_hsa_packets(self.reader, self.metadata["addr"], start, end, emit=emit)

    # -- SDMA/XGMI: walk once, cache blocks -----------------------------
    def _ensure_sdma_walked(self):
        if self._sdma_blocks is not None:
            return
        lines = []
        qd.decode_sdma_packets(
            self.reader, self.metadata["addr"], self.metadata["size"], emit=lines.append
        )
        # Each packet is now bounded by its own opening AND closing separator
        # (queue_decode.py's _render_sdma_packet: SEP, TITLE, SEP, ...rows...,
        # SEP), so a plain "line == separator" split would create a spurious
        # extra block between every pair of packets (packet N's closing
        # separator immediately followed by packet N+1's opening one).
        # Split on the "Packet #N at 0x..." title line instead -- it appears
        # exactly once per packet -- and stop absorbing lines into the
        # current block as soon as its own *second* separator (the closing
        # one; the first is the one right after its title) has been seen,
        # so the next packet's leading separator doesn't get glued onto the
        # tail of the previous block. Lines before the very first title (the
        # first packet's opening separator) are discarded, not stored.
        blocks = []
        addrs = []
        current = None
        seps_in_block = 0
        for line in lines:
            if _PACKET_TITLE_RE.match(line):
                current = [line]
                blocks.append(current)
                seps_in_block = 0
                continue
            if current is None:
                continue
            current.append(line)
            if line == _SEPARATOR:
                seps_in_block += 1
                if seps_in_block >= 2:
                    current = None  # block fully closed
        for block in blocks:
            m = _PACKET_TITLE_RE.match(block[0]) if block else None
            addrs.append(int(m.group(1), 16) if m else None)
        self._sdma_blocks = blocks
        self._sdma_addrs = addrs

    def _sdma_count(self):
        self._ensure_sdma_walked()
        return len(self._sdma_blocks)

    def _print_sdma_range(self, start, end, emit=print):
        self._ensure_sdma_walked()
        for i in range(start, min(end, len(self._sdma_blocks))):
            emit(_SEPARATOR)
            for line in self._sdma_blocks[i]:
                emit(line)

    # -- shared entry points used by the REPL and the web view ---------
    def packet_count(self):
        return self._hsa_count() if self.is_hsa else self._sdma_count()

    def print_packets(self, start, end, emit=print):
        """[start, end) -- end is exclusive, matches decode_hsa_packets."""
        if self.is_hsa:
            self._print_hsa_range(start, end, emit=emit)
        else:
            self._print_sdma_range(start, end, emit=emit)

    def print_raw(self, idx, emit=print):
        if self.is_hsa:
            addr = self.metadata["addr"] + idx * 64
            length = 64
        else:
            self._ensure_sdma_walked()
            if idx >= len(self._sdma_addrs) or self._sdma_addrs[idx] is None:
                emit(f"packet {idx} has no known address (nothing cached?)")
                return
            addr = self._sdma_addrs[idx]
            if idx + 1 < len(self._sdma_addrs) and self._sdma_addrs[idx + 1] is not None:
                length = self._sdma_addrs[idx + 1] - addr
            else:
                length = 64  # last packet -- no next-packet boundary to size against
        try:
            data = self.reader.read(addr, length)
        except qd.MemoryReadError as e:
            emit(f"Cannot read raw bytes for packet {idx}: {e}")
            return
        emit(f"raw bytes for packet {idx} at 0x{addr:x} ({length} bytes):")
        for off in range(0, len(data), 16):
            chunk = data[off : off + 16]
            hexpart = " ".join(f"{b:02x}" for b in chunk)
            emit(f"  0x{addr + off:x}: {hexpart}")

    # -- jump straight to the rp/wp slot ---------------------------
    def _resolve_pointer(self, which):
        """Resolve rp ('read')/wp ('write') to a packet index, quietly
        (no printing) -- shared by jump_to_pointer (which prints a
        diagnostic message built from this) and resolve_pointer_index
        (which just wants the index, e.g. for the REPL's 'wp'/'rp'
        expression support). Returns a dict; 'idx' is None when
        unavailable/unresolvable, always with enough info in the dict to
        explain why.
        """
        val = self.metadata.get(which)
        if val is None:
            return {"idx": None, "reason": "missing"}

        if self.is_hsa:
            count = self._hsa_count()
            if count == 0:
                return {"idx": None, "reason": "empty"}
            # Read/Write from `info queue` are raw, monotonically-increasing
            # packet-ID counters, not slot indices -- same wraparound
            # `dump_hsa_queue` itself applies (see rocgdb_helper.py:
            # `idx %= size_bytes // 64`).
            idx = val % count
            return {"idx": idx, "reason": None, "val": val, "count": count}

        # SDMA/XGMI: `val` is a raw, un-wrapped dword counter -- same storage
        # convention as HSA's raw packet-ID counter (see
        # rocgdb_helper.py's _enrich_sdma_pointers() docstring) -- but a
        # different *unit*: a ring-relative dword position, not a packet
        # index, since SDMA packets are variable-length and there's no
        # equivalent "packet index" concept at the hardware level. Wrap it
        # to a ring position the same way HSA wraps to a slot index, then
        # convert to a byte offset and find which already-decoded packet
        # contains it.
        self._ensure_sdma_walked()
        ring_size_dwords = self.metadata["size"] // 4
        dword_slot = val % ring_size_dwords
        byte_offset = dword_slot * 4
        byte_addr = self.metadata["addr"] + byte_offset
        ring_end = self.metadata["addr"] + self.metadata["size"]
        idx = None
        for i, start in enumerate(self._sdma_addrs):
            if start is None:
                continue
            end = ring_end
            if i + 1 < len(self._sdma_addrs) and self._sdma_addrs[i + 1] is not None:
                end = self._sdma_addrs[i + 1]
            if start <= byte_addr < end:
                idx = i
                break
        return {
            "idx": idx,
            "reason": None if idx is not None else "not_found",
            "val": val,
            "dword_slot": dword_slot,
            "byte_offset": byte_offset,
            "count": self._sdma_count(),
        }

    def resolve_pointer_index(self, which):
        """Quiet version of jump_to_pointer's resolution: returns the
        packet index (int) rp/wp currently points to, or None if it
        isn't available/resolvable for this dump. Used by the REPL to
        support 'wp'/'rp' (optionally with a +N/-N offset) as
        packet-index arguments to packet/range/raw."""
        return self._resolve_pointer(which)["idx"]

    def jump_to_pointer(self, which, emit=print):
        """which: 'read' (rp) or 'write' (wp)."""
        label = "rp" if which == "read" else "wp"
        info = self._resolve_pointer(which)

        if info.get("reason") == "missing":
            if self.is_hsa:
                emit(f"no {label} recorded in this dump's metadata")
            else:
                # `info queue` never reports Read/Write for DMA/XGMI rows,
                # so this is only available when rocgdb_helper.py's
                # best-effort SDMA-pointer enrichment (reads KFD debugfs,
                # needs root) found and recorded a value -- see README.
                emit(
                    f"no {label} recorded in this dump's metadata -- {self.qtype} "
                    f"rp/wp requires the dump-time SDMA enrichment step "
                    f"(root access to KFD debugfs) to have found this queue"
                )
            return

        if self.is_hsa:
            if info.get("reason") == "empty":
                emit("queue has no packet slots (size 0)")
                return
            idx, count = info["idx"], info["count"]
            emit(f"{label} (raw={info['val']}) -> slot index {idx} (of {count})")
            self.print_packets(idx, idx + 1, emit=emit)
            return

        prefix = (
            f"{label} (raw={info['val']}) -> dword slot {info['dword_slot']} "
            f"-> byte offset 0x{info['byte_offset']:x}"
        )
        if info["idx"] is None:
            emit(f"{prefix}, but no decoded packet contains it")
            return
        emit(f"{prefix} -> packet index {info['idx']} (of {info['count']})")
        self.print_packets(info["idx"], info["idx"] + 1, emit=emit)


HELP_TEXT = """\
Commands:
  info                 show queue metadata (qid/type/addr/rp/wp/size/...)
  packet N   (p N)     decode and show packet index N
  range A B  (r A B)   decode and show packets A..B (inclusive)
  all                  decode the whole ring
  raw N                hex-dump the raw bytes for packet/slot N
  rp                   jump to and decode the packet at the read pointer
                       (HSA: always available; DMA/XGMI: only if the dump
                       carries SDMA rptr/wptr enrichment -- see README)
  wp                   jump to and decode the packet at the write pointer
                       (same availability note as rp)
  help                 show this list
  quit / exit          leave

N/A/B above accept a plain integer (decimal or 0x-prefixed hex), or 'rp'/
'wp' optionally followed by +N/-N, resolved the same way the rp/wp
commands do, e.g.:
  packet wp             decode the packet currently at the write pointer
  range rp rp+5          decode from the read pointer through 5 packets later
  raw wp-1               hex-dump the packet just before the write pointer
"""

_PTR_EXPR_RE = re.compile(r"^(rp|wp)\s*([+-]\s*\d+)?$", re.IGNORECASE)


def _parse_index(dump, token):
    """Parse a packet-index argument for the REPL: a plain integer (any
    base int() accepts, e.g. '0x10'), or 'rp'/'wp' optionally followed
    by a +N/-N offset (e.g. 'wp+1', 'rp-2'), resolved via the same
    logic the rp/wp commands use (QueueDump.resolve_pointer_index).
    Raises ValueError with a clear message on failure -- callers already
    catch ValueError from the plain int() case, so this fits the same
    error-handling path without any extra code at the call sites."""
    m = _PTR_EXPR_RE.match(token)
    if not m:
        return int(token, 0)
    which = "read" if m.group(1).lower() == "rp" else "write"
    idx = dump.resolve_pointer_index(which)
    if idx is None:
        raise ValueError(f"{m.group(1)} is not available for this dump")
    offset_str = (m.group(2) or "").replace(" ", "")
    return idx + (int(offset_str) if offset_str else 0)


def _dispatch_command(dump, cmd, parts):
    """Execute one REPL command against `dump`. Returns True to keep the
    REPL loop running, False if this was a quit/exit command. Raises
    ValueError/IndexError on bad input -- callers already catch those and
    print 'error: ...' the same way, for both the single-file REPL
    (run_repl) and the multi-queue directory REPL (run_repl_dir)."""
    if cmd in ("quit", "exit", "q"):
        return False
    elif cmd == "help":
        print(HELP_TEXT)
    elif cmd == "info":
        dump.print_info()
    elif cmd in ("packet", "p"):
        if len(parts) != 2:
            print("usage: packet N (N: int, or rp/wp +/-N)")
            return True
        n = _parse_index(dump, parts[1])
        dump.print_packets(n, n + 1)
    elif cmd in ("range", "r"):
        if len(parts) != 3:
            print("usage: range A B (A/B: int, or rp/wp +/-N)")
            return True
        a, b = _parse_index(dump, parts[1]), _parse_index(dump, parts[2])
        dump.print_packets(a, b + 1)
    elif cmd == "all":
        dump.print_packets(0, dump.packet_count())
    elif cmd == "raw":
        if len(parts) != 2:
            print("usage: raw N (N: int, or rp/wp +/-N)")
            return True
        dump.print_raw(_parse_index(dump, parts[1]))
    elif cmd == "rp":
        dump.jump_to_pointer("read")
    elif cmd == "wp":
        dump.jump_to_pointer("write")
    else:
        print(f"unknown command: {cmd!r} (try 'help')")
    return True


def run_repl(dump):
    print(f"Loaded {dump.path} ({dump.qtype}, qid={dump.metadata.get('qid')})")
    try:
        count = dump.packet_count()
        print(f"{count} packet(s) decoded/available (indices 0..{max(count - 1, 0)})")
    except Exception as e:
        print(f"warning: could not pre-scan packets: {e}")
    print("Type 'help' for commands.")

    while True:
        try:
            line = input("(queue_viewer) > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        parts = line.split()
        cmd = parts[0].lower()

        try:
            if not _dispatch_command(dump, cmd, parts):
                break
        except (ValueError, IndexError) as e:
            print(f"error: {e}")


def list_bin_files(path):
    """Return sorted [(name, full_path), ...] of .bin dumps under path.
    If path is itself a .bin file (not a directory), returns just that one
    entry -- lets `--web` work the same way against a single file or a
    whole dump_all_queues output directory."""
    if os.path.isdir(path):
        names = sorted(f for f in os.listdir(path) if f.endswith(".bin"))
        return [(n, os.path.join(path, n)) for n in names]
    return [(os.path.basename(path), path)]


def _peek_dump_metadata(path):
    """Read just a .bin dump's header (qid/type/size/target_id/...) without
    loading the rest of the file -- DMA/XGMI rings are commonly several MB,
    so this keeps `run_repl_dir`'s 'list' command fast and light regardless
    of how many/how large the queues in a directory are. Full ring bytes
    are only loaded once a queue is actually selected via 'use'."""
    with open(path, "rb") as f:
        return qd.read_dump_header(f)


def run_repl_dir(path):
    """Directory version of the REPL: list every .bin dump under `path` and
    let the user switch between them at any time with 'use', instead of
    fixing the REPL to a single file for the whole session (run_repl). Each
    dump is loaded lazily -- only once actually selected -- and cached for
    the rest of the session, mirroring `--web`'s per-queue lazy loading."""
    files = list_bin_files(path)
    if not files:
        print(f"No .bin files found under {path}", file=sys.stderr)
        return 1

    names = [n for n, _ in files]
    paths_by_name = dict(files)
    dumps = {}  # name -> loaded QueueDump, filled in on first 'use'
    current = [None]  # mutable cell so the nested closures below see updates

    def get(name):
        if name not in dumps:
            dumps[name] = QueueDump(paths_by_name[name])
        return dumps[name]

    def resolve_name(token):
        """'use'/list-selector argument -> exact name, or None if it
        doesn't match anything. Accepts a 0-based index into the sorted
        listing, an exact filename, or an unambiguous filename prefix."""
        if token in paths_by_name:
            return token
        if token.isdigit() and 0 <= int(token) < len(names):
            return names[int(token)]
        matches = [n for n in names if n.startswith(token)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ValueError(
                f"{token!r} matches {len(matches)} queues, be more specific: "
                + ", ".join(matches)
            )
        return None

    def print_list():
        for i, name in enumerate(names):
            marker = "*" if name == current[0] else " "
            try:
                m = _peek_dump_metadata(paths_by_name[name])
                # qid/target_id are already encoded in the filename itself
                # (e.g. dma_QID11_GPU_7_Queue_22.bin) -- no need to repeat
                # them here. rp/wp (raw, un-wrapped -- same convention as
                # 'info') are the useful at-a-glance values instead, since
                # they're not derivable from the filename.
                detail = (
                    f"type={m.get('type')} size={m.get('size')} "
                    f"rp={m.get('read')} wp={m.get('write')}"
                )
            except Exception as e:
                detail = f"error: {e}"
            print(f" {marker} [{i}] {name}  {detail}")

    print(f"{len(names)} queue dump(s) found under {path}")
    print_list()
    print("Type 'use <index_or_name>' to select one, 'list' to see this again, 'help' for commands.")

    while True:
        prompt = f"(queue_viewer:{current[0]}) > " if current[0] else "(queue_viewer) > "
        try:
            line = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        parts = line.split()
        cmd = parts[0].lower()

        try:
            if cmd in ("quit", "exit", "q"):
                break
            elif cmd in ("list", "ls", "queues"):
                print_list()
            elif cmd == "use":
                if len(parts) != 2:
                    print("usage: use <index_or_name>")
                    continue
                name = resolve_name(parts[1])
                if name is None:
                    print(f"no such queue: {parts[1]!r} (try 'list')")
                    continue
                try:
                    dump = get(name)  # trigger load now, surfacing any error here
                except (OSError, ValueError) as e:
                    print(f"Cannot open {name}: {e}")
                    continue
                current[0] = name
                print(f"switched to {name} ({dump.qtype}, qid={dump.metadata.get('qid')})")
                try:
                    count = dump.packet_count()
                    print(f"{count} packet(s) decoded/available (indices 0..{max(count - 1, 0)})")
                except Exception as e:
                    print(f"warning: could not pre-scan packets: {e}")
            elif cmd == "help":
                print("Directory commands:")
                print("  list / ls / queues     show queue dumps found under this directory")
                print("  use <index_or_name>    switch to that queue (accepts a name prefix too)")
                print()
                print(HELP_TEXT)
            elif current[0] is None:
                print("no queue selected -- try 'list' then 'use <index_or_name>'")
            else:
                if not _dispatch_command(get(current[0]), cmd, parts):
                    break
        except (ValueError, IndexError) as e:
            print(f"error: {e}")
    return 0


def capture(bound_method, *args, **kwargs):
    """Call a QueueDump print_*/jump_to_pointer method with emit=list.append
    instead of emit=print, and return the collected lines. Every such method
    takes the same `emit=print` keyword, so this works generically."""
    lines = []
    bound_method(*args, emit=lines.append, **kwargs)
    return lines


# --- minimal web UI -----------------------------------------------------
#
# Deliberately dependency-free (stdlib http.server only, no Flask/etc) and
# text-only (no ring visualization) -- a browser-based version of the REPL:
# pick a queue from the sidebar, click a button or type a packet index, see
# the same decoded text the REPL would have printed.

_INDEX_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>queue_viewer</title>
<style>
  :root {
    --bg: #f4f5f7; --panel: #ffffff; --border: #dde1e6; --text: #1c2128;
    --muted: #6b7280; --accent: #2563eb; --accent-bg: #eaf1ff;
    --hsa: #2563eb; --dma: #059669; --xgmi: #7c3aed;
    --mono: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
    --ui: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  }
  * { box-sizing: border-box; }
  body { font-family: var(--ui); margin: 0; height: 100vh; display: flex; flex-direction: column; color: var(--text); background: var(--bg); }
  #appbar { flex: none; display: flex; align-items: baseline; gap: 10px; padding: 10px 16px; background: var(--panel); border-bottom: 1px solid var(--border); }
  #appbar h1 { font-size: 15px; margin: 0; font-weight: 600; }
  #appbar .src { font-size: 12px; color: var(--muted); font-family: var(--mono); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  #appbar .spacer { flex: 1; }
  #appbar button { font-family: var(--ui); }
  #body { flex: 1; display: flex; min-height: 0; }
  #sidebar { width: 300px; flex: none; overflow-y: auto; border-right: 1px solid var(--border); background: var(--panel); }
  #qlist { padding: 6px; }
  #main { flex: 1; display: flex; flex-direction: column; padding: 12px; min-width: 0; }
  #title { font-size: 14px; margin-bottom: 10px; color: var(--muted); }
  #title b { color: var(--text); font-family: var(--mono); }
  #controls { margin-bottom: 10px; background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; }
  .row { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; margin: 4px 0; }
  .row label { color: var(--muted); font-size: 12px; width: 46px; }
  button {
    font-family: var(--ui); font-size: 13px; padding: 5px 12px; margin: 0;
    border: 1px solid var(--border); border-radius: 6px; background: #fff; color: var(--text); cursor: pointer;
  }
  button:hover { background: var(--accent-bg); border-color: var(--accent); }
  button:active { transform: translateY(1px); }
  button.primary { background: var(--accent); color: #fff; border-color: var(--accent); }
  button.primary:hover { filter: brightness(1.08); }
  input[type=text] {
    font-family: var(--mono); font-size: 13px; padding: 5px 8px; width: 130px;
    border: 1px solid var(--border); border-radius: 6px;
  }
  input[type=text]:focus, button:focus { outline: 2px solid var(--accent); outline-offset: 1px; }
  #output {
    flex: 1; overflow: auto; background: #0d1117; color: #d8dee9; padding: 12px 14px;
    white-space: pre; font-family: var(--mono); font-size: 13px; line-height: 1.45;
    border-radius: 8px; border: 1px solid var(--border);
  }
  #output.empty { color: #6b7280; white-space: pre-wrap; font-family: var(--ui); }
  .qitem { padding: 8px 10px; margin: 3px 4px; cursor: pointer; border-radius: 6px; border: 1px solid transparent; }
  .qitem:hover { background: var(--bg); }
  .qitem.selected { background: var(--accent-bg); border-color: var(--accent); }
  .qname { font-family: var(--mono); font-size: 12.5px; word-break: break-all; }
  .qmeta { color: var(--muted); font-size: 11px; margin-top: 3px; display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
  .badge { display: inline-block; padding: 1px 7px; border-radius: 10px; font-size: 10px; font-weight: 600; color: #fff; letter-spacing: 0.02em; }
  .badge.hsa { background: var(--hsa); }
  .badge.dma { background: var(--dma); }
  .badge.xgmi { background: var(--xgmi); }
  .badge.err { background: #dc2626; }
  #empty-hint { padding: 30px 8px; color: var(--muted); font-size: 13px; }
</style>
</head>
<body>
<div id="appbar">
  <h1>queue_viewer</h1>
  <span class="src" id="src"></span>
  <span class="spacer"></span>
  <button onclick="doHelp()" title="command reference">help</button>
</div>
<div id="body">
  <div id="sidebar"><div id="qlist">loading...</div></div>
  <div id="main">
    <div id="title"><b>No queue selected</b> -- pick one from the sidebar</div>
    <div id="controls">
      <div class="row">
        <button onclick="doInfo()">info</button>
        <button onclick="doAll()">all</button>
        <button class="primary" onclick="doRp()">rp</button>
        <button class="primary" onclick="doWp()">wp</button>
      </div>
      <div class="row">
        <label>packet</label>
        <input type="text" id="pkt" placeholder="N or wp-1" onkeydown="if(event.key==='Enter')doPacket()">
        <button onclick="doPacket()">go</button>
      </div>
      <div class="row">
        <label>range</label>
        <input type="text" id="rangeA" placeholder="rp" style="width:90px" onkeydown="if(event.key==='Enter')doRange()">
        <input type="text" id="rangeB" placeholder="wp+5" style="width:90px" onkeydown="if(event.key==='Enter')doRange()">
        <button onclick="doRange()">go</button>
      </div>
      <div class="row">
        <label>raw</label>
        <input type="text" id="rawn" placeholder="N or rp" onkeydown="if(event.key==='Enter')doRaw()">
        <button onclick="doRaw()">go</button>
      </div>
    </div>
    <div id="output" class="empty">Pick a queue on the left, then use the buttons above (or 'help' for the
full command reference -- N/A/B accept a plain integer or rp/wp optionally followed by +N/-N,
e.g. "wp-1" or "range rp rp+5").</div>
  </div>
</div>
<script>
let current = null;

function api(path) {
  return fetch(path).then(r => r.json());
}

function show(obj) {
  const out = document.getElementById('output');
  out.classList.remove('empty');
  if (obj.error) { out.textContent = 'error: ' + obj.error; return; }
  out.textContent = (obj.lines || []).join('\\n');
}

function badgeClass(type) {
  const t = (type || '').toLowerCase();
  return (t === 'hsa' || t === 'dma' || t === 'xgmi') ? t : 'err';
}

function loadList() {
  api('/api/list').then(items => {
    const qlist = document.getElementById('qlist');
    qlist.innerHTML = '';
    if (!items.length) {
      qlist.innerHTML = '<div id="empty-hint">no .bin dumps found</div>';
      return;
    }
    items.forEach(it => {
      const div = document.createElement('div');
      div.className = 'qitem';
      div.dataset.name = it.name;
      if (it.error) {
        div.innerHTML = '<div class="qname">' + it.name + '</div>' +
          '<div class="qmeta"><span class="badge err">error</span>' + it.error + '</div>';
      } else {
        const count = (it.count === null || it.count === undefined) ? '?' : it.count;
        div.innerHTML = '<div class="qname">' + it.name + '</div>' +
          '<div class="qmeta"><span class="badge ' + badgeClass(it.type) + '">' + it.type + '</span>' +
          'qid ' + it.qid + ' &middot; ' + count + ' pkt &middot; ' + it.size + ' B</div>';
      }
      div.onclick = () => selectQueue(it.name);
      qlist.appendChild(div);
    });
  });
}

function selectQueue(name) {
  current = name;
  document.querySelectorAll('.qitem').forEach(el => {
    el.classList.toggle('selected', el.dataset.name === name);
  });
  document.getElementById('title').innerHTML = '<b>' + name + '</b>';
  doInfo();
}

function need() {
  if (!current) { alert('pick a queue first'); return false; }
  return true;
}

function q(path) { return '/api/queue/' + encodeURIComponent(current) + path; }

function doHelp()  { api('/api/help').then(show); }
function doInfo()  { if (need()) api(q('/info')).then(show); }
function doAll()   { if (need()) api(q('/all')).then(show); }
function doRp()  { if (need()) api(q('/rp')).then(show); }
function doWp()  { if (need()) api(q('/wp')).then(show); }
function doPacket() {
  if (!need()) return;
  const n = document.getElementById('pkt').value.trim();
  if (!n) return;
  api(q('/packet/' + encodeURIComponent(n))).then(show);
}
function doRange() {
  if (!need()) return;
  const a = document.getElementById('rangeA').value.trim(), b = document.getElementById('rangeB').value.trim();
  if (!a || !b) return;
  api(q('/range/' + encodeURIComponent(a) + '/' + encodeURIComponent(b))).then(show);
}
function doRaw() {
  if (!need()) return;
  const n = document.getElementById('rawn').value.trim();
  if (!n) return;
  api(q('/raw/' + encodeURIComponent(n))).then(show);
}

document.getElementById('src').textContent = __ROOT_PATH_JSON__;
loadList();
</script>
</body>
</html>
"""


class _QueueWebState:
    def __init__(self, files, root_path=""):
        self.files = dict(files)  # name -> path
        self.root_path = root_path  # the dir/file the server was pointed at, for display only
        self._dumps = {}

    def get(self, name):
        if name not in self.files:
            raise KeyError(name)
        if name not in self._dumps:
            self._dumps[name] = QueueDump(self.files[name])
        return self._dumps[name]

    def listing(self):
        out = []
        for name in self.files:
            try:
                d = self.get(name)
                try:
                    count = d.packet_count()
                except Exception:
                    count = None  # e.g. truncated dump -- info/raw may still work
                out.append(
                    {
                        "name": name,
                        "qid": d.metadata.get("qid"),
                        "type": d.metadata.get("type"),
                        "size": d.metadata.get("size"),
                        "target_id": d.metadata.get("target_id"),
                        "count": count,
                    }
                )
            except Exception as e:
                out.append({"name": name, "error": str(e)})
        return out


def _make_handler(state):
    import http.server
    import urllib.parse

    index_html = _INDEX_HTML.replace("__ROOT_PATH_JSON__", json.dumps(state.root_path))

    class Handler(http.server.BaseHTTPRequestHandler):
        def _send_json(self, obj, status=200):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, html, status=200):
            body = html.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            pass  # keep the terminal quiet -- this is a local debug tool

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            # Percent-decode every path segment (not just the queue name) --
            # index arguments can now be 'rp'/'wp' expressions, and while
            # the frontend already encodeURIComponent()s them, decoding here
            # unconditionally keeps this endpoint correct for any client.
            parts = [urllib.parse.unquote(p) for p in parsed.path.split("/") if p]

            try:
                if not parts:
                    self._send_html(index_html)
                    return
                if parts == ["api", "list"]:
                    self._send_json(state.listing())
                    return
                if parts == ["api", "help"]:
                    self._send_json({"lines": HELP_TEXT.splitlines()})
                    return
                if len(parts) >= 3 and parts[0] == "api" and parts[1] == "queue":
                    name = parts[2]
                    try:
                        dump = state.get(name)
                    except KeyError:
                        self._send_json({"error": f"unknown queue file {name!r}"}, status=404)
                        return

                    action = parts[3] if len(parts) > 3 else "info"
                    # packet/range/raw indices accept the same syntax as the
                    # REPL: a plain int, or 'rp'/'wp' optionally followed
                    # by +N/-N -- see _parse_index.
                    if action == "info":
                        self._send_json({"lines": capture(dump.print_info)})
                    elif action == "packet" and len(parts) == 5:
                        n = _parse_index(dump, parts[4])
                        self._send_json({"lines": capture(dump.print_packets, n, n + 1)})
                    elif action == "range" and len(parts) == 6:
                        a, b = _parse_index(dump, parts[4]), _parse_index(dump, parts[5])
                        self._send_json({"lines": capture(dump.print_packets, a, b + 1)})
                    elif action == "all":
                        count = dump.packet_count()
                        capped = min(count, _WEB_ALL_CAP)
                        lines = capture(dump.print_packets, 0, capped)
                        if capped < count:
                            lines.append(
                                f"... capped at {_WEB_ALL_CAP} of {count} packets -- "
                                "use 'range'/'packet' for the rest"
                            )
                        self._send_json({"lines": lines})
                    elif action == "raw" and len(parts) == 5:
                        n = _parse_index(dump, parts[4])
                        self._send_json({"lines": capture(dump.print_raw, n)})
                    elif action in ("rp", "wp"):
                        which = "read" if action == "rp" else "write"
                        self._send_json({"lines": capture(dump.jump_to_pointer, which)})
                    else:
                        self._send_json({"error": f"unknown action {action!r}"}, status=404)
                    return
                self._send_json({"error": "not found"}, status=404)
            except (ValueError, IndexError) as e:
                self._send_json({"error": str(e)}, status=400)
            except Exception as e:
                self._send_json({"error": f"internal error: {e}"}, status=500)

    return Handler


def run_web(path, host, port):
    import http.server
    import socketserver

    # http.server.ThreadingHTTPServer only exists from Python 3.7 -- build
    # the equivalent by hand so this still runs on older 3.6 interpreters.
    class _ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True

    files = list_bin_files(path)
    if not files:
        print(f"No .bin files found under {path}", file=sys.stderr)
        return 1

    state = _QueueWebState(files, root_path=os.path.abspath(path))
    server = _ThreadingHTTPServer((host, port), _make_handler(state))
    print(f"Serving {len(files)} queue dump(s) from {path}")
    print(f"Open http://{host}:{port}/ in a browser (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    return 0


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", help="a .bin dump file, or a directory of them")
    parser.add_argument("--web", action="store_true", help="serve a browser UI instead of the REPL")
    parser.add_argument("--host", default="127.0.0.1", help="--web bind address (default: 127.0.0.1, localhost only)")
    parser.add_argument("--port", type=int, default=8765, help="--web bind port (default: 8765)")
    args = parser.parse_args()

    if args.web:
        return run_web(args.path, args.host, args.port)

    if os.path.isdir(args.path):
        return run_repl_dir(args.path)

    try:
        dump = QueueDump(args.path)
    except (OSError, ValueError) as e:
        print(f"Cannot open {args.path}: {e}", file=sys.stderr)
        return 1

    run_repl(dump)
    return 0


if __name__ == "__main__":
    sys.exit(main())
