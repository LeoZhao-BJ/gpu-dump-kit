"""Shared, gdb-independent HSA/SDMA packet decoding + binary dump format.

This module has NO dependency on gdb -- it's imported both by
`queue_script.py` (running live inside rocgdb's embedded Python interpreter,
via a GdbReader adapter around `inferior.read_memory()`) and by
`queue_viewer.py` (a standalone tool that reads packets out of a binary dump
file on disk, with no gdb or live process involved at all).

Keeping the packet-format knowledge in exactly one place means the live path
and the offline viewer can never drift apart on how a packet is decoded, and
keeping the binary dump container format (write_dump_header/read_dump_header
below) here too means the writer (queue_script.py) and reader
(queue_viewer.py) can never disagree on the file layout either.

Callers of decode_hsa_packets/decode_sdma_packets provide a `reader`: any
object with a `.read(addr, size) -> bytes` method that raises
`MemoryReadError` on failure (out-of-range, unmapped, etc). `addr` is always
an absolute address in the same address space `base` was given in.
"""

import json
import struct


class MemoryReadError(Exception):
    """Raised by a reader's .read() when the requested bytes aren't available."""


# --- binary dump container format -------------------------------------
#
# One file per queue, written by queue_script.py's dump_all_queues_bin and
# read by queue_viewer.py -- kept here so both sides can never disagree on
# the wire format:
#
#   offset 0:              magic       8 bytes,  b"RGQDUMP1" (format v1)
#   offset 8:               header_len  4 bytes,  uint32 little-endian
#   offset 12:               header_json header_len bytes, UTF-8 JSON
#   offset 12+header_len:    raw ring bytes, exactly metadata["size"] bytes
#
# JSON (rather than a fixed C struct) so new metadata fields can be added
# later without a format/version bump.

MAGIC = b"RGQDUMP1"


def write_dump_header(f, metadata):
    """Write the magic + JSON metadata header to an already-open, writable
    binary file. Caller writes the raw ring bytes immediately afterward."""
    header_bytes = json.dumps(metadata).encode("utf-8")
    f.write(MAGIC)
    f.write(struct.pack("<I", len(header_bytes)))
    f.write(header_bytes)


def read_dump_header(f):
    """Read the magic + JSON metadata header from an already-open, readable
    binary file. Returns the metadata dict; leaves the file position at the
    start of the raw ring bytes. Raises ValueError if this isn't a
    recognized dump (bad magic, truncated header, invalid JSON)."""
    magic = f.read(len(MAGIC))
    if magic != MAGIC:
        raise ValueError(f"Not a recognized queue dump (bad magic: {magic!r})")
    len_bytes = f.read(4)
    if len(len_bytes) != 4:
        raise ValueError("Truncated dump: missing header length")
    (header_len,) = struct.unpack("<I", len_bytes)
    header_bytes = f.read(header_len)
    if len(header_bytes) != header_len:
        raise ValueError("Truncated dump: header shorter than declared length")
    try:
        return json.loads(header_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError(f"Corrupt dump header: {e}")


def decode_hsa_packets(reader, base, start_idx, end_idx, emit=print, symbol_lookup=None):
    """Decode HSA AQL packets [start_idx, end_idx) (64-byte slots) at base.

    start_idx/end_idx must already be resolved slot indices (no modulo
    wraparound applied here) -- callers that accept raw/absolute rptr-wptr
    counters are responsible for wrapping them into range first.

    symbol_lookup(addr) -> str | None, when given, is used to resolve a
    kernel_object address to a human-readable name (e.g. via a live
    process's symbol table). When None, kernel dispatch packets just show
    the raw address.
    """
    for i in range(start_idx, end_idx):
        addr = base + i * 64
        try:
            data = reader.read(addr, 64)
        except MemoryReadError:
            emit(f"Cannot read memory at 0x{addr:x}")
            continue

        (header,) = struct.unpack_from("<H", data, 0)
        type_ = header & 0xFF
        (completion_signal,) = struct.unpack_from("<Q", data, 56)

        emit("-" * 30)
        emit(
            f"Packet #{i} at 0x{addr:x}: header=0x{header:04x} (type={type_}, barrier={(header >> 8) & 1}, acquire={(header >> 9) & 3}, release={(header >> 11) & 3})"
        )

        if type_ == 1:
            emit("Invalid packet type, raw dump:")
            emit(" ".join(f"{b:02x}" for b in data[:64]))
            try:
                (second_word,) = struct.unpack_from("<I", data, 4)
                type_ = 2 if second_word != 0 else 3
                emit(f"Read invalid packet as type {type_}\n")
            except struct.error:
                pass

        if type_ == 2:  # Kernel dispatch
            try:
                (
                    setup,
                    wg_x,
                    wg_y,
                    wg_z,
                    grid_x,
                    grid_y,
                    grid_z,
                    pvt,
                    grp,
                    kern_obj,
                    kern_arg,
                ) = struct.unpack_from("<H HHH xx III II QQ", data, 2)
                kern_name = None
                if symbol_lookup is not None:
                    kern_name = symbol_lookup(kern_obj)
                emit("Kernel Dispatch Packet Fields:")
                emit(f"  setup=0x{setup:x}")
                emit(f"  workgroup=[{wg_x},{wg_y},{wg_z}]")
                emit(f"  grid=[{grid_x},{grid_y},{grid_z}]")
                emit(f"  private_segment_size={pvt}, group_segment_size={grp}")
                if kern_name:
                    emit(f'  kernel_object=0x{kern_obj:x} "{kern_name}"')
                else:
                    emit(f"  kernel_object=0x{kern_obj:x}")
                emit(f"  kernarg_address=0x{kern_arg:x}")
            except struct.error:
                emit("  Failed to decode kernel dispatch packet")

        elif type_ in (3, 5):  # Barrier And / Barrier Or
            try:
                dep_signals = struct.unpack_from("<5Q", data, 8)
                emit("Barrier Packet Fields:")
                for j, s in enumerate(dep_signals):
                    emit(f"  dep_signal[{j}]=0x{s:x}")
            except struct.error:
                emit("  Failed to decode barrier packet")

        elif type_ == 4:  # Agent dispatch
            try:
                (agend_type,) = struct.unpack_from("<H", data, 2)
                emit("Agent Dispatch Packet Fields:")
                emit(f"  type=0x{agend_type:x}")
            except struct.error:
                emit("  Failed to decode agent dispatch packet")

        else:
            emit("Unknown packet type, raw dump:")
            emit(" ".join(f"{b:02x}" for b in data[:64]))

        emit(f"  completion_signal=0x{completion_signal:x}")


def decode_sdma_packets(reader, base, max_size, emit=print):
    """Walk and decode SDMA packets starting at base for up to max_size bytes.

    Stops early on a null (op==0) opcode, an unreadable/unknown byte, or
    hitting max_size -- whichever comes first.
    """
    addr = base
    end = base + max_size
    i = 0
    while addr < end:
        try:
            data = reader.read(addr, 1)
        except MemoryReadError:
            emit(f"Cannot read memory at 0x{addr:x}")
            break

        op = data[0]

        if op == 0:
            break

        emit("-" * 30)
        emit(f"Packet #{i} at 0x{addr:x}: op=0x{op:x}")

        if op == 1:  # SDMA_OP_COPY
            try:
                data = reader.read(addr, 28)
            except MemoryReadError:
                emit(f"Cannot read memory at 0x{addr:x}")
                break
            try:
                (
                    sub_op,
                    count,
                    parameter,
                    src_addr,
                    dst_addr,
                ) = struct.unpack_from("<B xx II QQ", data, 1)
                emit("Copy Packet Fields:")
                emit(f"  sub_op={sub_op}")
                emit(f"  count={count + 1}")
                emit(f"  parameter=0x{parameter:x}")
                emit(f"  src_addr=0x{src_addr:x}")
                emit(f"  dst_addr=0x{dst_addr:x}")
            except struct.error:
                emit("  Failed to decode copy packet")
            addr += 28
        elif op == 5:  # SDMA_OP_FENCE
            try:
                data = reader.read(addr, 16)
            except MemoryReadError:
                emit(f"Cannot read memory at 0x{addr:x}")
                break
            try:
                (
                    header,
                    addr_,
                    data_,
                ) = struct.unpack_from("<IQI", data, 0)
                emit("Fence Packet Fields:")
                emit(f"  header={header >> 16}")
                emit(f"  addr=0x{addr_:x}")
                emit(f"  data={data_}")
            except struct.error:
                emit("  Failed to decode fence packet")
            addr += 16
        elif op == 6:  # SDMA_OP_TRAP
            try:
                data = reader.read(addr, 8)
            except MemoryReadError:
                emit(f"Cannot read memory at 0x{addr:x}")
                break
            try:
                (
                    context,
                ) = struct.unpack_from("<I", data, 4)
                emit("Trap Packet Fields:")
                emit(f"  context={context}")
            except struct.error:
                emit("  Failed to decode trap packet")
            addr += 8
        elif op == 8:  # SDMA_OP_POLLREGMEM
            try:
                data = reader.read(addr, 24)
            except MemoryReadError:
                emit(f"Cannot read memory at 0x{addr:x}")
                break
            try:
                (
                    sub_op,
                    addr_,
                    value,
                    mask,
                    interval,
                    retry_count,
                ) = struct.unpack_from("<B xx Q iI HH", data, 1)
                emit("Poll Packet Fields:")
                emit(f"  sub_op={sub_op}")
                emit(f"  addr=0x{addr_:x} #signal(0x{(addr_ - 8):x})")
                emit(f"  value={value}")
                emit(f"  mask=0x{mask:x}")
                emit(f"  interval={interval}")
                emit(f"  retry_count={retry_count}")
            except struct.error:
                emit("  Failed to decode poll packet")
            addr += 24
        elif op == 10:  # SDMA_OP_ATOMIC
            try:
                data = reader.read(addr, 32)
            except MemoryReadError:
                emit(f"Cannot read memory at 0x{addr:x}")
                break
            try:
                (
                    sub_op,
                    op_,
                    addr_,
                    src_data,
                    cmp_data,
                    interval,
                ) = struct.unpack_from("<B x B Q qq I", data, 1)
                emit("Atomic Packet Fields:")
                emit(f"  sub_op={sub_op}")
                emit(f"  op={op_ >> 1}")
                emit(f"  addr=0x{addr_:x}")
                emit(f"  src_data={src_data}")
                emit(f"  cmp_data={cmp_data}")
                emit(f"  interval={interval}")
            except struct.error:
                emit("  Failed to decode atomic packet")
            addr += 32
        else:
            emit("Unknown packet type")
            break
        i += 1
