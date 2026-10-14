"""Small scanner state in a local file or a dedicated GitHub branch."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

STATE_BRANCH = "scanner-state"
STATE_PATH = "market-state.json"
LOCAL_PATH = Path(__file__).resolve().parent / ".state" / STATE_PATH


def empty_state() -> dict:
    return {"version": 2, "history": {}, "episodes": {}, "price_snapshots": {},
            "oi_snapshots": {}, "last_digest_day": ""}


class LocalStateStore:
    def __init__(self, path: Path = LOCAL_PATH):
        self.path = path

    def load(self) -> dict:
        if not self.path.exists():
            return empty_state()
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if data.get("version") != 2:
            raise ValueError("Incompatible local scanner state")
        return data

    def save(self, state: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        pending = self.path.with_suffix(".tmp")
        pending.write_text(json.dumps(state, ensure_ascii=False, separators=(",", ":")),
                           encoding="utf-8")
        pending.replace(self.path)


class GitHubStateStore:
    """Use GITHUB_TOKEN on a state branch; never store Telegram credentials there."""

    def __init__(self, repository: str, token: str):
        if not repository or "/" not in repository or not token:
            raise ValueError("GitHub repository and GITHUB_TOKEN are required")
        self.repository = repository
        self.token = token
        self.sha: str | None = None
        self.branch_ready = False

    def api(self, method: str, path: str, payload: dict | None = None) -> dict:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request("https://api.github.com/repos/" + self.repository + path,
                          data=body, method=method,
                          headers={"Authorization": "Bearer " + self.token,
                                   "Accept": "application/vnd.github+json",
                                   "X-GitHub-Api-Version": "2026-03-10",
                                   "User-Agent": "BingX-public-market-monitor/2.0",
                                   "Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=20) as response:
                return json.load(response)
        except HTTPError as error:
            if error.code == 404:
                return {"_not_found": True}
            raise RuntimeError(f"GitHub state API {method} {path}: HTTP {error.code}") from None

    def load(self) -> dict:
        path = "/contents/" + quote(STATE_PATH) + "?ref=" + STATE_BRANCH
        result = self.api("GET", path)
        if result.get("_not_found"):
            return empty_state()
        self.sha = result.get("sha")
        self.branch_ready = True
        data = json.loads(base64.b64decode(result["content"]).decode("utf-8"))
        if data.get("version") != 2:
            raise ValueError("Incompatible GitHub scanner state")
        return data

    def ensure_branch(self) -> None:
        if self.branch_ready:
            return
        existing = self.api("GET", "/git/ref/heads/" + quote(STATE_BRANCH))
        if not existing.get("_not_found"):
            self.branch_ready = True
            return
        repository = self.api("GET", "")
        default_branch = repository.get("default_branch", "main")
        ref = self.api("GET", "/git/ref/heads/" + quote(default_branch))
        if ref.get("_not_found"):
            raise RuntimeError("GitHub default branch not found")
        result = self.api("POST", "/git/refs", {"ref": "refs/heads/" + STATE_BRANCH,
                                                 "sha": ref["object"]["sha"]})
        if result.get("_not_found"):
            raise RuntimeError("Cannot create GitHub state branch")
        self.branch_ready = True

    def save(self, state: dict) -> None:
        self.ensure_branch()
        encoded = base64.b64encode(json.dumps(state, ensure_ascii=False,
                                              separators=(",", ":")).encode("utf-8")).decode("ascii")
        payload = {"message": "Update public-market scanner state", "content": encoded,
                   "branch": STATE_BRANCH}
        if self.sha:
            payload["sha"] = self.sha
        result = self.api("PUT", "/contents/" + quote(STATE_PATH), payload)
        if not result.get("content", {}).get("sha"):
            raise RuntimeError("GitHub did not confirm scanner state update")
        self.sha = result["content"]["sha"]


def configured_store() -> LocalStateStore | GitHubStateStore:
    token = os.environ.get("GITHUB_TOKEN", "")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    return GitHubStateStore(repository, token) if token and repository else LocalStateStore()
