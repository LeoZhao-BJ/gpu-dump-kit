import gdb
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
    _SCRIPT_DIR = "/home/liangzh/umr/debug_tools/rocgdb_info"
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


def parse_info_queue():
    """Parse `info queue` into a list of dicts:
    {id, target_id, qid, type ('HSA'/'DMA'/'XGMI'/...), read, write, size, addr}.

    `info queue` is the only source for this -- rocgdb has no Python API for
    queues (checked: gdb.Inferior has no queue-related attributes).

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


class DumpAllQueues(gdb.Command):
    """Automatically dump every HSA/DMA queue (full ring, text decode) plus
    all-thread backtraces into a directory -- no manual info-queue copy/paste.

    dump_all_queues [output_dir]
    """

    def __init__(self):
        super(DumpAllQueues, self).__init__("dump_all_queues", gdb.COMMAND_USER)

    def invoke(self, arg, from_tty):
        args = gdb.string_to_argv(arg)
        if len(args) > 1:
            print("usage: dump_all_queues [output_dir]")
            return

        try:
            pid = gdb.selected_inferior().pid
        except Exception:
            pid = 0

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

        rows = parse_info_queue()
        if not rows:
            print(
                "No queues found (is a process attached? try 'info queue' directly to check) "
                "-- still capturing backtraces"
            )

        inferior = gdb.selected_inferior()
        hsa_count = 0
        dma_count = 0
        failures = []

        for row in rows:
            qid = row["qid"]
            qtype = row["type"]
            addr = row["addr"]
            size = row["size"]
            label = f"QID{qid}"

            if qtype == "HSA":
                filename = os.path.join(out_dir, f"hsa_queue_{label}.log")
            elif qtype in _SDMA_LIKE_TYPES:
                # DMA and XGMI are both SDMA-engine rings (XGMI is just the
                # cross-die-interconnect variant of the same DMA engine) --
                # same packet format, decode both the same way.
                filename = os.path.join(out_dir, f"{qtype.lower()}_queue_{label}.log")
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

        print("-" * 30)
        print(f"dump_all_queues complete: {out_dir}")
        print(f"  HSA queues captured: {hsa_count}")
        print(f"  DMA queues captured: {dma_count}")
        if failures:
            print(f"  failures: {len(failures)}")
            for label, qtype, err in failures:
                print(f"    {label} ({qtype}): {err}")
        if bt_path:
            print(f"  backtraces: {bt_path}")


DumpAllQueues()


class DumpAllQueuesBinary(gdb.Command):
    """Fast binary capture: dump every HSA/DMA/XGMI queue's raw bytes (one
    bulk read per queue, no live packet decode) plus per-queue metadata,
    into one .bin file per queue -- decode them later, offline, with
    queue_viewer.py. Much faster than dump_all_queues on a hung process,
    since nothing round-trips through gdb's memory channel packet-by-packet.

    dump_all_queues_bin [output_dir]
    """

    def __init__(self):
        super(DumpAllQueuesBinary, self).__init__("dump_all_queues_bin", gdb.COMMAND_USER)

    def invoke(self, arg, from_tty):
        args = gdb.string_to_argv(arg)
        if len(args) > 1:
            print("usage: dump_all_queues_bin [output_dir]")
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

        rows = parse_info_queue()
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

        for row in rows:
            qid = row["qid"]
            qtype = row["type"]
            addr = row["addr"]
            size = row["size"]
            label = f"QID{qid}"

            if qtype == "HSA":
                filename = os.path.join(out_dir, f"hsa_queue_{label}.bin")
            elif qtype in _SDMA_LIKE_TYPES:
                filename = os.path.join(out_dir, f"{qtype.lower()}_queue_{label}.bin")
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

        print("-" * 30)
        print(f"dump_all_queues_bin complete: {out_dir}")
        print(f"  HSA queues captured: {hsa_count}")
        print(f"  DMA/XGMI queues captured: {dma_count}")
        if failures:
            print(f"  failures: {len(failures)}")
            for label, qtype, err in failures:
                print(f"    {label} ({qtype}): {err}")
        if bt_path:
            print(f"  backtraces: {bt_path}")
        print("view with: python3 queue_viewer.py <output_dir>/<file>.bin")


DumpAllQueuesBinary()