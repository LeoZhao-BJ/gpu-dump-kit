#!/usr/bin/env bash
# Automates the "attach + dump_all_queues" workflow -- same idea as the older
# `sudo rocgdb -x save_info.gdb` one-shot script, except the pid comes from a
# command-line argument instead of being hardcoded/hand-edited into a .gdb
# file, and it uses rocgdb_helper.py's dump_all_queues command (fast binary
# capture -- see README) instead of save_info.gdb's plain `info` commands.
#
# Usage:
#   ./dump_all_queues.sh <pid> [output_dir]
#   ./dump_all_queues.sh --txt <pid> [output_dir]   # slower live text decode
#                                                    # instead of the binary
#                                                    # capture (dump_all_queues_txt)
#
# Requires root (or passwordless ptrace access) to attach to another user's
# process -- re-invokes itself via sudo automatically if not already root.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPER="$SCRIPT_DIR/rocgdb_helper.py"

DUMP_COMMAND="dump_all_queues"

usage() {
    cat >&2 <<EOF
Usage: $(basename "$0") [--txt] <pid> [output_dir]

  <pid>          pid of the process to attach to and dump queues from
  [output_dir]   optional -- dump_all_queues picks its own timestamped
                 directory name if omitted (see README)
  --txt          use dump_all_queues_txt (live text decode, slower) instead
                 of the default dump_all_queues (fast binary capture)

Examples:
  $(basename "$0") 12345
  $(basename "$0") 12345 /tmp/my_capture
  $(basename "$0") --txt 12345
EOF
    exit 1
}

while [ $# -gt 0 ] && [[ "$1" == --* ]]; do
    case "$1" in
        --txt)
            DUMP_COMMAND="dump_all_queues_txt"
            shift
            ;;
        --help|-h)
            usage
            ;;
        *)
            echo "error: unknown option '$1'" >&2
            usage
            ;;
    esac
done

[ $# -ge 1 ] && [ $# -le 2 ] || usage

PID="$1"
OUTDIR="${2:-}"

if ! [[ "$PID" =~ ^[0-9]+$ ]]; then
    echo "error: '$PID' is not a valid pid (expected a number)" >&2
    exit 1
fi

if [ ! -d "/proc/$PID" ]; then
    echo "error: no process with pid $PID (checked /proc/$PID)" >&2
    exit 1
fi

if [ ! -f "$HELPER" ]; then
    echo "error: rocgdb_helper.py not found at $HELPER" >&2
    exit 1
fi

if ! command -v rocgdb >/dev/null 2>&1; then
    echo "error: rocgdb not found on PATH" >&2
    exit 1
fi

DUMP_INVOCATION="$DUMP_COMMAND"
if [ -n "$OUTDIR" ]; then
    DUMP_INVOCATION="$DUMP_COMMAND $OUTDIR"
fi

SUDO=()
if [ "$(id -u)" -ne 0 ]; then
    SUDO=(sudo)
fi

LOGFILE="dump_all_queues_pid${PID}_$(date +%Y%m%d_%H%M%S).log"

echo "Attaching rocgdb to pid $PID, running '$DUMP_INVOCATION' ..."
"${SUDO[@]}" rocgdb -q -batch \
    -ex "attach $PID" \
    -ex "source $HELPER" \
    -ex "$DUMP_INVOCATION" \
    -ex "detach" \
    -ex "quit" \
    2>&1 | tee "$LOGFILE"

echo
echo "Full rocgdb session log saved to: $LOGFILE"
