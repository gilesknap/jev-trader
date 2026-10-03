"""Deploy plan (#39): is everything since the last deploy a merged pull request?

`trading-deploy` runs this from the *currently deployed* checkout (trusted code and key),
never from the commit being deployed. A commit counts as a merged PR only if it is a
two-parent merge commit titled "Merge pull request #N", GPG-signed by GitHub's web-flow
key: GitHub signs merges it performs itself, and a plain `git push` can't forge that.
Single-parent commits are never trusted, even when GitHub signed them, because GitHub also
signs web-editor and contents-API commits that land directly on main. So squash and
rebase merges, or anything pushed directly, get the full diff review instead.

Exit codes for `trader deploy-plan`: 0 all merged PRs, 3 something needs review, 2 error.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

# GitHub's web-flow commit-signing key (https://github.com/web-flow.gpg), in use since
# 2024-01-16. If GitHub rotates it, deploys fall back to full review until a PR adds the new one.
WEB_FLOW_FINGERPRINTS = frozenset({"968479A1AFF927E37D1A566BB5690EEEBB952194"})
KEY_FILE = Path(__file__).resolve().parents[2] / "deploy" / "github-web-flow.gpg"

MERGE_SUBJECT = re.compile(r"^Merge pull request #(\d+) from \S+$")


@dataclass
class Plan:
    prs: list[tuple[str, int, str]] = field(default_factory=list)  # (sha, number, title)
    review: list[tuple[str, str]] = field(default_factory=list)  # (sha, why)
    error: str | None = None

    @property
    def code(self) -> int:
        return 2 if self.error else 3 if self.review else 0

    def lines(self) -> list[str]:
        if self.error:
            return [f"ERROR {self.error}"]
        return [f"PR {sha[:10]} #{n} {title}" for sha, n, title in self.prs] + [
            f"REVIEW {sha} {why}" for sha, why in self.review
        ]


def _git(repo: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=env)


@contextmanager
def _gnupg_home() -> Iterator[str]:
    """A throwaway GNUPGHOME. The key import autostarts a gpg-agent for it, with its sockets in a
    per-homedir directory under /run/user/<uid>/gnupg: both are removed on the way out, or every
    plan would leak them."""
    with tempfile.TemporaryDirectory() as home:
        try:
            yield home
        finally:
            for args in (("--kill", "gpg-agent"), ("--remove-socketdir",)):
                try:
                    subprocess.run(["gpgconf", "--homedir", home, *args], capture_output=True, timeout=10)
                except (OSError, subprocess.TimeoutExpired):
                    pass  # cleanup only: never fail a deploy plan over it


def _signer(repo: Path, sha: str, gnupghome: str) -> str | None:
    """Fingerprint of a good signature on the commit, or None."""
    import os

    env = os.environ | {"GNUPGHOME": gnupghome}
    r = _git(repo, "-c", "gpg.program=gpg", "-c", "gpg.format=openpgp", "verify-commit", "--raw", sha, env=env)
    good = valid = None
    for line in r.stderr.splitlines():
        parts = line.split()
        if parts[:2] == ["[GNUPG:]", "GOODSIG"]:
            good = True
        elif parts[:2] == ["[GNUPG:]", "VALIDSIG"] and len(parts) > 2:
            valid = parts[2]
        elif parts[:2] == ["[GNUPG:]", "BADSIG"] or parts[:2] == ["[GNUPG:]", "ERRSIG"]:
            return None
    return valid if (r.returncode == 0 and good) else None


def plan(
    repo: Path, base: str, target: str, key_file: Path = KEY_FILE, fingerprints: frozenset[str] = WEB_FLOW_FINGERPRINTS
) -> Plan:
    p = Plan()
    shas = {}
    for name, ref in (("base", base), ("target", target)):
        r = _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        if r.returncode != 0:
            p.error = f"unknown {name} {ref!r}"
            return p
        shas[name] = r.stdout.strip()
    base, target = shas["base"], shas["target"]  # full shas from here on (the script parses them)
    if _git(repo, "merge-base", "--is-ancestor", base, target).returncode != 0:
        p.review.append((target, f"history rewritten: the deployed {base[:7]} is not an ancestor"))
        return p
    listed = _git(repo, "rev-list", "--first-parent", f"{base}..{target}")
    if listed.returncode != 0:
        p.error = listed.stderr.strip()[:200]
        return p
    if not key_file.exists():
        p.error = f"missing {key_file}"
        return p
    with _gnupg_home() as home:
        imp = subprocess.run(
            ["gpg", "--homedir", home, "--batch", "--quiet", "--import", str(key_file)], capture_output=True, text=True
        )
        if imp.returncode != 0:
            p.error = f"could not import the web-flow key: {imp.stderr.strip()[:200]}"
            return p
        for sha in reversed(listed.stdout.split()):  # oldest first
            info = _git(repo, "show", "-s", "--format=%P%n%s%n%b", sha).stdout.split("\n")
            parents, subject = info[0].split(), info[1] if len(info) > 1 else ""
            title = next((line.strip() for line in info[2:] if line.strip()), "")
            m = MERGE_SUBJECT.match(subject)
            if len(parents) != 2 or not m:
                p.review.append((sha, f"not a PR merge commit: {subject[:80]}"))
            elif _signer(repo, sha, home) not in fingerprints:
                p.review.append((sha, f"not signed by GitHub: {subject[:80]}"))
            else:
                p.prs.append((sha, int(m.group(1)), title))
    return p
