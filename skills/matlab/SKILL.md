---
name: matlab
description: Drive MATLAB/Simulink through the matlab MCP server (persistent session, job queue, workspace variables, figures, Simulink). Use whenever a task needs MATLAB code executed, variables inspected, plots produced, or Simulink models simulated — including as the execution channel of matlab-sim grading workflows.
---

# MATLAB via MCP

One persistent MATLAB session backs all tools; variables survive between calls.
First call may take 10–20 s while MATLAB warms up.

## Everyday execution

- `run_code(code, timeout_s)` — run in the base workspace; returns Command Window
  output; on MATLAB errors returns the full error text so you can fix and rerun.
  Auto-interrupts on timeout.
- `run_script(path)` — run a .m file by absolute path.
- Long or parallel work: `job_submit(code)` returns a job id immediately, then
  `job_status(job_id, wait_s)` / `job_result(job_id, wait_s)` to poll, and
  `job_cancel` to abort. All submissions (run_code included) share one FIFO
  queue, so multiple agents can submit concurrently without racing the engine.

## Results and evidence

- `list_variables` — what is in the workspace (name/class/size).
- `get_variable(name)` — small values come back in full; big arrays return a
  summary (shape, min/max/mean, head). Use these values as numeric evidence —
  do not eyeball disp output when a formula must be checked.
- `set_variable(name, value)` — scalars/strings/nested lists (JSON objects
  become structs); >1 MB payloads go through a file instead.
- `export_figure(resolution)` — every open figure → PNG paths. Read the PNGs
  to check plots, adjust the code, and re-export.

## Script gate before running generated code

`lint_script(path)` runs checkcode with severities: `ok=false` only on syntax
errors (severity 3, with line numbers). Gate every generated .m through it and
send failures back to the coding step; warnings are reported but pass.

## Simulink

- `run_simulation(model_path)` — blocking; results (logsout, To Workspace
  variables) land in the base workspace.
- `sim_start` → `sim_snapshot(watch, cap)` every 1–3 s → `sim_stop` — live
  view; re-render a chart from each snapshot. Signals must be logged
  (signal logging → logsout, or To Workspace blocks).
