gpu dump kit for low level dumping and debugging

## Components

### [rocgdb-dump](rocgdb-dump/)
rocgdb tooling for inspecting HSA/SDMA user-queue state (and HSA signals) on a hung or
running ROCm process: dump every queue automatically -- as text, or as a fast binary capture
for large/many rings -- and browse the result offline via an interactive REPL or a small local
web UI. See [rocgdb-dump/README.md](rocgdb-dump/README.md) for usage and origin.
