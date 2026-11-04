"""MATLAB engine session: lifecycle, execution with timeout/interrupt, output capture.

All engine access is serialized by _call_lock (the agent calls tools sequentially).
interrupt() is the exception: it only touches the Python-side Future so it can fire
while run() holds the lock.
"""
import collections
import itertools
import json
import os
import tempfile
import threading
import time

import matlab
import matlab.engine

MAX_FULL = 1000  # elements before get_variable switches to a summary

_eng = None
_call_lock = threading.Lock()
_start_lock = threading.Lock()
_current = None  # running engine Future, for interrupt()
_workdir = os.getcwd()
_state = {"model": None}
_hookdir = os.path.join(tempfile.gettempdir(), "matlab_mcp")
_diary = os.path.join(_hookdir, "diary.txt")

# Job queue: one FIFO + one worker serializes concurrent agent submissions.
_TERMINAL = ("done", "error", "timeout", "cancelled")
_jobs = {}  # job id -> dict; every field access is guarded by _job_cv
_job_queue = collections.deque()
_job_cv = threading.Condition()
_job_seq = itertools.count(1)
_worker_alive = False

# Helper deployed next to the engine: Dataset -> cell of plain structs for JSON export.
_SNAPSHOT_FN = """function out = matlab_mcp_dataset_signals(ds, cap)
%MATLAB_MCP_DATASET_SIGNALS Dataset -> cell of structs {name,t,y} with <=cap time points.
out = cell(1, 0);
for k = 1:ds.numElements
    e = ds.get(k);
    nm = '';
    if isa(e, 'Simulink.SimulationData.Signal')
        nm = char(string(e.Name));
        e = e.Values;
    elseif isa(e, 'matlab.io.datastore.SimulationDatastore')
        try, e = e.getValue; catch, continue; end
    end
    if ~isa(e, 'timeseries'), continue; end
    if iscell(e.Data), continue; end
    if isempty(nm), nm = char(string(e.Name)); end
    if isempty(nm), nm = sprintf('element_%d', k); end
    t = double(squeeze(e.Time));
    y = double(squeeze(e.Data));
    if ~isvector(t), t = t(:); end
    if size(y, 1) ~= numel(t)
        if size(y, 2) == numel(t), y = y.'; else, continue; end
    end
    n = numel(t);
    if n > cap
        idx = unique([1, round(linspace(1, n, cap)), n]);
        t = t(idx);
        y = y(idx, :);
    end
    out{end+1} = struct('name', nm, 't', t(:).', 'y', y); %#ok<AGROW>
end
end
"""


def _m(p):
    return p.replace("\\", "/")


def _mq(s):
    return str(s).replace("'", "''")  # escape for a single-quoted MATLAB literal


def _try(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def set_workdir(path):
    global _workdir
    _workdir = path


def get_engine():
    global _eng
    with _start_lock:
        if _eng is None:
            os.makedirs(_hookdir, exist_ok=True)
            with open(os.path.join(_hookdir, "matlab_mcp_dataset_signals.m"), "w",
                      encoding="ascii") as f:
                f.write(_SNAPSHOT_FN)
            eng = matlab.engine.start_matlab()
            eng.addpath(_m(_hookdir), nargout=0)
            eng.cd(_m(_workdir), nargout=0)
            _eng = eng
        return _eng


def warmup():
    # Start MATLAB in the background so the first tool call does not pay 15s.
    threading.Thread(target=_try, args=(get_engine,), daemon=True).start()


def restart():
    global _eng
    with _call_lock, _start_lock:
        if _eng is not None:
            _try(_eng.quit)
            _eng = None
        get_engine()
    _state["model"] = None
    return "session restarted, workspace cleared"


def _read_diary():
    if not os.path.exists(_diary):
        return ""
    data = open(_diary, "rb").read()
    for enc in ("utf-8", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _execute(code, timeout_s=120.0, job=None):
    """Run code under the lock; return (output, err) with err None on success."""
    global _current
    try:
        eng = get_engine()
    except Exception as e:
        return "", f"cannot start MATLAB engine: {e}"
    _try(lambda: os.remove(_diary))
    with _call_lock:
        if job is not None:
            with _job_cv:
                if job["status"] == "cancelled":
                    return "", None
                job["status"] = "running"
                job["started"] = time.time()
        try:
            eng.eval(f"diary('{_m(_diary)}')", nargout=0)
        except Exception as e:
            return "", f"engine call failed: {e}"
        err = None
        try:
            fut = eng.eval(code, nargout=0, background=True)
            _current = fut
            if job is not None:
                with _job_cv:
                    job["_fut"] = fut
            try:
                fut.result(timeout=timeout_s)
            except Exception as e:
                if fut.done():
                    err = str(e)
                else:
                    _try(fut.cancel)
                    err = (f"timed out after {timeout_s}s, interrupt sent; "
                           "the workspace may hold partial state from the aborted code")
        finally:
            _current = None
            if job is not None:
                with _job_cv:
                    job.pop("_fut", None)
            _try(lambda: eng.eval("diary('off')", nargout=0))
        out = _read_diary()
    return out, err


def run(code, timeout_s=120.0):
    """Execute code in the base workspace; return Command Window output or the error."""
    out, err = _execute(code, timeout_s)
    if err:
        return f"ERROR: {err}\n--- output before the error ---\n{out or '(none)'}"
    return out or "(no output)"


# --- Job queue ----------------------------------------------------------------

def _job_worker():
    while True:
        with _job_cv:
            while not _job_queue:
                _job_cv.wait()
            jid = _job_queue.popleft()
        j = _jobs[jid]
        if j["status"] == "cancelled":
            continue
        out, err = _execute(j["code"], j["timeout_s"], job=j)
        with _job_cv:
            j["ended"] = time.time()
            if j["status"] == "cancelled":
                continue
            j["output"] = out
            if j["status"] == "cancel_requested":
                j["status"] = "cancelled" if err else "done"
            elif err:
                j["error"] = err
                j["status"] = "timeout" if err.startswith("timed out") else "error"
                j["output"] = f"ERROR: {err}\n--- output before the error ---\n{out or '(none)'}"
            else:
                j["status"] = "done"


def _ensure_worker():
    global _worker_alive
    with _start_lock:
        if _worker_alive:
            return
        _worker_alive = True
        threading.Thread(target=_job_worker, daemon=True).start()


def job_submit(code, timeout_s=120.0):
    _ensure_worker()
    jid = f"job-{next(_job_seq)}"
    with _job_cv:
        _jobs[jid] = {"id": jid, "code": code, "timeout_s": timeout_s,
                      "status": "queued", "created": time.time(),
                      "started": None, "ended": None, "output": None, "error": None}
        _job_queue.append(jid)
        pos = _job_queue.index(jid)
        _job_cv.notify()
    return {"id": jid, "queue_position": pos}


def _job_view(j):
    return {k: j[k] for k in ("id", "status", "created", "started", "ended", "error")
            if j[k] is not None}


def job_status(jid=None, wait_s=0.0):
    deadline = time.time() + wait_s
    while True:
        with _job_cv:
            if jid is None:
                return {"jobs": [_job_view(j) for j in _jobs.values()]}
            j = _jobs.get(jid)
            if j is None:
                return {"error": f"unknown job '{jid}'"}
            if j["status"] in _TERMINAL or time.time() >= deadline:
                return _job_view(j)
        time.sleep(0.2)


def job_result(jid, wait_s=0.0):
    deadline = time.time() + wait_s
    while True:
        with _job_cv:
            j = _jobs.get(jid)
            if j is None:
                return {"error": f"unknown job '{jid}'"}
            if j["status"] in _TERMINAL or time.time() >= deadline:
                r = _job_view(j)
                r["output"] = j["output"]
                return r
        time.sleep(0.2)


def job_cancel(jid):
    with _job_cv:
        j = _jobs.get(jid)
        if j is None:
            return {"error": f"unknown job '{jid}'"}
        st = j["status"]
        if st == "queued":
            j["status"] = "cancelled"
            return f"{jid} cancelled (was queued)"
        if st in _TERMINAL:
            return f"{jid} already {st}"
        fut = j.get("_fut")
        j["status"] = "cancel_requested"
    if fut is not None:
        _try(fut.cancel)
    return f"{jid} interrupt sent (was running)"


def lint_script(path):
    """checkcode gate: ok=false only on syntax errors (severity 3)."""
    if not os.path.isfile(path):
        return {"file": path, "ok": False, "errors": [], "messages": [],
                "error": "file not found"}
    eng = get_engine()
    with _call_lock:
        txt = eng.eval(f"jsonencode(checkcode('{_mq(path)}', '-severity'))", nargout=1)
    msgs = json.loads(txt) if txt else []
    if isinstance(msgs, dict):  # a single message comes back as one object
        msgs = [msgs]
    errors = [m for m in msgs if m.get("severity") == 3]
    return {"file": path, "ok": not errors,
            "errors": [{"line": m.get("line"), "message": m["message"]} for m in errors],
            "messages": [{"line": m.get("line"), "severity": m.get("severity"),
                          "message": m["message"]} for m in msgs]}


def interrupt():
    fut = _current
    if fut is None:
        return "nothing is running"
    try:
        ok = fut.cancel()
    except Exception as e:
        return f"interrupt failed: {e}"
    return "interrupt sent" if ok else "could not interrupt (already finishing)"


def whos_list():
    eng = get_engine()
    with _call_lock:
        return json.loads(eng.eval("jsonencode(whos)", nargout=1))


def _numeric_payload(v):
    import numpy as np
    a = np.array(v)
    if a.size <= MAX_FULL:
        return {"full": a.item() if a.size == 1 else a.tolist()}
    s = {"shape": list(a.shape), "class": type(v).__name__}
    with __import__("warnings").catch_warnings():
        __import__("warnings").simplefilter("ignore")
        try:
            s["min"] = float(np.nanmin(a))
            s["max"] = float(np.nanmax(a))
            s["mean"] = float(np.nanmean(a))
        except Exception:
            pass
    if a.ndim == 1:
        s["head"] = a[:10].tolist()
    elif a.ndim == 2:
        s["head"] = a[:3].tolist()
    else:
        s["head"] = a.ravel()[:10].tolist()
    return {"summary": s}


def variable_payload(eng, name):
    """Value of a base-workspace variable as {"full": ...} or {"summary": ...}."""
    if eng.eval(f"isnumeric({name}) || islogical({name})", nargout=1):
        with _call_lock:
            return _numeric_payload(eng.workspace[name])
    try:
        with _call_lock:
            txt = eng.eval(f"jsonencode({name})", nargout=1)
    except Exception as e:
        return {"summary": {"error": f"not JSON-serializable: {e}"}}
    if len(txt) <= 65536:
        return {"full": json.loads(txt)}
    return {"summary": {"preview": txt[:2000], "truncated": True}}


def get_variable(name):
    eng = get_engine()
    with _call_lock:
        if eng.eval(f"exist('{_mq(name)}', 'var')", nargout=1) == 0.0:
            return {"error": f"variable '{name}' does not exist"}
    return variable_payload(eng, name)


def set_variable(name, value):
    eng = get_engine()
    if isinstance(value, (int, float, bool, str)) or value is None:
        with _call_lock:
            eng.workspace[name] = value
        return f"{name} set"
    txt = json.dumps(value)
    if len(txt) > 1_000_000:
        return "ERROR: payload too large (>1MB), write the data to a file and load it in MATLAB"
    with _call_lock:
        eng.eval(f"{name} = jsondecode('{txt.replace(chr(92), chr(92) * 2).replace(chr(39), chr(39) * 2)}');",
                 nargout=0)
    return f"{name} set"


def export_figures(resolution=150):
    eng = get_engine()
    figdir = os.path.join(_hookdir, "figs")
    os.makedirs(figdir, exist_ok=True)
    fd = _m(figdir)
    # eng.eval with nargout=1 only accepts a pure expression (loops fail to parse),
    # so count first, then export each figure via nargout=0 statements.
    with _call_lock:
        n = int(eng.eval("numel(findall(0, 'Type', 'figure'))", nargout=1) or 0)
        for i in range(1, n + 1):
            eng.eval(f"figs = sort(findall(0, 'Type', 'figure')); "
                     f"exportgraphics(figs({i}), sprintf('{fd}/fig%d.png', {i}), "
                     f"'Resolution', {int(resolution)});", nargout=0)
    paths = [os.path.join(figdir, f"fig{i}.png") for i in range(1, int(n or 0) + 1)]
    return paths or "no open figures"


def change_dir(path):
    eng = get_engine()
    with _call_lock:
        eng.cd(_m(path), nargout=0)
        pwd = eng.pwd(nargout=1)
        d = json.loads(eng.eval("jsonencode(dir)", nargout=1))
    return {"pwd": pwd, "entries": [{"name": x["name"], "isdir": bool(x["isdir"])}
                                    for x in d if x["name"] not in (".", "..")]}


def doc_text(name):
    return run(f"help('{_mq(name)}')")


# --- Simulink ---------------------------------------------------------------

def _require_simulink(eng):
    with _call_lock:
        lic = eng.eval("license('test', 'Simulink')", nargout=1)
    return None if lic else "ERROR: no Simulink license on this machine"


def sim_start(model_path=None):
    eng = get_engine()
    err = _require_simulink(eng)
    if err:
        return err
    with _call_lock:
        if model_path:
            eng.eval(f"load_system('{_mq(model_path)}')", nargout=0)
        try:
            mdl = eng.eval("bdroot", nargout=1)
        except Exception:
            return "ERROR: no model loaded, pass model_path"
        eng.eval(f"set_param('{_mq(mdl)}', 'ReturnWorkspaceOutputs', 'off'); "
                 f"set_param('{_mq(mdl)}', 'SimulationCommand', 'start');", nargout=0)
        _state["model"] = str(mdl)
    return f"simulation started: {mdl}"


def sim_snapshot(watch=None, cap=500):
    eng = get_engine()
    mdl = _state.get("model")
    if not mdl:
        return "ERROR: no simulation running, call sim_start first"
    with _call_lock:
        status = eng.eval(f"get_param('{_mq(mdl)}', 'SimulationStatus')", nargout=1)
        t = _try(lambda: eng.eval(f"get_param('{_mq(mdl)}', 'SimulationTime')", nargout=1))
        payload = {"model": mdl, "status": str(status),
                   "sim_time": float(t) if t is not None else None, "signals": []}
        running = payload["status"] not in ("stopped", "terminating")
        was_paused = payload["status"] == "paused"
        # logsout only materializes mid-run while paused: peek via pause/continue
        if running and not was_paused:
            try:
                eng.eval(f"set_param('{_mq(mdl)}', 'SimulationCommand', 'pause');", nargout=0)
                for _ in range(100):
                    if eng.eval(f"get_param('{_mq(mdl)}', 'SimulationStatus')",
                                nargout=1) == "paused":
                        break
                    time.sleep(0.05)
            except Exception:
                pass
        try:
            _read_watch(eng, payload, watch, cap, running)
        finally:
            if running and not was_paused:
                _try(lambda: eng.eval(f"set_param('{_mq(mdl)}', 'SimulationCommand', "
                                      f"'continue');", nargout=0))
    return json.dumps(payload)


def _read_watch(eng, payload, watch, cap, running):
    for name in (watch or ["logsout"]):
        if eng.eval(f"exist('{_mq(name)}', 'var')", nargout=1) == 0.0:
            payload["signals"].append({"name": name, "error": "not in base workspace"})
            continue
        if eng.eval(f"isa({name}, 'Simulink.SimulationData.Dataset')", nargout=1):
            txt = eng.eval(f"jsonencode(matlab_mcp_dataset_signals({name}, {int(cap)}))",
                           nargout=1)
            sigs = json.loads(txt)
            for s in sigs:  # jsonencode flattens single-column signals to 1-D
                y = s.get("y")
                if isinstance(y, list) and y and isinstance(y[0], (int, float)):
                    s["y"] = [y]
            payload["signals"].append({"name": name, "type": "dataset",
                                       "series": sigs})
        elif running:
            # reading a variable Simulink is writing can block; wait for the end
            payload["signals"].append({"name": name, "type": "variable",
                                       "note": "available after the simulation stops"})
        else:
            payload["signals"].append({"name": name, "type": "variable",
                                       "data": variable_payload(eng, name)})


def sim_stop():
    eng = get_engine()
    mdl = _state.get("model")
    if not mdl:
        return "ERROR: no simulation running, call sim_start first"
    with _call_lock:
        eng.eval(f"set_param('{_mq(mdl)}', 'SimulationCommand', 'stop');", nargout=0)
        status = eng.eval(f"get_param('{_mq(mdl)}', 'SimulationStatus')", nargout=1)
    return f"stop requested, status: {status}"


def run_simulation(model_path=None, timeout_s=600.0):
    eng = get_engine()
    err = _require_simulink(eng)
    if err:
        return err
    code = ""
    if model_path:
        code += f"load_system('{_mq(model_path)}'); "
    code += "set_param(bdroot, 'ReturnWorkspaceOutputs', 'off'); sim(bdroot); disp('sim done');"
    out = run(code, timeout_s)
    if out.startswith("ERROR"):
        return out
    try:
        names = ", ".join(v["name"] for v in whos_list())
    except Exception:
        names = "(whos failed)"
    return out + "\n--- base workspace after sim: " + names + \
        "\n(use get_variable / list_variables for results)"
