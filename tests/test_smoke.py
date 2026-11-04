"""Smoke tests against the real local MATLAB (R2025b). Slow: starts a real engine.

Run: .venv/Scripts/python -m pytest tests/test_smoke.py -v
"""
import numpy as np
import pytest

from matlab_mcp import engine


@pytest.fixture(scope="module")
def eng():
    e = engine.get_engine()
    yield e


def test_calc(eng):
    out = engine.run("disp(2+2)")
    assert "4" in out and not out.startswith("ERROR")


def test_output_capture(eng):
    out = engine.run("fprintf('hello %d\\n', 42); x = 1;")
    assert "hello 42" in out


def test_error_message(eng):
    out = engine.run("disp('before'); y = nosuchvar + 1;")
    assert out.startswith("ERROR") and "nosuchvar" in out and "before" in out


def test_timeout_interrupts(eng):
    out = engine.run("pause(30)", timeout_s=2)
    assert "timed out" in out
    out = engine.run("disp('alive')")
    assert "alive" in out and not out.startswith("ERROR")


def test_variable_roundtrip(eng):
    msg = engine.set_variable("mcp_test_mat", [[1.0, 2.0], [3.0, 4.5]])
    assert "set" in msg
    out = engine.run("mcp_test_sum = sum(mcp_test_mat(:)); disp(mcp_test_sum)")
    assert "10.5" in out
    payload = engine.get_variable("mcp_test_mat")
    assert payload["full"] == [[1.0, 2.0], [3.0, 4.5]]


def test_get_variable_summary(eng):
    engine.run("mcp_big = rand(500);")  # 250k elements -> summary
    p = engine.get_variable("mcp_big")
    assert "summary" in p and p["summary"]["shape"] == [500, 500]
    assert 0.0 <= p["summary"]["min"] <= p["summary"]["max"] <= 1.0


def test_set_struct(eng):
    engine.set_variable("mcp_s", {"a": 1, "b": [1, 2, 3]})
    out = engine.run("disp(mcp_s.b(2))")
    assert "2" in out


def test_list_variables(eng):
    info = engine.whos_list()
    names = {v["name"] for v in info}
    assert "mcp_test_mat" in names
    m = next(v for v in info if v["name"] == "mcp_test_mat")
    assert m["class"] == "double" and list(m["size"]) == [2, 2]


def test_export_figure(eng):
    engine.run("f = figure; plot(1:10); title('mcp smoke');")
    paths = engine.export_figures()
    assert isinstance(paths, list) and paths
    import os
    assert all(os.path.exists(p) and os.path.getsize(p) > 1000 for p in paths)


def test_dataset_snapshot(eng):
    engine.run(
        "ts = timeseries(sin(0:0.1:10)', 0:0.1:10, 'Name', 'sig1');"
        "ds = Simulink.SimulationData.Dataset;"
        "ds = ds.add(ts);"
    )
    import json

    txt = eng.eval("jsonencode(matlab_mcp_dataset_signals(ds, 500))", nargout=1)
    series = json.loads(txt)
    assert series[0]["name"] == "sig1", txt[:300]
    t = series[0]["t"]
    y = series[0]["y"]
    if isinstance(y[0], (int, float)):  # jsonencode flattens single-column data
        y = [y]
    y = y[0]
    assert len(t) == len(y) > 50, txt[:300]
    assert abs(max(y)) <= 1.001 and abs(y[-1] - np.sin(10)) < 1e-9


def test_simulink_live(eng):
    import json
    import time as _t

    if not eng.eval("license('test', 'Simulink')", nargout=1):
        pytest.skip("no Simulink license")
    build = (
        "mdl = 'mcp_demo_sig';"
        "if bdIsLoaded(mdl), close_system(mdl, 0); end;"
        "new_system(mdl);"
        "add_block('simulink/Sources/Sine Wave', [mdl '/Sine'], 'Amplitude', '2');"
        "add_block('simulink/Sinks/To Workspace', [mdl '/Sink'], "
        "'VariableName', 'mcp_y', 'SaveFormat', 'Timeseries');"
        "add_line(mdl, 'Sine/1', 'Sink/1');"
        "set_param(mdl, 'StopTime', '2.0', 'ReturnWorkspaceOutputs', 'off');"
        "ph = get_param([mdl '/Sine'], 'PortHandles');"
        "set_param(ph.Outport(1), 'DataLogging', 'on', 'DataLoggingNameMode', 'Custom', "
        "'DataLoggingName', 'sig1');"
    )
    out = engine.run(build)
    assert not out.startswith("ERROR"), out
    try:
        assert "started" in engine.sim_start()
        data = None
        for _ in range(30):
            out = engine.sim_snapshot(["logsout"])
            if out.startswith("ERROR"):
                break
            data = json.loads(out)
            if data["status"] in ("stopped", "terminating"):
                break
            _t.sleep(0.3)
        assert data is not None and data["status"] == "stopped", data
        sig = [s for s in data["signals"] if s["name"] == "logsout"][0]
        series = sig["series"]
        assert series[0]["name"] == "sig1"
        assert len(series[0]["t"]) > 10
        assert abs(max(series[0]["y"][0]) - 2) < 0.01
    finally:
        engine._try(engine.sim_stop)
        _t.sleep(1)
        engine.run("if exist('mdl', 'var'), close_system(mdl, 0); end;")


def test_change_dir(eng, tmp_path):
    res = engine.change_dir(str(tmp_path))
    assert res["pwd"].lower() == str(tmp_path).lower()


def test_lint_gate(eng, tmp_path):
    bad = tmp_path / "bad.m"
    bad.write_text("x = 1\ny = z + ;\n", encoding="ascii")
    r = engine.lint_script(str(bad))
    assert not r["ok"] and r["errors"] and r["errors"][0]["line"] == 2
    good = tmp_path / "good.m"
    good.write_text("z = 3;\n", encoding="ascii")
    r = engine.lint_script(str(good))
    assert r["ok"] and not r["errors"]
    missing = engine.lint_script(str(tmp_path / "nope.m"))
    assert not missing["ok"] and missing.get("error")


def test_job_queue_order(eng):
    j1 = engine.job_submit("pause(1.5); disp('first')", timeout_s=60)
    j2 = engine.job_submit("disp('second')", timeout_s=60)
    assert j1["id"] != j2["id"] and j2["queue_position"] >= 1
    r2 = engine.job_result(j2["id"], wait_s=60)
    r1 = engine.job_result(j1["id"], wait_s=60)
    assert r1["status"] == "done" and "first" in r1["output"]
    assert r2["status"] == "done" and "second" in r2["output"]
    assert r1["started"] <= r2["started"]  # FIFO: one worker runs jobs in order


def test_job_error_and_status(eng):
    j = engine.job_submit("disp('pre'); q = nosuchvar + 1;", timeout_s=60)
    r = engine.job_result(j["id"], wait_s=60)
    assert r["status"] == "error" and "nosuchvar" in r["output"]
    st = engine.job_status()
    assert any(x["id"] == j["id"] for x in st["jobs"])


def test_job_cancel(eng):
    import time
    j = engine.job_submit("pause(60)", timeout_s=120)
    for _ in range(50):  # wait until the worker picked it up (or it starts running)
        if engine.job_status(j["id"])["status"] == "running":
            break
        time.sleep(0.05)
    msg = engine.job_cancel(j["id"])
    assert "interrupt" in msg or "queued" in msg
    r = engine.job_result(j["id"], wait_s=15)
    assert r["status"] == "cancelled"
    out = engine.run("disp('recovered')")
    assert "recovered" in out and not out.startswith("ERROR")
