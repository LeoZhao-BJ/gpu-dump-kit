import gdb
import json
import struct
import os
import re
import sys
import time

# queue_decode.py is a sibling file with no gdb dependency, shared with the
# standalone queue_viewer.py offline tool. `source`-ing this file in gdb
# doesn't reliably add its own directory to sys.path, so do it explicitly.
try:
    _SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
except NameError:
    _SCRIPT_DIR = "/home/liangzh/umr/gpu-dump-kit/rocgdb-dump"
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import queue_decode as qd


class GdbReader:
    """queue_decode reader interface backed by a live gdb.Inferior."""

    def __init__(self, inferior):
        self.inferior = inferior

    def read(self, addr, size):
        try:
            return self.inferior.read_memory(addr, size).tobytes()
        except gdb.MemoryError as e:
            raise qd.MemoryReadError(str(e))


def _symbol_lookup(addr):
    """Resolve a kernel_object address to a name via the live process's
    symbol table. Returns None (decoder just shows the raw address) if gdb
    can't resolve it."""
    try:
        return gdb.execute(f"info symbol 0x{addr:x}", to_string=True).split(" in ")[0]
    except gdb.error:
        return None


def decode_hsa_queue(inferior, base, start_idx, end_idx, emit=print):
    """Decode HSA AQL packets [start_idx, end_idx) (64-byte slots) at base.
    Thin wrapper around the shared queue_decode.decode_hsa_packets()."""
    qd.decode_hsa_packets(
        GdbReader(inferior), base, start_idx, end_idx, emit=emit, symbol_lookup=_symbol_lookup
    )


def decode_sdma_queue(inferior, base, max_size, emit=print):
    """Walk and decode SDMA packets at base, up to max_size bytes.
    Thin wrapper around the shared queue_decode.decode_sdma_packets()."""
    qd.decode_sdma_packets(GdbReader(inferior), base, max_size, emit=emit)


class DumpHsaQueue(gdb.Command):
    """Dump HSA queue"""

    def __init__(self):
        super(DumpHsaQueue, self).__init__("dump_hsa_queue", gdb.COMMAND_USER)

    def invoke(self, arg, from_tty):
        args = gdb.string_to_argv(arg)
        if len(args) != 3 and len(args) != 4:
            print("usage: dump_hsa_queue <queue> <start> <end> [size_bytes]")
            return

        base = int(gdb.parse_and_eval(args[0]))
        start_idx = int(gdb.parse_and_eval(args[1]))
        end_idx = int(gdb.parse_and_eval(args[2]))
        size_bytes = 0 if len(args) == 3 else int(gdb.parse_and_eval(args[3]))
        mod = size_bytes // 64

        start_idx %= mod
        end_idx %= mod

        assert start_idx < end_idx

        inferior = gdb.selected_inferior()
        decode_hsa_queue(inferior, base, start_idx, end_idx, emit=print)


DumpHsaQueue()

class DumpHsaQueueSearch(gdb.Command):
    """Dump HSA queue"""

    def __init__(self):
        super(DumpHsaQueueSearch, self).__init__("dump_hsa_queue_search", gdb.COMMAND_USER)

    def invoke(self, arg, from_tty):
        args = gdb.string_to_argv(arg)
        if len(args) != 3 and len(args) != 4:
            print("usage: dump_hsa_queue_search <queue> <start> <end> [size_bytes] <signal>")
            return

        base = int(gdb.parse_and_eval(args[0]))
        start_idx = int(gdb.parse_and_eval(args[1]))
        end_idx = int(gdb.parse_and_eval(args[2]))
        size_bytes = 0 if len(args) == 3 else int(gdb.parse_and_eval(args[3]))
        target_signal = int(gdb.parse_and_eval(args[4]))
        mod = size_bytes // 64
        
        start_idx %= mod
        end_idx %= mod
        
        assert start_idx < end_idx

        inferior = gdb.selected_inferior()

        for i in range(start_idx, end_idx):
            addr = base + i * 64
            try:
                data = inferior.read_memory(addr, 64).tobytes()
            except gdb.MemoryError:
                print(f"Cannot read memory at 0x{addr:x}")
                continue

            (completion_signal,) = struct.unpack_from("<Q", data, 56)
            
            if completion_signal == target_signal:
                print("-" * 30)
                print(f"  packet_idx={i} completion_signal=0x{completion_signal:x}")


DumpHsaQueueSearch()

class DumpHsaSignal(gdb.Command):
    """Dump HSA signal"""

    def __init__(self):
        super(DumpHsaSignal, self).__init__("dump_hsa_signal", gdb.COMMAND_USER)

    def invoke(self, arg, from_tty):
        args = gdb.string_to_argv(arg)

        if len(args) != 1:
            print("usage: dump_hsa_signal <signal>")
            return

        addr = int(gdb.parse_and_eval(args[0]))

        inferior = gdb.selected_inferior()

        try:
            data = inferior.read_memory(addr, 64).tobytes()
        except gdb.MemoryError:
            print(f"Cannot read memory at 0x{addr:x}")
            return
        try:
            (
                kind,
                value,
                mailbox_ptr,
                event_id,
                start_ts,
                end_ts,
                queue_ptr,
            ) = struct.unpack_from("<qQ QI xxxx QQ Q", data, 0)
            kind= {0: "invalid(0)", 1: "user(1)", -1: "doorbell(-1)", -2: "legacy(-2)"}.get(
                kind, kind
            )
            print(f"Signal at 0x{addr:x}:")
            print("Signal Fields:")
            print(f"  kind={kind}")
            print(f"  value={value}")
            print(f"  mailbox_ptr=0x{mailbox_ptr:x}")
            print(f"  event_id={event_id}")
            print(f"  start_ts={start_ts}, end_ts={end_ts}")
            print(f"  queue_ptr=0x{queue_ptr:x}")
        except struct.error as e:
            print(e)
            print("  Failed to decode hsa signal")


DumpHsaSignal()

class ModifyHsaSignal(gdb.Command):
    """
    ModifyHsaSignal : modify_hsa_signal <signal> value
    """
    
    def __init__(self):
        super(ModifyHsaSignal, self).__init__("modify_hsa_signal", gdb.COMMAND_USER)
        
    def invoke(self, arg, from_tty):
        args = gdb.string_to_argv(arg)
        if len(args) != 2:
            print("usage: modify_hsa_signal <signal> value")
            return
        
        signal_addr = int(gdb.parse_and_eval(args[0]))
        val = int(gdb.parse_and_eval(args[1]))
        inferior = gdb.selected_inferior()

        try:
            data = inferior.read_memory(signal_addr, 64).tobytes()
        except gdb.MemoryError:
            print(f"Cannot read memory at 0x{signal_addr:x}")
            return
        
        try:
            (
                kind,
                value,
                mailbox_ptr,
                event_id,
                start_ts,
                end_ts,
                queue_ptr,
            ) = struct.unpack_from("<qQ QI xxxx QQ Q", data, 0)
            kind= {0: "invalid(0)", 1: "user(1)", -1: "doorbell(-1)", -2: "legacy(-2)"}.get(
                kind, kind
            )
            print(f"Signal at 0x{signal_addr:x}:")
            print("Signal Fields:")
            print(f"  kind={kind}")
            print(f"  value={value}")
            print(f"  mailbox_ptr=0x{mailbox_ptr:x}")
            print(f"  event_id={event_id}")
            print(f"  start_ts={start_ts}, end_ts={end_ts}")
            print(f"  queue_ptr=0x{queue_ptr:x}")
        except struct.error as e:
            print(e)
            print("  Failed to decode hsa signal")
            return
        
        # Modify the value field (offset 8 bytes for the 'value' field)
        try:
            new_data = bytearray(data)
            struct.pack_into("<Q", new_data, 8, val)
            inferior.write_memory(signal_addr, new_data)
            print(f" Modified signal at 0x{signal_addr:x} - value changed from {value} to {val}")
            
            # Verify the change
            # verify_data = inferior.read_memory(signal_addr, 64).tobytes()
            # new_value = struct.unpack_from("<Q", verify_data, 8)[0]
            # print(f"Verified new value: {new_value}")
            
        except gdb.MemoryError:
            print(f"Cannot write memory at 0x{signal_addr:x}")
        except Exception as e:
            print(f"Error modifying signal: {e}")
        

ModifyHsaSignal()

class DumpSdmaQueue(gdb.Command):
    """Dump SDMA queue"""

    def __init__(self):
        super(DumpSdmaQueue, self).__init__("dump_sdma_queue", gdb.COMMAND_USER)

    def invoke(self, arg, from_tty):
        args = gdb.string_to_argv(arg)
        if len(args) != 1 and len(args) != 2:
            print("usage: dump_sdma_queue <queue> [max_size]")
            return

        base = int(gdb.parse_and_eval(args[0]))
        max_size = 1024 * 1024 if len(args) == 1 else int(gdb.parse_and_eval(args[1]))

        inferior = gdb.selected_inferior()
        decode_sdma_queue(inferior, base, max_size, emit=print)


DumpSdmaQueue()

class DumpQueueMemory(gdb.Command):
    """Dump Queue Memory
       dump_queue_memory binary queue_1 0x000000324434 1048576
    """
    FORMATS = {
        'binary': 'binary',
        'ihex': 'ihex', 
        'srec': 'srec',
        'tekhex': 'tekhex',
        'verilog': 'verilog'
    }
    
    def __init__(self):
        super(DumpQueueMemory, self).__init__("dump_queue_memory", gdb.COMMAND_USER)
    
    def _execute_gdb_command(self, command: str) -> str:
        try:
            result = gdb.execute(command, to_string=True)
            return result.strip()
        except gdb.error as e:
            raise RuntimeError(f"failed: {command}\n error: {e}")
        
    def invoke(self, arg, from_tty):
        args = gdb.string_to_argv(arg)
        if len(args) != 4:
            print("usage: dump_queue_memory [format] [filename] <queue> [size_bytes]")
            return
        
        format_type = str(args[0])
        filename = str(args[1])
        start_addr = int(gdb.parse_and_eval(args[2]))
        end_addr =  start_addr + int(gdb.parse_and_eval(args[3]))
  
        command = f"dump {self.FORMATS[format_type]} memory {filename} {start_addr:#x} {end_addr:#x}"
        
        try:
            result = self._execute_gdb_command(command)
            if os.path.exists(filename) and os.path.getsize(filename) > 0:
                print(f"success: {filename} (size: {os.path.getsize(filename)} bytes)")
            else:
                print("warning: {filename} is empty or save memory to file failed")
        except Exception as e:
            print(f"{e}")

DumpQueueMemory()


_INFO_QUEUE_ROW_RE = re.compile(
    r"^\s*(\d+)\s+(AMDGPU Queue \S+ \(QID (\d+)\))\s*(.*)$"
)

# Queue types decoded with decode_sdma_queue() -- same SDMA packet format,
# just different physical transport (PCIe DMA vs cross-die XGMI interconnect).
# Add new engine-type strings here as they show up in `info queue` output.
_SDMA_LIKE_TYPES = {"DMA", "XGMI"}


def parse_info_queue(text=None):
    """Parse `info queue` into a list of dicts:
    {id, target_id, qid, type ('HSA'/'DMA'/'XGMI'/...), read, write, size, addr}.

    `info queue` is the only source for this -- rocgdb has no Python API for
    queues (checked: gdb.Inferior has no queue-related attributes).

    text: pre-captured `info queue`/`info queues` output to parse instead of
    executing the command again -- used by _capture_and_patch_info_queues so
    the exact same captured text is both parsed and (after enrichment)
    patched and saved, rather than running the command twice and risking the
    two runs seeing different state. Defaults to executing the command
    itself when omitted, preserving this function's original standalone
    behavior.

    The columns are NOT reliably separated by a fixed number of spaces: gdb
    pads "Target Id" to a minimum width, but once the string itself (which
    grows with the Id/QID's digit count, e.g. "Queue 7:10 (QID 23)" vs
    "Queue 7:1 (QID 32)") reaches that width, the gap to the next column
    collapses to a single space -- splitting on "2+ spaces" silently merges
    "Target Id" and "Type" together on wider rows and misreads Type as the
    Read value. To avoid that, anchor on the fixed "AMDGPU Queue ... (QID N)"
    shape of Target Id via regex, then plain-whitespace-split whatever
    remains (Type [Read Write] Size Address), where no field itself contains
    embedded spaces so a plain split is unambiguous.
    """
    if text is None:
        text = gdb.execute("info queue", to_string=True)
    rows = []
    for line in text.splitlines():
        m = _INFO_QUEUE_ROW_RE.match(line)
        if not m:
            continue
        row_id, target_id, qid = m.group(1), m.group(2), m.group(3)
        rest = m.group(4).split()

        try:
            if len(rest) == 5:
                qtype, read_s, write_s, size_s, addr_s = rest
                read, write = int(read_s), int(write_s)
            elif len(rest) == 3:
                qtype, size_s, addr_s = rest
                read = write = None
            else:
                continue
            size = int(size_s)
            addr = int(addr_s, 16)
        except (ValueError, IndexError):
            # doesn't look like a data row after all -- skip rather than abort
            continue

        qid = int(qid)
        rows.append(
            {
                "id": row_id,
                "target_id": target_id,
                "qid": qid,
                "type": qtype,
                "read": read,
                "write": write,
                "size": size,
                "addr": addr,
            }
        )
    return rows


_MQD_PROCESS_RE = re.compile(r"^Process (\d+) PASID \d+:\s*$")
_MQD_QUEUE_TAG_RE = re.compile(r"^\s*(\S+) queue on device ([0-9a-fA-F]+)\s*$")
_MQD_HEX_LINE_RE = re.compile(r"^\s*[0-9a-fA-F]+:\s+((?:[0-9a-fA-F]{8}\s*)+)$")

# Per-generation SDMA MQD field offsets (dword indices), transcribed from
# UMR's umr/src/lib/lowlevel/linux/parse_clientid.c (init_gfx9_queue /
# init_gfx10_queue / init_gfx11_queue / init_gfx12_queue, case UMR_QUEUE_SDMA).
# rb_base is identical across all four generations; only the RPTR/WPTR
# report-address word indices -- and their lo/hi ordering -- differ, which is
# why this is a literal transcription rather than a derived/guessed table:
# gfx11 and gfx12 each swap the lo/hi order differently from gfx9/10 and from
# each other. Verified against real hardware for gfx_maj=9 only (see the
# design doc for this feature); gfx10/11/12 rows are transcribed but not
# independently hardware-verified.
_SDMA_MQD_OFFSETS = {
    9:  {"rptr": (8, 9), "wptr": (28, 29)},
    10: {"rptr": (8, 9), "wptr": (28, 29)},
    11: {"rptr": (7, 8), "wptr": (26, 27)},
    12: {"rptr": (8, 7), "wptr": (25, 24)},
}


def _mqd_rb_base(mqdwords):
    """sdmax_rlcx_rb_base -- identical dword indices across gfx9-12."""
    return ((mqdwords[2] << 32) | mqdwords[1]) << 8


def _resolve_gfx_maj(device_hex):
    """Resolve an MQD 'on device <hex>' tag (a KFD gpu_id, hex) to the GFX IP
    major version of that GPU -- entirely via root-free kernel sysfs, no umr
    binary and no ASIC database. Chain: gpu_id -> matching KFD topology node
    -> its drm_render_minor -> /sys/class/drm/renderD<N>/device -> the
    card<M> whose own device symlink resolves to that same path -> that
    card's ip_discovery GC major. Returns None if any step fails (e.g. an
    older kernel without ip_discovery, or no matching topology node)."""
    try:
        gpu_id = int(device_hex, 16)
    except ValueError:
        return None

    topo_root = "/sys/class/kfd/kfd/topology/nodes"
    render_minor = None
    try:
        for node in os.listdir(topo_root):
            gpu_id_path = os.path.join(topo_root, node, "gpu_id")
            try:
                with open(gpu_id_path) as f:
                    if int(f.read().strip()) != gpu_id:
                        continue
            except (OSError, ValueError):
                continue
            with open(os.path.join(topo_root, node, "properties")) as f:
                for line in f:
                    parts = line.split()
                    if len(parts) == 2 and parts[0] == "drm_render_minor":
                        render_minor = int(parts[1])
                        break
            break
    except OSError:
        return None
    if render_minor is None:
        return None

    render_device = os.path.realpath(f"/sys/class/drm/renderD{render_minor}/device")
    try:
        for card in os.listdir("/sys/class/drm"):
            if not re.match(r"^card\d+$", card):
                continue
            card_device = os.path.realpath(f"/sys/class/drm/{card}/device")
            if card_device == render_device:
                major_path = f"/sys/class/drm/{card}/device/ip_discovery/die/0/GC/0/major"
                with open(major_path) as f:
                    return int(f.read().strip())
    except OSError:
        return None
    return None


def _parse_mqds_sdma_queues():
    """Parse /sys/kernel/debug/kfd/mqds for every SDMA queue on the system.
    Returns a list of (device_hex, mqdwords) tuples -- raw dword arrays, one
    per SDMA queue block found (there may be several, and several may share
    the same device_hex). Returns [] if there are no SDMA queues anywhere;
    raises OSError/PermissionError if the file itself can't be read (root
    required) -- the caller decides how to report that.

    Deliberately NOT filtered by pid: `/sys/kernel/debug/kfd/mqds` is read
    via the host's KFD debugfs and reports the process's real, HOST-level
    pid -- but when rocgdb runs inside a container (its own pid namespace,
    no `--pid=host`), `gdb.selected_inferior().pid` is a container-local pid
    that generally has no relationship to that host pid at all (e.g. the
    containerized process may see itself as pid 1, while debugfs reports
    some large host pid like 158222). There's no unprivileged way to
    recover the host pid from inside the container's own namespace, so
    filtering by pid here would silently match nothing. Instead, every
    SDMA queue on the system is parsed and matched against our own rows
    purely by ring base address in `_enrich_sdma_pointers()` -- addresses
    are effectively unique across processes (huge, ASLR'd/mmap'd VAs), so
    this is safe and sidesteps the pid-namespace problem entirely."""
    with open("/sys/kernel/debug/kfd/mqds") as f:
        text = f.read()

    queues = []
    cur_device = None
    cur_words = None

    for line in text.splitlines():
        if _MQD_PROCESS_RE.match(line):
            if cur_device is not None:
                queues.append((cur_device, cur_words))
                cur_device = None
            continue

        m = _MQD_QUEUE_TAG_RE.match(line)
        if m:
            if cur_device is not None:
                queues.append((cur_device, cur_words))
            qtype, dev_hex = m.group(1), m.group(2)
            if qtype == "SDMA":
                cur_device, cur_words = dev_hex, []
            else:
                cur_device, cur_words = None, None
            continue

        if cur_device is not None:
            m = _MQD_HEX_LINE_RE.match(line)
            if m:
                cur_words.extend(int(w, 16) for w in m.group(1).split())

    if cur_device is not None:
        queues.append((cur_device, cur_words))
    return queues


def _enrich_sdma_pointers(rows):
    """Best-effort: fill in read/write for DMA/XGMI rows by parsing the SDMA
    queue's MQD straight out of KFD debugfs -- the same data UMR's
    --list-uq/--print-uq are built on (see this feature's design doc for how
    the field offsets and the address-matching approach were derived and
    verified against real hardware). Mutates `rows` in place; silently
    leaves rows unchanged (still None) on any failure -- this must never
    abort a dump.

    Storage convention matches HSA's read/write exactly: a raw, un-wrapped
    dword counter (byte offset = value * 4 before wrapping) -- NOT yet
    reduced modulo the ring size. `queue_viewer.py` wraps it to a ring
    position at use time, same as it already does for HSA's raw AQL
    packet-ID counter. IMPORTANT: the *unit* still differs from HSA -- this
    is a dword position, not a monotonic packet ID, since SDMA packets are
    variable-length and there's no equivalent "packet index" concept at the
    hardware level. Do not conflate the two when consuming this field.

    Residual caveat: matching is purely by ring base virtual address across
    every SDMA queue on the (possibly shared, multi-tenant) system, since
    there's no reliable pid to filter by (see _parse_mqds_sdma_queues's
    docstring). Virtual addresses are per-process, so it's theoretically
    possible -- if very unlikely, given these are high-entropy mmap'd
    addresses -- for a *different* process's queue to coincidentally share
    the same rb_base as one of ours. Because the actual memory read only
    ever goes through *our* attached inferior (ptrace enforces this; we can
    never read another process's memory even if its address coincidentally
    matches), such a collision would silently attribute an unrelated,
    successfully-read value to our queue rather than erroring out. Accepted
    as a low-probability trade-off for a best-effort diagnostic feature.
    """
    sdma_rows = [r for r in rows if r["type"] in _SDMA_LIKE_TYPES and r["read"] is None]
    if not sdma_rows:
        return

    try:
        mqd_queues = _parse_mqds_sdma_queues()
    except PermissionError:
        print(
            "SDMA rptr/wptr enrichment skipped: no permission to read "
            "/sys/kernel/debug/kfd/mqds (need root)"
        )
        return
    except OSError as e:
        print(f"SDMA rptr/wptr enrichment skipped: {e}")
        return

    if not mqd_queues:
        return

    # `_parse_mqds_sdma_queues()` is system-wide (see its docstring) -- on a
    # shared host, most of what it finds belongs to OTHER processes we have
    # no ptrace access to. Computing rb_base is cheap, pure arithmetic on the
    # MQD words (no memory read, no generation lookup needed -- rb_base's
    # offset is identical across every generation we support), so narrow
    # down to only the queues whose rb_base matches one of our own rows
    # *before* attempting the (fallible, and for everyone else's queues,
    # guaranteed-to-fail) rptr/wptr memory reads. Without this, a busy host
    # prints a wall of expected "Cannot access memory" failures for queues
    # that were never going to be ours.
    wanted_addrs = {row["addr"] for row in sdma_rows}
    candidates = []
    for device_hex, mqdwords in mqd_queues:
        try:
            rb_base = _mqd_rb_base(mqdwords)
        except IndexError:
            continue
        if rb_base in wanted_addrs:
            candidates.append((device_hex, mqdwords, rb_base))

    if not candidates:
        return

    gfx_maj_cache = {}
    inferior = gdb.selected_inferior()
    pointers_by_addr = {}
    warned_gens = set()

    for device_hex, mqdwords, rb_base in candidates:
        try:
            if device_hex not in gfx_maj_cache:
                gfx_maj_cache[device_hex] = _resolve_gfx_maj(device_hex)
            gfx_maj = gfx_maj_cache[device_hex]

            if gfx_maj not in _SDMA_MQD_OFFSETS:
                if device_hex not in warned_gens:
                    warned_gens.add(device_hex)
                    if gfx_maj is None:
                        print(
                            f"SDMA rptr/wptr enrichment: could not determine "
                            f"GPU generation for device {device_hex}, skipping"
                        )
                    else:
                        print(
                            f"SDMA rptr/wptr enrichment: unsupported gfx_maj="
                            f"{gfx_maj} for device {device_hex}, skipping"
                        )
                continue

            offsets = _SDMA_MQD_OFFSETS[gfx_maj]

            r_hi, r_lo = offsets["rptr"]
            rptr_addr = ((mqdwords[r_hi] << 32) | mqdwords[r_lo]) & ~7

            w_hi, w_lo = offsets["wptr"]
            wptr_addr = (mqdwords[w_hi] << 32) | mqdwords[w_lo]
            if wptr_addr == 0:
                # MQD doesn't carry an explicit WPTR poll address here -- the
                # live WPTR immediately precedes RPTR in this layout (mirrors
                # UMR's legacy-KFD fallback; see design doc).
                wptr_addr = rptr_addr - 8

            rptr_raw = struct.unpack("<Q", inferior.read_memory(rptr_addr, 8).tobytes())[0]
            wptr_raw = struct.unpack("<Q", inferior.read_memory(wptr_addr, 8).tobytes())[0]
            pointers_by_addr[rb_base] = (rptr_raw >> 2, wptr_raw >> 2)
        except (gdb.MemoryError, IndexError, struct.error) as e:
            print(f"SDMA rptr/wptr enrichment: failed for device {device_hex}: {e}")
            continue

    for row in sdma_rows:
        hit = pointers_by_addr.get(row["addr"])
        if hit is None:
            continue
        # Store the raw, un-wrapped dword counter -- same convention as
        # HSA's read/write (a raw AQL packet-ID counter, e.g. "read: 438427"
        # on a 16384-slot ring). Wrapping to a ring-relative position
        # happens later, at use time, in queue_viewer.py -- not here.
        row["read"] = hit[0]
        row["write"] = hit[1]


_INFO_QUEUES_HEADER_COLS = ("Type", "Read", "Write", "Size")


def _info_queues_field_widths(header_line):
    """Return (type_width, read_width, write_width), derived from where each
    of _INFO_QUEUES_HEADER_COLS starts in `info queues`'s own header line.

    gdb left-justifies every data row's Type/Read/Write value to exactly the
    width implied by these header word positions -- verified empirically
    against real captures: Size/Address always start at the same column
    whether or not a given row's Read/Write happen to be blank (DMA/XGMI) or
    filled in (HSA), and this holds across both narrow (single-digit) and
    wide (6-digit) Read/Write values. These widths are therefore exactly
    what's needed to reconstruct a patched row that lines up with the rest
    of the table. Returns None if the header doesn't have the expected
    column words in order (unexpected rocgdb version/format) -- callers
    should skip patching rather than guess.
    """
    positions = []
    pos = 0
    for col in _INFO_QUEUES_HEADER_COLS:
        idx = header_line.find(col, pos)
        if idx == -1:
            return None
        positions.append(idx)
        pos = idx + len(col)
    type_pos, read_pos, write_pos, size_pos = positions
    return read_pos - type_pos, write_pos - read_pos, size_pos - write_pos


def _patch_info_queues_text(text, rows):
    """Rewrite rocgdb's own `info queues` output, filling in Read/Write for
    DMA/XGMI rows that _enrich_sdma_pointers() successfully resolved -- IN
    PLACE, in the same Type/Read/Write/Size/Address table rocgdb itself
    printed (matching column widths, not a separate appended section).

    Deliberately does not rely on any *fixed* column offset for where a
    row's Type text starts: `parse_info_queue`'s docstring already explains
    that Target Id's padding can collapse for wide QID numbers, shifting
    where Type begins on that specific row. Instead, each row's own Type
    start position comes from the same regex match used to parse it
    (`m.start(4)`, i.e. wherever that row's own Target Id actually ended),
    and only the *widths* of the Type/Read/Write fields (derived once from
    the header) are reused across rows -- so a row with a long Target Id
    still gets patched at the right place. A row is only ever patched if
    it's independently confirmed to have been blank in the original text
    (three tokens: Type, Size, Address -- no Read/Write) and enrichment
    found something for it. As a last defensive check, the reconstructed
    line's Size field is compared against the row's already-parsed size
    before committing -- if it doesn't match (meaning the width assumptions
    didn't hold for this line, for whatever reason), that one line is left
    untouched rather than risk corrupting it. Returns `text` completely
    unchanged if the header doesn't have the expected column shape.
    """
    lines = text.splitlines(keepends=True)
    if not lines:
        return text

    widths = _info_queues_field_widths(lines[0])
    if widths is None:
        return text
    type_width, read_width, write_width = widths

    def pad(s, width):
        # Left-justify to the header-derived width as usual, BUT if a value
        # is as wide as (or wider than) its column -- e.g. a large raw SDMA
        # dword position -- .ljust() alone would add no padding at all,
        # gluing it directly onto the next field with zero separating space
        # (a real bug caught in testing: Write "2000000" immediately
        # followed by Size "8388608" rendered as one unparseable number,
        # "20000008388608"). Guarantee at least one space in that case,
        # sacrificing column alignment for that one field rather than
        # corrupting the row.
        return s.ljust(width) if len(s) < width else s + " "

    by_target_id = {r["target_id"]: r for r in rows}

    out = [lines[0]]
    for line in lines[1:]:
        m = _INFO_QUEUE_ROW_RE.match(line)
        row = by_target_id.get(m.group(2)) if m else None
        rest = m.group(4).split() if m else []
        if (
            row is not None
            and row["type"] in _SDMA_LIKE_TYPES
            and row["read"] is not None
            and len(rest) == 3  # Type, Size, Address only -- i.e. still blank
        ):
            type_start = m.start(4)
            read_start = type_start + type_width
            write_start = read_start + read_width
            size_start = write_start + write_width
            tail = line[size_start:]
            if tail.lstrip().startswith(str(row["size"])):
                line = (
                    line[:type_start]
                    + pad(row["type"], type_width)
                    + pad(str(row["read"]), read_width)
                    + pad(str(row["write"]), write_width)
                    + tail
                )
        out.append(line)
    return "".join(out)


def _capture_and_patch_info_queues(out_dir):
    """Capture rocgdb's own `info queues` output, parse it into rows, run the
    best-effort SDMA rptr/wptr enrichment (_enrich_sdma_pointers) for DMA/
    XGMI rows, patch any successfully-enriched values directly into the
    Read/Write columns of the saved table (_patch_info_queues_text), and
    write the result to out_dir/info_queues.log. The command is captured
    exactly once and reused for both parsing and the file that gets saved
    (rather than running `info queue(s)` twice and risking the two runs
    seeing different state).

    Returns (path, rows): path is None if the command itself failed to
    execute or the file couldn't be written (rows is still returned/usable
    for the rest of the dump either way -- info_queues.log just won't have
    them saved to it in that case).
    """
    try:
        text = gdb.execute("info queues", to_string=True)
    except Exception as e:
        print(f"Failed to capture 'info queues': {e}")
        return None, []

    rows = parse_info_queue(text)
    _enrich_sdma_pointers(rows)
    text = _patch_info_queues_text(text, rows)

    path = os.path.join(out_dir, "info_queues.log")
    try:
        with open(path, "w") as f:
            f.write(text)
    except OSError as e:
        print(f"Failed to write info_queues.log: {e}")
        return None, rows
    return path, rows


_TARGET_ID_QID_SUFFIX_RE = re.compile(r"\s*\(QID \d+\)\s*$")
_TARGET_ID_RE = re.compile(r"^AMDGPU Queue (\d+):(\d+)\s*\(QID \d+\)\s*$")


def _sanitize_target_id(target_id):
    """Turn a target_id like 'AMDGPU Queue 5:27 (QID 6)' into a
    filesystem-safe fragment ('GPU_5_Queue_27') for use in dump filenames.
    The QID is dropped since it's already encoded separately in the
    filename (e.g. 'QID6')."""
    m = _TARGET_ID_RE.match(target_id)
    if m:
        gpu, queue = m.group(1), m.group(2)
        return f"GPU_{gpu}_Queue_{queue}"
    # Fallback for anything that doesn't match the expected shape -- sanitize
    # generically rather than failing outright.
    stem = _TARGET_ID_QID_SUFFIX_RE.sub("", target_id)
    safe = re.sub(r"[^A-Za-z0-9]+", "_", stem).strip("_")
    return safe or "unknown"


def _capture_gdb_command(out_dir, filename, command):
    """Run a gdb command and save its output to out_dir/filename. Returns the
    path on success, None on failure (failure is reported but non-fatal --
    dumping queues shouldn't abort just because one extra info command
    failed)."""
    path = os.path.join(out_dir, filename)
    try:
        text = gdb.execute(command, to_string=True)
        with open(path, "w") as f:
            f.write(text)
        return path
    except Exception as e:
        print(f"Failed to capture '{command}': {e}")
        return None


def write_dump_summary(out_dir, summary):
    """Write dump_summary.json in out_dir, recording what kinds of data this
    dump_all_queues/dump_all_queues_txt run actually captured (queue counts
    and files by type, backtrace/info-command captures, failures)."""
    path = os.path.join(out_dir, "dump_summary.json")
    try:
        with open(path, "w") as f:
            json.dump(summary, f, indent=2)
            f.write("\n")
    except OSError as e:
        print(f"Failed to write dump summary: {e}")
        return None
    return path


class DumpAllQueues(gdb.Command):
    """Automatically dump every HSA/DMA queue (full ring, text decode) plus
    all-thread backtraces into a directory -- no manual info-queue copy/paste.
    Decodes every packet to text while attached live -- see dump_all_queues
    (the binary/fast dump command) for a much faster alternative on a hung
    process, since this one round-trips through gdb's memory channel
    packet-by-packet.

    dump_all_queues_txt [output_dir]
    """

    def __init__(self):
        super(DumpAllQueues, self).__init__("dump_all_queues_txt", gdb.COMMAND_USER)

    def invoke(self, arg, from_tty):
        args = gdb.string_to_argv(arg)
        if len(args) > 1:
            print("usage: dump_all_queues_txt [output_dir]")
            return

        try:
            pid = gdb.selected_inferior().pid
        except Exception:
            pid = 0

        try:
            with open(f"/proc/{pid}/comm") as f:
                comm = f.read().strip()
        except OSError:
            comm = "unknown"

        try:
            host = os.uname().nodename
        except Exception:
            host = "unknown"

        out_dir = (
            args[0]
            if args
            else f"rocgdb_dump_pid{pid}_{time.strftime('%Y%m%d_%H%M%S')}"
        )
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError as e:
            print(f"Cannot create output directory {out_dir}: {e}")
            return

        dump_time = time.strftime("%Y-%m-%dT%H:%M:%S")

        info_dispatches_path = _capture_gdb_command(
            out_dir, "info_dispatches.log", "info dispatches -full"
        )
        info_queues_path, rows = _capture_and_patch_info_queues(out_dir)
        if not rows:
            print(
                "No queues found (is a process attached? try 'info queue' directly to check) "
                "-- still capturing backtraces"
            )

        inferior = gdb.selected_inferior()
        hsa_count = 0
        dma_count = 0
        failures = []
        dumped_files = []

        for row in rows:
            qid = row["qid"]
            qtype = row["type"]
            addr = row["addr"]
            size = row["size"]
            label = f"QID{qid}"
            target_frag = _sanitize_target_id(row["target_id"])

            if qtype == "HSA":
                filename = os.path.join(out_dir, f"hsa_{label}_{target_frag}.log")
            elif qtype in _SDMA_LIKE_TYPES:
                # DMA and XGMI are both SDMA-engine rings (XGMI is just the
                # cross-die-interconnect variant of the same DMA engine) --
                # same packet format, decode both the same way.
                filename = os.path.join(
                    out_dir, f"{qtype.lower()}_{label}_{target_frag}.log"
                )
            else:
                print(f"Skipping unrecognized queue type '{qtype}' for {row['target_id']}")
                continue

            try:
                with open(filename, "w") as f:
                    def emit(line, _f=f):
                        _f.write(str(line) + "\n")

                    emit(f"# {row['target_id']}")
                    emit(
                        f"Dumping full ring for {label} type={qtype} "
                        f"addr=0x{addr:x} size={size} read={row['read']} write={row['write']}"
                    )
                    if qtype == "HSA":
                        decode_hsa_queue(inferior, addr, 0, size // 64, emit=emit)
                        hsa_count += 1
                    else:
                        decode_sdma_queue(inferior, addr, size, emit=emit)
                        dma_count += 1
                dumped_files.append(filename)
            except Exception as e:
                # one bad queue shouldn't stop the rest of the batch
                failures.append((label, qtype, str(e)))
                print(f"Failed to dump {label} ({qtype}): {e}")

        bt_path = os.path.join(out_dir, "backtrace_all_threads.log")
        try:
            bt_text = gdb.execute("thread apply all bt", to_string=True)
            with open(bt_path, "w") as f:
                f.write(bt_text)
        except Exception as e:
            print(f"Failed to capture backtraces: {e}")
            bt_path = None

        summary_path = write_dump_summary(
            out_dir,
            {
                "command": "dump_all_queues_txt",
                "output_dir": out_dir,
                "pid": pid,
                "comm": comm,
                "host": host,
                "dump_time": dump_time,
                "queues": {
                    "hsa": hsa_count,
                    "dma_xgmi": dma_count,
                    "total": hsa_count + dma_count,
                    "files": dumped_files,
                },
                "backtrace_all_threads": bt_path,
                "info_queues": info_queues_path,
                "info_dispatches": info_dispatches_path,
                "failures": [
                    {"label": label, "type": qtype, "error": err}
                    for label, qtype, err in failures
                ],
            },
        )

        print("-" * 30)
        print(f"dump_all_queues_txt complete: {out_dir}")
        print(f"  HSA queues captured: {hsa_count}")
        print(f"  DMA queues captured: {dma_count}")
        if failures:
            print(f"  failures: {len(failures)}")
            for label, qtype, err in failures:
                print(f"    {label} ({qtype}): {err}")
        if bt_path:
            print(f"  backtraces: {bt_path}")
        if info_queues_path:
            print(f"  info queues: {info_queues_path}")
        if info_dispatches_path:
            print(f"  info dispatches: {info_dispatches_path}")
        if summary_path:
            print(f"  summary: {summary_path}")


DumpAllQueues()


class DumpAllQueuesBinary(gdb.Command):
    """Fast binary capture: dump every HSA/DMA/XGMI queue's raw bytes (one
    bulk read per queue, no live packet decode) plus per-queue metadata,
    into one .bin file per queue -- decode them later, offline, with
    queue_viewer.py. Much faster than dump_all_queues_txt on a hung process,
    since nothing round-trips through gdb's memory channel packet-by-packet.

    dump_all_queues [output_dir]
    """

    def __init__(self):
        super(DumpAllQueuesBinary, self).__init__("dump_all_queues", gdb.COMMAND_USER)

    def invoke(self, arg, from_tty):
        args = gdb.string_to_argv(arg)
        if len(args) > 1:
            print("usage: dump_all_queues [output_dir]")
            return

        try:
            pid = gdb.selected_inferior().pid
        except Exception:
            pid = 0

        try:
            with open(f"/proc/{pid}/comm") as f:
                comm = f.read().strip()
        except OSError:
            comm = "unknown"

        try:
            host = os.uname().nodename
        except Exception:
            host = "unknown"

        out_dir = (
            args[0]
            if args
            else f"rocgdb_dump_bin_pid{pid}_{time.strftime('%Y%m%d_%H%M%S')}"
        )
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError as e:
            print(f"Cannot create output directory {out_dir}: {e}")
            return

        info_dispatches_path = _capture_gdb_command(
            out_dir, "info_dispatches.log", "info dispatches -full"
        )
        info_queues_path, rows = _capture_and_patch_info_queues(out_dir)
        if not rows:
            print(
                "No queues found (is a process attached? try 'info queue' directly to check) "
                "-- still capturing backtraces"
            )

        inferior = gdb.selected_inferior()
        reader = GdbReader(inferior)
        dump_time = time.strftime("%Y-%m-%dT%H:%M:%S")
        hsa_count = 0
        dma_count = 0
        failures = []
        dumped_files = []

        for row in rows:
            qid = row["qid"]
            qtype = row["type"]
            addr = row["addr"]
            size = row["size"]
            label = f"QID{qid}"
            target_frag = _sanitize_target_id(row["target_id"])

            if qtype == "HSA":
                filename = os.path.join(out_dir, f"hsa_{label}_{target_frag}.bin")
            elif qtype in _SDMA_LIKE_TYPES:
                filename = os.path.join(
                    out_dir, f"{qtype.lower()}_{label}_{target_frag}.bin"
                )
            else:
                print(f"Skipping unrecognized queue type '{qtype}' for {row['target_id']}")
                continue

            try:
                # one bulk read -- this is the whole point, no per-packet
                # round-trips through gdb's memory channel
                raw = reader.read(addr, size)
                metadata = {
                    "qid": qid,
                    "type": qtype,
                    "target_id": row["target_id"],
                    "addr": addr,
                    "size": size,
                    "read": row["read"],
                    "write": row["write"],
                    "pid": pid,
                    "comm": comm,
                    "host": host,
                    "dump_time": dump_time,
                }
                with open(filename, "wb") as f:
                    qd.write_dump_header(f, metadata)
                    f.write(raw)
                if qtype == "HSA":
                    hsa_count += 1
                else:
                    dma_count += 1
                dumped_files.append(filename)
            except Exception as e:
                # one bad queue shouldn't stop the rest of the batch
                failures.append((label, qtype, str(e)))
                print(f"Failed to dump {label} ({qtype}): {e}")

        bt_path = os.path.join(out_dir, "backtrace_all_threads.log")
        try:
            bt_text = gdb.execute("thread apply all bt", to_string=True)
            with open(bt_path, "w") as f:
                f.write(bt_text)
        except Exception as e:
            print(f"Failed to capture backtraces: {e}")
            bt_path = None

        summary_path = write_dump_summary(
            out_dir,
            {
                "command": "dump_all_queues",
                "output_dir": out_dir,
                "pid": pid,
                "comm": comm,
                "host": host,
                "dump_time": dump_time,
                "queues": {
                    "hsa": hsa_count,
                    "dma_xgmi": dma_count,
                    "total": hsa_count + dma_count,
                    "files": dumped_files,
                },
                "backtrace_all_threads": bt_path,
                "info_queues": info_queues_path,
                "info_dispatches": info_dispatches_path,
                "failures": [
                    {"label": label, "type": qtype, "error": err}
                    for label, qtype, err in failures
                ],
            },
        )

        print("-" * 30)
        print(f"dump_all_queues complete: {out_dir}")
        print(f"  HSA queues captured: {hsa_count}")
        print(f"  DMA/XGMI queues captured: {dma_count}")
        if failures:
            print(f"  failures: {len(failures)}")
            for label, qtype, err in failures:
                print(f"    {label} ({qtype}): {err}")
        if bt_path:
            print(f"  backtraces: {bt_path}")
        if info_queues_path:
            print(f"  info queues: {info_queues_path}")
        if info_dispatches_path:
            print(f"  info dispatches: {info_dispatches_path}")
        if summary_path:
            print(f"  summary: {summary_path}")
        print("view with: python3 queue_viewer.py <output_dir>/<file>.bin")


DumpAllQueuesBinary()