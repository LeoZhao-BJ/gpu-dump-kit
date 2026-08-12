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
# One file per queue, written by rocgdb_helper.py's dump_all_queues and
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


def write_dump_txt_header(emit, metadata):
    """Emit the two header lines that precede a queue's full packet decode in
    a `dump_all_queues_txt`-style text dump: `# <target_id>` then a
    `Dumping full ring for QID<n> type=... addr=... size=... read=...
    write=...` summary line. Shared by rocgdb_helper.py's live
    dump_all_queues_txt (writing straight to a .log file while attached) and
    queue_viewer.py's offline `--to-txt` (.bin -> .log conversion with no
    gdb involved) so the two can never format-drift apart -- a `.log`
    produced live and one produced by converting the matching `.bin` later
    should read identically."""
    emit(f"# {metadata['target_id']}")
    emit(
        f"Dumping full ring for QID{metadata['qid']} type={metadata['type']} "
        f"addr=0x{metadata['addr']:x} size={metadata['size']} "
        f"read={metadata['read']} write={metadata['write']}"
    )


# hsa_signal_condition_t (from hsa.h) -- used by _looks_like_barrier_value's
# COND sanity check and to name the field once detected.
_SIGNAL_CONDITIONS = ["EQ", "NE", "LT", "GTE"]


def _looks_like_barrier_value(words):
    """True if an AQL-type-1 ("invalid") packet's leftover bytes match the
    shape of an AMD vendor-specific hsa_amd_barrier_value_packet_t rather
    than a plain kernel dispatch or barrier-and/or packet -- see
    _hsa_decode_fields's was_invalid reinterpretation for why type=1 shows
    up here at all (real-world capture confirmed: a live BarrierDispatch
    packet -- verified against the runtime's own debug log for the exact
    same packet -- has header type=1, not HSA_PACKET_TYPE_BARRIER_AND/OR,
    so it lands in the same "reinterpret a type-1 slot" path as everything
    else here).

    hsa_amd_barrier_value_packet_t (bytes 8-63, i.e. words[2:16]):
        signal (8B) / value (8B) / mask (8B) / cond (4B) / reserved1 (4B) /
        reserved2 (8B) / reserved3 (8B) / completion_signal (8B, handled by
        the caller already)
    Every "reserved" field there is documented "Must be 0" when a real
    barrier-value packet is constructed, and `cond` is a 4-value enum.

    The reserved1/reserved2/reserved3 + cond-in-range check alone is NOT
    enough, though it looks that way at first: a plain barrier-and/or
    packet using fewer than 4 of its 5 dep_signal slots (extremely common
    -- most barriers wait on 1-2 signals, not all 5) legitimately has
    dep_signal[3]/[4] == 0 too, which trivially satisfies "reserved == 0"
    and "cond (dep_signal[3]'s low dword) == 0" by coincidence, not because
    it's really a barrier-value packet. Caught exactly this on a real
    18-file capture: requiring only the reserved/cond shape produced ~13k
    matches, of which mask (words[6:8]) was 0 in all but 3.4k -- a mask of
    0 makes the AND-compare degenerate (always "equal", since
    signal_value & 0 == 0 regardless of the signal's real value), which a
    real runtime never constructs on purpose, so requiring mask != 0 is
    what actually separates genuine hits from this false-positive shape.
    The surviving 3.4k were internally consistent (100% COND=LT, plausible
    heap-pointer-looking signal addresses, plausible small compare values)
    -- see TEST_PLAN.md for the exact counts.

    (Caller has already confirmed words[1] == 0, matching
    hsa_amd_barrier_value_packet_t's reserved0.)"""
    return (
        words[9] == 0
        and words[10] == 0
        and words[11] == 0
        and words[12] == 0
        and words[13] == 0
        and 0 <= words[8] < len(_SIGNAL_CONDITIONS)
        and (words[6] != 0 or words[7] != 0)  # mask -- see docstring
    )


def _hsa_decode_fields(words, symbol_lookup):
    """Decode one 64-byte HSA AQL packet's fields. `words[i]` is the dword
    at byte offset 4*i -- unlike the SDMA decoder, HSA's own header dword
    (words[0]) is NOT tracked separately: it packs 'setup' (Kernel
    Dispatch) or 'type' (Agent Dispatch) into its own upper 16 bits, so
    those sub-fields naturally flow through the same word_ref=0 group as
    the HEADER entry (see _emit_field_groups).

    symbol_lookup(addr) -> str | None, when given, resolves a kernel_object
    address to a human-readable name (e.g. via a live process's symbol
    table); appended in quotes after the raw address when found.

    Returns (label, fields, decoded) -- same shape as _sdma_decode_fields().
    `fields[0]` is always the HEADER entry; `COMPLETION_SIGNAL` is always
    appended last regardless of type, matching every AQL packet having one.
    """
    header = words[0] & 0xFFFF
    type_ = header & 0xFF
    barrier = (header >> 8) & 1
    acquire = (header >> 9) & 3
    release = (header >> 11) & 3

    fields = [(0, f"HEADER type={type_} barrier={barrier} acquire={acquire} release={release}", None)]

    def hx(name, value, word=None):
        fields.append((word, name, f"0x{value:x}"))

    def dec(name, value, word=None):
        fields.append((word, name, str(value)))

    def reserved(word):
        fields.append((word, "(reserved)", None))

    def note(text):
        fields.append((None, text, None))

    was_invalid = type_ == 1
    barrier_value = False
    if was_invalid:
        # Invalid packet -- peek the leftover bytes to guess the real type,
        # the same heuristic the original decoder used: word[1] nonzero ->
        # kernel dispatch, word[1] zero -> barrier-shaped. Real-world
        # capture showed a third shape hiding behind "word[1] zero": AMD's
        # vendor-specific hsa_amd_barrier_value_packet_t (a barrier that
        # waits on a signal/value/mask/cond comparison instead of up to 5
        # dep_signal handles) ALSO reports header type=1 -- see
        # _looks_like_barrier_value's docstring for the content-based check
        # that tells the two apart. Falls through to decode as whichever
        # type this resolves to (still useful to see what the reinterpreted
        # fields look like), but the packet *title* stays "INVALID" -- see
        # below -- rather than showing the guessed type as if it were a
        # real one. No note is emitted about the reinterpretation; the
        # INVALID title already says everything that matters.
        if words[1] == 0 and _looks_like_barrier_value(words):
            barrier_value = True
        else:
            type_ = 2 if words[1] != 0 else 3

    label = None
    decoded = False

    if barrier_value:  # AMD vendor-specific hsa_amd_barrier_value_packet_t
        label = "BARRIER_VALUE"
        reserved(1)
        hx("DEP_SIGNAL", (words[3] << 32) | words[2], word=(2, 3))
        hx("VALUE", (words[5] << 32) | words[4], word=(4, 5))
        hx("MASK", (words[7] << 32) | words[6], word=(6, 7))
        cond = words[8]
        fields.append((8, "COND", f"{_SIGNAL_CONDITIONS[cond]}({cond})"))
        reserved(9)
        reserved((10, 11))
        reserved((12, 13))
        decoded = True

    elif type_ == 2:  # Kernel dispatch
        label = "KERNEL_DISPATCH"
        setup = (words[0] >> 16) & 0xFFFF
        hx("SETUP", setup, word=0)
        dec("WORKGROUP_X", words[1] & 0xFFFF, word=1)
        dec("WORKGROUP_Y", (words[1] >> 16) & 0xFFFF, word=1)
        dec("WORKGROUP_Z", words[2] & 0xFFFF, word=2)
        reserved(2)  # reserved0 -- the upper 16 bits of word 2, after workgroup_size_z
        dec("GRID_X", words[3], word=3)
        dec("GRID_Y", words[4], word=4)
        dec("GRID_Z", words[5], word=5)
        dec("PRIVATE_SEGMENT_SIZE", words[6], word=6)
        dec("GROUP_SEGMENT_SIZE", words[7], word=7)
        kernel_object = (words[9] << 32) | words[8]
        kern_name = symbol_lookup(kernel_object) if symbol_lookup else None
        value = f"0x{kernel_object:x}" + (f' "{kern_name}"' if kern_name else "")
        fields.append(((8, 9), "KERNEL_OBJECT", value))
        hx("KERNARG_ADDRESS", (words[11] << 32) | words[10], word=(10, 11))
        reserved((12, 13))
        decoded = True

    elif type_ in (3, 5):  # Barrier And / Barrier Or
        label = "BARRIER_AND" if type_ == 3 else "BARRIER_OR"
        reserved(1)
        for j in range(5):
            lo, hi = 2 + 2 * j, 3 + 2 * j
            hx(f"DEP_SIGNAL_{j}", (words[hi] << 32) | words[lo], word=(lo, hi))
        reserved((12, 13))
        decoded = True

    elif type_ == 4:  # Agent dispatch
        label = "AGENT_DISPATCH"
        agent_type = (words[0] >> 16) & 0xFFFF
        hx("TYPE", agent_type, word=0)
        note("(bytes 8-47 not decoded in detail)")
        decoded = True

    hx("COMPLETION_SIGNAL", (words[15] << 32) | words[14], word=(14, 15))
    if was_invalid:
        label = "INVALID"  # never show the guessed type as if it were real
    return label, fields, decoded


def _render_hsa_packet(emit, addr, i, words, label, fields, decoded, use_color=False):
    """Render one decoded HSA packet in the same two-column hex/field layout
    as SDMA (see _emit_field_groups); `words[i]` starts at byte offset 4*i
    (the packet's very first byte), since HSA has no separately-tracked
    header dword the way SDMA does."""
    _render_packet_title(
        emit, addr, i, 64, label if label else "UNKNOWN", use_color=use_color
    )  # AQL packets are always 64 bytes
    _emit_field_groups(emit, fields, words, lambda w: 4 * w)
    if not decoded:
        _pkt_row(emit, "", "(not decoded in detail)")
    emit("-" * _PKT_SEPARATOR_WIDTH)


def decode_hsa_packets(reader, base, start_idx, end_idx, emit=print, symbol_lookup=None, use_color=False):
    """Decode HSA AQL packets [start_idx, end_idx) (64-byte slots) at base.

    start_idx/end_idx must already be resolved slot indices (no modulo
    wraparound applied here) -- callers that accept raw/absolute rptr-wptr
    counters are responsible for wrapping them into range first.

    symbol_lookup(addr) -> str | None, when given, is used to resolve a
    kernel_object address to a human-readable name (e.g. via a live
    process's symbol table). When None, kernel dispatch packets just show
    the raw address.

    use_color: colorize each packet's title (red INVALID / green everything
    else) -- only pass True when `emit` goes straight to a real terminal for
    a human to read; see the ANSI comment above _render_packet_title for why
    this must stay off for file/JSON destinations.

    Rendered via _render_hsa_packet() as the same two-column hex/field view
    used by the SDMA decoder -- see _hsa_decode_fields()'s docstring for
    exactly what's decoded per packet type.
    """
    for i in range(start_idx, end_idx):
        addr = base + i * 64
        try:
            data = reader.read(addr, 64)
        except MemoryReadError:
            emit(f"Cannot read memory at 0x{addr:x}")
            continue

        words = list(struct.unpack_from("<16I", data, 0))
        label, fields, decoded = _hsa_decode_fields(words, symbol_lookup)
        _render_hsa_packet(emit, addr, i, words, label, fields, decoded, use_color=use_color)


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


def _sdma_decode_fields(op, sub_op, header_dw, words):
    """Decode one SDMA packet's body into a structured field list.
    `words[i]` is the dword at byte offset 4+4*i, i.e. immediately after the
    header dword.

    Returns (label, fields, decoded):
      - label: ALL-CAPS packet type name (e.g. "COPY (LINEAR)"), or None
        if the opcode/sub-opcode wasn't recognized.
      - fields: list of (word_ref, name, value_str) in display order.
        word_ref is None (derived from the header dword itself), an int i
        (single word, byte offset 4+4*i), or a 2-tuple (i, j) with j==i+1
        (a 64-bit LO/HI field spanning two consecutive words) -- the
        caller's renderer uses this to lay out the hex/field two-column
        view and group fields that share a dword.
      - decoded: False means the caller should fall back to a generic dump
        (still correctly sized -- see _sdma_nwords -- just not broken down
        field-by-field).

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

    fields = []

    def hx(name, value, word=None):
        fields.append((word, name, f"0x{value:x}"))

    def dec(name, value, word=None):
        fields.append((word, name, str(value)))

    if op == 1:  # COPY
        if sub_op == 0:  # LINEAR (broadcast or not)
            broadcast = bool(header_dw & (1 << 27))
            label = f"COPY ({'LINEAR BROADCAST' if broadcast else 'LINEAR'})"
            dec("ENCRYPT", (header_dw >> 16) & 0x1)
            dec("TMZ", (header_dw >> 18) & 0x1)
            if not broadcast:
                dec("BACKWARDS", (header_dw >> 25) & 0x1)
            dec("BROADCAST", (header_dw >> 27) & 0x1)
            dec("COPY_COUNT", words[0], word=0)
            if not broadcast:
                dec("DST_SW", (words[1] >> 16) & 3, word=1)
                dec("DST_CACHE_POLICY", (words[1] >> 18) & 0x7, word=1)
                dec("SRC_SW", (words[1] >> 24) & 3, word=1)
                dec("SRC_CACHE_POLICY", (words[1] >> 26) & 0x7, word=1)
                hx("SRC_ADDR", (words[3] << 32) | words[2], word=(2, 3))
                hx("DST_ADDR", (words[5] << 32) | words[4], word=(4, 5))
            else:
                dec("DST2_SW", (words[1] >> 8) & 3, word=1)
                dec("DST2_CACHE_POLICY", (words[1] >> 10) & 0x7, word=1)
                dec("DST_SW", (words[1] >> 16) & 3, word=1)
                dec("DST_CACHE_POLICY", (words[1] >> 18) & 0x7, word=1)
                dec("SRC_SW", (words[1] >> 24) & 3, word=1)
                dec("SRC_CACHE_POLICY", (words[1] >> 26) & 0x7, word=1)
                hx("SRC_ADDR", (words[3] << 32) | words[2], word=(2, 3))
                hx("DST_ADDR", (words[5] << 32) | words[4], word=(4, 5))
                hx("DST2_ADDR", (words[7] << 32) | words[6], word=(6, 7))
            return label, fields, True

        if sub_op == 1:  # TILED (L2T broadcast/frame-to-field, or plain)
            if header_dw & (3 << 26):
                f2f = bool(header_dw & (1 << 26))
                label = f"COPY ({'L2T_FRAME_TO_FIELD' if f2f else 'L2T_BROADCAST'})"
                dec("ENCRYPT", (header_dw >> 16) & 0x1)
                dec("TMZ", (header_dw >> 18) & 0x1)
                dec("MIP_MAX", (header_dw >> 20) & 0xF)
                dec("VIDEOCOPY", (header_dw >> 26) & 0x1)
                dec("BROADCAST", (header_dw >> 27) & 0x1)
                hx("TILED_ADDR0", (words[1] << 32) | words[0], word=(0, 1))
                hx("TILED_ADDR1", (words[3] << 32) | words[2], word=(2, 3))
                dec("WIDTH", words[4] & 0x3FFF, word=4)
                dec("HEIGHT", words[5] & 0x3FFF, word=5)
                dec("DEPTH", (words[5] >> 16) & 0x1FFF, word=5)
                dec("ELEMENT_SIZE", words[6] & 0x7, word=6)
                dec("SWIZZLE_MODE", (words[6] >> 3) & 0x1F, word=6)
                dec("DIMENSION", (words[6] >> 9) & 0x3, word=6)
                dec("EPITCH", (words[6] >> 16) & 0xFFFF, word=6)
                dec("X", words[7] & 0x3FFF, word=7)
                dec("Y", (words[7] >> 16) & 0x3FFF, word=7)
                dec("Z", words[8] & 0x7FF, word=8)
                dec("DST2_SW", (words[9] >> 8) & 0x3, word=9)
                dec("DST2_CACHE_POLICY", (words[9] >> 10) & 0x7, word=9)
                dec("LINEAR_SW", (words[9] >> 16) & 0x3, word=9)
                dec("LINEAR_CACHE_POLICY", (words[9] >> 18) & 0x7, word=9)
                dec("TILE_SW", (words[9] >> 24) & 0x3, word=9)
                dec("TILE_CACHE_POLICY", (words[9] >> 26) & 0x7, word=9)
                hx("LINEAR_ADDR", (words[11] << 32) | words[10], word=(10, 11))
                dec("LINEAR_PITCH", words[12] & 0x7FFFF, word=12)
                dec("LINEAR_SLICE_PITCH", words[13], word=13)
                dec("COUNT", words[14] & 0x3FFFFF, word=14)
            else:
                label = "COPY (TILED)"
                dec("ENCRYPT", (header_dw >> 16) & 0x1)
                dec("TMZ", (header_dw >> 18) & 0x1)
                dec("MIP_MAX", (header_dw >> 20) & 0xF)
                dec("VIDEOCOPY", (header_dw >> 26) & 0x1)
                dec("BROADCAST", (header_dw >> 27) & 0x1)
                dec("DETILE", (header_dw >> 31) & 0x1)
                hx("TILED_ADDR", (words[1] << 32) | words[0], word=(0, 1))
                dec("WIDTH", words[2] & 0x3FFF, word=2)
                dec("HEIGHT", words[3] & 0x3FFF, word=3)
                dec("DEPTH", (words[3] >> 16) & 0x1FFF, word=3)
                dec("ELEMENT_SIZE", words[4] & 0x7, word=4)
                dec("SWIZZLE_MODE", (words[4] >> 3) & 0x1F, word=4)
                dec("DIMENSION", (words[4] >> 9) & 0x3, word=4)
                dec("EPITCH", (words[4] >> 16) & 0xFFFF, word=4)
                dec("X", words[5] & 0x3FFF, word=5)
                dec("Y", (words[5] >> 16) & 0x3FFF, word=5)
                dec("Z", words[6] & 0x1FFF, word=6)
                dec("LINEAR_SW", (words[6] >> 16) & 0x3, word=6)
                dec("LINEAR_CACHE_POLICY", (words[6] >> 18) & 0x7, word=6)
                dec("TILE_SW", (words[6] >> 24) & 0x3, word=6)
                dec("TILE_CACHE_POLICY", (words[6] >> 26) & 0x7, word=6)
                hx("LINEAR_ADDR", (words[8] << 32) | words[7], word=(7, 8))
                dec("LINEAR_PITCH", words[9] & 0x7FFFF, word=9)
                dec("LINEAR_SLICE_PITCH", words[10], word=10)
                dec("COUNT", words[11] & 0x3FFFFFFF, word=11)
            return label, fields, True

        if sub_op == 3:  # STRUCTURE/SOA
            label = "COPY (STRUCT)"
            dec("TMZ", (header_dw >> 18) & 0x1)
            dec("DETILE", (header_dw >> 31) & 0x1)
            hx("SB_ADDR", (words[1] << 32) | words[0], word=(0, 1))
            dec("START_INDEX", words[2], word=2)
            dec("COUNT", words[3], word=3)
            dec("STRIDE", words[4] & 0x7FF, word=4)
            dec("LINEAR_SW", (words[4] >> 16) & 0x3, word=4)
            dec("LINEAR_CACHE_POLICY", (words[4] >> 18) & 0x7, word=4)
            dec("STRUCT_SW", (words[4] >> 24) & 0x3, word=4)
            dec("STRUCT_CACHE_POLICY", (words[4] >> 26) & 0x7, word=4)
            hx("LINEAR_ADDR", (words[6] << 32) | words[5], word=(5, 6))
            return label, fields, True

        if sub_op == 4:  # LINEAR_SUB_WINDOW
            label = "COPY (LINEAR_SUB_WINDOW)"
            dec("TMZ", (header_dw >> 18) & 0x1)
            dec("ELEMENTSIZE", (header_dw >> 29) & 0x7)
            hx("SRC_ADDR", (words[1] << 32) | words[0], word=(0, 1))
            dec("SRC_X", words[2] & 0x3FFF, word=2)
            dec("SRC_Y", (words[2] >> 16) & 0x3FFF, word=2)
            dec("SRC_Z", words[3] & z_mask, word=3)
            dec("SRC_PITCH", (words[3] >> pitch_shift) & pitch_mask, word=3)
            dec("SRC_SLICE_PITCH", words[4] & 0xFFFFFFF, word=4)
            hx("DST_ADDR", (words[6] << 32) | words[5], word=(5, 6))
            dec("DST_X", words[7] & 0x3FFF, word=7)
            dec("DST_Y", (words[7] >> 16) & 0x3FFF, word=7)
            dec("DST_Z", words[8] & z_mask, word=8)
            dec("DST_PITCH", (words[8] >> pitch_shift) & pitch_mask, word=8)
            dec("DST_SLICE_PITCH", words[9] & 0xFFFFFFF, word=9)
            dec("RECT_X", words[10] & 0x3FFF, word=10)
            dec("RECT_Y", (words[10] >> 16) & 0x3FFF, word=10)
            dec("RECT_Z", words[11] & 0x1FFF, word=11)
            dec("DST_SW", (words[11] >> 16) & 0x3, word=11)
            dec("DST_CACHE_POLICY", (words[11] >> 18) & 0x7, word=11)
            dec("SRC_SW", (words[11] >> 24) & 0x3, word=11)
            dec("SRC_CACHE_POLICY", (words[11] >> 26) & 0x7, word=11)
            return label, fields, True

        if sub_op == 5:  # TILED_SUB_WINDOW
            label = "COPY (TILED_SUB_WINDOW)"
            dec("TMZ", (header_dw >> 18) & 0x1)
            dec("MIP_MAX", (words[0] >> 20) & 0xF, word=0)
            dec("MIP_ID", (words[0] >> 24) & 0xF, word=0)
            dec("DETILE", header_dw >> 31)
            hx("TILED_ADDR", (words[1] << 32) | words[0], word=(0, 1))
            dec("TILED_X", words[2] & 0x3FFF, word=2)
            dec("TILED_Y", (words[2] >> 16) & 0x3FFF, word=2)
            dec("TILED_Z", words[3] & z_mask, word=3)
            dec("WIDTH", (words[3] >> 16) & 0x3FFF, word=3)
            dec("HEIGHT", words[4] & 0x3FFF, word=4)
            dec("DEPTH", (words[4] >> 16) & z_mask, word=4)
            dec("ELEMENT_SIZE", words[5] & 0x7, word=5)
            dec("SWIZZLE_MODE", (words[5] >> 3) & 0x1F, word=5)
            dec("DIMENSION", (words[5] >> 9) & 0x3, word=5)
            dec("EPITCH", (words[5] >> 16) & 0xFFFF, word=5)
            hx("LINEAR_ADDR", (words[7] << 32) | words[6], word=(6, 7))
            dec("LINEAR_X", words[8] & 0x3FFF, word=8)
            dec("LINEAR_Y", words[8] & 0x3FFF, word=8)
            dec("LINEAR_Z", words[9] & z_mask, word=9)
            dec("LINEAR_PITCH", (words[9] >> 16) & 0x3FFF, word=9)
            dec("LINEAR_SLICE_PITCH", words[10] & 0xFFFFFFF, word=10)
            dec("RECT_X", words[11] & 0x3FFF, word=11)
            dec("RECT_Y", (words[11] >> 16) & 0x3FFF, word=11)
            dec("RECT_Z", words[12] & z_mask, word=12)
            dec("LINEAR_SW", (words[12] >> 16) & 0x3, word=12)
            dec("LINEAR_CACHE_POLICY", (words[12] >> 18) & 0x7, word=12)
            return label, fields, True

        if sub_op == 6:  # T2T_SUB_WINDOW
            label = "COPY (T2T_SUB_WINDOW)"
            dec("TMZ", (header_dw >> 18) & 0x1)
            dec("MIP_MAX", (header_dw >> 20) & 0xF)
            hx("SRC_ADDR", (words[1] << 32) | words[0], word=(0, 1))
            dec("SRC_X", words[2] & 0x3FFF, word=2)
            dec("SRC_Y", (words[2] >> 16) & 0x3FFF, word=2)
            dec("SRC_Z", words[3] & z_mask, word=3)
            dec("SRC_WIDTH", (words[3] >> 16) & 0x3FFF, word=3)
            dec("SRC_HEIGHT", words[4] & 0x3FFF, word=4)
            dec("SRC_DEPTH", (words[4] >> 16) & z_mask, word=4)
            dec("SRC_ELEMENT_SIZE", words[5] & 0x7, word=5)
            dec("SRC_SWIZZLE_MODE", (words[5] >> 3) & 0x1F, word=5)
            dec("SRC_DIMENSION", (words[5] >> 9) & 0x3, word=5)
            dec("SRC_EPITCH", (words[5] >> 16) & 0xFFFF, word=5)
            hx("DST_ADDR", (words[7] << 32) | words[6], word=(6, 7))
            dec("DST_X", words[8] & 0x3FFF, word=8)
            dec("DST_Y", (words[8] >> 16) & 0x3FFF, word=8)
            dec("DST_Z", words[9] & z_mask, word=9)
            dec("DST_WIDTH", (words[9] >> 16) & 0x3FFF, word=9)
            dec("DST_HEIGHT", words[10] & 0x3FFF, word=10)
            dec("DST_DEPTH", (words[10] >> 16) & z_mask, word=10)
            dec("DST_ELEMENT_SIZE", words[11] & 0x7, word=11)
            dec("DST_SWIZZLE_MODE", (words[11] >> 3) & 0x1F, word=11)
            dec("DST_DIMENSION", (words[11] >> 9) & 0x3, word=11)
            dec("DST_EPITCH", (words[11] >> 16) & 0xFFFF, word=11)
            dec("RECT_X", words[12] & 0x3FFF, word=12)
            dec("RECT_Y", (words[12] >> 16) & 0x3FFF, word=12)
            dec("RECT_Z", words[13] & z_mask, word=13)
            dec("DST_SW", (words[13] >> 16) & 0x3, word=13)
            dec("DST_CACHE_POLICY", (words[13] >> 18) & 0x7, word=13)
            dec("SRC_SW", (words[13] >> 22) & 0x3, word=13)
            dec("SRC_CACHE_POLICY", (words[13] >> 26) & 0x7, word=13)
            return label, fields, True

        if sub_op == 7:  # DIRTY_PAGE
            label = "COPY (DIRTY_PAGE)"
            dec("TMZ", (header_dw >> 18) & 0x1)
            dec("ALL", (header_dw >> 31) & 0x1)
            dec("COUNT", words[0] & 0x3FFFFF, word=0)
            dec("DST_CACHE_POLICY", (words[1] >> 5) & 0x3, word=1)
            dec("SRC_CACHE_POLICY", (words[1] >> 13) & 0x3, word=1)
            dec("DST_SW", (words[1] >> 16) & 0x3, word=1)
            dec("DST_GCC", (words[1] >> 19) & 0x1, word=1)
            dec("DST_SYS", (words[1] >> 20) & 0x1, word=1)
            dec("DST_SNOOP", (words[1] >> 22) & 0x1, word=1)
            dec("DST_GPA", (words[1] >> 23) & 0x1, word=1)
            dec("SRC_SW", (words[1] >> 24) & 0x3, word=1)
            dec("SRC_SYS", (words[1] >> 28) & 0x1, word=1)
            dec("SRC_SNOOP", (words[1] >> 30) & 0x1, word=1)
            dec("SRC_GPA", (words[1] >> 31) & 0x1, word=1)
            hx("SRC_ADDR", (words[3] << 32) | words[2], word=(2, 3))
            hx("DST_ADDR", (words[5] << 32) | words[4], word=(4, 5))
            return label, fields, True

        if sub_op == 8:  # LINEAR_PHY
            label = "COPY (LINEAR_PHY)"
            dec("TMZ", (header_dw >> 18) & 0x1)
            dec("COUNT", words[0] & 0x3FFFFF, word=0)
            dec("DST_CACHE_POLICY", (words[1] >> 5) & 0x3, word=1)
            dec("SRC_CACHE_POLICY", (words[1] >> 13) & 0x3, word=1)
            dec("DST_SW", (words[1] >> 16) & 0x3, word=1)
            dec("DST_GCC", (words[1] >> 19) & 0x1, word=1)
            dec("DST_SYS", (words[1] >> 20) & 0x1, word=1)
            dec("DST_LOG", (words[1] >> 21) & 0x1, word=1)
            dec("DST_SNOOP", (words[1] >> 22) & 0x1, word=1)
            dec("DST_GPA", (words[1] >> 23) & 0x1, word=1)
            dec("SRC_SW", (words[1] >> 24) & 0x3, word=1)
            dec("SRC_GCC", (words[1] >> 27) & 0x1, word=1)
            dec("SRC_SYS", (words[1] >> 28) & 0x1, word=1)
            dec("SRC_SNOOP", (words[1] >> 30) & 0x1, word=1)
            dec("SRC_GPA", (words[1] >> 31) & 0x1, word=1)
            n = 2
            idx = 0
            while n + 3 < len(words):
                hx(f"SRC_ADDR{idx}", (words[n + 1] << 32) | words[n], word=(n, n + 1))
                hx(f"DST_ADDR{idx}", (words[n + 3] << 32) | words[n + 2], word=(n + 2, n + 3))
                n += 4
                idx += 1
            return label, fields, True

        return None, fields, False  # 16/17/20/21/22/36 -- legacy _BC variants, see docstring

    if op == 2:  # WRITE
        if sub_op == 0:  # LINEAR
            label = "WRITE (LINEAR)"
            dec("ENCRYPT", (header_dw >> 16) & 0x1)
            dec("TMZ", (header_dw >> 18) & 0x1)
            hx("DST_ADDR", (words[1] << 32) | words[0], word=(0, 1))
            dec("COUNT", words[2], word=2)
            dec("SWAP", (words[2] >> 24) & 0x3, word=2)
            dec("CACHE_POLICY", (words[2] >> 26) & 0x7, word=2)
            for n in range(3, len(words)):
                hx(f"DATA_{n - 3}", words[n], word=n)
            return label, fields, True

        if sub_op == 1:  # TILED
            label = "WRITE (TILED)"
            dec("ENCRYPT", (header_dw >> 16) & 0x1)
            dec("TMZ", (header_dw >> 18) & 0x1)
            hx("DST_ADDR", (words[1] << 32) | words[0], word=(0, 1))
            dec("WIDTH", (words[2] >> 16) & 0x3FFF, word=2)
            dec("HEIGHT", words[3] & 0x3FFF, word=3)
            dec("DEPTH", (words[3] >> 16) & z_mask, word=3)
            dec("ELEMENT_SIZE", words[4] & 0x7, word=4)
            dec("SWIZZLE_MODE", (words[4] >> 3) & 0x1F, word=4)
            dec("DIMENSION", (words[4] >> 9) & 0x3, word=4)
            dec("EPITCH", (words[4] >> 16) & 0xFFFF, word=4)
            dec("X", words[5] & 0x3FFF, word=5)
            dec("Y", (words[5] >> 16) & 0x3FFF, word=5)
            dec("Z", words[6] & z_mask, word=6)
            dec("SW", (words[6] >> 24) & 0x3, word=6)
            dec("CACHE_POLICY", (words[6] >> 26) & 0x7, word=6)
            dec("COUNT", words[7] & 0xFFFFF, word=7)
            for n in range(8, len(words)):
                hx(f"DATA_{n - 8}", words[n], word=n)
            return label, fields, True

        return None, fields, False  # 17 (TILED_BC) -- legacy, see docstring

    if op == 4:  # INDIRECT -- see module docstring re: this deviation
        label = "INDIRECT_BUFFER"
        dec("VMID", (header_dw >> 16) & 0xF)
        dec("PRIV", (header_dw >> 31) & 0x1)
        hx("IB_ADDR", (words[1] << 32) | words[0], word=(0, 1))
        dec("IB_SIZE", words[2], word=2)
        if len(words) > 4:
            hx("IB_CSA_ADDR", (words[4] << 32) | words[3], word=(3, 4))
        return label, fields, True

    if op == 5:  # FENCE
        label = "FENCE"
        dec("L2_POLICY", (header_dw >> 24) & 0x3)
        dec("LLC_POLICY", (header_dw >> 26) & 0x1)
        hx("FENCE_ADDR", (words[1] << 32) | words[0], word=(0, 1))
        dec("FENCE_DATA", words[2], word=2)
        return label, fields, True

    if op == 6:  # TRAP -- kept beyond decode_upto_ai's own coverage, see docstring
        label = "TRAP"
        hx("TRAP_INT_CONTEXT", words[0] & 0xFFFFFF, word=0)
        return label, fields, True

    if op == 7:  # SEM / MEM_INCR
        if sub_op == 0:  # SEM
            label = "SEM"
            dec("WRITE_ONE", (header_dw >> 29) & 1)
            dec("SIGNAL", (header_dw >> 30) & 1)
            dec("MAILBOX", (header_dw >> 31) & 1)
            hx("SEMAPHORE_ADDR", (words[1] << 32) | words[0], word=(0, 1))
            return label, fields, True
        if sub_op == 1:  # MEM_INCR
            label = "SEM (MEM_INCR)"
            dec("L2_POLICY", (header_dw >> 24) & 0x3)
            dec("LLC_POLICY", (header_dw >> 26) & 0x1)
            hx("ADDR", (words[1] << 32) | words[0], word=(0, 1))
            return label, fields, True
        return None, fields, False

    if op == 8:  # POLL_REGMEM
        if sub_op == 0:  # POLL_REGMEM (register or memory)
            label = "POLL_REGMEM"
            dec("CACHE_POLICY", (header_dw >> 20) & 0x7)
            dec("HDP_FLUSH", (header_dw >> 26) & 1)
            fields.append((None, "FUNCTION", _POLL_REGMEM_FUNCS[(header_dw >> 28) & 7]))
            mem_poll = bool(header_dw & (1 << 31))
            dec("MEM_POLL", int(mem_poll))
            if not mem_poll:
                hx("REGISTER", (words[0] >> 2) & 0x3FFFF, word=0)
                if ((header_dw >> 26) & 3) == 1:  # HDP_FLUSH provides a write register
                    hx("REGISTER", (words[1] >> 2) & 0xFFFF, word=1)
                else:
                    hx("RESERVED", words[1], word=1)
            else:
                hx("POLL_REGMEM_ADDR", (words[1] << 32) | words[0], word=(0, 1))
            hx("VALUE", words[2], word=2)
            hx("MASK", words[3], word=3)
            dec("INTERVAL", words[4] & 0xFFFF, word=4)
            dec("RETRY_COUNT", (words[4] >> 16) & 0xFFF, word=4)
            return label, fields, True
        if sub_op == 1:  # POLL_REG_WRITE_MEM
            label = "POLL_REG_WRITE_MEM"
            dec("CACHE_POLICY", (header_dw >> 24) & 0x7)
            hx("SRC_ADDR", words[0], word=0)
            hx("DST_ADDR", (words[2] << 32) | words[1], word=(1, 2))
            return label, fields, True
        if sub_op == 2:  # POLL_DBIT_WRITE_MEM
            label = "POLL_DBIT_WRITE_MEM"
            dec("EA", (header_dw >> 16) & 0x3)
            dec("CACHE_POLICY", (header_dw >> 24) & 0x7)
            hx("DST_ADDR", (words[1] << 32) | words[0], word=(0, 1))
            dec("START_PAGE", (words[2] >> 4) & 0xFFFFFFF, word=2)
            dec("PAGE_NUM", words[3], word=3)
            return label, fields, True
        if sub_op == 3:  # MEM_VERIFY
            label = "MEM_VERIFY"
            dec("CACHE_POLICY", (header_dw >> 24) & 0x7)
            dec("MODE", (header_dw >> 31) & 0x1)
            hx("PATTERN", words[0], word=0)
            hx("CMP0_ADDR_START", (words[2] << 32) | words[1], word=(1, 2))
            hx("CMP0_ADDR_END", (words[4] << 32) | words[3], word=(3, 4))
            hx("CMP1_ADDR_START", (words[6] << 32) | words[5], word=(5, 6))
            hx("CMP1_ADDR_END", (words[8] << 32) | words[7], word=(7, 8))
            hx("REC_ADDR", (words[10] << 32) | words[9], word=(9, 10))
            return label, fields, True
        if sub_op == 4:  # INVALIDATION
            label = "INVALIDATION"
            hx("INVALIDATEREQ", words[0], word=0)
            hx("ADDRESSRANGE", words[1], word=1)
            dec("INVALIDATEACK", words[2] & 0xFFFF, word=2)
            dec("ADDRESSRANGE_HI", (words[2] >> 16) & 0x1F, word=2)
            dec("INVALIDATEGFXHUB", (words[2] >> 21) & 0x1, word=2)
            dec("INVALIDATEMMHUB", (words[2] >> 22) & 0x1, word=2)
            return label, fields, True
        return None, fields, False

    if op == 9:  # COND_EXE
        label = "COND_EXE"
        dec("CACHE_POLICY", (header_dw >> 24) & 0x7)
        hx("ADDR", (words[1] << 32) | words[0], word=(0, 1))
        dec("REFERENCE", words[2], word=2)
        dec("EXEC_COUNT", words[3], word=3)
        return label, fields, True

    if op == 10:  # ATOMIC
        label = "ATOMIC"
        dec("LOOP", (header_dw >> 16) & 1)
        dec("TMZ", (header_dw >> 18) & 0x1)
        dec("CACHE_POLICY", (header_dw >> 20) & 0x7)
        hx("OP", (header_dw >> 25) & 0x7F)
        hx("ADDR", (words[1] << 32) | words[0], word=(0, 1))
        hx("SRC_DATA", (words[3] << 32) | words[2], word=(2, 3))
        hx("CMP_DATA", (words[5] << 32) | words[4], word=(4, 5))
        dec("LOOP_INTERVAL", words[6] & 0x1FFF, word=6)
        return label, fields, True

    if op == 11:  # FILL
        if sub_op == 0:  # CONST_FILL
            label = "FILL"
            dec("SWAP", (header_dw >> 16) & 0x3)
            dec("CACHE_POLICY", (header_dw >> 24) & 0x7)
            dec("FILL_SIZE", (header_dw >> 30) & 0x3)
            hx("CONST_FILL_DST", (words[1] << 32) | words[0], word=(0, 1))
            hx("CONST_FILL_DATA", words[2], word=2)
            dec("CONST_FILL_BYTE_COUNT", words[3], word=3)
            return label, fields, True
        if sub_op == 1:  # DATA_FILL_MULTI
            label = "FILL (DATA_FILL_MULTI)"
            dec("MEMLOG_CLR", (header_dw >> 31) & 0x1)
            dec("BYTE_STRIDE", words[0], word=0)
            dec("DMA_COUNT", words[1], word=1)
            hx("DST_ADDR", (words[3] << 32) | words[2], word=(2, 3))
            dec("COUNT", words[4] & 0x3FFFFFF, word=4)
            return label, fields, True
        return None, fields, False

    if op == 12:  # PTE
        if sub_op == 0:  # GEN_PTEPDE
            label = "PTE (GEN_PTEPDE)"
            hx("DST_ADDR", (words[1] << 32) | words[0], word=(0, 1))
            dec("CACHE_POLICY", (header_dw >> 24) & 0x7)
            hx("MASK", (words[3] << 32) | words[2], word=(2, 3))
            hx("INIT", (words[5] << 32) | words[4], word=(4, 5))
            hx("INCR", (words[7] << 32) | words[6], word=(6, 7))
            dec("COUNT", words[8] & 0x7FFFF, word=8)
            return label, fields, True
        if sub_op == 1:  # COPY
            label = "PTE (COPY)"
            dec("TMZ", (header_dw >> 18) & 0x1)
            dec("PTEPDE_OP", (header_dw >> 31) & 0x1)
            hx("SRC_ADDR", (words[1] << 32) | words[0], word=(0, 1))
            hx("DST_ADDR", (words[3] << 32) | words[2], word=(2, 3))
            hx("MASK", (words[5] << 32) | words[4], word=(4, 5))
            dec("COUNT", words[6] & 0x7FFFF, word=6)
            dec("DST_CACHE_POLICY", (words[6] >> 22) & 0x7, word=6)
            dec("SRC_CACHE_POLICY", (words[6] >> 29) & 0x7, word=6)
            return label, fields, True
        if sub_op == 2:  # RMW
            label = "PTE (RMW)"
            dec("MTYPE", (header_dw >> 16) & 0x7)
            dec("GCC", (header_dw >> 19) & 0x1)
            dec("SYS", (header_dw >> 20) & 0x1)
            dec("SNP", (header_dw >> 22) & 0x1)
            dec("GPA", (header_dw >> 23) & 0x1)
            dec("L2_POLICY", (header_dw >> 24) & 0x3)
            dec("LLC_POLICY", (header_dw >> 26) & 0x1)
            hx("ADDR", (words[1] << 32) | words[0], word=(0, 1))
            hx("MASK", (words[3] << 32) | words[2], word=(2, 3))
            hx("VALUE", (words[5] << 32) | words[4], word=(4, 5))
            dec("NUM_OF_PTE", words[6], word=6)
            return label, fields, True
        return None, fields, False

    if op == 13:  # TIMESTAMP
        if sub_op == 0:  # SET
            label = "TIMESTAMP (SET)"
            hx("INIT_DATA", (words[1] << 32) | words[0], word=(0, 1))
            return label, fields, True
        if sub_op in (1, 2):  # GET / GET_GLOBAL
            label = f"TIMESTAMP ({'GET_GLOBAL' if sub_op == 2 else 'GET'})"
            dec("L2_POLICY", (header_dw >> 24) & 0x3)
            dec("LLC_POLICY", (header_dw >> 26) & 0x1)
            hx("WRITE_ADDR", (words[1] << 32) | words[0], word=(0, 1))
            return label, fields, True
        return None, fields, False

    if op == 14:  # SRBM_WRITE / RMW_REGISTER
        if sub_op == 0:  # SRBM_WRITE
            label = "SRBM_WRITE"
            dec("BYTE_ENABLE", header_dw >> 28)
            hx("SRBM_WRITE_ADDR", words[0] & 0x3FFFF, word=0)
            hx("SRBM_WRITE_DATA", words[1], word=1)
            return label, fields, True
        return None, fields, False  # RMW_REGISTER -- UMR itself doesn't decode this for AI

    return None, fields, False  # SEM.default / PRE_EXE / GPUVM_TLB_INV / GCR -- not ported


_PKT_LEFTCOL_WIDTH = 35  # width of the hex column (before "| "), confirmed with the user
_PKT_SEPARATOR_WIDTH = 84

# ANSI, used only when a caller opts in via use_color=True (see
# _render_packet_title) -- callers writing to a real terminal for a human to
# read (queue_viewer.py's REPL, rocgdb's interactive dump_hsa_queue/
# dump_sdma_queue commands) pass True; anything writing to a file or
# collecting lines for JSON (dump_all_queues_txt's per-queue .log files,
# queue_viewer.py --web's HTTP responses) must NOT -- raw escape codes would
# either sit uselessly in a saved log or render as garbage in a browser.
_ANSI_RED = "\033[1;31m"
_ANSI_GREEN = "\033[1;32m"
_ANSI_RESET = "\033[0m"


def _pkt_hex_bytes(dword):
    return " ".join(f"{b:02x}" for b in struct.pack("<I", dword & 0xFFFFFFFF))


def _render_packet_title(emit, addr, i, size, type_label, use_color=False):
    """Emit the "Packet #N at 0x... (N bytes) <TYPE LABEL>" title framed by
    separator lines, shared by the HSA and SDMA renderers. When use_color is
    set, the type label itself is colored -- red for INVALID (an AQL type-1
    slot, usually meaning idle/already-consumed rather than a real packet),
    green for everything else -- so a scroll of packet titles is easy to
    scan by eye for the invalid ones. Padding is computed from the
    *uncolored* label length so the ANSI escape bytes (invisible on screen)
    don't throw off the column alignment."""
    title = f"Packet #{i} at 0x{addr:x} ({size} bytes)"
    pad = max(1, _PKT_SEPARATOR_WIDTH - 2 - len(title) - len(type_label))
    displayed_label = type_label
    if use_color:
        color = _ANSI_RED if type_label == "INVALID" else _ANSI_GREEN
        displayed_label = f"{color}{type_label}{_ANSI_RESET}"
    emit("-" * _PKT_SEPARATOR_WIDTH)
    emit(f"{title}{' ' * pad}{displayed_label}")
    emit("-" * _PKT_SEPARATOR_WIDTH)


def _pkt_row(emit, left_text, field_text=None):
    """Emit one two-column row: hex-only (no trailing pad) when field_text
    is None -- e.g. a bracket-open row -- otherwise left-padded hex, "| ",
    then the field text (already "NAME = value" or "NAME" for a bare note
    like "(reserved)")."""
    if field_text is None:
        emit(left_text)
    else:
        emit(f"{left_text.ljust(_PKT_LEFTCOL_WIDTH)}| {field_text}")


def _emit_field_groups(emit, fields, words, byte_offset):
    """Shared two-column rendering core for a packet's field list (used by
    both the SDMA and HSA decoders -- see their respective callers for what
    "fields"/"words" mean in each case). Groups consecutive fields sharing
    the same word_ref so the dword's hex is shown only once; draws a
    "┌"/"┘" bracket connecting the two dwords of a 64-bit LO/HI field
    (word_ref a 2-tuple), with the field text on the closing row only.
    word_ref is None for fields with no independent hex to show.

    `byte_offset(word_index) -> int` maps a word index to its "+0xNN" label
    -- SDMA's words[] starts right after a separately-tracked header dword
    (offset 4+4*i), while HSA's starts at the very first byte of the packet
    (offset 4*i), since HSA packs 'header'/'setup'-like sub-fields into the
    same dword rather than keeping a dedicated header dword.
    """
    idx = 0
    n = len(fields)
    while idx < n:
        word_ref = fields[idx][0]
        group = []
        while idx < n and fields[idx][0] == word_ref:
            group.append((fields[idx][1], fields[idx][2]))
            idx += 1

        if isinstance(word_ref, tuple):
            lo, hi = word_ref
            _pkt_row(emit, f"+0x{byte_offset(lo):02x}  {_pkt_hex_bytes(words[lo])} ┌")
            first_left = f"+0x{byte_offset(hi):02x}  {_pkt_hex_bytes(words[hi])} ┘"
        elif word_ref is None:
            first_left = ""
        else:
            first_left = f"+0x{byte_offset(word_ref):02x}  {_pkt_hex_bytes(words[word_ref])}"

        name, value = group[0]
        _pkt_row(emit, first_left, name if value is None else f"{name} = {value}")
        for name, value in group[1:]:
            _pkt_row(emit, "", name if value is None else f"{name} = {value}")


def _render_sdma_packet(emit, addr, i, op, sub_op, header_dw, words, label, fields, decoded, size, use_color=False):
    """Render one decoded SDMA packet in the two-column hex/field layout
    (see _emit_field_groups for the shared mechanics). `words[i]` is the
    dword at byte offset 4+4*i -- right after the header dword, which is
    tracked separately and always shown as its own first row.
    """
    type_label = label if label else f"OP=0x{op:x} SUB_OP=0x{sub_op:x}"
    _render_packet_title(emit, addr, i, size, type_label, use_color=use_color)
    _pkt_row(emit, f"+0x00  {_pkt_hex_bytes(header_dw)}", f"HEADER op=0x{op:x} sub_op=0x{sub_op:x}")
    _emit_field_groups(emit, fields, words, lambda w: 4 + 4 * w)

    if not decoded:
        _pkt_row(emit, "", "(recognized, not decoded in detail)")

    emit("-" * _PKT_SEPARATOR_WIDTH)


def decode_sdma_packets(reader, base, max_size, emit=print, _depth=0, use_color=False):
    """Walk and decode SDMA packets starting at base for up to max_size
    bytes. Sizing is a full port of UMR's sized_oss1_5() (all opcodes/
    sub-opcodes, generation-agnostic); field-level decoding matches
    decode_upto_ai(), the generation confirmed for this host's real
    hardware (SDMA IP major 4) -- see _sdma_decode_fields()'s docstring for
    exactly what that covers and the two deliberate additions beyond it.
    Rendered via _render_sdma_packet() as a two-column hex/field view.

    Stops early on a null (op==0, size-0) opcode, an unreadable/unknown
    byte, an opcode/sub-opcode this port doesn't recognize at all (can't
    size it -- no safe way to keep walking), or hitting max_size.

    INDIRECT packets are followed one level via `reader.read()` at the IB's
    address -- this works transparently when `reader` can read anywhere in
    the process (the live rocgdb path), and cleanly fails with "IB not
    available in this dump" when it can't (the offline queue_viewer.py path,
    whose BufferReader only has the ring's own dumped bytes). `_depth`
    guards against runaway/cyclic IB chains.

    use_color: colorize each packet's title -- see decode_hsa_packets'
    docstring for when this is/isn't safe to pass True. SDMA packets have no
    "INVALID" concept (that's AQL/HSA-specific), so every title renders
    green when this is set.
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

        if nwords is None:
            emit("-" * _PKT_SEPARATOR_WIDTH)
            emit(f"Packet #{i} at 0x{addr:x}: op=0x{op:x} sub_op=0x{sub_op:x} (unrecognized opcode, stopping)")
            break

        size = 4 + nwords * 4

        try:
            data = reader.read(addr, size)
        except MemoryReadError:
            emit(f"Cannot read memory at 0x{addr:x}")
            break
        words = list(struct.unpack_from(f"<{nwords}I", data, 4)) if nwords else []

        try:
            label, fields, decoded = _sdma_decode_fields(op, sub_op, header_dw, words)
        except IndexError:
            label, fields, decoded = None, [], False
        _render_sdma_packet(emit, addr, i, op, sub_op, header_dw, words, label, fields, decoded, size, use_color=use_color)

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
                    decode_sdma_packets(reader, ib_addr, ib_size, emit=emit, _depth=_depth + 1, use_color=use_color)

        addr += size
        i += 1
