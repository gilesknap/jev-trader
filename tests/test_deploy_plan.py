"""Deploy plan (#39): only GitHub-signed PR merge commits skip the diff review."""

import os
import shutil
import subprocess

import pytest

from trader.deploy import plan

pytestmark = pytest.mark.skipif(not shutil.which("gpg"), reason="gpg not installed")


def sh(*args, cwd=None, env=None):
    return subprocess.run(args, cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture(scope="module")
def keys(tmp_path_factory):
    home = tmp_path_factory.mktemp("gnupg")
    os.chmod(home, 0o700)
    fprs = {}
    for name in ("github", "mallory"):
        sh("gpg", "--homedir", str(home), "--batch", "--passphrase", "", "--quick-gen-key", f"{name} <{name}@example.com>", "ed25519", "sign", "never")
        out = sh("gpg", "--homedir", str(home), "--batch", "--with-colons", "--list-keys", f"{name}@example.com")
        fprs[name] = next(line.split(":")[9] for line in out.splitlines() if line.startswith("fpr"))
    pub = home / "github.gpg"
    pub.write_text(sh("gpg", "--homedir", str(home), "--batch", "--armor", "--export", fprs["github"]))
    yield home, fprs, pub
    # Key generation and signing start a gpg-agent for this homedir that would outlive the run.
    if shutil.which("gpgconf"):
        subprocess.run(["gpgconf", "--homedir", str(home), "--kill", "gpg-agent"], capture_output=True)
        subprocess.run(["gpgconf", "--homedir", str(home), "--remove-socketdir"], capture_output=True)


class Repo:
    def __init__(self, path, home):
        self.path, self.env = path, os.environ | {"GNUPGHOME": str(home), "GIT_CONFIG_GLOBAL": "/dev/null"}
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "t")
        self.git("config", "user.email", "t@example.com")
        self.commit("base")

    def git(self, *a):
        return sh("git", *a, cwd=self.path, env=self.env)

    def commit(self, msg, key=None):
        (self.path / "f").write_text(msg)
        self.git("add", "f")
        self.git("commit", "-q", "-m", msg, *([f"-S{key}"] if key else []))
        return self.git("rev-parse", "HEAD")

    def pr(self, n, title, key=None, subject=None):
        self.git("checkout", "-q", "-b", f"pr{n}")
        self.commit(f"work {n}")
        self.git("checkout", "-q", "main")
        self.git("merge", "-q", "--no-ff", f"pr{n}", "-m", subject or f"Merge pull request #{n} from o/pr{n}", "-m", title,
                 *([f"-S{key}"] if key else []))
        return self.git("rev-parse", "HEAD")


def run(repo, base, keys, **kw):
    home, fprs, pub = keys
    return plan(repo.path, base, "HEAD", key_file=kw.get("key_file", pub), fingerprints=frozenset({fprs["github"]}))


def test_signed_pr_merges_need_no_review(tmp_path, keys):
    r = Repo(tmp_path, keys[0])
    base = r.git("rev-parse", "HEAD")
    r.pr(5, "Add a thing", keys[1]["github"])
    r.pr(6, "Fix a thing", keys[1]["github"])
    p = run(r, base, keys)
    assert p.code == 0 and [(n, t) for _, n, t in p.prs] == [(5, "Add a thing"), (6, "Fix a thing")]


@pytest.mark.parametrize("case", ["direct push", "signed single-parent", "wrong key", "unsigned merge", "not a PR subject"])
def test_anything_else_needs_review(tmp_path, keys, case):
    r = Repo(tmp_path, keys[0])
    base = r.git("rev-parse", "HEAD")
    gh, mal = keys[1]["github"], keys[1]["mallory"]
    r.pr(5, "Fine", gh)
    if case == "direct push":
        r.commit("sneaky")
    elif case == "signed single-parent":  # e.g. the contents API: GitHub signs it, but it's no PR
        r.commit("Merge pull request #9 from o/x", key=gh)
    elif case == "wrong key":
        r.pr(7, "Forged", mal)
    elif case == "unsigned merge":
        r.pr(7, "Local merge")
    else:
        r.pr(7, "Octo", gh, subject="Merge branch 'x'")
    r.pr(8, "Later fine", gh)  # a good merge on top doesn't launder what's below
    p = run(r, base, keys)
    assert p.code == 3 and len(p.review) == 1 and [n for _, n, _ in p.prs] == [5, 8]


def test_rewritten_history_needs_review(tmp_path, keys):
    r = Repo(tmp_path, keys[0])
    r.pr(5, "Fine", keys[1]["github"])
    old = r.git("rev-parse", "HEAD")
    r.git("reset", "-q", "--hard", "HEAD~1")
    r.pr(6, "Other", keys[1]["github"])
    p = run(r, old, keys)
    assert p.code == 3 and "not an ancestor" in p.review[0][1]


def test_missing_key_is_an_error(tmp_path, keys):
    r = Repo(tmp_path, keys[0])
    base = r.git("rev-parse", "HEAD")
    r.pr(5, "Fine", keys[1]["github"])
    assert run(r, base, keys, key_file=tmp_path / "nope.gpg").code == 2


def test_shipped_key_is_githubs(keys):
    from trader.deploy import KEY_FILE, WEB_FLOW_FINGERPRINTS

    home = keys[0]  # not the real ~/.gnupg, which even a show-only import creates and locks
    out = subprocess.run(["gpg", "--homedir", str(home), "--batch", "--with-colons", "--import-options", "show-only",
                          "--import", str(KEY_FILE)],
                         capture_output=True, text=True).stdout
    assert WEB_FLOW_FINGERPRINTS <= {line.split(":")[9] for line in out.splitlines() if line.startswith("fpr")}
