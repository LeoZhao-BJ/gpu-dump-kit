"""Shared, gdb-independent HSA/SDMA packet decoding + binary dump format.

This module has NO dependency on gdb -- it's imported both by
`rocgdb_helper.py` (running live inside rocgdb's embedded Python interpreter,
via a GdbReader adapter around `inferior.read_memory()`) and by
`queue_viewer.py` (a standalone tool that reads packets out of a binary dump
file on disk, with no gdb or live process involved at all).

Keeping the packet-format knowledge in exactly one place means the live path
and the offline viewer can never drift apart on how a packet is decoded, and
keeping the binary dump container format (write_dump_header/read_dump_header
below) here too means the writer (rocgdb_helper.py) and reader
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
# One file per queue, written by rocgdb_helper.py's dump_all_queues_bin and
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


_POLL_REGMEM_FUNCS = ["always", "<", "<=", "==", "!=", ">=", ">", "N/A"]

# This host's confirmed asic->family bucket (UMR's own internal generation
# enum, distinct from both "GFX IP major" and "SDMA/OSS IP major" -- checked
# via /sys/class/drm/card*/device/ip_discovery, this host is aqua_vanjaram/
# CDNA3 = FAMILY_AI in UMR's umr.h enum SI=0,...,VI,AI,NV,...). Only affects
# a handful of sizes below (COPY.TILED/.TILED_SUB_WINDOW/.T2T_SUB_WINDOW,
# INDIRECT's vmid masking) -- see umr/src/lib/packet/sdma/read_sdma_stream.c.
_SDMA_FAMILY_AI_OR_LATER = True
_SDMA_FAMILY_NV_OR_LATER = False


def _sdma_peek_word(reader, addr, word_idx):
    """Read the (0-based) word_idx'th dword AFTER the header dword at addr
    -- used by the handful of opcodes whose size depends on packet content."""
    off = 4 + word_idx * 4
    data = reader.read(addr, off + 4)
    return struct.unpack_from("<I", data, off)[0]


def _sdma_nwords(reader, addr, op, sub_op, header_dw):
    """How many dwords follow the header dword for this opcode/sub-opcode.
    Port of UMR's sized_oss1_5() (read_sdma_stream.c) -- generation-agnostic
    across SDMA/OSS IP versions 1-6 (this host's SDMA IP major is 4, checked
    via ip_discovery). Returns None for an opcode/sub-opcode this port
    doesn't recognize at all (caller must stop the walk -- there's no way
    to know how many bytes to skip)."""
    if op == 0:  # NOP -- a real, sized NOP is rare; see decode_sdma_packets
        return (header_dw >> 16) & 0x3FFF
    if op == 1:  # COPY
        if sub_op == 0:  # LINEAR
            return 8 if (header_dw & (1 << 27)) else 6  # BROADCAST
        if sub_op == 1:  # TILED
            if header_dw & (3 << 26):  # L2T broadcast / frame-to-field
                return 15 if _SDMA_FAMILY_AI_OR_LATER else 14
            return 12 if _SDMA_FAMILY_AI_OR_LATER else 11
        if sub_op == 3:  # STRUCTURE/SOA
            return 7
        if sub_op == 4:  # LINEAR_SUB_WINDOW
            return 12
        if sub_op == 5:  # TILED_SUB_WINDOW
            return 16 if _SDMA_FAMILY_NV_OR_LATER else 13
        if sub_op == 6:  # T2T_SUB_WINDOW
            return 17 if _SDMA_FAMILY_NV_OR_LATER else 14
        if sub_op == 7:  # DIRTY_PAGE
            return 6
        if sub_op == 8:  # LINEAR_PHY -- size depends on word[0]'s top byte
            w0 = _sdma_peek_word(reader, addr, 0)
            return 6 + 4 * (w0 >> 24)
        if sub_op == 16:  # LINEAR_BC (legacy)
            return 6
        if sub_op == 17:  # TILED_BC (legacy)
            return 15 if (header_dw & (3 << 26)) else 12
        if sub_op == 20:  # LINEAR_SUB_WINDOW_BC (legacy)
            return 12
        if sub_op == 21:  # TILED_SUB_WINDOW_BC (legacy)
            return 13
        if sub_op == 22:  # T2T_SUB_WINDOW_BC (legacy)
            return 14
        if sub_op == 36:  # LINEAR_SUB_WINDOW_LARGE
            return 19
        return None
    if op == 2:  # WRITE
        if sub_op == 0:  # LINEAR -- size depends on word[2]'s count field
            w2 = _sdma_peek_word(reader, addr, 2)
            return 4 + (w2 & 0xFFFFF)
        if sub_op in (1, 2):  # TILED / TILED_BC -- size depends on word[7]
            w7 = _sdma_peek_word(reader, addr, 7)
            return 9 + (w7 & 0xFFFFF)
        return None
    if op == 4:  # INDIRECT
        return 5
    if op == 5:  # FENCE
        return {0: 3, 1: 7, 3: 0}.get(sub_op)
    if op == 6:  # TRAP
        return 1
    if op == 7:  # SEM / MEM_INCR
        return 2
    if op == 8:  # POLL_REGMEM
        return {0: 5, 1: 3, 2: 4, 3: 12, 4: 3}.get(sub_op)
    if op == 9:  # COND_EXE
        return 4
    if op == 10:  # ATOMIC
        return 7
    if op == 11:  # FILL
        return {0: 4, 1: 5}.get(sub_op)
    if op == 12:  # PTE (GEN_PTEPDE / COPY / RMW)
        return {0: 9, 1: 7, 2: 7}.get(sub_op)
    if op == 13:  # TIMESTAMP
        return {0: 2, 1: 2, 2: 2}.get(sub_op)
    if op == 14:  # SRBM_WRITE / RMW_REGISTER
        return {0: 2, 1: 3}.get(sub_op)
    if op == 15:  # PRE_EXE
        return 1
    if op == 16:  # GPUVM_TLB_INV (NV and beyond -- shouldn't occur on this host)
        return 3
    if op == 17:  # GCR
        return 4
    return None


def _sdma_decode_fields(op, sub_op, header_dw, words, emit):
    """Decode and emit the field breakdown for one SDMA packet's body.
    `words[i]` is the dword at byte offset 4+4*i, i.e. immediately after the
    header dword. Returns True if this opcode/sub-opcode was decoded in
    detail; False means the caller should fall back to a generic dump.

    Port of sdma_decode_opcodes.c's decode_upto_ai() -- the one generation
    that matches this host's confirmed SDMA IP version (OSS 4.4, see
    decode_sdma_packets' module docstring) -- plus the handful of opcodes
    (INDIRECT, TRAP, SEM, TIMESTAMP_SET) that decode_upto_ai itself falls
    through to the older, generation-agnostic decode_upto_vi for. Fixed
    constants for OSS 4.4: has_cp_fields=True, has_cpv_flag=False,
    z_mask=0x7FF, pitch_mask=0x7FFFF, pitch_shift=13 (see sdma_decode_opcodes.c's
    sdma_config resolution logic).

    Deliberate deviations from strict 1:1 fidelity, both additive:
      - INDIRECT prints its descriptor fields (addr/vmid/size) even though
        UMR's own AI-generation decoder doesn't bother (see design doc).
      - TRAP keeps a decoded "context" field for the same reason.
    Legacy "_BC" tiling variants (COPY sub-opcodes 16/17/20/21/22/36, WRITE
    sub-opcode 17) are intentionally NOT field-decoded here -- they're
    pre-GCN/early-GCN tiling modes that don't occur on this host's hardware
    in practice, and decode_upto_ai itself only decodes them by falling
    back to decode_upto_vi's legacy tables. They're still sized correctly
    (see _sdma_nwords) so the ring walk never desyncs; the caller shows a
    generic "not decoded in detail" line for these.
    """
    z_mask = 0x7FF
    pitch_mask = 0x7FFFF
    pitch_shift = 13

    def hx(name, value):
        emit(f"  {name}=0x{value:x}")

    def dec(name, value):
        emit(f"  {name}={value}")

    if op == 1:  # COPY
        if sub_op == 0:  # LINEAR (broadcast or not)
            broadcast = bool(header_dw & (1 << 27))
            emit(f"Copy Packet Fields ({'LINEAR BROADCAST' if broadcast else 'LINEAR'}):")
            dec("ENCRYPT", (header_dw >> 16) & 0x1)
            dec("TMZ", (header_dw >> 18) & 0x1)
            if not broadcast:
                dec("BACKWARDS", (header_dw >> 25) & 0x1)
            dec("BROADCAST", (header_dw >> 27) & 0x1)
            dec("COPY_COUNT", words[0])
            if not broadcast:
                dec("DST_SW", (words[1] >> 16) & 3)
                dec("DST_CACHE_POLICY", (words[1] >> 18) & 0x7)
                dec("SRC_SW", (words[1] >> 24) & 3)
                dec("SRC_CACHE_POLICY", (words[1] >> 26) & 0x7)
                hx("SRC_ADDR", (words[3] << 32) | words[2])
                hx("DST_ADDR", (words[5] << 32) | words[4])
            else:
                dec("DST2_SW", (words[1] >> 8) & 3)
                dec("DST2_CACHE_POLICY", (words[1] >> 10) & 0x7)
                dec("DST_SW", (words[1] >> 16) & 3)
                dec("DST_CACHE_POLICY", (words[1] >> 18) & 0x7)
                dec("SRC_SW", (words[1] >> 24) & 3)
                dec("SRC_CACHE_POLICY", (words[1] >> 26) & 0x7)
                hx("SRC_ADDR", (words[3] << 32) | words[2])
                hx("DST_ADDR", (words[5] << 32) | words[4])
                hx("DST2_ADDR", (words[7] << 32) | words[6])
            return True

        if sub_op == 1:  # TILED (L2T broadcast/frame-to-field, or plain)
            if header_dw & (3 << 26):
                f2f = bool(header_dw & (1 << 26))
                emit(f"Copy Packet Fields ({'L2T_FRAME_TO_FIELD' if f2f else 'L2T_BROADCAST'}):")
                dec("ENCRYPT", (header_dw >> 16) & 0x1)
                dec("TMZ", (header_dw >> 18) & 0x1)
                dec("MIP_MAX", (header_dw >> 20) & 0xF)
                dec("VIDEOCOPY", (header_dw >> 26) & 0x1)
                dec("BROADCAST", (header_dw >> 27) & 0x1)
                hx("TILED_ADDR0", (words[1] << 32) | words[0])
                hx("TILED_ADDR1", (words[3] << 32) | words[2])
                dec("WIDTH", words[4] & 0x3FFF)
                dec("HEIGHT", words[5] & 0x3FFF)
                dec("DEPTH", (words[5] >> 16) & 0x1FFF)
                dec("ELEMENT_SIZE", words[6] & 0x7)
                dec("SWIZZLE_MODE", (words[6] >> 3) & 0x1F)
                dec("DIMENSION", (words[6] >> 9) & 0x3)
                dec("EPITCH", (words[6] >> 16) & 0xFFFF)
                dec("X", words[7] & 0x3FFF)
                dec("Y", (words[7] >> 16) & 0x3FFF)
                dec("Z", words[8] & 0x7FF)
                dec("DST2_SW", (words[9] >> 8) & 0x3)
                dec("DST2_CACHE_POLICY", (words[9] >> 10) & 0x7)
                dec("LINEAR_SW", (words[9] >> 16) & 0x3)
                dec("LINEAR_CACHE_POLICY", (words[9] >> 18) & 0x7)
                dec("TILE_SW", (words[9] >> 24) & 0x3)
                dec("TILE_CACHE_POLICY", (words[9] >> 26) & 0x7)
                hx("LINEAR_ADDR", (words[11] << 32) | words[10])
                dec("LINEAR_PITCH", words[12] & 0x7FFFF)
                dec("LINEAR_SLICE_PITCH", words[13])
                dec("COUNT", words[14] & 0x3FFFFF)
            else:
                emit("Copy Packet Fields (TILED):")
                dec("ENCRYPT", (header_dw >> 16) & 0x1)
                dec("TMZ", (header_dw >> 18) & 0x1)
                dec("MIP_MAX", (header_dw >> 20) & 0xF)
                dec("VIDEOCOPY", (header_dw >> 26) & 0x1)
                dec("BROADCAST", (header_dw >> 27) & 0x1)
                dec("DETILE", (header_dw >> 31) & 0x1)
                hx("TILED_ADDR", (words[1] << 32) | words[0])
                dec("WIDTH", words[2] & 0x3FFF)
                dec("HEIGHT", words[3] & 0x3FFF)
                dec("DEPTH", (words[3] >> 16) & 0x1FFF)
                dec("ELEMENT_SIZE", words[4] & 0x7)
                dec("SWIZZLE_MODE", (words[4] >> 3) & 0x1F)
                dec("DIMENSION", (words[4] >> 9) & 0x3)
                dec("EPITCH", (words[4] >> 16) & 0xFFFF)
                dec("X", words[5] & 0x3FFF)
                dec("Y", (words[5] >> 16) & 0x3FFF)
                dec("Z", words[6] & 0x1FFF)
                dec("LINEAR_SW", (words[6] >> 16) & 0x3)
                dec("LINEAR_CACHE_POLICY", (words[6] >> 18) & 0x7)
                dec("TILE_SW", (words[6] >> 24) & 0x3)
                dec("TILE_CACHE_POLICY", (words[6] >> 26) & 0x7)
                hx("LINEAR_ADDR", (words[8] << 32) | words[7])
                dec("LINEAR_PITCH", words[9] & 0x7FFFF)
                dec("LINEAR_SLICE_PITCH", words[10])
                dec("COUNT", words[11] & 0x3FFFFFFF)
            return True

        if sub_op == 3:  # STRUCTURE/SOA
            emit("Copy Packet Fields (STRUCT):")
            dec("TMZ", (header_dw >> 18) & 0x1)
            dec("DETILE", (header_dw >> 31) & 0x1)
            hx("SB_ADDR", (words[1] << 32) | words[0])
            dec("START_INDEX", words[2])
            dec("COUNT", words[3])
            dec("STRIDE", words[4] & 0x7FF)
            dec("LINEAR_SW", (words[4] >> 16) & 0x3)
            dec("LINEAR_CACHE_POLICY", (words[4] >> 18) & 0x7)
            dec("STRUCT_SW", (words[4] >> 24) & 0x3)
            dec("STRUCT_CACHE_POLICY", (words[4] >> 26) & 0x7)
            hx("LINEAR_ADDR", (words[6] << 32) | words[5])
            return True

        if sub_op == 4:  # LINEAR_SUB_WINDOW
            emit("Copy Packet Fields (LINEAR_SUB_WINDOW):")
            dec("TMZ", (header_dw >> 18) & 0x1)
            dec("ELEMENTSIZE", (header_dw >> 29) & 0x7)
            hx("SRC_ADDR", (words[1] << 32) | words[0])
            dec("SRC_X", words[2] & 0x3FFF)
            dec("SRC_Y", (words[2] >> 16) & 0x3FFF)
            dec("SRC_Z", words[3] & z_mask)
            dec("SRC_PITCH", (words[3] >> pitch_shift) & pitch_mask)
            dec("SRC_SLICE_PITCH", words[4] & 0xFFFFFFF)
            hx("DST_ADDR", (words[6] << 32) | words[5])
            dec("DST_X", words[7] & 0x3FFF)
            dec("DST_Y", (words[7] >> 16) & 0x3FFF)
            dec("DST_Z", words[8] & z_mask)
            dec("DST_PITCH", (words[8] >> pitch_shift) & pitch_mask)
            dec("DST_SLICE_PITCH", words[9] & 0xFFFFFFF)
            dec("RECT_X", words[10] & 0x3FFF)
            dec("RECT_Y", (words[10] >> 16) & 0x3FFF)
            dec("RECT_Z", words[11] & 0x1FFF)
            dec("DST_SW", (words[11] >> 16) & 0x3)
            dec("DST_CACHE_POLICY", (words[11] >> 18) & 0x7)
            dec("SRC_SW", (words[11] >> 24) & 0x3)
            dec("SRC_CACHE_POLICY", (words[11] >> 26) & 0x7)
            return True

        if sub_op == 5:  # TILED_SUB_WINDOW
            emit("Copy Packet Fields (TILED_SUB_WINDOW):")
            dec("TMZ", (header_dw >> 18) & 0x1)
            dec("MIP_MAX", (words[0] >> 20) & 0xF)
            dec("MIP_ID", (words[0] >> 24) & 0xF)
            dec("DETILE", header_dw >> 31)
            hx("TILED_ADDR", (words[1] << 32) | words[0])
            dec("TILED_X", words[2] & 0x3FFF)
            dec("TILED_Y", (words[2] >> 16) & 0x3FFF)
            dec("TILED_Z", words[3] & z_mask)
            dec("WIDTH", (words[3] >> 16) & 0x3FFF)
            dec("HEIGHT", words[4] & 0x3FFF)
            dec("DEPTH", (words[4] >> 16) & z_mask)
            dec("ELEMENT_SIZE", words[5] & 0x7)
            dec("SWIZZLE_MODE", (words[5] >> 3) & 0x1F)
            dec("DIMENSION", (words[5] >> 9) & 0x3)
            dec("EPITCH", (words[5] >> 16) & 0xFFFF)
            hx("LINEAR_ADDR", (words[7] << 32) | words[6])
            dec("LINEAR_X", words[8] & 0x3FFF)
            dec("LINEAR_Y", words[8] & 0x3FFF)
            dec("LINEAR_Z", words[9] & z_mask)
            dec("LINEAR_PITCH", (words[9] >> 16) & 0x3FFF)
            dec("LINEAR_SLICE_PITCH", words[10] & 0xFFFFFFF)
            dec("RECT_X", words[11] & 0x3FFF)
            dec("RECT_Y", (words[11] >> 16) & 0x3FFF)
            dec("RECT_Z", words[12] & z_mask)
            dec("LINEAR_SW", (words[12] >> 16) & 0x3)
            dec("LINEAR_CACHE_POLICY", (words[12] >> 18) & 0x7)
            return True

        if sub_op == 6:  # T2T_SUB_WINDOW
            emit("Copy Packet Fields (T2T_SUB_WINDOW):")
            dec("TMZ", (header_dw >> 18) & 0x1)
            dec("MIP_MAX", (header_dw >> 20) & 0xF)
            hx("SRC_ADDR", (words[1] << 32) | words[0])
            dec("SRC_X", words[2] & 0x3FFF)
            dec("SRC_Y", (words[2] >> 16) & 0x3FFF)
            dec("SRC_Z", words[3] & z_mask)
            dec("SRC_WIDTH", (words[3] >> 16) & 0x3FFF)
            dec("SRC_HEIGHT", words[4] & 0x3FFF)
            dec("SRC_DEPTH", (words[4] >> 16) & z_mask)
            dec("SRC_ELEMENT_SIZE", words[5] & 0x7)
            dec("SRC_SWIZZLE_MODE", (words[5] >> 3) & 0x1F)
            dec("SRC_DIMENSION", (words[5] >> 9) & 0x3)
            dec("SRC_EPITCH", (words[5] >> 16) & 0xFFFF)
            hx("DST_ADDR", (words[7] << 32) | words[6])
            dec("DST_X", words[8] & 0x3FFF)
            dec("DST_Y", (words[8] >> 16) & 0x3FFF)
            dec("DST_Z", words[9] & z_mask)
            dec("DST_WIDTH", (words[9] >> 16) & 0x3FFF)
            dec("DST_HEIGHT", words[10] & 0x3FFF)
            dec("DST_DEPTH", (words[10] >> 16) & z_mask)
            dec("DST_ELEMENT_SIZE", words[11] & 0x7)
            dec("DST_SWIZZLE_MODE", (words[11] >> 3) & 0x1F)
            dec("DST_DIMENSION", (words[11] >> 9) & 0x3)
            dec("DST_EPITCH", (words[11] >> 16) & 0xFFFF)
            dec("RECT_X", words[12] & 0x3FFF)
            dec("RECT_Y", (words[12] >> 16) & 0x3FFF)
            dec("RECT_Z", words[13] & z_mask)
            dec("DST_SW", (words[13] >> 16) & 0x3)
            dec("DST_CACHE_POLICY", (words[13] >> 18) & 0x7)
            dec("SRC_SW", (words[13] >> 22) & 0x3)
            dec("SRC_CACHE_POLICY", (words[13] >> 26) & 0x7)
            return True

        if sub_op == 7:  # DIRTY_PAGE
            emit("Copy Packet Fields (DIRTY_PAGE):")
            dec("TMZ", (header_dw >> 18) & 0x1)
            dec("ALL", (header_dw >> 31) & 0x1)
            dec("COUNT", words[0] & 0x3FFFFF)
            dec("DST_CACHE_POLICY", (words[1] >> 5) & 0x3)
            dec("SRC_CACHE_POLICY", (words[1] >> 13) & 0x3)
            dec("DST_SW", (words[1] >> 16) & 0x3)
            dec("DST_GCC", (words[1] >> 19) & 0x1)
            dec("DST_SYS", (words[1] >> 20) & 0x1)
            dec("DST_SNOOP", (words[1] >> 22) & 0x1)
            dec("DST_GPA", (words[1] >> 23) & 0x1)
            dec("SRC_SW", (words[1] >> 24) & 0x3)
            dec("SRC_SYS", (words[1] >> 28) & 0x1)
            dec("SRC_SNOOP", (words[1] >> 30) & 0x1)
            dec("SRC_GPA", (words[1] >> 31) & 0x1)
            hx("SRC_ADDR", (words[3] << 32) | words[2])
            hx("DST_ADDR", (words[5] << 32) | words[4])
            return True

        if sub_op == 8:  # LINEAR_PHY
            emit("Copy Packet Fields (LINEAR_PHY):")
            dec("TMZ", (header_dw >> 18) & 0x1)
            dec("COUNT", words[0] & 0x3FFFFF)
            dec("DST_CACHE_POLICY", (words[1] >> 5) & 0x3)
            dec("SRC_CACHE_POLICY", (words[1] >> 13) & 0x3)
            dec("DST_SW", (words[1] >> 16) & 0x3)
            dec("DST_GCC", (words[1] >> 19) & 0x1)
            dec("DST_SYS", (words[1] >> 20) & 0x1)
            dec("DST_LOG", (words[1] >> 21) & 0x1)
            dec("DST_SNOOP", (words[1] >> 22) & 0x1)
            dec("DST_GPA", (words[1] >> 23) & 0x1)
            dec("SRC_SW", (words[1] >> 24) & 0x3)
            dec("SRC_GCC", (words[1] >> 27) & 0x1)
            dec("SRC_SYS", (words[1] >> 28) & 0x1)
            dec("SRC_SNOOP", (words[1] >> 30) & 0x1)
            dec("SRC_GPA", (words[1] >> 31) & 0x1)
            n = 2
            idx = 0
            while n + 3 < len(words):
                hx(f"SRC_ADDR{idx}", (words[n + 1] << 32) | words[n])
                hx(f"DST_ADDR{idx}", (words[n + 3] << 32) | words[n + 2])
                n += 4
                idx += 1
            return True

        return False  # 16/17/20/21/22/36 -- legacy _BC variants, see docstring

    if op == 2:  # WRITE
        if sub_op == 0:  # LINEAR
            emit("Write Packet Fields (LINEAR):")
            dec("ENCRYPT", (header_dw >> 16) & 0x1)
            dec("TMZ", (header_dw >> 18) & 0x1)
            hx("DST_ADDR", (words[1] << 32) | words[0])
            dec("COUNT", words[2])
            dec("SWAP", (words[2] >> 24) & 0x3)
            dec("CACHE_POLICY", (words[2] >> 26) & 0x7)
            for n in range(3, len(words)):
                hx(f"DATA_{n - 3}", words[n])
            return True

        if sub_op == 1:  # TILED
            emit("Write Packet Fields (TILED):")
            dec("ENCRYPT", (header_dw >> 16) & 0x1)
            dec("TMZ", (header_dw >> 18) & 0x1)
            hx("DST_ADDR", (words[1] << 32) | words[0])
            dec("WIDTH", (words[2] >> 16) & 0x3FFF)
            dec("HEIGHT", words[3] & 0x3FFF)
            dec("DEPTH", (words[3] >> 16) & z_mask)
            dec("ELEMENT_SIZE", words[4] & 0x7)
            dec("SWIZZLE_MODE", (words[4] >> 3) & 0x1F)
            dec("DIMENSION", (words[4] >> 9) & 0x3)
            dec("EPITCH", (words[4] >> 16) & 0xFFFF)
            dec("X", words[5] & 0x3FFF)
            dec("Y", (words[5] >> 16) & 0x3FFF)
            dec("Z", words[6] & z_mask)
            dec("SW", (words[6] >> 24) & 0x3)
            dec("CACHE_POLICY", (words[6] >> 26) & 0x7)
            dec("COUNT", words[7] & 0xFFFFF)
            for n in range(8, len(words)):
                hx(f"DATA_{n - 8}", words[n])
            return True

        return False  # 17 (TILED_BC) -- legacy, see docstring

    if op == 4:  # INDIRECT -- see module docstring re: this deviation
        emit("Indirect Buffer Packet Fields:")
        dec("VMID", (header_dw >> 16) & 0xF)
        dec("PRIV", (header_dw >> 31) & 0x1)
        hx("IB_ADDR", (words[1] << 32) | words[0])
        dec("IB_SIZE", words[2])
        if len(words) > 4:
            hx("IB_CSA_ADDR", (words[4] << 32) | words[3])
        return True

    if op == 5:  # FENCE
        emit("Fence Packet Fields:")
        dec("L2_POLICY", (header_dw >> 24) & 0x3)
        dec("LLC_POLICY", (header_dw >> 26) & 0x1)
        hx("FENCE_ADDR", (words[1] << 32) | words[0])
        dec("FENCE_DATA", words[2])
        return True

    if op == 6:  # TRAP -- kept beyond decode_upto_ai's own coverage, see docstring
        emit("Trap Packet Fields:")
        hx("TRAP_INT_CONTEXT", words[0] & 0xFFFFFF)
        return True

    if op == 7:  # SEM / MEM_INCR
        if sub_op == 0:  # SEM
            emit("Sem Packet Fields:")
            dec("WRITE_ONE", (header_dw >> 29) & 1)
            dec("SIGNAL", (header_dw >> 30) & 1)
            dec("MAILBOX", (header_dw >> 31) & 1)
            hx("SEMAPHORE_ADDR", (words[1] << 32) | words[0])
            return True
        if sub_op == 1:  # MEM_INCR
            emit("Sem Packet Fields (MEM_INCR):")
            dec("L2_POLICY", (header_dw >> 24) & 0x3)
            dec("LLC_POLICY", (header_dw >> 26) & 0x1)
            hx("ADDR", (words[1] << 32) | words[0])
            return True
        return False

    if op == 8:  # POLL_REGMEM
        if sub_op == 0:  # POLL_REGMEM (register or memory)
            emit("Poll Packet Fields:")
            dec("CACHE_POLICY", (header_dw >> 20) & 0x7)
            dec("HDP_FLUSH", (header_dw >> 26) & 1)
            emit(f"  FUNCTION={_POLL_REGMEM_FUNCS[(header_dw >> 28) & 7]}")
            mem_poll = bool(header_dw & (1 << 31))
            dec("MEM_POLL", int(mem_poll))
            if not mem_poll:
                hx("REGISTER", (words[0] >> 2) & 0x3FFFF)
                if ((header_dw >> 26) & 3) == 1:  # HDP_FLUSH provides a write register
                    hx("REGISTER", (words[1] >> 2) & 0xFFFF)
                else:
                    hx("RESERVED", words[1])
            else:
                hx("POLL_REGMEM_ADDR", (words[1] << 32) | words[0])
            hx("VALUE", words[2])
            hx("MASK", words[3])
            dec("INTERVAL", words[4] & 0xFFFF)
            dec("RETRY_COUNT", (words[4] >> 16) & 0xFFF)
            return True
        if sub_op == 1:  # POLL_REG_WRITE_MEM
            emit("Poll Packet Fields (POLL_REG_WRITE_MEM):")
            dec("CACHE_POLICY", (header_dw >> 24) & 0x7)
            hx("SRC_ADDR", words[0])
            hx("DST_ADDR", (words[2] << 32) | words[1])
            return True
        if sub_op == 2:  # POLL_DBIT_WRITE_MEM
            emit("Poll Packet Fields (POLL_DBIT_WRITE_MEM):")
            dec("EA", (header_dw >> 16) & 0x3)
            dec("CACHE_POLICY", (header_dw >> 24) & 0x7)
            hx("DST_ADDR", (words[1] << 32) | words[0])
            dec("START_PAGE", (words[2] >> 4) & 0xFFFFFFF)
            dec("PAGE_NUM", words[3])
            return True
        if sub_op == 3:  # MEM_VERIFY
            emit("Poll Packet Fields (MEM_VERIFY):")
            dec("CACHE_POLICY", (header_dw >> 24) & 0x7)
            dec("MODE", (header_dw >> 31) & 0x1)
            hx("PATTERN", words[0])
            hx("CMP0_ADDR_START", (words[2] << 32) | words[1])
            hx("CMP0_ADDR_END", (words[4] << 32) | words[3])
            hx("CMP1_ADDR_START", (words[6] << 32) | words[5])
            hx("CMP1_ADDR_END", (words[8] << 32) | words[7])
            hx("REC_ADDR", (words[10] << 32) | words[9])
            return True
        if sub_op == 4:  # INVALIDATION
            emit("Poll Packet Fields (INVALIDATION):")
            hx("INVALIDATEREQ", words[0])
            hx("ADDRESSRANGE", words[1])
            dec("INVALIDATEACK", words[2] & 0xFFFF)
            dec("ADDRESSRANGE_HI", (words[2] >> 16) & 0x1F)
            dec("INVALIDATEGFXHUB", (words[2] >> 21) & 0x1)
            dec("INVALIDATEMMHUB", (words[2] >> 22) & 0x1)
            return True
        return False

    if op == 9:  # COND_EXE
        emit("Cond_Exe Packet Fields:")
        dec("CACHE_POLICY", (header_dw >> 24) & 0x7)
        hx("ADDR", (words[1] << 32) | words[0])
        dec("REFERENCE", words[2])
        dec("EXEC_COUNT", words[3])
        return True

    if op == 10:  # ATOMIC
        emit("Atomic Packet Fields:")
        dec("LOOP", (header_dw >> 16) & 1)
        dec("TMZ", (header_dw >> 18) & 0x1)
        dec("CACHE_POLICY", (header_dw >> 20) & 0x7)
        hx("OP", (header_dw >> 25) & 0x7F)
        hx("ADDR", (words[1] << 32) | words[0])
        hx("SRC_DATA", (words[3] << 32) | words[2])
        hx("CMP_DATA", (words[5] << 32) | words[4])
        dec("LOOP_INTERVAL", words[6] & 0x1FFF)
        return True

    if op == 11:  # FILL
        if sub_op == 0:  # CONST_FILL
            emit("Fill Packet Fields:")
            dec("SWAP", (header_dw >> 16) & 0x3)
            dec("CACHE_POLICY", (header_dw >> 24) & 0x7)
            dec("FILL_SIZE", (header_dw >> 30) & 0x3)
            hx("CONST_FILL_DST", (words[1] << 32) | words[0])
            hx("CONST_FILL_DATA", words[2])
            dec("CONST_FILL_BYTE_COUNT", words[3])
            return True
        if sub_op == 1:  # DATA_FILL_MULTI
            emit("Fill Packet Fields (DATA_FILL_MULTI):")
            dec("MEMLOG_CLR", (header_dw >> 31) & 0x1)
            dec("BYTE_STRIDE", words[0])
            dec("DMA_COUNT", words[1])
            hx("DST_ADDR", (words[3] << 32) | words[2])
            dec("COUNT", words[4] & 0x3FFFFFF)
            return True
        return False

    if op == 12:  # PTE
        if sub_op == 0:  # GEN_PTEPDE
            emit("Pte Packet Fields (GEN_PTEPDE):")
            hx("DST_ADDR", (words[1] << 32) | words[0])
            dec("CACHE_POLICY", (header_dw >> 24) & 0x7)
            hx("MASK", (words[3] << 32) | words[2])
            hx("INIT", (words[5] << 32) | words[4])
            hx("INCR", (words[7] << 32) | words[6])
            dec("COUNT", words[8] & 0x7FFFF)
            return True
        if sub_op == 1:  # COPY
            emit("Pte Packet Fields (COPY):")
            dec("TMZ", (header_dw >> 18) & 0x1)
            dec("PTEPDE_OP", (header_dw >> 31) & 0x1)
            hx("SRC_ADDR", (words[1] << 32) | words[0])
            hx("DST_ADDR", (words[3] << 32) | words[2])
            hx("MASK", (words[5] << 32) | words[4])
            dec("COUNT", words[6] & 0x7FFFF)
            dec("DST_CACHE_POLICY", (words[6] >> 22) & 0x7)
            dec("SRC_CACHE_POLICY", (words[6] >> 29) & 0x7)
            return True
        if sub_op == 2:  # RMW
            emit("Pte Packet Fields (RMW):")
            dec("MTYPE", (header_dw >> 16) & 0x7)
            dec("GCC", (header_dw >> 19) & 0x1)
            dec("SYS", (header_dw >> 20) & 0x1)
            dec("SNP", (header_dw >> 22) & 0x1)
            dec("GPA", (header_dw >> 23) & 0x1)
            dec("L2_POLICY", (header_dw >> 24) & 0x3)
            dec("LLC_POLICY", (header_dw >> 26) & 0x1)
            hx("ADDR", (words[1] << 32) | words[0])
            hx("MASK", (words[3] << 32) | words[2])
            hx("VALUE", (words[5] << 32) | words[4])
            dec("NUM_OF_PTE", words[6])
            return True
        return False

    if op == 13:  # TIMESTAMP
        if sub_op == 0:  # SET
            emit("Timestamp Packet Fields (SET):")
            hx("INIT_DATA", (words[1] << 32) | words[0])
            return True
        if sub_op in (1, 2):  # GET / GET_GLOBAL
            emit(f"Timestamp Packet Fields ({'GET_GLOBAL' if sub_op == 2 else 'GET'}):")
            dec("L2_POLICY", (header_dw >> 24) & 0x3)
            dec("LLC_POLICY", (header_dw >> 26) & 0x1)
            hx("WRITE_ADDR", (words[1] << 32) | words[0])
            return True
        return False

    if op == 14:  # SRBM_WRITE / RMW_REGISTER
        if sub_op == 0:  # SRBM_WRITE
            emit("Srbm_Write Packet Fields:")
            dec("BYTE_ENABLE", header_dw >> 28)
            hx("SRBM_WRITE_ADDR", words[0] & 0x3FFFF)
            hx("SRBM_WRITE_DATA", words[1])
            return True
        return False  # RMW_REGISTER -- UMR itself doesn't decode this for AI

    return False  # SEM.default / PRE_EXE / GPUVM_TLB_INV / GCR -- not ported


def decode_sdma_packets(reader, base, max_size, emit=print, _depth=0):
    """Walk and decode SDMA packets starting at base for up to max_size
    bytes. Sizing is a full port of UMR's sized_oss1_5() (all opcodes/
    sub-opcodes, generation-agnostic); field-level decoding matches
    decode_upto_ai(), the generation confirmed for this host's real
    hardware (SDMA IP major 4) -- see _sdma_decode_fields()'s docstring for
    exactly what that covers and the two deliberate additions beyond it.

    Stops early on a null (op==0, size-0) opcode, an unreadable/unknown
    byte, an opcode/sub-opcode this port doesn't recognize at all (can't
    size it -- no safe way to keep walking), or hitting max_size.

    INDIRECT packets are followed one level via `reader.read()` at the IB's
    address -- this works transparently when `reader` can read anywhere in
    the process (the live rocgdb path), and cleanly fails with "IB not
    available in this dump" when it can't (the offline queue_viewer.py path,
    whose BufferReader only has the ring's own dumped bytes). `_depth`
    guards against runaway/cyclic IB chains.
    """
    addr = base
    end = base + max_size
    i = 0
    while addr < end:
        try:
            header_bytes = reader.read(addr, 4)
        except MemoryReadError:
            emit(f"Cannot read memory at 0x{addr:x}")
            break

        (header_dw,) = struct.unpack_from("<I", header_bytes, 0)
        op = header_dw & 0xFF
        sub_op = (header_dw >> 8) & 0xFF

        if op == 0 and ((header_dw >> 16) & 0x3FFF) == 0:
            break  # unsized NOP/padding -- treated as end of committed ring content

        try:
            nwords = _sdma_nwords(reader, addr, op, sub_op, header_dw)
        except MemoryReadError:
            emit(f"Cannot read memory at 0x{addr:x}")
            break

        emit("-" * 30)
        if nwords is None:
            emit(f"Packet #{i} at 0x{addr:x}: op=0x{op:x} sub_op=0x{sub_op:x} (unrecognized opcode, stopping)")
            break

        size = 4 + nwords * 4
        emit(f"Packet #{i} at 0x{addr:x}: op=0x{op:x} sub_op=0x{sub_op:x}")

        try:
            data = reader.read(addr, size)
        except MemoryReadError:
            emit(f"Cannot read memory at 0x{addr:x}")
            break
        words = list(struct.unpack_from(f"<{nwords}I", data, 4)) if nwords else []

        try:
            decoded = _sdma_decode_fields(op, sub_op, header_dw, words, emit)
        except IndexError:
            emit("  Failed to decode packet (short on fields)")
            decoded = True
        if not decoded:
            emit(f"  (recognized, {size} bytes, not decoded in detail)")

        if op == 4 and nwords >= 3:  # INDIRECT -- follow one level, see docstring
            ib_vmid = (header_dw >> 16) & 0xF
            ib_addr = (words[1] << 32) | words[0]
            ib_size = words[2]
            if _depth >= 8:
                emit(f"  (not following IB at 0x{ib_addr:x}: max recursion depth reached)")
            else:
                try:
                    reader.read(ib_addr, 4)  # probe: is this address reachable at all?
                except MemoryReadError:
                    emit(f"  IB at 0x{ib_addr:x} (vmid={ib_vmid}, size={ib_size}) not available in this dump")
                else:
                    emit(f"  -- following indirect buffer at 0x{ib_addr:x} (vmid={ib_vmid}, size={ib_size}) --")
                    decode_sdma_packets(reader, ib_addr, ib_size, emit=emit, _depth=_depth + 1)

        addr += size
        i += 1
