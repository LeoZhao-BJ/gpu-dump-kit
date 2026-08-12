# umr-dump

A shell script that wraps [UMR](https://gitlab.freedesktop.org/tomstdenis/umr) ("User Mode
Register Debugger for AMDGPU Hardware") to capture hardware/queue state -- KFD queue snapshots
(`mqds`/`hqds`/`rls`), CPC state (incl. HQD pipe/queue registers), CU wave state, and (optionally)
full user-queue ring contents for one process -- for an explicitly selected subset of GPUs on a
multi-GPU host.

## Why not just run `umr` by hand?

- `umr -i <n>` addresses GPUs by UMR's own "instance" number, which is **not** the GPU index you'd
  see in `amd-smi`/`rocm-smi` -- it's whichever `/sys/kernel/debug/dri/<N>` debugfs directory the
  card happened to land on (can be an arbitrary number like 33, 41, 49 on an 8-GPU box). This
  script lets you pick GPUs the way you already think about them (`amd-smi` GPU ID), and resolves
  the right UMR instance for you.
- VM-partition count (XCD count on MI300-style multi-die ASICs) varies by ASIC/config; the script
  auto-detects it per GPU instead of assuming a fixed number.
- Bundles the handful of commands you'd otherwise run by hand (`--tool cpc` per partition,
  `--waves`, KFD debugfs dumps, optionally `--list-uq`/`--dump-uq` per queue) into one timestamped,
  tarred-up output directory.

## Requirements

- **`umr`** built with debugfs support (see "Building UMR" below). No specific version pinned, but
  needs a build where `--tool cpc` exists (older builds used the short flag `-cpc` instead) and
  `--waves`/`-wa` supports the `all=` prefix.
- **`amd-smi`** + **`jq`** -- only needed for `-i <amd-smi-id>` / `-l`. If you don't have them, use
  `-p <pci-bdf>` instead, which never touches amd-smi.
- **root** -- `umr` needs MMIO access and KFD debugfs is root-only.
- Run on the **host**, not inside a container: a containerized debugfs bind-mount generally can't
  perform the MMIO bank-select writes `--tool cpc`/`--waves` need (wave halt, SRBM banking).

## Building UMR

```bash
git clone https://gitlab.freedesktop.org/tomstdenis/umr.git
cd umr
cmake -DUMR_NO_LLVM=ON -DUMR_NO_GUI=ON -B build-dir -S .
cmake --build build-dir
sudo cmake --install build-dir
```

- `-DUMR_NO_LLVM=ON` skips the LLVM-based shader disassembler -- not needed for anything this
  script does (register/queue/wave dumps only), and saves you an LLVM dev-package dependency.
- `-DUMR_NO_GUI=ON` skips the SDL/OpenGL GUI (and, as a side effect, the `--server` mode) -- avoids
  needing `libgbm`/`libSDL2`/OpenGL dev packages for a headless dump tool. Without this flag, a
  CMake conditional-compilation quirk can also make the build unconditionally require `gbm.h` even
  on a host where the GUI would end up disabled anyway.
- `cmake --install` puts the `umr` binary at `/usr/local/bin/umr` by default -- this script looks
  there first (see "Locating the `umr` binary" below), so a plain `sudo cmake --install build-dir`
  is enough; no extra `PATH`/env setup needed.
- Still required unconditionally by UMR's own `CMakeLists.txt`: `libpciaccess`, `ncurses`, and
  `libdrm`/`libdrm_amdgpu` (dev packages). Install these first if configure fails looking for them.

## Usage

```bash
sudo ./umr_dump_selected.sh -l                        # list GPUs (amd-smi id <-> PCI <-> umr instance) and exit
sudo ./umr_dump_selected.sh -i 0,3                     # dump amd-smi GPU IDs 0 and 3
sudo ./umr_dump_selected.sh -i all                     # dump every amd-smi-listed GPU
sudo ./umr_dump_selected.sh -p 0000:0a:00.0,0000:c8:00.0  # select by PCI BDF instead (no amd-smi needed)
sudo ./umr_dump_selected.sh -i 0 --no-waves            # skip the wave halt/dump step
sudo ./umr_dump_selected.sh -i 0 --max-part 3          # override auto-detected VM-partition count
sudo ./umr_dump_selected.sh --umr-bin /path/to/umr -i 0   # use a specific umr binary
sudo ./umr_dump_selected.sh -i 0,1,2 --pid 12345       # + full user-queue ring dump for PID 12345
sudo ./umr_dump_selected.sh -i 0 --pid 12345 --queue 0 # only that PID's local queue index 0
```

### GPU selection: `-i` (amd-smi ID) vs `-p` (PCI BDF)

`-i` takes the GPU ID(s) as shown by `amd-smi list`/`rocm-smi`, comma-separated, or `all`. The
script cross-references `amd-smi list --json`'s PCI BDF against `umr -e`'s own enumeration to find
the matching UMR instance -- nothing is hardcoded, so this works unmodified on any host. `-p` skips
that lookup entirely and selects by PCI BDF (`dddd:bb:ss.f`) directly; use it if `amd-smi`/`jq`
aren't available.

Run `-l` first if you're not sure which GPU ID/BDF you want -- it prints the full mapping table and
exits without dumping anything (no root strictly required for this, but the script still checks,
since it also needs to run `umr -e`, which does).

### `--pid PID`: full user-queue ring dump

Optional. When given, in addition to the CPC/waves/KFD steps, the script also finds every KFD user
queue owned by that PID on each selected GPU (via `umr --list-uq`) and dumps each one's **full
retained ring** (not just the current rptr/wptr window) via `umr --dump-uq`. `--queue N` restricts
this to one specific local queue index (as shown by `--list-uq`, independent of KFD's own queue
numbering); `--uq-vmp N` (default `0`) sets which VM partition to query.

Retained rings can be large -- a long-running SDMA queue can retain hundreds of thousands of
already-consumed packets, so a full dump across several busy queues can run into the gigabytes. If
you only need to know whether a queue is stuck, `umr --list-uq`'s own `rptr=`/`wptr=` (also visible
in this script's `40_uq_list_<label>.txt` output) is much cheaper than a full ring dump.

### Locating the `umr` binary

Priority order: `--umr-bin PATH` flag, then `$UMR_BIN` env var, then `/usr/local/bin/umr` /
`/usr/bin/umr` (a real system install, e.g. from `cmake --install` above), then anything else on
`PATH`, then (lowest priority, dev convenience) `./bin/umr` or `./umr/build-dir/src/app/umr`
relative to this script.

## Output

Each run creates `umrdump-<host>-<selected-labels>[-pid<PID>]-<timestamp>/` in the current
directory, plus a matching `.tar.gz`:

| File | Contents |
|---|---|
| `00_enumerate.txt` | Full `umr -e` output (used to build the amd-smi/PCI/instance mapping) |
| `10_kfd.txt` | `/sys/kernel/debug/kfd/{rls,mqds,hqds}` -- **system-wide**, not filtered to the selected GPUs (there's no umr-native way to filter these) |
| `20_cpc_<label>_vmp<N>.txt` | `umr --tool cpc` output per selected GPU, per VM partition |
| `30_waves_<label>.txt` | `umr --waves all=none` output per selected GPU (all XCDs in one call); skipped with `--no-waves` |
| `40_uq_list_<label>.txt` | `umr --list-uq` output per selected GPU; only with `--pid` |
| `40_uq_<label>_queue<N>_full_ring.txt` | `umr --dump-uq` full retained ring for local queue `N`; only with `--pid` |
| `99_summary.txt` | Grep'd highlights: active queues, non-zero `CP_HPD_STATUS0`, halted wave count, user-queue capture count |

`<label>` is `smi<id>` when selected via `-i`, or a sanitized PCI BDF (e.g. `pci0000_0a_00_0`) when
selected via `-p`.
