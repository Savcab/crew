#!/usr/bin/env python3
"""gitpr — GitHub PR + native git-worktree diff helpers for the crew dashboard.

Ported verbatim (behavior-identical) from the OLD monolith dashboard.py. Two jobs:

  * Render a GitHub PR locally via the already-authenticated `gh` CLI. GitHub
    blocks iframe embedding (X-Frame-Options) and public CORS proxies can't see
    private-repo auth cookies — so the ONLY way to show a PR locally is `gh`.
    We render the PR natively from its JSON + unified diff.
  * Diff whatever a worker has checked out in its worktree via the local `git`
    CLI — same browser-side render path as a PR diff, no GitHub round-trip.

Everything here shells out to `gh` / `git` and is READ-ONLY (never mutates a
repo). Pure stdlib, no third-party deps, no side effects at import time.
"""
import json
import os
import re
import shutil
import subprocess

# Resolve the external CLIs once. `shutil.which` honors the user's PATH; the
# fallbacks keep the dashboard working when launched from a stripped env (a
# detached tmux/launchd context) where PATH may not include the brew bin dir.
GH = shutil.which("gh") or "/opt/homebrew/bin/gh"
GIT = shutil.which("git") or "/usr/bin/git"

# GitHub blocks iframe embedding (X-Frame-Options) and public CORS proxies can't
# see private-repo auth cookies — so the ONLY way to show a PR locally is the
# already-authenticated `gh` CLI. We render the PR natively from its JSON + diff.
PR_URL_RE = re.compile(r"^https?://github\.com/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)/pull/(\d+)")
MAX_DIFF = 400_000  # chars of diff shipped to the browser (huge PRs get truncated)
# Max untracked files we render a unified diff hunk for (one `git diff --no-index`
# subprocess each). A worktree full of build artifacts / `dev start` output can have
# thousands; rendering all of them is the difference between ~0.8s and ~23s. We still
# LIST every untracked file — only the per-file hunk rendering is capped.
UNTRACKED_DIFF_CAP = 25


def parse_pr_url(url):
    """(owner, repo, number) for a github PR URL, or None. The trailing path
    after the number (…/files, #discussion, ?w=1) is tolerated."""
    if not url:
        return None
    m = PR_URL_RE.match(url.strip())
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3)


def gh_pr(url):
    """Render a GitHub PR natively via the authenticated `gh` CLI (no iframe).

    Returns {ok, meta, diff, truncated} or {ok:False, error, ...}. The repo is
    passed explicitly (parsed from the URL) so this works regardless of the
    dashboard's cwd, and a clear error is returned if `gh` is missing or unauthed.
    """
    parsed = parse_pr_url(url)
    if not parsed:
        return {"ok": False, "error": "not a GitHub PR URL"}
    owner, repo, num = parsed
    nwo = f"{owner}/{repo}"
    if not (GH and os.path.exists(GH)):
        return {"ok": False, "error": "gh CLI not found — install it to view PRs",
                "url": url}
    fields = ("number,title,state,isDraft,author,additions,deletions,changedFiles,"
              "baseRefName,headRefName,url,reviewDecision,mergeable,body,files,"
              "statusCheckRollup,createdAt,updatedAt,labels")
    try:
        p = subprocess.run([GH, "pr", "view", num, "--repo", nwo, "--json", fields],
                           capture_output=True, text=True, timeout=30)
    except Exception as e:
        return {"ok": False, "error": f"gh failed: {e}", "url": url}
    if p.returncode != 0:
        err = (p.stderr or "").strip() or "gh pr view failed"
        low = err.lower()
        if "auth" in low or "logged" in low or "gh auth login" in low:
            err = "gh is not authenticated — run `gh auth login`"
        return {"ok": False, "error": err, "url": url}
    try:
        meta = json.loads(p.stdout or "{}")
    except Exception as e:
        return {"ok": False, "error": f"bad gh JSON: {e}", "url": url}
    # diff is a separate call (gh has no JSON field for the unified diff)
    diff, truncated = "", False
    try:
        d = subprocess.run([GH, "pr", "diff", num, "--repo", nwo],
                           capture_output=True, text=True, timeout=30)
        if d.returncode == 0:
            diff = d.stdout or ""
            if len(diff) > MAX_DIFF:
                diff = diff[:MAX_DIFF]
                truncated = True
    except Exception:
        diff = ""  # metadata is still useful without the diff
    return {"ok": True, "meta": meta, "diff": diff, "truncated": truncated,
            "repo": nwo}


_HTML_TAG = re.compile(r"<[^>]+>")
_MD_IMG = re.compile(r"!\[[^\]]*\]\([^)]*\)")          # ![alt](url)
_MD_LINK = re.compile(r"\[([^\]]+)\]\((?:[^)]*)\)")    # [text](url) -> text
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
_MULTI_NL = re.compile(r"\n{3,}")
_ENTITIES = {"&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"',
             "&#39;": "'", "&nbsp;": " "}


def clean_comment_body(body):
    """Turn a raw PR/review comment (often dense bot HTML+markdown, e.g. the
    Graphite stack comment) into short, readable plain text for the dock thread.

    Strips HTML, drops images / link-chrome, collapses blank runs, recognises the
    Graphite stack-bot boilerplate and replaces it with a one-line marker, and
    caps length. Returns (text, is_bot_boilerplate)."""
    if not body:
        return "", False
    s = str(body)
    # Graphite's auto stack comment — almost pure noise in a review thread.
    low = s.lower()
    if ("this stack of pull requests is managed by" in low
            or "stack-comment-icon" in low
            or "utm_source=stack-comment" in low):
        return "📚 Graphite stack comment (PR links + dependency tree)", True
    s = _HTML_COMMENT.sub("", s)
    s = _MD_IMG.sub("", s)
    s = _HTML_TAG.sub("", s)               # drop any inline HTML
    s = _MD_LINK.sub(r"\1", s)             # keep link text, drop the URL
    for ent, ch in _ENTITIES.items():
        s = s.replace(ent, ch)
    # tidy whitespace: trim each line, collapse 3+ blank lines to one gap
    s = "\n".join(line.rstrip() for line in s.splitlines())
    s = _MULTI_NL.sub("\n\n", s).strip()
    if len(s) > 600:
        s = s[:600].rstrip() + " …"
    return s, False


def gh_pr_for_worktree(cwd):
    """The PR for whatever branch is checked out in `cwd`, via the authed gh CLI.
    `gh pr view` (no number) auto-detects the current branch's PR. Returns a small
    dict {url, number, state, isDraft, checks:{pass,fail,pend}, reviewDecision,
    comments:[...] } or None if there's no PR / gh unavailable. Read-only."""
    if not (GH and os.path.exists(GH) and cwd):
        return None
    fields = ("number,title,url,state,isDraft,reviewDecision,statusCheckRollup,"
              "comments,reviews")
    try:
        p = subprocess.run([GH, "pr", "view", "--json", fields],
                           capture_output=True, text=True, timeout=25, cwd=cwd)
    except Exception:
        return None
    if p.returncode != 0:
        return None  # no PR for this branch (or not a gh repo) — silently omit
    try:
        m = json.loads(p.stdout or "{}")
    except Exception:
        return None
    # CI rollup → pass/fail/pending counts (same buckets as renderPr)
    pass_, fail, pend = 0, 0, 0
    checks = []
    for c in (m.get("statusCheckRollup") or []):
        s = (c.get("conclusion") or c.get("state") or c.get("status") or "").upper()
        name = c.get("name") or c.get("context") or "check"
        if s in ("SUCCESS", "NEUTRAL", "SKIPPED"):
            pass_ += 1; bucket = "pass"
        elif s in ("FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED",
                   "STARTUP_FAILURE"):
            fail += 1; bucket = "fail"
        else:
            pend += 1; bucket = "pend"
        checks.append({"name": name, "bucket": bucket, "state": s,
                       "url": c.get("detailsUrl") or c.get("targetUrl") or ""})
    # merge PR-level comments + review bodies into one time-ordered thread, with
    # each body cleaned to readable plain text (raw bot HTML/markdown is noise).
    comments = []
    for c in (m.get("comments") or []):
        text, is_bot = clean_comment_body(c.get("body") or "")
        if not text:
            continue
        comments.append({"author": (c.get("author") or {}).get("login", ""),
                         "body": text, "at": c.get("createdAt") or "",
                         "kind": "comment", "bot": is_bot})
    for r in (m.get("reviews") or []):
        text, is_bot = clean_comment_body(r.get("body") or "")
        state = (r.get("state") or "").upper()
        # keep a review with a state even if its body is empty (e.g. a bare
        # APPROVED), but drop an empty COMMENTED review (pure noise).
        if not text and state in ("", "COMMENTED", "PENDING"):
            continue
        comments.append({"author": (r.get("author") or {}).get("login", ""),
                         "body": text, "at": r.get("submittedAt") or r.get("createdAt") or "",
                         "kind": "review", "state": state, "bot": is_bot})
    comments.sort(key=lambda x: x["at"])
    return {"url": m.get("url"), "number": m.get("number"), "title": m.get("title"),
            "state": m.get("state"), "isDraft": m.get("isDraft"),
            "reviewDecision": m.get("reviewDecision"),
            "checks": {"pass": pass_, "fail": fail, "pend": pend, "items": checks},
            "comments": comments}


def _git(cwd, *args, timeout=15):
    """Run a read-only git command in `cwd`. Returns (ok, stdout). Never mutates."""
    try:
        p = subprocess.run([GIT, "-C", cwd, *args],
                           capture_output=True, text=True, timeout=timeout)
        return p.returncode == 0, (p.stdout if p.returncode == 0 else p.stderr)
    except Exception as e:
        return False, str(e)


def git_diff(cwd, base=None):
    """Native worktree diff via the local `git` CLI — what this worker has changed.
    Same render path as a PR diff (browser-side renderDiff), no GitHub round-trip.

    Strategy (all read-only):
      * Resolve the worktree root from `cwd` (a worker pane's cwd may be a subdir).
      * Pick a base to diff against: the caller's `base`, else the merge-base with
        the repo's default branch (origin/HEAD → main/master), else just HEAD.
        Merge-base means we show the WHOLE branch's work, not just uncommitted
        edits — and never the trunk commits the branch sits on top of.
      * Diff `base...` (three-dot: changes on this branch since it forked) PLUS
        the working tree, so committed + staged + unstaged + untracked all show.
    Returns {ok, diff, truncated, root, base, branch, files:[{path,additions,deletions}]}.
    """
    if not (GIT and os.path.exists(GIT)):
        return {"ok": False, "error": "git not found"}
    if not cwd:
        return {"ok": False, "error": "no worktree path"}
    ok, root = _git(cwd, "rev-parse", "--show-toplevel")
    if not ok:
        return {"ok": False, "error": f"not a git repo: {cwd.strip()}"}
    root = root.strip()
    # current branch (for the header; detached HEAD → short sha)
    okb, branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    branch = branch.strip() if okb else ""
    if branch == "HEAD":
        oksha, sha = _git(root, "rev-parse", "--short", "HEAD")
        branch = f"detached @ {sha.strip()}" if oksha else "detached HEAD"

    # resolve a base ref to diff from
    ref = (base or "").strip()
    if not ref:
        # default branch via origin/HEAD, else common names, else first parent
        okh, head = _git(root, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
        if okh and head.strip():
            ref = head.strip().replace("refs/remotes/", "")  # e.g. origin/main
        else:
            for cand in ("origin/main", "origin/master", "main", "master"):
                if _git(root, "rev-parse", "--verify", "--quiet", cand)[0]:
                    ref = cand
                    break
    mergebase = ""
    if ref:
        okm, mb = _git(root, "merge-base", ref, "HEAD")
        if okm and mb.strip():
            mergebase = mb.strip()

    # numstat, the unified diff, and the untracked-file scan are INDEPENDENT and
    # each spawns its own git process. The untracked scan (`ls-files --others`) is
    # by far the slowest on a big tree (~0.6s+), so run all three CONCURRENTLY in
    # threads instead of serially — wall time becomes max(), not sum().
    spec = [f"{mergebase}"] if mergebase else []
    files = []
    _res = {}
    def _run(key, *args):
        _res[key] = _git(root, *args)
    import threading
    ths = [
        threading.Thread(target=_run, args=("num", "diff", "--numstat", *spec)),
        threading.Thread(target=_run, args=("diff", "-c", "color.ui=never", "diff", *spec)),
        threading.Thread(target=_run, args=("untr", "ls-files", "--others", "--exclude-standard")),
    ]
    for t in ths: t.start()
    for t in ths: t.join()
    okn, num = _res["num"]
    if okn:
        for line in num.splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                add, dele, path = parts[0], parts[1], "\t".join(parts[2:])
                files.append({"path": path,
                              "additions": 0 if add == "-" else int(add or 0),
                              "deletions": 0 if dele == "-" else int(dele or 0)})
    okd, diff = _res["diff"]
    diff = diff if okd else ""
    truncated = False
    # include untracked files so brand-new files show up too (git diff hides them).
    # CAP the inline per-file rendering: each untracked file costs a `git diff
    # --no-index` subprocess, and a worktree with build output / `dev start`
    # artifacts can have THOUSANDS (worker-2 had 2445 → 23s, 4900 spawns). We always
    # LIST every untracked file (cheap, from the single ls-files), but only render
    # the unified diff for the first UNTRACKED_DIFF_CAP of them; the rest are listed
    # without a hunk and `truncated` is set so the UI can say so. (Dropped the
    # redundant per-file --numstat call too — its result was always discarded.)
    oku, untr = _res["untr"]
    if oku and untr.strip():
        names = [f for f in untr.splitlines() if f.strip()]
        for i, f in enumerate(names):
            if i < UNTRACKED_DIFF_CAP:
                od, ud = _git(root, "diff", "--no-index", "--", "/dev/null", f)
                # --no-index returns rc=1 when files differ; capture stdout regardless
                if ud and "diff --git" in ud:
                    diff += ("\n" if diff else "") + ud
            files.append({"path": f, "additions": 0, "deletions": 0, "untracked": True})
        if len(names) > UNTRACKED_DIFF_CAP:
            truncated = True  # more untracked files than we rendered hunks for
    if len(diff) > MAX_DIFF:
        diff = diff[:MAX_DIFF]
        truncated = True
    return {"ok": True, "diff": diff, "truncated": truncated, "root": root,
            "base": ref or "(none)", "mergebase": mergebase[:9],
            "branch": branch, "files": files}
