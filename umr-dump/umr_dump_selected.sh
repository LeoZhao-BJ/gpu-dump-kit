#!/usr/bin/env bash
#
# umr_dump_selected.sh - dump mqds/hqds/rls (KFD debugfs), CPC (incl. HQD
# pipe/queue registers), and CU wave state for an explicitly selected subset
# of GPUs, using the umr build at /home/liangzh/umr/umr/build-dir.
#
# Pattern follows umr-portable/umr_dump.sh (root check, binary resolution,
# timestamped output dir, `run()` wrapper, tar.gz at the end) but adds
# explicit multi-GPU subset selection and targets this repo's current umr
# CLI, where the old short flag `-cpc` no longer exists -- it was replaced
# by `--tool cpc` (verified: `grep -rn '"-cpc"' src/` in umr/ returns nothing
# in the freshly built commit, and `--help` only lists `--tool [cpc, cpg, sdma]`).
# `--waves`/`-wa` itself is unchanged, but this build adds an `all=` prefix
# that scans every XCD/GFX block in one call, so there's no need to loop
# `-vmp` for waves the way umr_dump.sh and debug_scripts/08_dump_all_gpu_waves.sh
# do (those predate `all=` or target an older umr).
#
# `-i` takes amd-smi GPU IDs, NOT umr's own "instance" numbers.
# Verified on real hardware that these are NOT the same numbering: `umr -e`'s
# instance is whichever /sys/kernel/debug/dri/<N> directory the card landed
# on (e.g. 1, 9, 17, 25, 33, 41, 49, 57 on this 8-GPU box, since every
# render/control debugfs node -- ~190 of them here -- gets its own directory
# number), while `amd-smi list` numbers only the real GPUs sequentially
# (0..7). The two are cross-referenced here by PCI BDF, which both tools
# print in the identical "dddd:bb:ss.f" form (confirmed byte-for-byte equal
# between `amd-smi list --json`'s .bdf and `umr -e`'s "asic.devname =="),
# so `-i 0,3` means "amd-smi GPU 0 and 3" and the script looks up the right
# `-i <umr instance>` to actually pass to umr. `-p <bdf,...>` remains
# available as a direct escape hatch that never touches amd-smi at all.
#
# NOTE: /sys/kernel/debug/kfd/{rls,mqds,hqds} are KFD debugfs files, not umr
# flags, and are inherently system-wide (they list every GPU's queues in one
# file). There is no umr-native way to filter them to just the selected
# GPUs, so they're captured once, unfiltered, same as umr_dump.sh's own kfd
# dump -- labelled clearly below so that's not mistaken for a per-GPU dump.
#
# --pid PID (optional): also dump the FULL retained user-queue ring (not just
# the live rptr/wptr window) for every KFD user queue owned by that process,
# on each of the -i/-p-selected GPUs. Ports debug_scripts/10_dump_full_user_
# queue_rings.sh's core two-step approach:
#   1. `umr --by-pci <bdf> --vm-partition <N> --list-uq` lists, per device,
#      every process's queues with umr's own LOCAL queue index (0-based, per
#      device) -- this is NOT the KFD sysfs queue directory number, the two
#      numbering schemes are unrelated.
#   2. for each local index found under this PID's "Client #: ... tgid=<PID>"
#      block, `--user-queue kfd,pid=<PID>,queue=<local id> -O
#      use_full_user_queue,no_backtrace --dump-uq` dumps that queue's full
#      retained ring.
# Simplification vs. the reference script: that script first scans KFD sysfs
# (/sys/class/kfd/kfd/proc/<pid>/queues + topology) to discover which GPUs to
# even bother querying, since it sweeps every GPU on the box. This script
# already has an explicit, small, user-picked GPU set (-i/-p), so it skips
# that sysfs/topology discovery machinery entirely and just runs --list-uq
# directly on each selected GPU -- if the PID has no queues there, the
# Client-block parse below simply finds nothing and says so.
#
# Portability to other machines: nothing here hardcodes a PCI BDF, umr
# instance number, or amd-smi ID -- both mapping tables are rebuilt fresh
# from `amd-smi list`/`umr -e` every time the script runs, so a different
# machine with completely different BDFs/instance numbers works the same
# way with no edits. Two things ARE machine/build-specific and are handled
# as follows:
#   - the umr binary path: resolved in priority order --umr-bin PATH, then
#     $UMR_BIN, then a real system install (/usr/local/bin/umr, /usr/bin/umr,
#     or anything else on PATH), then this repo's own dev build tree or a
#     bundled ./bin/umr next to this script (matches umr-portable's own
#     tarball layout) as a last resort. See BIN_CANDS below.
#   - VM-partition count (XCD count for MI300-style multi-die ASICs): this
#     varies by ASIC/config, so instead of assuming a fixed number it's
#     auto-detected per selected GPU from `umr -e`'s own IP Blocks listing
#     (counting "gfx<family>{N}" instances -- confirmed on real MI300X
#     hardware: probing a -vmp index past the real count doesn't hang, but
#     it does return a `[ERROR]: ...UNSORTED database` line instead of real
#     register data, so guessing too high pollutes output files, and a
#     fixed guess could also be too LOW on a machine with more partitions
#     than this one). Override with --max-part N if detection is wrong for
#     some ASIC.
#
# Usage:
#   sudo ./umr_dump_selected.sh -i 0,3          # amd-smi GPU IDs 0 and 3
#   sudo ./umr_dump_selected.sh -p 0000:0a:00.0,0000:c8:00.0
#   sudo ./umr_dump_selected.sh -i all          # every amd-smi-listed GPU
#   sudo ./umr_dump_selected.sh -i 0 --no-waves
#   sudo ./umr_dump_selected.sh -i 0 --max-part 3  # override auto-detected partition count
#   sudo ./umr_dump_selected.sh --umr-bin /path/to/umr -i 0  # explicit umr binary
#   sudo ./umr_dump_selected.sh -i 0,1,2 --pid 12345          # + full user-queue rings for PID 12345
#   sudo ./umr_dump_selected.sh -i 0 --pid 12345 --queue 0    # only local queue index 0
#   sudo ./umr_dump_selected.sh -l              # print the amd-smi<->umr mapping and exit
#
# Output: ./umrdump-<host>-<timestamp>/ (+ a .tar.gz next to it).
#
set -uo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---- args -----------------------------------------------------------------
SMI_ARG=""
PCI_ARG=""
DO_WAVES=1
LIST_ONLY=0
MAX_PART_ARG="auto"   # "auto" = detect per-GPU from `umr -e`; or an explicit 0..N override
OUT=""
UMR_BIN_ARG=""
PID_ARG=""
UQ_VMP=0
QUEUE_FILTERS=()

usage() { sed -n '79,90p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        -i) SMI_ARG="$2"; shift 2;;
        -p|--pci) PCI_ARG="$2"; shift 2;;
        --no-waves) DO_WAVES=0; shift;;
        --max-part) MAX_PART_ARG="$2"; shift 2;;
        -o|--outdir) OUT="$2"; shift 2;;
        --umr-bin) UMR_BIN_ARG="$2"; shift 2;;
        --pid) PID_ARG="$2"; shift 2;;
        --queue) QUEUE_FILTERS+=("$2"); shift 2;;
        --uq-vmp) UQ_VMP="$2"; shift 2;;
        -l|--list) LIST_ONLY=1; shift;;
        -h|--help) usage; exit 0;;
        *) echo "unknown arg: $1" >&2; usage; exit 1;;
    esac
done

if [[ -n "$PID_ARG" && ! "$PID_ARG" =~ ^[1-9][0-9]*$ ]]; then
    echo "ERROR: --pid must be a positive integer: $PID_ARG" >&2
    exit 1
fi
for q in "${QUEUE_FILTERS[@]}"; do
    if [[ ! "$q" =~ ^[0-9]+$ ]]; then
        echo "ERROR: --queue must be a non-negative integer: $q" >&2
        exit 1
    fi
done
if [[ ! "$UQ_VMP" =~ ^[0-9]+$ ]]; then
    echo "ERROR: --uq-vmp must be a non-negative integer: $UQ_VMP" >&2
    exit 1
fi

# ---- locate the umr binary -------------------------------------------------
# Priority: explicit user choice first (--umr-bin flag, then $UMR_BIN env var),
# then a real system install (/usr/local/bin, /usr/bin -- what you'd get from
# a package or `make install`), then PATH (covers any other system dir), and
# only as a last resort this repo's own dev build tree / a bundled ./bin/umr
# -- a system install should win over a stale local build if both exist.
if [[ -n "$UMR_BIN_ARG" && ! -x "$UMR_BIN_ARG" ]]; then
    echo "WARNING: --umr-bin $UMR_BIN_ARG is not an executable file; ignoring it." >&2
fi
if [[ -n "${UMR_BIN:-}" && ! -x "${UMR_BIN:-}" ]]; then
    echo "WARNING: \$UMR_BIN=$UMR_BIN is not an executable file; ignoring it." >&2
fi
BIN_CANDS=(
    "$UMR_BIN_ARG"
    "${UMR_BIN:-}"
    "/usr/local/bin/umr"
    "/usr/bin/umr"
    "$(command -v umr 2>/dev/null || true)"
    "$SELF_DIR/bin/umr"
    "$SELF_DIR/umr/build-dir/src/app/umr"
)
UMR_BIN=""
for c in "${BIN_CANDS[@]}"; do [[ -n "$c" && -x "$c" ]] && { UMR_BIN="$c"; break; }; done
[[ -z "$UMR_BIN" ]] && { echo "ERROR: umr binary not found. Pass --umr-bin PATH, set \$UMR_BIN, install umr to /usr/local/bin or /usr/bin, or place one at $SELF_DIR/bin/umr." >&2; exit 1; }

# amd-smi is only required for -i/-l (amd-smi-ID-based selection); -p alone
# never needs it.
AMD_SMI_BIN="$(command -v amd-smi 2>/dev/null || true)"
if [[ -z "$AMD_SMI_BIN" && ( -n "$SMI_ARG" || "$LIST_ONLY" -eq 1 ) ]]; then
    echo "ERROR: amd-smi not found on PATH, required for -i/-l. Use -p <bdf,...> instead if amd-smi is unavailable." >&2
    exit 1
fi
if [[ -n "$AMD_SMI_BIN" ]] && ! command -v jq >/dev/null 2>&1; then
    echo "ERROR: jq not found on PATH, required to parse 'amd-smi list --json'." >&2
    exit 1
fi

# ---- must be root (skip for --help, handled above) -------------------------
if [[ "$(id -u)" -ne 0 ]]; then
    echo "ERROR: must run as root (sudo). umr needs MMIO write access, and KFD debugfs is root-only." >&2
    exit 1
fi

echo "## umr binary : $UMR_BIN"
ENUM_OUT="$("$UMR_BIN" -e 2>&1)"
n_enum="$(echo "$ENUM_OUT" | grep -c '^GPU #')"
if [[ "$LIST_ONLY" -eq 1 ]]; then
    echo "## umr -e     : $n_enum device(s) enumerated"
else
    echo "## umr -e     : $n_enum device(s) enumerated (full text saved to 00_enumerate.txt)"
fi

# ---- umr instance <-> PCI BDF, from `umr -e` --------------------------------
# `umr -e` prints, per device, a block starting "GPU #<inst> => <asicname>",
# followed eventually by "asic.devname == <pci bdf>" (see src/app/print_config.c)
# and an "IP Blocks:" list containing lines like "asicname.gfx944{3}" -- the
# "{N}" suffix is the per-block instance index, which for the gfx block is
# the VM-partition/XCD count for that device (see the portability note in
# the header comment for why this is auto-detected instead of hardcoded).
declare -A PCI_OF_INST=()
declare -A PARTCOUNT_OF_INST=()
inst=""
while IFS= read -r line; do
    if [[ "$line" =~ ^GPU\ \#([0-9]+) ]]; then
        inst="${BASH_REMATCH[1]}"
    elif [[ -n "$inst" && "$line" =~ asic\.devname\ ==\ (.+)$ ]]; then
        PCI_OF_INST["$inst"]="${BASH_REMATCH[1]}"
    elif [[ -n "$inst" && "$line" =~ gfx[0-9]+\{([0-9]+)\} ]]; then
        n=$(( BASH_REMATCH[1] + 1 ))
        cur="${PARTCOUNT_OF_INST[$inst]:-0}"
        (( n > cur )) && PARTCOUNT_OF_INST["$inst"]="$n"
    fi
done <<< "$ENUM_OUT"
declare -A INST_OF_PCI=()
for i in "${!PCI_OF_INST[@]}"; do INST_OF_PCI["${PCI_OF_INST[$i]}"]="$i"; done
[[ ${#PCI_OF_INST[@]} -eq 0 ]] && { echo "ERROR: no AMDGPU devices enumerated by 'umr -e' (as root)." >&2; exit 1; }

# get_max_part <inst> -> echoes the max vmp index to loop to (inclusive).
get_max_part() {
    if [[ "$MAX_PART_ARG" != "auto" ]]; then
        echo "$MAX_PART_ARG"; return
    fi
    local n="${PARTCOUNT_OF_INST[$1]:-0}"
    if [[ "$n" -eq 0 ]]; then
        echo "WARNING: could not detect a partition count for umr instance $1 from 'umr -e'; assuming 1 (vmp 0 only). Pass --max-part N to override." >&2
        echo 0
    else
        echo $(( n - 1 ))
    fi
}

# ---- amd-smi GPU ID <-> PCI BDF, from `amd-smi list` ------------------------
declare -A PCI_OF_SMI=()
if [[ -n "$AMD_SMI_BIN" ]]; then
    while IFS=$'\t' read -r gid bdf; do
        [[ -n "$gid" ]] && PCI_OF_SMI["$gid"]="$bdf"
    done < <("$AMD_SMI_BIN" list --json 2>/dev/null | jq -r '.[] | "\(.gpu)\t\(.bdf)"')
fi

if [[ "$LIST_ONLY" -eq 1 ]]; then
    echo
    printf "%-10s %-16s %-14s\n" "amd-smi" "PCI BDF" "umr instance"
    if [[ ${#PCI_OF_SMI[@]} -gt 0 ]]; then
        for gid in $(printf '%s\n' "${!PCI_OF_SMI[@]}" | sort -n); do
            bdf="${PCI_OF_SMI[$gid]}"
            printf "%-10s %-16s %-14s\n" "$gid" "$bdf" "${INST_OF_PCI[$bdf]:-<none>}"
        done
    else
        echo "(amd-smi not available; showing umr instances only)"
        for i in "${!PCI_OF_INST[@]}"; do printf "%-10s %-16s %-14s\n" "-" "${PCI_OF_INST[$i]}" "$i"; done
    fi
    exit 0
fi

# ---- resolve the requested subset --------------------------------------
# SEL_LABEL is used for output filenames (amd-smi ID when known, else a
# sanitized PCI BDF); SEL_INST is the real umr "-i" value passed to umr.
SEL_LABEL=()
SEL_INST=()
add_selection() {  # add_selection <label> <umr-instance>
    SEL_LABEL+=("$1"); SEL_INST+=("$2")
}

if [[ -n "$SMI_ARG" ]]; then
    if [[ "$SMI_ARG" == "all" ]]; then
        want_smi=($(printf '%s\n' "${!PCI_OF_SMI[@]}" | sort -n))
    else
        IFS=',' read -ra want_smi <<< "$SMI_ARG"
    fi
    for gid in "${want_smi[@]}"; do
        bdf="${PCI_OF_SMI[$gid]:-}"
        if [[ -z "$bdf" ]]; then
            echo "WARNING: amd-smi has no GPU $gid" >&2; continue
        fi
        inst="${INST_OF_PCI[$bdf]:-}"
        if [[ -z "$inst" ]]; then
            echo "WARNING: amd-smi GPU $gid (PCI $bdf) has no matching umr instance" >&2; continue
        fi
        add_selection "smi${gid}" "$inst"
    done
elif [[ -n "$PCI_ARG" ]]; then
    IFS=',' read -ra want_pci <<< "$PCI_ARG"
    for bdf in "${want_pci[@]}"; do
        inst="${INST_OF_PCI[$bdf]:-}"
        if [[ -z "$inst" ]]; then
            echo "WARNING: no umr instance matches PCI $bdf" >&2; continue
        fi
        add_selection "pci$(echo "$bdf" | tr ':.' '__')" "$inst"
    done
else
    echo "ERROR: specify which GPUs to dump: -i <amd-smi-id[,...]|all> or -p <pci[,...]>. See -l to list." >&2
    exit 1
fi
[[ ${#SEL_INST[@]} -eq 0 ]] && { echo "ERROR: no GPUs resolved from selection." >&2; exit 1; }
echo "## selected: $(paste -d' ' <(printf '%s\n' "${SEL_LABEL[@]}") <(printf '(umr -i %s)\n' "${SEL_INST[@]}") | paste -sd, -)"

# ---- output dir -------------------------------------------------------------
TS="$(date +%Y%m%d-%H%M%S)"
PID_SUFFIX=""
[[ -n "$PID_ARG" ]] && PID_SUFFIX="-pid${PID_ARG}"
[[ -z "$OUT" ]] && OUT="umrdump-$(hostname -s)-$(IFS=+; echo "${SEL_LABEL[*]}")${PID_SUFFIX}-${TS}"
mkdir -p "$OUT"
echo "## output dir : $OUT"
echo "$ENUM_OUT" > "$OUT/00_enumerate.txt"

run() {  # run() <logfile> <umr args...>
    local log="$1"; shift
    echo "  -> umr $* > $log"
    timeout 240 "$UMR_BIN" "$@" > "$OUT/$log" 2>&1
}

# ---- 1) system-wide KFD debugfs (rls/mqds/hqds cover every GPU at once) -----
{
    echo "===== /sys/kernel/debug/kfd/rls (system-wide, all GPUs) ====="
    cat /sys/kernel/debug/kfd/rls 2>&1
    echo; echo "===== /sys/kernel/debug/kfd/mqds (system-wide, all GPUs) ====="
    cat /sys/kernel/debug/kfd/mqds 2>&1
    echo; echo "===== /sys/kernel/debug/kfd/hqds (system-wide, all GPUs) ====="
    cat /sys/kernel/debug/kfd/hqds 2>&1
} > "$OUT/10_kfd.txt" 2>&1

# ---- 2) per-GPU CPC walk (includes per-pipe/queue HQD registers) -----------
for idx in "${!SEL_INST[@]}"; do
    label="${SEL_LABEL[$idx]}"; inst="${SEL_INST[$idx]}"
    mp="$(get_max_part "$inst")"
    echo "[$label (umr -i $inst)] partitions 0..$mp"
    for p in $(seq 0 "$mp"); do
        echo "[$label (umr -i $inst) / partition $p] CPC walk"
        run "20_cpc_${label}_vmp${p}.txt" -i "$inst" -vmp "$p" --tool cpc
    done
done

# ---- 3) per-GPU waves (all XCD/GFX blocks in one call) ---------------------
if [[ "$DO_WAVES" -eq 1 ]]; then
    for idx in "${!SEL_INST[@]}"; do
        label="${SEL_LABEL[$idx]}"; inst="${SEL_INST[$idx]}"
        echo "[$label (umr -i $inst)] wave halt + dump (all XCDs)"
        run "30_waves_${label}.txt" -i "$inst" -O bits,halt_waves --waves all=none
    done
fi

# ---- 4) full user-queue ring dump for --pid (skipped if --pid not given) --
# list_uq_local_queues <list_uq_log> <pid> -> "local_id<TAB>type" lines, one
# per queue umr's --list-uq shows under that PID's "Client #: ... tgid=<pid>"
# block. Ported as-is from debug_scripts/10_dump_full_user_queue_rings.sh.
list_uq_local_queues() {
    local path="$1" pid="$2" in_block=0 line
    while IFS= read -r line; do
        if [[ "$line" == "Client #: "*"tgid=${pid} type="* ]]; then
            in_block=1; continue
        fi
        if (( in_block )); then
            if [[ "$line" =~ queue=([0-9]+)\ type=([0-9]+) ]]; then
                printf '%s\t%s\n' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}"
            else
                in_block=0
            fi
        fi
    done < "$path"
}
queue_wanted() {  # queue_wanted <local_id> -> 0 (yes) if no --queue filter, else match
    [[ ${#QUEUE_FILTERS[@]} -eq 0 ]] && return 0
    local q; for q in "${QUEUE_FILTERS[@]}"; do [[ "$q" == "$1" ]] && return 0; done
    return 1
}

uq_captures=0
uq_failures=0
if [[ -n "$PID_ARG" ]]; then
    echo
    echo "## full user-queue ring dump for PID $PID_ARG (vm-partition $UQ_VMP)"
    for idx in "${!SEL_INST[@]}"; do
        label="${SEL_LABEL[$idx]}"; inst="${SEL_INST[$idx]}"
        bdf="${PCI_OF_INST[$inst]}"
        list_log="40_uq_list_${label}.txt"
        echo "[$label ($bdf)] listing user queues"
        run "$list_log" --by-pci "$bdf" --vm-partition "$UQ_VMP" --list-uq
        mapfile -t entries < <(list_uq_local_queues "$OUT/$list_log" "$PID_ARG")
        if [[ ${#entries[@]} -eq 0 ]]; then
            echo "  no queues found for PID $PID_ARG on $label ($bdf)"
            continue
        fi
        for entry in "${entries[@]}"; do
            local_id="${entry%%$'\t'*}"; qtype="${entry##*$'\t'}"
            queue_wanted "$local_id" || continue
            ring_log="40_uq_${label}_queue${local_id}_full_ring.txt"
            echo "  [$label] dumping local queue $local_id (type=$qtype)"
            run "$ring_log" --by-pci "$bdf" --vm-partition "$UQ_VMP" \
                --user-queue "kfd,pid=${PID_ARG},queue=${local_id}" \
                -O use_full_user_queue,no_backtrace --dump-uq
            if grep -Eq '^Dumping 0x[0-9a-fA-F]+ words from user queue-' "$OUT/$ring_log"; then
                uq_captures=$((uq_captures + 1))
            else
                uq_failures=$((uq_failures + 1))
                echo "  WARNING: $label queue $local_id: umr did not report a successful dump (see $ring_log)" >&2
            fi
        done
    done
fi

# ---- 5) summary ---------------------------------------------------------
{
    echo "==== ACTIVE QUEUES (CP_HQD_ACTIVE) ===="
    grep -rEh "me[0-9]\.pipe[0-9]\.queue[0-9]" "$OUT"/20_cpc_*.txt 2>/dev/null | sort -u
    echo
    echo "==== NON-ZERO CP_HPD_STATUS0 ===="
    grep -rh "HPD_STATUS0" "$OUT"/20_cpc_*.txt 2>/dev/null | grep -v "HPD_STATUS0: 00000000" | sort -u
    echo
    echo "==== HALTED WAVES ===="
    grep -rEh "se[0-9]+\.sh[0-9]+\.cu[0-9]+\.simd[0-9]+\.wave[0-9]+" "$OUT"/30_waves_*.txt 2>/dev/null | wc -l | xargs echo "wave count:"
    if [[ -n "$PID_ARG" ]]; then
        echo
        echo "==== USER QUEUE RINGS (PID $PID_ARG) ===="
        echo "captured: $uq_captures, failed: $uq_failures"
    fi
} > "$OUT/99_summary.txt" 2>&1

echo
echo "================= SUMMARY ================="
cat "$OUT/99_summary.txt"
echo "==========================================="

# ---- 6) tar it up ---------------------------------------------------------
tar czf "${OUT}.tar.gz" "$OUT"
case "$OUT" in
    /*) echo "## archive: ${OUT}.tar.gz" ;;
    *)  echo "## archive: $(pwd)/${OUT}.tar.gz" ;;
esac
