"""The custom-feature sandbox is the trader/runner security boundary (issue #13)."""

import os
import shutil
import subprocess
import textwrap

import pytest

from trader import features as F
from trader.features.harness import static_check
from trader.features.sandbox import FeatureSandbox, bwrap_cmd

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
