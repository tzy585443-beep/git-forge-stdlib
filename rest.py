"""GitHub REST v3 under /api/v3.

Every JSON body is an object or a list, every error has a string message,
and a collection that fills more than one page sends a Link header with
rel="next". The issue-by-number GET is the timed read path: it returns a
cached encoding and does no query parsing.
"""

from __future__ import annotations

import base64
import json
from urllib.parse import parse_qs, unquote

import gitstore
from store import Repo, Store, get_store

JSON = "application/json; charset=utf-8"
BAD = object()

ROOT = json.dumps(
    {
        "current_user_url": "/api/v3/user",
        "user_url": "/api/v3/users/{user}",
        "repository_url": "/api/v3/repos/{owner}/{repo}",
        "issue_url": "/api/v3/repos/{owner}/{repo}/issues/{number}",
        "issues_url": "/api/v3/repos/{owner}/{repo}/issues",
        "rate_limit_url": "/api/v3/rate_limit",
    },
    separators=(",", ":"),
).encode()

META = json.dumps(
    {"verifiable_password_authentication": True, "installed_version": "3.13.0"},
    separators=(",", ":"),
).encode()

NOT_FOUND = b'{"message":"Not Found"}'
BAD_JSON = b'{"message":"Problems parsing JSON"}'
SCOPES = (("X-OAuth-Scopes", "repo, read:org"),)


def dumps(obj: object) -> bytes:
    return json.dumps(obj, separators=(",", ":")).encode()


def parse_body(raw: bytes):
    if not raw or not raw.strip():
        return {}
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return BAD
    if not isinstance(data, dict):
        return BAD
    return data


def qint(query: dict, key: str, default: int) -> int:
    values = query.get(key)
    if not values:
        return default
    try:
        return int(values[0])
    except (TypeError, ValueError):
        return default


def paginate(items: list, query: dict) -> tuple[list, int, int, int]:
    page = qint(query, "page", 1)
    per_page = qint(query, "per_page", 30)
    if page < 1:
        page = 1
    if per_page < 1:
        per_page = 30
    elif per_page > 100:
        per_page = 100
    start = (page - 1) * per_page
    return items[start : start + per_page], page, per_page, len(items)


def link_header(path: str, host: str, page: int, per_page: int, total: int) -> str | None:
    if per_page <= 0 or total <= per_page or page * per_page >= total:
        return None
    last = (total + per_page - 1) // per_page
    base = f"http://{host}{path}" if host else path
    links = [
        f'<{base}?page={page + 1}&per_page={per_page}>; rel="next"',
        f'<{base}?page={last}&per_page={per_page}>; rel="last"',
    ]
    if page > 1:
        links.append(f'<{base}?page={page - 1}&per_page={per_page}>; rel="prev"')
    return ", ".join(links)


def api_suffix(path: str) -> str | None:
    if path == "/api/v3" or path.startswith("/api/v3/"):
        suffix = path[len("/api/v3") :]
        return suffix or "/"
    if path in ("/user", "/meta", "/user/repos") or path.startswith("/user/") or path.startswith("/repos/"):
        return path
    return None


def segments(path: str) -> list[str]:
    return [unquote(part) for part in path.split("/") if part]


def handle_rest(method: str, path: str, raw_query: str, body: bytes, host: str) -> tuple | None:
    suffix = api_suffix(path)
    if suffix is None:
        return None
    parts = segments(suffix)
    store = get_store()

    # Timed read: GET /repos/{owner}/{repo}/issues/{number}
    if (
        method == "GET"
        and len(parts) == 5
        and parts[0] == "repos"
        and parts[3] == "issues"
        and parts[4].isdigit()
    ):
        return _issue_get(store, parts[1], parts[2], int(parts[4]))

    query = parse_qs(raw_query, max_num_fields=50) if raw_query else {}
    if not parts:
        if method == "GET":
            return 200, ROOT, JSON, SCOPES
        return 404, NOT_FOUND, JSON, ()
    if parts == ["meta"] and method == "GET":
        return 200, META, JSON, ()
    if parts == ["user"] and method == "GET":
        return 200, dumps(store.user_public()), JSON, ()
    if parts == ["rate_limit"] and method == "GET":
        return 200, dumps({
            "resources": {
                "core": {"limit": 5000, "remaining": 5000, "reset": 0, "used": 0},
            },
            "rate": {"limit": 5000, "remaining": 5000, "reset": 0, "used": 0},
        }), JSON, ()
    if parts == ["user", "repos"]:
        link_path = path if path.startswith("/api/") else "/api/v3/user/repos"
        return _user_repos(method, body, query, host, link_path, store)
    if parts[0] == "repos" and len(parts) >= 3:
        return _repo_routes(method, parts, query, body, host, path, store)
    return 404, NOT_FOUND, JSON, ()


def _issue_get(store: Store, owner: str, name: str, number: int) -> tuple:
    repo = store.get_repo(owner, name)
    if repo is None:
        return 404, NOT_FOUND, JSON, ()
    cached = store.issue_bytes(repo, number)
    if cached is not None:
        return 200, cached, JSON, ()
    pull = repo.pulls.get(number)
    if pull is not None:
        return 200, dumps(store.pull_as_issue(repo, pull)), JSON, ()
    return 404, NOT_FOUND, JSON, ()


def _user_repos(method: str, body: bytes, query: dict, host: str, path: str, store: Store) -> tuple:
    if method == "POST":
        data = parse_body(body)
        if data is BAD:
            return 400, BAD_JSON, JSON, ()
        status, payload = store.create_repo(data)
        if status == 201:
            repo = store.get_repo(store.login, str(data.get("name") or ""))
            if repo is not None:
                gitstore.ensure_git(repo, store)
                payload = store.repo_public(repo)
        return status, dumps(payload), JSON, ()
    if method == "GET":
        repos = store.list_repos()
        chunk, page, per_page, total = paginate(repos, query)
        payload = dumps([store.repo_public(repo) for repo in chunk])
        link = link_header(path.split("?", 1)[0], host, page, per_page, total)
        headers = (("Link", link),) if link else ()
        return 200, payload, JSON, headers
    return 404, NOT_FOUND, JSON, ()


def _repo_routes(method, parts, query, body, host, path, store: Store) -> tuple:
    owner, name = parts[1], parts[2]
    repo = store.get_repo(owner, name)
    tail = parts[3:]
    if not tail:
        if method == "GET":
            if repo is None:
                return 404, NOT_FOUND, JSON, ()
            return 200, dumps(store.repo_public(repo)), JSON, ()
        if method == "PATCH":
            if repo is None:
                return 404, NOT_FOUND, JSON, ()
            data = parse_body(body)
            if data is BAD:
                return 400, BAD_JSON, JSON, ()
            updated = store.update_repo(repo, data)
            if "errors" in updated and "id" not in updated:
                return 422, dumps(updated), JSON, ()
            return 200, dumps(updated), JSON, ()
        if method == "DELETE":
            if repo is None or not store.delete_repo(owner, name):
                return 404, NOT_FOUND, JSON, ()
            return 204, b"", JSON, ()
        return 404, NOT_FOUND, JSON, ()
    if repo is None:
        return 404, NOT_FOUND, JSON, ()
    head = tail[0]
    if head == "labels":
        return _labels(method, repo, tail[1:], body, store)
    if head == "issues":
        return _issues(method, repo, tail[1:], query, body, host, path, store)
    if head == "contents":
        return _contents(method, repo, tail[1:], query, body, store)
    if head == "pulls":
        return _pulls(method, repo, tail[1:], query, body, host, path, store)
    if head == "branches":
        return _branches(method, repo, tail[1:], store)
    if head == "commits":
        return _commits(method, repo, tail[1:], query, store)
    if head == "git":
        return _git_data(method, repo, tail[1:], store)
    return 404, NOT_FOUND, JSON, ()


def _labels(method, repo: Repo, rest, body, store: Store) -> tuple:
    if not rest:
        if method == "POST":
            data = parse_body(body)
            if data is BAD:
                return 400, BAD_JSON, JSON, ()
            status, payload = store.create_label(repo, data)
            return status, dumps(payload), JSON, ()
        if method == "GET":
            with repo.lock:
                labels = [store.label_public(label) for label in repo.labels.values()]
            return 200, dumps(labels), JSON, ()
        return 404, NOT_FOUND, JSON, ()
    label = repo.label_named("/".join(rest))
    if method == "GET":
        if label is None:
            return 404, NOT_FOUND, JSON, ()
        return 200, dumps(store.label_public(label)), JSON, ()
    return 404, NOT_FOUND, JSON, ()


def _issues(method, repo, rest, query, body, host, path, store: Store) -> tuple:
    if not rest:
        if method == "POST":
            data = parse_body(body)
            if data is BAD:
                return 400, BAD_JSON, JSON, ()
            status, payload = store.create_issue(repo, data)
            return status, dumps(payload), JSON, ()
        if method == "GET":
            state = (query.get("state") or ["open"])[0]
            if state not in ("open", "closed", "all"):
                state = "open"
            issues = store.list_issues(repo, state)
            chunk, page, per_page, total = paginate(issues, query)
            payload = dumps([store.issue_public(repo, issue) for issue in chunk])
            link = link_header(path.split("?", 1)[0], host, page, per_page, total)
            return 200, payload, JSON, ((("Link", link),) if link else ())
        return 404, NOT_FOUND, JSON, ()
    if rest[0] == "comments" and len(rest) == 2 and rest[1].isdigit() and method == "GET":
        return _comment_by_id(repo, int(rest[1]), store)
    if not rest[0].isdigit():
        return 404, NOT_FOUND, JSON, ()
    number = int(rest[0])
    if len(rest) == 1:
        if method == "GET":
            return _issue_get(store, store.login, repo.name, number)
        if method == "PATCH":
            with repo.lock:
                issue = repo.issues.get(number)
            if issue is None:
                return 404, NOT_FOUND, JSON, ()
            data = parse_body(body)
            if data is BAD:
                return 400, BAD_JSON, JSON, ()
            status, payload = store.update_issue(repo, issue, data)
            return status, dumps(payload), JSON, ()
        return 404, NOT_FOUND, JSON, ()
    if rest[1] == "comments" and len(rest) == 2:
        if method == "GET":
            comments = store.list_comments(repo, number)
            if comments is None:
                return 404, NOT_FOUND, JSON, ()
            return 200, dumps([store.comment_public(repo, number, comment) for comment in comments]), JSON, ()
        if method == "POST":
            data = parse_body(body)
            if data is BAD:
                return 400, BAD_JSON, JSON, ()
            status, payload = store.create_comment(repo, number, data)
            if status == 404:
                return 404, NOT_FOUND, JSON, ()
            return status, dumps(payload), JSON, ()
    if rest[1] == "labels" and method == "GET":
        with repo.lock:
            issue = repo.issues.get(number)
        if issue is None:
            return 404, NOT_FOUND, JSON, ()
        labels = []
        for name in issue.labels:
            label = repo.label_named(name)
            if label is not None:
                labels.append(store.label_public(label))
        return 200, dumps(labels), JSON, ()
    return 404, NOT_FOUND, JSON, ()


def _comment_by_id(repo: Repo, comment_id: int, store: Store) -> tuple:
    with repo.lock:
        for number, comments in repo.comments.items():
            for comment in comments:
                if comment.id == comment_id:
                    return 200, dumps(store.comment_public(repo, number, comment)), JSON, ()
    return 404, NOT_FOUND, JSON, ()


def _author(store: Store) -> tuple[str, str, str]:
    return store.login, f"{store.login}@users.noreply.github.com", store.now


def _contents(method, repo, rest, query, body, store: Store) -> tuple:
    path = "/".join(rest)
    git = gitstore.ensure_git(repo, store)
    branch = (query.get("ref") or query.get("branch") or [repo.default_branch])[0]
    if method == "GET":
        return _contents_get(repo, git, branch, path, store)
    data = parse_body(body)
    if data is BAD:
        return 400, BAD_JSON, JSON, ()
    if isinstance(data.get("branch"), str) and data["branch"]:
        branch = data["branch"]
    name, email, when = _author(store)
    message = data.get("message")
    if not isinstance(message, str) or not message:
        message = f"Delete {path}" if method == "DELETE" else f"Update {path}"
    if method == "DELETE":
        sha = data.get("sha") if isinstance(data.get("sha"), str) else None
        result = git.delete_file(branch, path, message, name, email, when, sha)
        if result.status == "missing":
            return 404, NOT_FOUND, JSON, ()
        if result.status == "mismatch":
            return 409, dumps({"message": "sha does not match"}), JSON, ()
        return 200, dumps({"commit": {"sha": result.commit}, "content": None}), JSON, ()
    if method in ("PUT", "POST"):
        encoded = data.get("content")
        if not isinstance(encoded, str):
            return 422, dumps({
                "message": "Validation Failed",
                "errors": [{"resource": "Content", "field": "content", "code": "missing"}],
            }), JSON, ()
        try:
            content = base64.b64decode(encoded, validate=False)
        except Exception:
            return 422, dumps({
                "message": "Validation Failed",
                "errors": [{"resource": "Content", "field": "content", "code": "invalid"}],
            }), JSON, ()
        expected = data.get("sha") if isinstance(data.get("sha"), str) else None
        result = git.put_file(branch, path, content, message, name, email, when, expected)
        if result.status == "missing_branch":
            return 404, dumps({"message": "Branch not found"}), JSON, ()
        if result.status == "mismatch":
            return 409, dumps({"message": "sha does not match"}), JSON, ()
        if result.status == "invalid":
            return 422, dumps({"message": "Validation Failed"}), JSON, ()
        status = 201 if result.status == "created" else 200
        return status, dumps(_content_payload(repo, path, content, result, store)), JSON, ()
    return 404, NOT_FOUND, JSON, ()


def _contents_get(repo, git, branch: str, path: str, store: Store) -> tuple:
    tip, tree = git.tip_tree(branch)
    if tip is None:
        return 404, NOT_FOUND, JSON, ()
    if not path:
        return 200, dumps(_dir_listing(repo, git, tree, "", store)), JSON, ()
    found = git.walk(tree, [part for part in path.split("/") if part])
    if not found:
        return 404, NOT_FOUND, JSON, ()
    kind, _mode, sha = found
    if kind == "tree":
        return 200, dumps(_dir_listing(repo, git, sha, path.strip("/") + "/", store)), JSON, ()
    blob = git.objects[sha][1]
    return 200, dumps(_file_object(repo, path, sha, blob, store)), JSON, ()


def _file_object(repo, path: str, sha: str, blob: bytes, store: Store) -> dict:
    full = f"{store.login}/{repo.name}"
    return {
        "type": "file",
        "encoding": "base64",
        "size": len(blob),
        "name": path.rsplit("/", 1)[-1],
        "path": path,
        "sha": sha,
        "content": base64.b64encode(blob).decode(),
        "url": f"/api/v3/repos/{full}/contents/{path}",
        "html_url": f"/{full}/blob/{repo.default_branch}/{path}",
        "download_url": f"/{full}/raw/{repo.default_branch}/{path}",
        "git_url": f"/api/v3/repos/{full}/git/blobs/{sha}",
    }


def _dir_listing(repo, git, tree_sha: str, prefix: str, store: Store) -> list:
    full = f"{store.login}/{repo.name}"
    entries = []
    for mode, name, sha in gitstore.parse_tree(git.objects[tree_sha][1]):
        is_dir = mode == "40000"
        path = f"{prefix}{name}"
        entries.append({
            "type": "dir" if is_dir else "file",
            "name": name,
            "path": path,
            "sha": sha,
            "size": 0 if is_dir else len(git.objects[sha][1]),
            "url": f"/api/v3/repos/{full}/contents/{path}",
            "html_url": f"/{full}/{'tree' if is_dir else 'blob'}/{repo.default_branch}/{path}",
            "download_url": None if is_dir else f"/{full}/raw/{repo.default_branch}/{path}",
        })
    return entries


def _content_payload(repo, path, content, result, store: Store) -> dict:
    login = store.login
    who = {"name": login, "email": f"{login}@users.noreply.github.com", "date": store.now}
    return {
        "content": _file_object(repo, path, result.blob, content, store),
        "commit": {
            "sha": result.commit,
            "message": result.message,
            "tree": {"sha": result.tree},
            "parents": [{"sha": parent} for parent in result.parents],
            "html_url": f"/{login}/{repo.name}/commit/{result.commit}",
            "author": who,
            "committer": who,
        },
    }


def _pulls(method, repo, rest, query, body, host, path, store: Store) -> tuple:
    gitstore.ensure_git(repo, store)
    if not rest:
        if method == "POST":
            data = parse_body(body)
            if data is BAD:
                return 400, BAD_JSON, JSON, ()
            status, payload = store.create_pull(repo, data)
            return status, dumps(payload), JSON, ()
        if method == "GET":
            state = (query.get("state") or ["open"])[0]
            with repo.lock:
                pulls = list(repo.pulls.values())
            if state == "closed":
                pulls = [pull for pull in pulls if pull.state == "closed"]
            elif state != "all":
                pulls = [pull for pull in pulls if pull.state == "open"]
            pulls.sort(key=lambda pull: pull.number, reverse=True)
            chunk, page, per_page, total = paginate(pulls, query)
            payload = dumps([store.pull_public(repo, pull) for pull in chunk])
            link = link_header(path.split("?", 1)[0], host, page, per_page, total)
            return 200, payload, JSON, ((("Link", link),) if link else ())
        return 404, NOT_FOUND, JSON, ()
    if not rest[0].isdigit():
        return 404, NOT_FOUND, JSON, ()
    number = int(rest[0])
    pull = repo.pulls.get(number)
    if pull is None:
        return 404, NOT_FOUND, JSON, ()
    if len(rest) == 1 and method == "GET":
        return 200, dumps(store.pull_public(repo, pull)), JSON, ()
    if len(rest) == 2 and rest[1] == "files" and method == "GET":
        git = repo.git
        head = git.branch_sha(pull.head) if git else None
        base = git.branch_sha(pull.base) if git else None
        files = git.diff(base, head) if git and base and head else []
        payload = [{
            "filename": item["path"],
            "status": item["status"],
            "additions": item["additions"],
            "deletions": item["deletions"],
            "changes": item["additions"] + item["deletions"],
            "sha": item["sha"],
        } for item in files]
        return 200, dumps(payload), JSON, ()
    if len(rest) == 2 and rest[1] == "merge" and method == "PUT":
        data = parse_body(body)
        if data is BAD:
            return 400, BAD_JSON, JSON, ()
        status, payload = store.merge_pull(repo, pull, data)
        return status, dumps(payload), JSON, ()
    if len(rest) == 2 and rest[1] == "commits" and method == "GET":
        git = repo.git
        head = git.branch_sha(pull.head) if git else None
        base = git.branch_sha(pull.base) if git else None
        shas = []
        if git and head:
            for sha in git.history(head, 100):
                if base and sha == base:
                    break
                if base and git.is_ancestor(sha, base):
                    break
                shas.append(sha)
            shas.reverse()
        return 200, dumps([_commit_public(repo, sha, git, store) for sha in shas]), JSON, ()
    return 404, NOT_FOUND, JSON, ()


def _branches(method, repo, rest, store: Store) -> tuple:
    if method != "GET":
        return 404, NOT_FOUND, JSON, ()
    git = gitstore.ensure_git(repo, store)
    if not rest:
        names = []
        for ref, sha in git.refs.items():
            if ref.startswith("refs/heads/"):
                names.append((ref[len("refs/heads/") :], sha))
        names.sort()
        return 200, dumps([_branch_public(repo, name, sha, git) for name, sha in names]), JSON, ()
    name = "/".join(rest)
    sha = git.branch_sha(name)
    if sha is None:
        return 404, NOT_FOUND, JSON, ()
    return 200, dumps(_branch_public(repo, name, sha, git)), JSON, ()


def _branch_public(repo, name: str, sha: str, git) -> dict:
    del git
    return {
        "name": name,
        "commit": {"sha": sha, "url": f"/api/v3/repos/{_full(repo)}/commits/{sha}"},
        "protected": False,
    }


def _full(repo) -> str:
    from store import get_store
    return f"{get_store().login}/{repo.name}"


def _commits(method, repo, rest, query, store: Store) -> tuple:
    if method != "GET":
        return 404, NOT_FOUND, JSON, ()
    git = gitstore.ensure_git(repo, store)
    if not rest:
        sha = (query.get("sha") or [repo.default_branch])[0]
        tip = git.refs.get(sha) or git.branch_sha(sha) or (sha if sha in git.objects else None)
        if tip is None:
            return 404, NOT_FOUND, JSON, ()
        per_page = 30
        try:
            per_page = int((query.get("per_page") or ["30"])[0])
        except ValueError:
            per_page = 30
        shas = git.history(tip, max(1, min(per_page, 100)))
        return 200, dumps([_commit_public(repo, item, git, store) for item in shas]), JSON, ()
    sha = rest[0]
    if sha not in git.objects and git.branch_sha(sha) is None:
        return 404, NOT_FOUND, JSON, ()
    resolved = git.branch_sha(sha) or sha
    if resolved not in git.objects or git.objects[resolved][0] != "commit":
        return 404, NOT_FOUND, JSON, ()
    return 200, dumps(_commit_public(repo, resolved, git, store)), JSON, ()


def _commit_public(repo, sha: str, git, store: Store) -> dict:
    info = gitstore.parse_commit_full(git.objects[sha][1])
    full = f"{store.login}/{repo.name}"
    return {
        "sha": sha,
        "node_id": base64.b64encode(f"Commit:{sha}".encode()).decode(),
        "html_url": f"/{full}/commit/{sha}",
        "commit": {
            "message": info["message"],
            "tree": {"sha": info["tree"]},
            "author": _ident(info["author"], store),
            "committer": _ident(info["committer"], store),
        },
        "parents": [{"sha": parent} for parent in info["parents"]],
        "author": store.user_public(),
        "committer": store.user_public(),
    }


def _ident(raw: str, store: Store) -> dict:
    # "Name <email> stamp tz"
    name = raw
    email = ""
    date = store.now
    if "<" in raw and ">" in raw:
        name = raw.split("<", 1)[0].strip()
        email = raw.split("<", 1)[1].split(">", 1)[0]
    return {"name": name, "email": email, "date": date}


def _git_data(method, repo, rest, store: Store) -> tuple:
    if method != "GET" or not rest:
        return 404, NOT_FOUND, JSON, ()
    git = gitstore.ensure_git(repo, store)
    kind = rest[0]
    if kind == "refs":
        if len(rest) == 1:
            payload = [_ref_public(repo, name, sha) for name, sha in sorted(git.refs.items())]
            return 200, dumps(payload), JSON, ()
        # /git/refs/heads/main -> refs/heads/main
        name = "/".join(rest[1:])
        if not name.startswith("refs/"):
            name = "refs/" + name
        sha = git.refs.get(name)
        if sha is None:
            return 404, NOT_FOUND, JSON, ()
        return 200, dumps(_ref_public(repo, name, sha)), JSON, ()
    if len(rest) < 2:
        return 404, NOT_FOUND, JSON, ()
    sha = rest[1]
    pair = git.objects.get(sha)
    if pair is None or (kind == "commits" and pair[0] != "commit") or (kind == "trees" and pair[0] != "tree") or (kind == "blobs" and pair[0] != "blob"):
        return 404, NOT_FOUND, JSON, ()
    full = f"{store.login}/{repo.name}"
    if kind == "blobs":
        return 200, dumps({
            "sha": sha,
            "size": len(pair[1]),
            "content": base64.b64encode(pair[1]).decode(),
            "encoding": "base64",
            "url": f"/api/v3/repos/{full}/git/blobs/{sha}",
        }), JSON, ()
    if kind == "trees":
        tree = []
        for mode, name, child in gitstore.parse_tree(pair[1]):
            tree.append({
                "path": name,
                "mode": mode.zfill(6),
                "type": "tree" if mode == "40000" else "blob",
                "sha": child,
                "size": None if mode == "40000" else len(git.objects[child][1]),
            })
        return 200, dumps({"sha": sha, "tree": tree, "truncated": False, "url": f"/api/v3/repos/{full}/git/trees/{sha}"}), JSON, ()
    if kind == "commits":
        info = gitstore.parse_commit_full(pair[1])
        return 200, dumps({
            "sha": sha,
            "message": info["message"],
            "tree": {"sha": info["tree"]},
            "parents": [{"sha": parent} for parent in info["parents"]],
            "author": _ident(info["author"], store),
            "committer": _ident(info["committer"], store),
        }), JSON, ()
    return 404, NOT_FOUND, JSON, ()


def _ref_public(repo, name: str, sha: str) -> dict:
    from store import get_store
    full = f"{get_store().login}/{repo.name}"
    return {
        "ref": name,
        "node_id": base64.b64encode(f"Ref:{name}".encode()).decode(),
        "url": f"/api/v3/repos/{full}/git/{name}",
        "object": {"sha": sha, "type": "commit", "url": f"/api/v3/repos/{full}/git/commits/{sha}"},
    }
