"""The custom-feature sandbox is the trader/runner security boundary (issue #13)."""

import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from trader import features as F
from trader.features.harness import static_check
from trader.features.sandbox import FeatureSandbox, SandboxError, bwrap_cmd

needs_bwrap = pytest.mark.skipif(not shutil.which("bwrap"), reason="bubblewrap not installed")

GOOD = '''
import numpy as np
from trader.features import feature

@feature("sb_last_range_pct", source="custom")
def f(bars, ctx):
    """Last bar's high/low range in %."""
    b = bars.iloc[-1]
    return float((b.high / b.low - 1) * 100)
'''


def run_in_sandbox(tmp_path, code):
    cmd = bwrap_cmd(tmp_path / "custom")
    cmd = cmd[: cmd.index("-I") + 1] + ["-c", textwrap.dedent(code)]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60)


@needs_bwrap
def test_sandbox_hides_secrets_network_and_env(tmp_path, monkeypatch):
    (tmp_path / "custom").mkdir()
    secret = tmp_path / "secret.env"
    secret.write_text("ALPACA_LIVE_KEY=hunter2\n")
    monkeypatch.setenv("ALPACA_LIVE_KEY", "hunter2")
    r = run_in_sandbox(tmp_path, f'''
        import os, socket
        results = []
        for path in ["{secret}", os.path.expanduser("~runner/.config/trading/env"), "/srv/trading/runtime/golive.json"]:
            try:
                open(path).read(); results.append("READ " + path)
            except OSError:
                results.append("blocked")
        try:
            socket.create_connection(("1.1.1.1", 443), timeout=3); results.append("NET")
        except OSError:
            results.append("blocked")
        results.append("ENV" if "hunter2" in str(os.environ) else "blocked")
        print(",".join(results))
    ''')
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "blocked,blocked,blocked,blocked,blocked"


@needs_bwrap
def test_sandbox_cannot_write_code_root(tmp_path):
    (tmp_path / "custom").mkdir()
    from trader import config

    r = run_in_sandbox(tmp_path, f'''
        try:
            open("{config.CODE_ROOT}/pwned", "w"); print("WROTE")
        except OSError:
            print("blocked")
    ''')
    assert r.stdout.strip() == "blocked"
    assert not (config.CODE_ROOT / "pwned").exists()


def _legacy_bwrap_cmd(custom_dir, code_root):
    """Frozen copy of bwrap_cmd before #169 A2 (interpreter taken from code_root/.venv).

    It and test_bwrap_cmd_unchanged_when_venv_is_in_code_root can be deleted after the split
    cutover (#169 C2): they only prove A2 is a no-op in the monorepo layout.
    """
    code_root, custom_dir = Path(code_root).resolve(), Path(custom_dir).resolve()
    cmd = ["bwrap", "--die-with-parent", "--new-session", "--unshare-all", "--clearenv",
           "--ro-bind", "/usr", "/usr", "--ro-bind", "/etc", "/etc",
           "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    for link in ("bin", "lib", "lib64", "sbin"):
        p = Path("/") / link
        if p.is_symlink():
            cmd += ["--symlink", os.readlink(p), str(p)]
        elif p.exists():
            cmd += ["--ro-bind", str(p), str(p)]
    cmd += ["--ro-bind", str(code_root), str(code_root)]
    if custom_dir.exists() and not custom_dir.is_relative_to(code_root):
        cmd += ["--ro-bind", str(custom_dir), str(custom_dir)]
    cmd += ["--setenv", "PATH", "/usr/bin", "--setenv", "HOME", "/tmp",
            "--setenv", "PYTHONDONTWRITEBYTECODE", "1", "--setenv", "OMP_NUM_THREADS", "1",
            "--chdir", "/tmp"]
    python = str(code_root / ".venv" / "bin" / "python")
    if not Path(python).exists():
        python = sys.executable
    return cmd + [python, "-I", "-m", "trader.features.worker"]


def _fake_venv(path):
    (path / "bin").mkdir(parents=True)
    (path / "bin" / "python").symlink_to("/usr/bin/python3")
    return path


def _ro_binds(cmd):
    return [cmd[i + 1] for i, a in enumerate(cmd) if a == "--ro-bind"]


def _assert_locked_down(cmd):
    for flag in ("--die-with-parent", "--new-session", "--unshare-all", "--clearenv"):
        assert flag in cmd
    # every mount flag is read-only (catches --dev-bind-try, --bind-fd and future flags); no network
    assert {a for a in cmd if a.startswith("--") and "bind" in a} == {"--ro-bind"}
    assert "--share-net" not in cmd
    assert cmd[cmd.index("--tmpfs") + 1] == "/tmp" and cmd[-3:] == ["-I", "-m", "trader.features.worker"]


@pytest.mark.parametrize("custom_inside", [False, True])
def test_bwrap_cmd_unchanged_when_venv_is_in_code_root(tmp_path, custom_inside):
    """Today's layout (runner's sys.prefix is code_root/.venv): argv identical to before A2."""
    code = tmp_path / "code"
    _fake_venv(code / ".venv")
    custom = (code / "features" / "custom") if custom_inside else (tmp_path / "snap")
    custom.mkdir(parents=True)
    new = bwrap_cmd(custom, code_root=code, prefix=code / ".venv", base_prefix=Path("/usr"))
    assert new == _legacy_bwrap_cmd(custom, code)
    _assert_locked_down(new)


def test_bwrap_cmd_binds_out_of_tree_venv_read_only(tmp_path):
    """After the split the strategist's venv lives outside code_root: bind it ro and run its python."""
    code, custom = tmp_path / "code", tmp_path / "snap"
    code.mkdir(), custom.mkdir()
    venv = _fake_venv(tmp_path / "home" / "venv")
    cmd = bwrap_cmd(custom, code_root=code, prefix=venv, base_prefix=Path("/usr"))
    venv = venv.resolve()
    assert cmd[-4] == str(venv / "bin" / "python")
    binds = _ro_binds(cmd)
    assert binds.count(str(venv)) == 1 and str(code.resolve()) in binds and "/usr" in binds
    assert str(tmp_path / "home") not in binds  # only the venv itself, not its parent
    _assert_locked_down(cmd)
    # Everything else is exactly the legacy argv plus the one bind and the new interpreter.
    i = cmd.index(str(venv)) - 1
    assert cmd[:i] + cmd[i + 3:-4] == _legacy_bwrap_cmd(custom, code)[:-4]


def test_bwrap_cmd_binds_base_interpreter_outside_usr(tmp_path):
    """A venv on a uv-managed Python: its bin/python symlink and stdlib point at base_prefix."""
    code, custom = tmp_path / "code", tmp_path / "snap"
    code.mkdir(), custom.mkdir()
    base = tmp_path / "uv-python"
    base.mkdir()
    venv = _fake_venv(tmp_path / "venv")
    binds = _ro_binds(bwrap_cmd(custom, code_root=code, prefix=venv, base_prefix=base))
    assert str(venv.resolve()) in binds and str(base.resolve()) in binds
    # a system interpreter (prefix == base_prefix == /usr) adds nothing
    binds = _ro_binds(bwrap_cmd(custom, code_root=code, prefix=Path("/usr"), base_prefix=Path("/usr")))
    assert binds.count("/usr") == 1 and len(binds) == len(_ro_binds(_legacy_bwrap_cmd(custom, code)))


def test_bwrap_cmd_refuses_to_bind_root(tmp_path):
    (tmp_path / "code").mkdir()
    with pytest.raises(SandboxError, match="refusing"):
        bwrap_cmd(tmp_path, code_root=tmp_path / "code", prefix=Path("/"), base_prefix=Path("/usr"))
    with pytest.raises(SandboxError, match="refusing"):
        bwrap_cmd(tmp_path, code_root=tmp_path / "code", prefix=Path("/usr"), base_prefix=Path("/"))


@pytest.mark.parametrize("which", ["home", "runtime", "secrets"])
@pytest.mark.parametrize("equal", [False, True])
@pytest.mark.parametrize("arg", ["prefix", "base_prefix"])
def test_bwrap_cmd_refuses_binds_exposing_home_runtime_or_secrets(tmp_path, monkeypatch, which, equal, arg):
    """A venv that is (or contains) home, the runtime dir or a secrets file fails closed."""
    from trader import config

    code, outer = tmp_path / "code", tmp_path / "outer"
    code.mkdir()
    _fake_venv(outer)
    target = outer if equal else outer / "inside"
    target.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(tmp_path / "elsewhere-home"))
    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path / "elsewhere-runtime")
    monkeypatch.setattr(config, "SECRETS_FILES", [tmp_path / "elsewhere.env"])
    if which == "home":
        monkeypatch.setenv("HOME", str(target))
    elif which == "runtime":
        monkeypatch.setattr(config, "RUNTIME_DIR", target)
    else:
        secret = target / "env"
        secret.write_text("ALPACA_LIVE_KEY=x\n")
        monkeypatch.setattr(config, "SECRETS_FILES", [secret])
    kw = {"prefix": Path("/usr"), "base_prefix": Path("/usr")} | {arg: outer}
    with pytest.raises(SandboxError, match="refusing"):
        bwrap_cmd(tmp_path / "snap", code_root=code, **kw)
    # the same venv is fine once nothing protected lies inside it
    monkeypatch.setenv("HOME", str(tmp_path / "elsewhere-home"))
    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path / "elsewhere-runtime")
    monkeypatch.setattr(config, "SECRETS_FILES", [tmp_path / "elsewhere.env"])
    assert str(outer.resolve()) in _ro_binds(bwrap_cmd(tmp_path / "snap", code_root=code, **kw))


def test_unsafe_bind_disables_custom_features(tmp_path, monkeypatch):
    """start() turns the refusal into "custom features disabled" (fail closed, alert sent)."""
    from trader.features import sandbox as sbmod

    d = tmp_path / "custom"
    d.mkdir()
    (d / "mine.py").write_text(GOOD)
    monkeypatch.setattr(sbmod.shutil, "which", lambda name: "/usr/bin/bwrap")
    real = sbmod.bwrap_cmd
    monkeypatch.setattr(sbmod, "bwrap_cmd", lambda custom: real(custom, prefix=Path("/")))
    sent = []
    sb = FeatureSandbox(d, alert=lambda lvl, msg: sent.append(msg))
    sb.start([])
    assert sb.broken and "refusing" in sb.broken and not sb.names and sent


def test_bwrap_cmd_defaults_to_running_interpreter(tmp_path):
    (tmp_path / "custom").mkdir()
    cmd = bwrap_cmd(tmp_path / "custom")
    expect = Path(sys.prefix).resolve() / "bin" / "python"
    assert cmd[-4] == (str(expect) if expect.exists() else sys.executable)


@needs_bwrap
def test_sandbox_runs_on_the_callers_environment(tmp_path):
    """The venv's python symlink resolves inside the sandbox and its packages import."""
    (tmp_path / "custom").mkdir()
    r = run_in_sandbox(tmp_path, '''
        import sys, numpy, pandas, trader.features
        print(sys.prefix)
    ''')
    assert r.returncode == 0, r.stderr
    assert Path(r.stdout.strip()).resolve() == Path(sys.prefix).resolve()


@pytest.mark.parametrize("src", [
    "import trader.housekeeping",
    "import os",
    "from trader.features import harness",
    "from trader import features",
    "import pandas as pd\nx = pd.read_fwf('x')",
    "import pandas as pd\npd.io.common.os.system('id')",
    "import numpy as np\nnp.lib.format",
    "import numpy as np\nnp.load('x')",
    "p = '/home/runner/.config/trading/env'",
    "x = (1).__class__",
    "t = type(1)",
    "import importlib",
    "from . import x",
])
def test_static_check_blocks_known_bypasses(tmp_path, src):
    f = tmp_path / "evil.py"
    f.write_text(src + "\n")
    assert static_check(f), src


def test_static_check_allows_normal_feature(tmp_path):
    f = tmp_path / "ok.py"
    f.write_text(GOOD)
    assert static_check(f) == []


@needs_bwrap
def test_sandbox_end_to_end_matches_inprocess(tmp_path, session):
    d = tmp_path / "custom"
    d.mkdir()
    (d / "mine.py").write_text(GOOD)
    s = session()
    sb = FeatureSandbox(d)
    sb.start([(s, s, s)])
    try:
        assert sb.names == {"sb_last_range_pct"} and not sb.broken, sb.errors
        ctx = F.FeatureContext(s, s, 60, 330)
        got = sb.compute(["sb_last_range_pct"], s.iloc[:61], ctx)["sb_last_range_pct"]
        b = s.iloc[60]
        assert abs(got - (b.high / b.low - 1) * 100) < 1e-9
        assert "sb_last_range_pct" not in F.REGISTRY  # never imported in this process
    finally:
        sb.close()


@needs_bwrap
def test_hung_feature_fails_closed(tmp_path, session):
    d = tmp_path / "custom"
    d.mkdir()
    (d / "hang.py").write_text(GOOD.replace('b = bars.iloc[-1]', 'while True:\n        pass'))
    sent = []
    sb = FeatureSandbox(d, alert=lambda lvl, msg: sent.append(msg), gate_timeout=3)
    s = session()
    sb.start([(s, s, s)])
    assert sb.broken and sent
    out = sb.compute(["sb_last_range_pct"], s, F.FeatureContext(s, s, 60, 330))
    assert out["sb_last_range_pct"] != out["sb_last_range_pct"]  # NaN


@needs_bwrap
def test_redefining_library_feature_is_rejected(tmp_path, session):
    d = tmp_path / "custom"
    d.mkdir()
    (d / "clash.py").write_text(GOOD.replace("sb_last_range_pct", "vwap_dist_pct"))
    s = session()
    sb = FeatureSandbox(d)
    sb.start([(s, s, s)])
    try:
        assert "vwap_dist_pct" not in sb.names and "clash.py" in sb.errors
    finally:
        sb.close()


@needs_bwrap
def test_stdout_writes_cannot_corrupt_the_protocol(tmp_path, session):
    """Review of #27: strategist code writing to stdout could inject or corrupt protocol
    lines. The protocol now runs on private fds with request ids. DataFrame.info() writes
    to stdout and passes the static check, so it exercises the channel itself; forged-reply
    injection is covered by test_out_of_sequence_reply_fails_closed."""
    d = tmp_path / "custom"
    d.mkdir()
    (d / "noisy.py").write_text(GOOD.replace('b = bars.iloc[-1]', 'bars.info()\n    b = bars.iloc[-1]'))
    s = session()
    sb = FeatureSandbox(d)
    sb.start([(s, s, s)])
    try:
        assert sb.names == {"sb_last_range_pct"} and not sb.broken, sb.errors
        v = sb.compute(["sb_last_range_pct"], s.iloc[:61], F.FeatureContext(s, s, 60, 330))["sb_last_range_pct"]
        b = s.iloc[60]
        assert abs(v - (b.high / b.low - 1) * 100) < 1e-9 and not sb.broken
    finally:
        sb.close()


def test_print_is_rejected_statically(tmp_path):
    f = tmp_path / "p.py"
    f.write_text("print('x')\n")
    assert static_check(f)


@needs_bwrap
def test_memory_limit_applies_in_worker(tmp_path, session):
    d = tmp_path / "custom"
    d.mkdir()
    (d / "hog.py").write_text(GOOD.replace('b = bars.iloc[-1]', 'x = np.ones(400_000_000)\n    b = bars.iloc[-1]'))
    s = session()
    sb = FeatureSandbox(d)
    sb.start([(s, s, s)])
    try:
        # 3.2 GB allocation exceeds the 1.5 GB RLIMIT_AS: the gate rejects the feature, the worker survives
        assert "sb_last_range_pct" in sb.errors and "MemoryError" in sb.errors["sb_last_range_pct"]
        assert not sb.broken
    finally:
        sb.close()


def test_out_of_sequence_reply_fails_closed(tmp_path):
    import io

    sb = FeatureSandbox(tmp_path)
    sb.names = {"x"}

    class P:
        stdin = io.StringIO()
        stdout = io.StringIO('{"id": 99, "values": {"x": 1}}\n')

        def poll(self):
            return None

        def kill(self):
            pass

    sb._proc = P()
    s = __import__("conftest").make_session()
    out = sb.compute(["x"], s, F.FeatureContext(s, s, 60, 330))
    assert out["x"] != out["x"] and sb.broken


def test_reply_with_right_id_but_wrong_nonce_fails_closed(tmp_path):
    import io

    sb = FeatureSandbox(tmp_path)
    sb.names = {"x"}

    class P:
        stdin = io.StringIO()
        stdout = io.StringIO('{"id": 1, "nonce": "guess", "values": {"x": 1}}\n')  # id guessable, nonce not

        def poll(self):
            return None

        def kill(self):
            pass

    sb._proc = P()
    s = __import__("conftest").make_session()
    out = sb.compute(["x"], s, F.FeatureContext(s, s, 60, 330))
    assert out["x"] != out["x"] and "nonce" in sb.broken
