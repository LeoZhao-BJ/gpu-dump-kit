#!/usr/bin/env python3
"""Standalone offline viewer for the .bin queue dumps produced by
rocgdb_helper.py's `dump_all_queues_bin` rocgdb command.

No gdb dependency at all -- this reads the raw ring bytes straight out of
the dump file and decodes packets against an in-memory buffer, using the
exact same packet-format logic as the live path (see queue_decode.py).

Usage:
    python3 queue_viewer.py <dump.bin>              # interactive REPL, one file
    python3 queue_viewer.py <dir_or_file> --web      # browser UI (localhost only by default)

REPL prompt commands:
    info                 show the queue's metadata (qid/type/addr/rptr/wptr/size/...)
    packet N  (p N)       decode and show packet index N
    range A B             decode and show packets A..B (inclusive)
    all                   decode the whole ring
    raw N                 hex-dump the raw bytes for packet/slot N
    rptr / wptr           jump to the packet at the read/write pointer
                          (HSA: always available; DMA/XGMI: only if the dump
                          carries SDMA rptr/wptr enrichment -- see README)
    help                  show this list
    quit / exit           leave
"""

import json
import os
import re
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import queue_decode as qd

_SDMA_LIKE_TYPES = {"DMA", "XGMI"}
_SEPARATOR = "-" * 84  # must match queue_decode.py's _SDMA_SEPARATOR_WIDTH
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

    # -- jump straight to the rptr/wptr slot ---------------------------
    def jump_to_pointer(self, which, emit=print):
        """which: 'read' (rptr) or 'write' (wptr)."""
        label = "rptr" if which == "read" else "wptr"
        val = self.metadata.get(which)
        if val is None:
            if self.is_hsa:
                emit(f"no {label} recorded in this dump's metadata")
            else:
                # `info queue` never reports Read/Write for DMA/XGMI rows,
                # so this is only available when rocgdb_helper.py's
                # best-effort SDMA-pointer enrichment (reads KFD debugfs,
                # needs root) found and recorded a value -- see README.
                emit(
                    f"no {label} recorded in this dump's metadata -- {self.qtype} "
                    f"rptr/wptr requires the dump-time SDMA enrichment step "
                    f"(root access to KFD debugfs) to have found this queue"
                )
            return

        if self.is_hsa:
            count = self._hsa_count()
            if count == 0:
                emit("queue has no packet slots (size 0)")
                return
            # Read/Write from `info queue` are raw, monotonically-increasing
            # packet-ID counters, not slot indices -- same wraparound
            # `dump_hsa_queue` itself applies (see rocgdb_helper.py:
            # `idx %= size_bytes // 64`).
            idx = val % count
            emit(f"{label} (raw={val}) -> slot index {idx} (of {count})")
            self.print_packets(idx, idx + 1, emit=emit)
            return

        # SDMA/XGMI: `val` is a ring-relative *dword slot*, not a packet
        # index -- packets are variable-length, so there's no equivalent
        # "packet index" concept the way HSA has one (see rocgdb_helper.py's
        # _enrich_sdma_pointers() docstring). Convert to a byte offset from
        # the ring base and find which already-decoded packet contains it.
        self._ensure_sdma_walked()
        byte_addr = self.metadata["addr"] + val * 4
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
        if idx is None:
            emit(
                f"{label} (dword_slot={val}) -> byte offset 0x{val * 4:x}, "
                f"but no decoded packet contains it"
            )
            return
        emit(f"{label} (dword_slot={val}) -> byte offset 0x{val * 4:x} -> packet index {idx}")
        self.print_packets(idx, idx + 1, emit=emit)


HELP_TEXT = """\
Commands:
  info                 show queue metadata (qid/type/addr/rptr/wptr/size/...)
  packet N   (p N)     decode and show packet index N
  range A B            decode and show packets A..B (inclusive)
  all                  decode the whole ring
  raw N                hex-dump the raw bytes for packet/slot N
  rptr                 jump to and decode the packet at the read pointer
                       (HSA: always available; DMA/XGMI: only if the dump
                       carries SDMA rptr/wptr enrichment -- see README)
  wptr                 jump to and decode the packet at the write pointer
                       (same availability note as rptr)
  help                 show this list
  quit / exit          leave
"""


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
            if cmd in ("quit", "exit", "q"):
                break
            elif cmd == "help":
                print(HELP_TEXT)
            elif cmd == "info":
                dump.print_info()
            elif cmd in ("packet", "p"):
                if len(parts) != 2:
                    print("usage: packet N")
                    continue
                n = int(parts[1], 0)
                dump.print_packets(n, n + 1)
            elif cmd == "range":
                if len(parts) != 3:
                    print("usage: range A B")
                    continue
                a, b = int(parts[1], 0), int(parts[2], 0)
                dump.print_packets(a, b + 1)
            elif cmd == "all":
                dump.print_packets(0, dump.packet_count())
            elif cmd == "raw":
                if len(parts) != 2:
                    print("usage: raw N")
                    continue
                dump.print_raw(int(parts[1], 0))
            elif cmd == "rptr":
                dump.jump_to_pointer("read")
            elif cmd == "wptr":
                dump.jump_to_pointer("write")
            else:
                print(f"unknown command: {cmd!r} (try 'help')")
        except (ValueError, IndexError) as e:
            print(f"error: {e}")


def list_bin_files(path):
    """Return sorted [(name, full_path), ...] of .bin dumps under path.
    If path is itself a .bin file (not a directory), returns just that one
    entry -- lets `--web` work the same way against a single file or a
    whole dump_all_queues_bin output directory."""
    if os.path.isdir(path):
        names = sorted(f for f in os.listdir(path) if f.endswith(".bin"))
        return [(n, os.path.join(path, n)) for n in names]
    return [(os.path.basename(path), path)]


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
  body { font-family: monospace; margin: 0; display: flex; height: 100vh; }
  #sidebar { width: 320px; overflow-y: auto; border-right: 1px solid #ccc; padding: 8px; box-sizing: border-box; }
  #main { flex: 1; display: flex; flex-direction: column; padding: 8px; box-sizing: border-box; min-width: 0; }
  #controls { margin-bottom: 8px; }
  #controls button, #controls input { font-family: monospace; margin: 2px; }
  #output { flex: 1; overflow: auto; background: #111; color: #ddd; padding: 8px; white-space: pre-wrap; }
  .qitem { padding: 4px; cursor: pointer; border-bottom: 1px solid #eee; }
  .qitem:hover { background: #f0f0f0; }
  .qitem.selected { background: #dbe9ff; }
  .qtype { color: #666; font-size: 0.85em; }
</style>
</head>
<body>
<div id="sidebar"><div id="qlist">loading...</div></div>
<div id="main">
  <div id="title"><b>No queue selected</b></div>
  <div id="controls">
    <button onclick="doInfo()">info</button>
    <button onclick="doAll()">all</button>
    <button onclick="doRptr()">rptr</button>
    <button onclick="doWptr()">wptr</button>
    <br>
    packet <input id="pkt" size="6"><button onclick="doPacket()">go</button>
    range <input id="rangeA" size="6"> <input id="rangeB" size="6"><button onclick="doRange()">go</button>
    raw <input id="rawn" size="6"><button onclick="doRaw()">go</button>
  </div>
  <div id="output"></div>
</div>
<script>
let current = null;

function api(path) {
  return fetch(path).then(r => r.json());
}

function show(obj) {
  const out = document.getElementById('output');
  if (obj.error) { out.textContent = 'error: ' + obj.error; return; }
  out.textContent = (obj.lines || []).join('\\n');
}

function loadList() {
  api('/api/list').then(items => {
    const qlist = document.getElementById('qlist');
    qlist.innerHTML = '';
    items.forEach(it => {
      const div = document.createElement('div');
      div.className = 'qitem';
      div.dataset.name = it.name;
      if (it.error) {
        div.innerHTML = '<div>' + it.name + '</div><div class="qtype">error: ' + it.error + '</div>';
      } else {
        div.innerHTML = '<div>' + it.name + '</div><div class="qtype">qid=' + it.qid +
          ' type=' + it.type + ' size=' + it.size + '</div>';
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

function doInfo()  { if (need()) api('/api/queue/' + encodeURIComponent(current) + '/info').then(show); }
function doAll()   { if (need()) api('/api/queue/' + encodeURIComponent(current) + '/all').then(show); }
function doRptr()  { if (need()) api('/api/queue/' + encodeURIComponent(current) + '/rptr').then(show); }
function doWptr()  { if (need()) api('/api/queue/' + encodeURIComponent(current) + '/wptr').then(show); }
function doPacket() {
  if (!need()) return;
  const n = document.getElementById('pkt').value;
  api('/api/queue/' + encodeURIComponent(current) + '/packet/' + n).then(show);
}
function doRange() {
  if (!need()) return;
  const a = document.getElementById('rangeA').value, b = document.getElementById('rangeB').value;
  api('/api/queue/' + encodeURIComponent(current) + '/range/' + a + '/' + b).then(show);
}
function doRaw() {
  if (!need()) return;
  const n = document.getElementById('rawn').value;
  api('/api/queue/' + encodeURIComponent(current) + '/raw/' + n).then(show);
}

loadList();
</script>
</body>
</html>
"""


class _QueueWebState:
    def __init__(self, files):
        self.files = dict(files)  # name -> path
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
                out.append(
                    {
                        "name": name,
                        "qid": d.metadata.get("qid"),
                        "type": d.metadata.get("type"),
                        "size": d.metadata.get("size"),
                        "target_id": d.metadata.get("target_id"),
                    }
                )
            except Exception as e:
                out.append({"name": name, "error": str(e)})
        return out


def _make_handler(state):
    import http.server
    import urllib.parse

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
            parts = [p for p in parsed.path.split("/") if p]

            try:
                if not parts:
                    self._send_html(_INDEX_HTML)
                    return
                if parts == ["api", "list"]:
                    self._send_json(state.listing())
                    return
                if len(parts) >= 3 and parts[0] == "api" and parts[1] == "queue":
                    name = urllib.parse.unquote(parts[2])
                    try:
                        dump = state.get(name)
                    except KeyError:
                        self._send_json({"error": f"unknown queue file {name!r}"}, status=404)
                        return

                    action = parts[3] if len(parts) > 3 else "info"
                    if action == "info":
                        self._send_json({"lines": capture(dump.print_info)})
                    elif action == "packet" and len(parts) == 5:
                        n = int(parts[4])
                        self._send_json({"lines": capture(dump.print_packets, n, n + 1)})
                    elif action == "range" and len(parts) == 6:
                        a, b = int(parts[4]), int(parts[5])
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
                        n = int(parts[4])
                        self._send_json({"lines": capture(dump.print_raw, n)})
                    elif action in ("rptr", "wptr"):
                        which = "read" if action == "rptr" else "write"
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

    state = _QueueWebState(files)
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
    parser.add_argument("path", help="a .bin dump file, or a directory of them (for --web)")
    parser.add_argument("--web", action="store_true", help="serve a browser UI instead of the REPL")
    parser.add_argument("--host", default="127.0.0.1", help="--web bind address (default: 127.0.0.1, localhost only)")
    parser.add_argument("--port", type=int, default=8765, help="--web bind port (default: 8765)")
    args = parser.parse_args()

    if args.web:
        return run_web(args.path, args.host, args.port)

    try:
        dump = QueueDump(args.path)
    except (OSError, ValueError) as e:
        print(f"Cannot open {args.path}: {e}", file=sys.stderr)
        return 1

    run_repl(dump)
    return 0


if __name__ == "__main__":
    sys.exit(main())
