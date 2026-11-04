"""MCP tool definitions. Thin wrappers: keep usage guidance in the docstrings."""
import json

from mcp.server.fastmcp import FastMCP

from . import engine

mcp = FastMCP("matlab")


@mcp.tool()
def run_code(code: str, timeout_s: float = 120.0) -> str:
    """Run MATLAB code in the persistent base workspace (variables survive between calls).

    Returns the Command Window output (disp/fprintf); on MATLAB errors returns the
    full error text so you can fix and rerun. End statements with ';' to suppress echo.
    Timeouts send an interrupt automatically. Examples:
      run_code("a = magic(4); disp(a)")
      run_code("for k=1:5, s=sin(k*pi/5); fprintf('%f\\n', s); end")
    """
    return engine.run(code, timeout_s)


@mcp.tool()
def run_script(path: str, timeout_s: float = 120.0) -> str:
    """Run a .m script file by absolute path (results stay in the workspace)."""
    return engine.run(f"run('{engine._mq(path)}')", timeout_s)


@mcp.tool()
def list_variables() -> str:
    """List base-workspace variables: name, class, size, bytes (like MATLAB 'whos')."""
    return json.dumps(engine.whos_list(), ensure_ascii=False)


@mcp.tool()
def get_variable(name: str) -> str:
    """Read a workspace variable. Small values (<=1000 elements) come back in full;
    large numeric arrays return a summary (shape, min/max/mean, first rows).
    Cell/struct values are JSON-encoded. Datasets go through get_variable only if small —
    use sim_snapshot for simulation signals."""
    return json.dumps(engine.get_variable(name), ensure_ascii=False)


@mcp.tool()
def set_variable(name: str, value) -> str:
    """Write a value into the base workspace: scalar, string, or nested lists -> matrix
    (JSON objects become structs via jsondecode). For large data, write a file and
    load it in MATLAB instead."""
    return engine.set_variable(name, value)


@mcp.tool()
def export_figure(resolution: int = 150) -> str:
    """Export every open figure to PNG (Resolution=resolution) and return the file
    paths. Read the PNGs to inspect plots, adjust the plotting code, and re-export."""
    return json.dumps(engine.export_figures(resolution), ensure_ascii=False)


@mcp.tool()
def interrupt() -> str:
    """Interrupt the currently running MATLAB execution (e.g. an endless loop)."""
    return engine.interrupt()


@mcp.tool()
def restart_session() -> str:
    """Quit and restart MATLAB with an empty workspace. Use to clear all state."""
    return engine.restart()


@mcp.tool()
def change_dir(path: str) -> str:
    """cd MATLAB to a directory; returns the new pwd and a directory listing."""
    return json.dumps(engine.change_dir(path), ensure_ascii=False)


@mcp.tool()
def get_doc(name: str) -> str:
    """Get MATLAB help text for a function name, e.g. get_doc("sim")."""
    return engine.doc_text(name)


@mcp.tool()
def run_simulation(model_path: str = "", timeout_s: float = 600.0) -> str:
    """Run a Simulink model to completion (blocking). Results land in the base
    workspace (logsout / To Workspace variables) — inspect with list_variables and
    get_variable. For live progress use sim_start + sim_snapshot instead."""
    return engine.run_simulation(model_path or None, timeout_s)


@mcp.tool()
def sim_start(model_path: str = "") -> str:
    """Start a Simulink simulation in the background (non-blocking). Then poll
    sim_snapshot every 1-3s and re-render a chart for the user each poll (quasi
    real-time view); the run ends when status becomes 'stopped'. Signals must be
    logged (signal logging -> logsout, or To Workspace blocks)."""
    return engine.sim_start(model_path or None)


@mcp.tool()
def sim_snapshot(watch: str = "", cap: int = 500) -> str:
    """Snapshot a running simulation: status, sim time, and signal data (last `cap`
    points per signal). `watch` = comma-separated base-workspace variable names
    (default: logsout). Call repeatedly after sim_start to stream progress; re-render
    the chart with the fresh data on every poll."""
    names = [w.strip() for w in watch.split(",") if w.strip()] or None
    return engine.sim_snapshot(names, cap)


@mcp.tool()
def sim_stop() -> str:
    """Stop the background simulation started by sim_start."""
    return engine.sim_stop()


@mcp.tool()
def job_submit(code: str, timeout_s: float = 120.0) -> str:
    """Queue MATLAB code for background execution and return a job id immediately
    (one FIFO queue serializes concurrent agents sharing the session). Then use
    job_status / job_result to poll. Prefer this over run_code when the code runs
    long or several agents submit tasks in parallel."""
    return json.dumps(engine.job_submit(code, timeout_s), ensure_ascii=False)


@mcp.tool()
def job_status(job_id: str = "", wait_s: float = 0.0) -> str:
    """Status of one job (job_id) or of all jobs (omit job_id). With wait_s > 0,
    waits up to that many seconds for the job to finish before replying."""
    return json.dumps(engine.job_status(job_id or None, wait_s), ensure_ascii=False)


@mcp.tool()
def job_result(job_id: str, wait_s: float = 0.0) -> str:
    """Output of a job (waits up to wait_s seconds for completion). Returns the
    Command Window output, or the MATLAB error text with status 'error'/'timeout'."""
    return json.dumps(engine.job_result(job_id, wait_s), ensure_ascii=False)


@mcp.tool()
def job_cancel(job_id: str) -> str:
    """Cancel a queued job or interrupt a running one."""
    return engine.job_cancel(job_id)


@mcp.tool()
def lint_script(path: str) -> str:
    """checkcode gate for a .m file: ok=false only on syntax errors (severity 3,
    with line numbers); warnings/info are listed but do not fail the gate. Run this
    on a generated script before executing it, and send failures back for fixing."""
    return json.dumps(engine.lint_script(path), ensure_ascii=False)


if __name__ == "__main__":
    mcp.run()
