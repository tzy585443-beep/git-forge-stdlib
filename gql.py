"""GraphQL endpoint for the documents gh and github-mcp-server send.

Introspection describes Issue, PullRequest, Repository, and SearchType.
repository.issues honors states and orderBy. createPullRequest opens a pull
request on a branch that was pushed over git. A body that holds two operation
documents answers two results, in order.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import gitstore
from store import get_store, node_id, parse_node_id

JSON = "application/json; charset=utf-8"


def dumps(obj: object) -> bytes:
    return json.dumps(obj, separators=(",", ":")).encode()


# field -> (type, arguments)
SCHEMA: dict[str, dict[str, tuple[str, dict]]] = {
    "Query": {
        "repository": ("Repository", {"owner": "String!", "name": "String!"}),
        "node": ("Node", {"id": "ID!"}),
        "viewer": ("User", {}),
        "search": ("SearchResultItemConnection!", {"query": "String!", "type": "SearchType!", "first": "Int", "after": "String"}),
        "__type": ("__Type", {"name": "String!"}),
        "__schema": ("__Schema", {}),
    },
    "Mutation": {
        "createPullRequest": ("CreatePullRequestPayload", {"input": "CreatePullRequestInput!"}),
    },
    "Repository": {
        "id": ("ID!", {}),
        "databaseId": ("Int", {}),
        "name": ("String!", {}),
        "nameWithOwner": ("String!", {}),
        "owner": ("RepositoryOwner!", {}),
        "description": ("String", {}),
        "url": ("URI", {}),
        "isPrivate": ("Boolean!", {}),
        "defaultBranchRef": ("Ref", {}),
        "viewerPermission": ("RepositoryPermission", {}),
        "hasIssuesEnabled": ("Boolean!", {}),
        "issue": ("Issue", {"number": "Int!"}),
        "issues": ("IssueConnection!", {
            "first": "Int",
            "after": "String",
            "states": "[IssueState!]",
            "orderBy": "IssueOrder",
            "labels": "[String!]",
            "filterBy": "IssueFilters",
        }),
        "pullRequest": ("PullRequest", {"number": "Int!"}),
        "pullRequests": ("PullRequestConnection!", {
            "first": "Int",
            "states": "[PullRequestState!]",
            "orderBy": "IssueOrder",
            "headRefName": "String",
            "baseRefName": "String",
        }),
        "createdAt": ("DateTime", {}),
        "updatedAt": ("DateTime", {}),
    },
    "Issue": {
        "id": ("ID!", {}),
        "number": ("Int!", {}),
        "title": ("String!", {}),
        "state": ("IssueState!", {}),
        "body": ("String", {}),
        "url": ("URI", {}),
        "author": ("User", {}),
        "createdAt": ("DateTime", {}),
        "updatedAt": ("DateTime", {}),
        "closed": ("Boolean!", {}),
        "closedAt": ("DateTime", {}),
        "repository": ("Repository", {}),
        "labels": ("LabelConnection", {"first": "Int"}),
        "comments": ("IssueCommentConnection", {"first": "Int"}),
    },
    "PullRequest": {
        "id": ("ID!", {}),
        "number": ("Int!", {}),
        "title": ("String", {}),
        "body": ("String", {}),
        "state": ("PullRequestState!", {}),
        "merged": ("Boolean!", {}),
        "mergeable": ("MergeableState!", {}),
        "headRefName": ("String!", {}),
        "baseRefName": ("String!", {}),
        "headRefOid": ("GitObjectID", {}),
        "baseRefOid": ("GitObjectID", {}),
        "url": ("URI", {}),
        "files": ("PullRequestChangedFileConnection", {"first": "Int"}),
        "additions": ("Int!", {}),
        "deletions": ("Int!", {}),
        "changedFiles": ("Int!", {}),
        "author": ("User", {}),
        "createdAt": ("DateTime", {}),
        "updatedAt": ("DateTime", {}),
        "closed": ("Boolean!", {}),
        "closedAt": ("DateTime", {}),
    },
    "User": {
        "id": ("ID!", {}),
        "login": ("String!", {}),
        "name": ("String", {}),
        "url": ("URI", {}),
        "avatarUrl": ("URI", {}),
    },
    "RepositoryOwner": {
        "id": ("ID!", {}),
        "login": ("String!", {}),
    },
    "Ref": {
        "name": ("String!", {}),
        "prefix": ("String", {}),
        "target": ("GitObject", {}),
    },
    "GitObject": {
        "oid": ("GitObjectID", {}),
        "abbreviatedOid": ("String", {}),
    },
    "Commit": {
        "oid": ("GitObjectID", {}),
        "message": ("String", {}),
        "abbreviatedOid": ("String", {}),
    },
    "IssueConnection": {
        "nodes": ("[Issue]", {}),
        "edges": ("[IssueEdge]", {}),
        "pageInfo": ("PageInfo!", {}),
        "totalCount": ("Int!", {}),
    },
    "IssueEdge": {"cursor": ("String!", {}), "node": ("Issue", {})},
    "PullRequestConnection": {
        "nodes": ("[PullRequest]", {}),
        "edges": ("[PullRequestEdge]", {}),
        "pageInfo": ("PageInfo!", {}),
        "totalCount": ("Int!", {}),
    },
    "PullRequestEdge": {"cursor": ("String!", {}), "node": ("PullRequest", {})},
    "PullRequestChangedFileConnection": {
        "nodes": ("[PullRequestChangedFile]", {}),
        "edges": ("[PullRequestChangedFileEdge]", {}),
        "pageInfo": ("PageInfo!", {}),
        "totalCount": ("Int!", {}),
    },
    "PullRequestChangedFileEdge": {"cursor": ("String!", {}), "node": ("PullRequestChangedFile", {})},
    "PullRequestChangedFile": {
        "path": ("String!", {}),
        "additions": ("Int!", {}),
        "deletions": ("Int!", {}),
        "changeType": ("PatchStatus", {}),
    },
    "LabelConnection": {
        "nodes": ("[Label]", {}),
        "totalCount": ("Int!", {}),
        "pageInfo": ("PageInfo!", {}),
    },
    "Label": {"id": ("ID!", {}), "name": ("String!", {}), "color": ("String", {}), "description": ("String", {})},
    "IssueCommentConnection": {
        "totalCount": ("Int!", {}),
        "nodes": ("[IssueComment]", {}),
        "pageInfo": ("PageInfo!", {}),
    },
    "IssueComment": {"id": ("ID", {}), "body": ("String", {}), "author": ("User", {})},
    "PageInfo": {
        "hasNextPage": ("Boolean!", {}),
        "hasPreviousPage": ("Boolean!", {}),
        "startCursor": ("String", {}),
        "endCursor": ("String", {}),
    },
    "CreatePullRequestPayload": {
        "pullRequest": ("PullRequest", {}),
        "clientMutationId": ("String", {}),
    },
    "SearchResultItemConnection": {
        "nodes": ("[SearchResultItem]", {}),
        "edges": ("[SearchResultItemEdge]", {}),
        "pageInfo": ("PageInfo!", {}),
        "issueCount": ("Int!", {}),
        "repositoryCount": ("Int!", {}),
        "userCount": ("Int!", {}),
    },
    "SearchResultItemEdge": {"cursor": ("String!", {}), "node": ("SearchResultItem", {})},
    "Node": {"id": ("ID!", {})},
    "__Type": {
        "name": ("String", {}),
        "kind": ("__TypeKind", {}),
        "description": ("String", {}),
        "fields": ("[__Field!]", {"includeDeprecated": "Boolean"}),
        "enumValues": ("[__EnumValue!]", {"includeDeprecated": "Boolean"}),
        "interfaces": ("[__Type!]", {}),
        "possibleTypes": ("[__Type!]", {}),
        "inputFields": ("[__InputValue!]", {}),
        "ofType": ("__Type", {}),
    },
    "__Field": {
        "name": ("String!", {}),
        "description": ("String", {}),
        "args": ("[__InputValue!]!", {}),
        "type": ("__Type!", {}),
        "isDeprecated": ("Boolean!", {}),
    },
    "__EnumValue": {
        "name": ("String!", {}),
        "description": ("String", {}),
        "isDeprecated": ("Boolean!", {}),
    },
    "__InputValue": {
        "name": ("String!", {}),
        "description": ("String", {}),
        "type": ("__Type!", {}),
        "defaultValue": ("String", {}),
    },
    "__Schema": {
        "description": ("String", {}),
        "types": ("[__Type!]!", {}),
        "queryType": ("__Type!", {}),
        "mutationType": ("__Type", {}),
    },
}

ENUMS = {
    "IssueState": ["OPEN", "CLOSED"],
    "PullRequestState": ["OPEN", "CLOSED", "MERGED"],
    "MergeableState": ["MERGEABLE", "CONFLICTING", "UNKNOWN"],
    "RepositoryPermission": ["ADMIN", "MAINTAIN", "WRITE", "TRIAGE", "READ"],
    "SearchType": ["ISSUE", "REPOSITORY", "USER", "DISCUSSION"],
    "PatchStatus": ["ADDED", "MODIFIED", "DELETED", "RENAMED"],
    "IssueOrderField": ["CREATED_AT", "UPDATED_AT", "COMMENTS"],
    "OrderDirection": ["ASC", "DESC"],
    "__TypeKind": ["SCALAR", "OBJECT", "INTERFACE", "UNION", "ENUM", "INPUT_OBJECT", "LIST", "NON_NULL"],
}

INPUTS = {
    "CreatePullRequestInput": {
        "repositoryId": "ID!",
        "baseRefName": "String!",
        "headRefName": "String!",
        "title": "String!",
        "body": "String",
        "draft": "Boolean",
        "maintainerCanModify": "Boolean",
    },
    "IssueOrder": {
        "field": "IssueOrderField!",
        "direction": "OrderDirection!",
    },
    "IssueFilters": {
        "since": "DateTime",
        "labels": "[String!]",
        "states": "[IssueState!]",
    },
}

UNIONS = {
    "SearchResultItem": ["Issue", "PullRequest", "Repository", "User"],
    "Node": ["Repository", "Issue", "PullRequest", "User"],
    "GitObject": ["Commit"],
}

SCALARS = {"String", "Int", "Float", "Boolean", "ID", "URI", "DateTime", "GitObjectID"}


def type_kind(name: str) -> str:
    if name in SCALARS:
        return "SCALAR"
    if name in ENUMS:
        return "ENUM"
    if name in INPUTS:
        return "INPUT_OBJECT"
    if name in UNIONS:
        return "UNION"
    if name in ("RepositoryOwner", "Node", "GitObject"):
        return "INTERFACE"
    if name in SCHEMA:
        return "OBJECT"
    return "OBJECT"


def parse_type_name(text: str) -> dict:
    text = text.strip()
    if text.endswith("!"):
        return {"kind": "NON_NULL", "name": None, "ofType": parse_type_name(text[:-1]), "description": None}
    if text.startswith("[") and text.endswith("]"):
        return {"kind": "LIST", "name": None, "ofType": parse_type_name(text[1:-1]), "description": None}
    return {"kind": type_kind(text), "name": text, "ofType": None, "description": None}


def unwrap(text: str) -> str:
    text = text.strip()
    while text.endswith("!"):
        text = text[:-1]
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1].strip()
        while text.endswith("!"):
            text = text[:-1]
    return text


@dataclass
class Sel:
    kind: str
    name: str = ""
    alias: str = ""
    args: list = field(default_factory=list)
    selection: list = field(default_factory=list)
    on_type: str = ""
    directives: list = field(default_factory=list)
    line: int = 1
    col: int = 1


@dataclass
class Op:
    kind: str
    name: str
    selection: list
    defaults: dict = field(default_factory=dict)
    line: int = 1


@dataclass
class Frag:
    name: str
    on_type: str
    selection: list


class Parser:
    def __init__(self, source: str):
        self.s = source
        self.n = len(source)
        self.i = 0
        self.line = 1
        self.col = 1

    def parse(self) -> tuple[list[Op], dict[str, Frag]]:
        ops: list[Op] = []
        frags: dict[str, Frag] = {}
        self._skip()
        if self.i >= self.n:
            raise ValueError("empty document")
        while self.i < self.n:
            self._skip()
            if self.i >= self.n:
                break
            if self._peek("{") or self._peek_keyword("query") or self._peek_keyword("mutation") or self._peek_keyword("subscription"):
                ops.append(self._operation())
            elif self._peek_keyword("fragment"):
                frag = self._fragment()
                frags[frag.name] = frag
            else:
                raise ValueError(f"unexpected token near {self.s[self.i:self.i + 20]!r}")
            self._skip()
        if not ops:
            raise ValueError("document has no operation")
        return ops, frags

    def _operation(self) -> Op:
        line = self.line
        kind = "query"
        if self._keyword("query"):
            kind = "query"
        elif self._keyword("mutation"):
            kind = "mutation"
        elif self._keyword("subscription"):
            kind = "subscription"
        self._skip()
        name = self._name() if self._peek_name() else ""
        defaults: dict = {}
        if self._eat("("):
            self._skip()
            while not self._eat(")"):
                self._expect("$")
                var = self._name()
                self._skip()
                self._expect(":")
                self._parse_type_expr()
                self._skip()
                if self._eat("="):
                    defaults[var] = self._value()
                self._skip()
                self._eat(",")
                self._skip()
        self._directives()
        return Op(kind, name, self._selection_set(), defaults, line)

    def _fragment(self) -> Frag:
        self._expect_keyword("fragment")
        name = self._name()
        self._expect_keyword("on")
        on_type = self._name()
        self._directives()
        return Frag(name, on_type, self._selection_set())

    def _selection_set(self) -> list:
        self._expect("{")
        items = []
        self._skip()
        while not self._eat("}"):
            items.append(self._selection())
            self._skip()
            self._eat(",")
            self._skip()
            if self.i >= self.n:
                raise ValueError("unterminated selection set")
        return items

    def _selection(self) -> Sel:
        line, col = self.line, self.col
        if self._eat("..."):
            self._skip()
            if self._keyword("on"):
                on_type = self._name()
                directives = self._directives()
                return Sel("inline", on_type=on_type, selection=self._selection_set(), directives=directives, line=line, col=col)
            if self._peek_name():
                name = self._name()
                return Sel("spread", name=name, directives=self._directives(), line=line, col=col)
            directives = self._directives()
            return Sel("inline", selection=self._selection_set(), directives=directives, line=line, col=col)
        name = self._name()
        alias = ""
        self._skip()
        if self._eat(":"):
            alias, name = name, self._name()
            self._skip()
        args = []
        if self._eat("("):
            self._skip()
            while not self._eat(")"):
                aname = self._name()
                self._expect(":")
                args.append((aname, self._value()))
                self._skip()
                self._eat(",")
                self._skip()
        directives = self._directives()
        selection = self._selection_set() if self._peek("{") else []
        return Sel("field", name=name, alias=alias, args=args, selection=selection, directives=directives, line=line, col=col)

    def _directives(self) -> list:
        found = []
        self._skip()
        while self._eat("@"):
            name = self._name()
            args = []
            if self._eat("("):
                self._skip()
                while not self._eat(")"):
                    aname = self._name()
                    self._expect(":")
                    args.append((aname, self._value()))
                    self._skip()
                    self._eat(",")
                    self._skip()
            found.append((name, args))
            self._skip()
        return found

    def _value(self):
        self._skip()
        if self.s.startswith('"""', self.i):
            return ("str", self._block_string())
        if self._eat("$"):
            return ("var", self._name())
        if self._eat("["):
            items = []
            self._skip()
            while not self._eat("]"):
                items.append(self._value())
                self._skip()
                self._eat(",")
                self._skip()
            return ("list", items)
        if self._eat("{"):
            obj = []
            self._skip()
            while not self._eat("}"):
                key = self._name()
                self._expect(":")
                obj.append((key, self._value()))
                self._skip()
                self._eat(",")
                self._skip()
            return ("obj", obj)
        if self._eat('"'):
            return ("str", self._string())
        if self._keyword("true"):
            return ("bool", True)
        if self._keyword("false"):
            return ("bool", False)
        if self._keyword("null"):
            return ("null", None)
        if self.i < self.n and (self.s[self.i].isdigit() or (self.s[self.i] == "-" and self.i + 1 < self.n and self.s[self.i + 1].isdigit())):
            return ("num", self._number())
        if self._peek_name():
            return ("enum", self._name())
        raise ValueError(f"bad value near {self.s[self.i:self.i + 16]!r}")

    def _block_string(self) -> str:
        self._bump(3)
        end = self.s.find('"""', self.i)
        if end < 0:
            raise ValueError("unterminated block string")
        text = self.s[self.i:end]
        self._bump(end - self.i + 3)
        if text.startswith("\n"):
            text = text[1:]
        return text

    def _string(self) -> str:
        out = []
        while self.i < self.n:
            ch = self.s[self.i]
            if ch == '"':
                self._bump()
                return "".join(out)
            if ch == "\\":
                self._bump()
                if self.i >= self.n:
                    break
                esc = self.s[self.i]
                self._bump()
                mapping = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "/": "/"}
                if esc in mapping:
                    out.append(mapping[esc])
                elif esc == "u":
                    hexes = self.s[self.i:self.i + 4]
                    self._bump(4)
                    out.append(chr(int(hexes, 16)))
                else:
                    out.append(esc)
            else:
                out.append(ch)
                self._bump()
        raise ValueError("unterminated string")

    def _number(self):
        start = self.i
        if self.s[self.i] == "-":
            self._bump()
        while self.i < self.n and self.s[self.i].isdigit():
            self._bump()
        if self.i < self.n and self.s[self.i] == ".":
            self._bump()
            while self.i < self.n and self.s[self.i].isdigit():
                self._bump()
            return float(self.s[start:self.i])
        return int(self.s[start:self.i])

    def _parse_type_expr(self) -> None:
        self._skip()
        if self._eat("["):
            self._parse_type_expr()
            self._expect("]")
        else:
            self._name()
        self._eat("!")

    def _at_keyword(self, word: str) -> bool:
        end = self.i + len(word)
        return self.s.startswith(word, self.i) and (end >= self.n or not (self.s[end].isalnum() or self.s[end] == "_"))

    def _peek_keyword(self, word: str) -> bool:
        self._skip()
        return self._at_keyword(word)

    def _keyword(self, word: str) -> bool:
        self._skip()
        if self._at_keyword(word):
            self._bump(len(word))
            return True
        return False

    def _expect_keyword(self, word: str) -> None:
        if not self._keyword(word):
            raise ValueError(f"expected {word}")

    def _name(self) -> str:
        self._skip()
        start = self.i
        if self.i >= self.n or not (self.s[self.i].isalpha() or self.s[self.i] == "_"):
            raise ValueError(f"expected name near {self.s[self.i:self.i + 12]!r}")
        self._bump()
        while self.i < self.n and (self.s[self.i].isalnum() or self.s[self.i] == "_"):
            self._bump()
        return self.s[start:self.i]

    def _peek_name(self) -> bool:
        self._skip()
        return self.i < self.n and (self.s[self.i].isalpha() or self.s[self.i] == "_")

    def _peek(self, token: str) -> bool:
        self._skip()
        return self.s.startswith(token, self.i)

    def _eat(self, token: str) -> bool:
        self._skip()
        if self.s.startswith(token, self.i):
            self._bump(len(token))
            return True
        return False

    def _expect(self, token: str) -> None:
        if not self._eat(token):
            raise ValueError(f"expected {token!r}")

    def _skip(self) -> None:
        while self.i < self.n:
            ch = self.s[self.i]
            if ch in " \t\r\n,":
                self._bump()
                continue
            if ch == "#":
                while self.i < self.n and self.s[self.i] != "\n":
                    self._bump()
                continue
            break

    def _bump(self, count: int = 1) -> None:
        for _ in range(count):
            if self.i < self.n and self.s[self.i] == "\n":
                self.line += 1
                self.col = 1
            else:
                self.col += 1
            self.i += 1


def _eval(node, variables: dict):
    kind = node[0]
    if kind == "var":
        return variables.get(node[1])
    if kind == "list":
        return [_eval(item, variables) for item in node[1]]
    if kind == "obj":
        return {key: _eval(value, variables) for key, value in node[1]}
    return node[1]


def _args(sel: Sel, variables: dict) -> dict:
    return {name: _eval(value, variables) for name, value in sel.args}


def _skipped(sel: Sel, variables: dict) -> bool:
    for name, args in sel.directives:
        values = {key: _eval(value, variables) for key, value in args}
        if name == "skip" and values.get("if"):
            return True
        if name == "include" and not values.get("if"):
            return True
    return False


class GRepo:
    __slots__ = ("repo", "store")

    def __init__(self, repo, store):
        self.repo = repo
        self.store = store


class GIssue:
    __slots__ = ("repo", "issue", "store")

    def __init__(self, repo, issue, store):
        self.repo = repo
        self.issue = issue
        self.store = store


class GPull:
    __slots__ = ("repo", "pull", "store")

    def __init__(self, repo, pull, store):
        self.repo = repo
        self.pull = pull
        self.store = store


class Conn:
    __slots__ = ("nodes", "total", "issue_count", "repository_count", "user_count")

    def __init__(self, nodes, total=None, issue_count=0, repository_count=0, user_count=0):
        self.nodes = nodes
        self.total = len(nodes) if total is None else total
        self.issue_count = issue_count
        self.repository_count = repository_count
        self.user_count = user_count


class Intro:
    __slots__ = ("name", "kind")

    def __init__(self, name: str | None, kind: str | None = None):
        self.name = name
        self.kind = kind or (type_kind(name) if name else None)


def _runtime(value, fallback: str) -> str:
    if isinstance(value, GIssue):
        return "Issue"
    if isinstance(value, GPull):
        return "PullRequest"
    if isinstance(value, GRepo):
        return "Repository"
    if isinstance(value, dict) and "login" in value and "number" not in value and "path" not in value:
        return "User"
    return fallback


def _applies(on_type: str, schema_type: str, runtime: str) -> bool:
    if not on_type or on_type in (schema_type, runtime):
        return True
    if on_type == "RepositoryOwner" and runtime == "User":
        return True
    if on_type == "Node":
        return True
    if on_type == "GitObject" and runtime in ("Commit", "GitObject"):
        return True
    if on_type in UNIONS and runtime in UNIONS.get(on_type, ()):
        return True
    return False


def _user(store) -> dict:
    return {
        "id": node_id("User", store.user_id),
        "login": store.login,
        "name": store.login,
        "url": f"/{store.login}",
        "avatarUrl": "",
    }


def _page(conn: Conn) -> dict:
    size = len(conn.nodes)
    return {
        "hasNextPage": conn.total > size,
        "hasPreviousPage": False,
        "startCursor": "0" if size else None,
        "endCursor": str(size - 1) if size else None,
    }


def _resolve(type_name: str, field: str, parent, args: dict, store):
    if type_name == "Query":
        if field == "repository":
            repo = store.get_repo(str(args.get("owner") or ""), str(args.get("name") or ""))
            return None if repo is None else GRepo(repo, store)
        if field == "viewer":
            return _user(store)
        if field == "node":
            return _node(store, args.get("id"))
        if field == "search":
            return _search(store, args)
        if field == "__type":
            name = args.get("name")
            if name in SCHEMA or name in ENUMS or name in SCALARS or name in INPUTS or name in UNIONS:
                return Intro(name)
            return None
        if field == "__schema":
            names = list(SCHEMA) + list(ENUMS) + list(INPUTS)
            return {
                "description": "forge",
                "types": [Intro(name) for name in names],
                "queryType": Intro("Query"),
                "mutationType": Intro("Mutation"),
            }
    if type_name == "Mutation" and field == "createPullRequest":
        return _create_pull(store, args.get("input") or {})
    if type_name == "Repository":
        return _repo_field(parent, field, args)
    if type_name == "Issue":
        return _issue_field(parent, field, args)
    if type_name == "PullRequest":
        return _pull_field(parent, field, args)
    if type_name in (
        "Ref", "GitObject", "Commit", "User", "RepositoryOwner", "PageInfo",
        "PullRequestChangedFile", "Label", "CreatePullRequestPayload",
        "IssueEdge", "PullRequestEdge", "PullRequestChangedFileEdge",
        "IssueComment", "SearchResultItemEdge",
    ):
        if isinstance(parent, dict):
            return parent.get(field)
    if type_name in (
        "IssueConnection", "PullRequestConnection", "PullRequestChangedFileConnection",
        "LabelConnection", "IssueCommentConnection", "SearchResultItemConnection",
    ):
        if field == "nodes":
            return parent.nodes
        if field == "totalCount":
            return parent.total
        if field == "pageInfo":
            return _page(parent)
        if field == "edges":
            return [{"cursor": str(index), "node": node} for index, node in enumerate(parent.nodes)]
        if field == "issueCount":
            return parent.issue_count
        if field == "repositoryCount":
            return parent.repository_count
        if field == "userCount":
            return parent.user_count
    if type_name == "__Type":
        return _intro_type_field(parent, field, args)
    if type_name in ("__Field", "__EnumValue", "__InputValue", "__Schema"):
        if isinstance(parent, dict):
            return parent.get(field)
    if isinstance(parent, dict) and field in parent:
        return parent[field]
    return None


def _node(store, gid):
    parsed = parse_node_id(str(gid)) if gid is not None else None
    if not parsed:
        return None
    kind, num = parsed
    if kind == "Repository":
        repo = store.get_repo_by_id(num)
        return GRepo(repo, store) if repo else None
    if kind == "Issue":
        for repo in store.list_repos():
            for issue in repo.issues.values():
                if issue.id == num:
                    return GIssue(repo, issue, store)
        return None
    if kind == "PullRequest":
        for repo in store.list_repos():
            for pull in repo.pulls.values():
                if pull.id == num:
                    return GPull(repo, pull, store)
        return None
    if kind == "User":
        return _user(store)
    return None


def _repo_field(parent: GRepo, field: str, args: dict):
    repo = parent.repo
    store = parent.store
    login = store.login
    if field == "id":
        return node_id("Repository", repo.id)
    if field == "databaseId":
        return repo.id
    if field == "name":
        return repo.name
    if field == "nameWithOwner":
        return f"{login}/{repo.name}"
    if field == "owner":
        return {"id": node_id("User", store.user_id), "login": login}
    if field == "description":
        return repo.description
    if field == "url":
        return f"/{login}/{repo.name}"
    if field == "isPrivate":
        return repo.private
    if field == "viewerPermission":
        return "ADMIN"
    if field == "hasIssuesEnabled":
        return True
    if field == "createdAt":
        return repo.created_at
    if field == "updatedAt":
        return repo.updated_at
    if field == "defaultBranchRef":
        git = gitstore.ensure_git(repo, store)
        sha = git.branch_sha(repo.default_branch)
        if sha is None:
            return None
        return {"name": repo.default_branch, "prefix": "refs/heads/", "target": {"oid": sha, "abbreviatedOid": sha[:7]}}
    if field == "issue":
        issue = repo.issues.get(int(args.get("number") or 0))
        return None if issue is None else GIssue(repo, issue, store)
    if field == "issues":
        return _issue_conn(parent, args)
    if field == "pullRequest":
        pull = repo.pulls.get(int(args.get("number") or 0))
        return None if pull is None else GPull(repo, pull, store)
    if field == "pullRequests":
        return _pull_conn(parent, args)
    return None


def _issue_conn(parent: GRepo, args: dict) -> Conn:
    repo = parent.repo
    states = args.get("states")
    filters = args.get("filterBy") or {}
    if not states and isinstance(filters, dict):
        states = filters.get("states")
    wanted = None
    if states:
        wanted = set()
        for state in states:
            if state == "OPEN":
                wanted.add("open")
            elif state == "CLOSED":
                wanted.add("closed")
    labels = args.get("labels")
    if not labels and isinstance(filters, dict):
        labels = filters.get("labels")
    with repo.lock:
        items = [repo.issues[number] for number in repo.issue_order if number in repo.issues]
    if wanted is not None:
        items = [issue for issue in items if issue.state in wanted]
    if labels:
        needed = {str(label).lower() for label in labels}
        items = [issue for issue in items if needed <= {name.lower() for name in issue.labels}]
    order = args.get("orderBy") or {"field": "CREATED_AT", "direction": "DESC"}
    direction = (order or {}).get("direction") or "DESC"
    field_name = (order or {}).get("field") or "CREATED_AT"
    reverse = direction != "ASC"

    def sort_key(issue):
        primary = issue.updated_at if field_name == "UPDATED_AT" else issue.created_at
        return (primary, issue.number)

    items.sort(key=sort_key, reverse=reverse)
    total = len(items)
    first = args.get("first")
    if first is not None:
        items = items[: max(0, int(first))]
    return Conn([GIssue(repo, issue, parent.store) for issue in items], total)


def _pull_conn(parent: GRepo, args: dict) -> Conn:
    repo = parent.repo
    with repo.lock:
        pulls = list(repo.pulls.values())
    states = args.get("states")
    if states:
        wanted = set(states)

        def keep(pull):
            if pull.merged:
                return "MERGED" in wanted or "CLOSED" in wanted
            return pull.state.upper() in wanted

        pulls = [pull for pull in pulls if keep(pull)]
    head = args.get("headRefName")
    base = args.get("baseRefName")
    if head:
        pulls = [pull for pull in pulls if pull.head == head]
    if base:
        pulls = [pull for pull in pulls if pull.base == base]
    pulls.sort(key=lambda pull: (pull.created_at, pull.number), reverse=True)
    total = len(pulls)
    first = args.get("first")
    if first is not None:
        pulls = pulls[: max(0, int(first))]
    return Conn([GPull(repo, pull, parent.store) for pull in pulls], total)


def _issue_field(parent: GIssue, field: str, args: dict):
    issue = parent.issue
    repo = parent.repo
    store = parent.store
    if field == "id":
        return node_id("Issue", issue.id)
    if field == "number":
        return issue.number
    if field == "title":
        return issue.title
    if field == "state":
        return "OPEN" if issue.state == "open" else "CLOSED"
    if field == "body":
        return issue.body
    if field == "url":
        return f"/{store.login}/{repo.name}/issues/{issue.number}"
    if field == "author":
        return _user(store)
    if field == "createdAt":
        return issue.created_at
    if field == "updatedAt":
        return issue.updated_at
    if field == "closed":
        return issue.state == "closed"
    if field == "closedAt":
        return issue.closed_at
    if field == "repository":
        return GRepo(repo, store)
    if field == "labels":
        nodes = []
        for name in issue.labels:
            label = repo.label_named(name)
            if label is not None:
                nodes.append({
                    "id": node_id("Label", label.id),
                    "name": label.name,
                    "color": label.color,
                    "description": label.description,
                })
        total = len(nodes)
        first = args.get("first")
        if first is not None:
            nodes = nodes[: int(first)]
        return Conn(nodes, total)
    if field == "comments":
        comments = repo.comments.get(issue.number, [])
        nodes = [{"id": node_id("IssueComment", comment.id), "body": comment.body, "author": _user(store)} for comment in comments]
        return Conn(nodes, len(comments))
    return None


def _change_type(status: str) -> str:
    return {"added": "ADDED", "removed": "DELETED", "modified": "MODIFIED"}.get(status, "MODIFIED")


def _pull_files(git, base_sha, head_sha) -> list[dict]:
    if not git or not base_sha or not head_sha:
        return []
    return git.diff(base_sha, head_sha)


def _pull_field(parent: GPull, field: str, args: dict):
    pull = parent.pull
    repo = parent.repo
    store = parent.store
    git = gitstore.ensure_git(repo, store)
    head_sha = git.branch_sha(pull.head)
    base_sha = git.branch_sha(pull.base)
    if field == "id":
        return node_id("PullRequest", pull.id)
    if field == "number":
        return pull.number
    if field == "title":
        return pull.title
    if field == "body":
        return pull.body
    if field == "state":
        if pull.merged:
            return "MERGED"
        return "OPEN" if pull.state == "open" else "CLOSED"
    if field == "merged":
        return pull.merged
    if field == "mergeable":
        if pull.state != "open" or not head_sha or not base_sha:
            return "UNKNOWN"
        return "MERGEABLE" if git.mergeable(base_sha, head_sha) else "CONFLICTING"
    if field == "headRefName":
        return pull.head
    if field == "baseRefName":
        return pull.base
    if field == "headRefOid":
        return head_sha
    if field == "baseRefOid":
        return base_sha
    if field == "url":
        return f"/{store.login}/{repo.name}/pull/{pull.number}"
    if field == "author":
        return _user(store)
    if field == "createdAt":
        return pull.created_at
    if field == "updatedAt":
        return pull.updated_at
    if field == "closed":
        return pull.state != "open"
    if field == "closedAt":
        return pull.closed_at
    if field in ("files", "additions", "deletions", "changedFiles"):
        files = _pull_files(git, base_sha, head_sha)
        if field == "files":
            nodes = [{
                "path": item["path"],
                "additions": item["additions"],
                "deletions": item["deletions"],
                "changeType": _change_type(item["status"]),
            } for item in files]
            total = len(nodes)
            first = args.get("first")
            if first is not None:
                nodes = nodes[: int(first)]
            return Conn(nodes, total)
        if field == "additions":
            return sum(item["additions"] for item in files)
        if field == "deletions":
            return sum(item["deletions"] for item in files)
        return len(files)
    return None


def _create_pull(store, payload: dict) -> dict:
    client = payload.get("clientMutationId")
    repo = None
    gid = payload.get("repositoryId")
    parsed = parse_node_id(str(gid)) if gid is not None else None
    if parsed and parsed[0] == "Repository":
        repo = store.get_repo_by_id(parsed[1])
    elif isinstance(gid, int) or (isinstance(gid, str) and str(gid).isdigit()):
        repo = store.get_repo_by_id(int(gid))
    if repo is None:
        return {"pullRequest": None, "clientMutationId": client, "_error": "repository not found"}
    gitstore.ensure_git(repo, store)
    title = payload.get("title") or "Pull request"
    status, body = store.create_pull(repo, {
        "title": title,
        "head": payload.get("headRefName") or "",
        "base": payload.get("baseRefName") or repo.default_branch,
        "body": payload.get("body"),
    })
    if status != 201:
        message = body.get("message", "validation failed") if isinstance(body, dict) else "validation failed"
        return {"pullRequest": None, "clientMutationId": client, "_error": message}
    pull = repo.pulls[body["number"]]
    return {"pullRequest": GPull(repo, pull, store), "clientMutationId": client}


def _search(store, args: dict) -> Conn:
    query = str(args.get("query") or "")
    kind = args.get("type") or "ISSUE"
    owner = None
    repo_name = None
    state = None
    text: list[str] = []
    want_pr = False
    for token in query.split():
        lowered = token.lower()
        if lowered.startswith("repo:") and "/" in token:
            owner, repo_name = token.split(":", 1)[1].split("/", 1)
        elif lowered in ("is:open", "state:open"):
            state = "open"
        elif lowered in ("is:closed", "state:closed"):
            state = "closed"
        elif lowered in ("is:pr", "is:pull-request", "type:pr"):
            want_pr = True
        elif lowered in ("is:issue", "type:issue"):
            want_pr = False
        elif lowered.startswith("is:") or lowered.startswith("type:") or lowered.startswith("repo:"):
            continue
        else:
            text.append(lowered)
    needle = " ".join(text)
    nodes = []
    issue_count = repository_count = user_count = 0
    if kind == "REPOSITORY":
        for repo in store.list_repos():
            if repo_name and repo.name.lower() != repo_name.lower():
                continue
            if needle and needle not in repo.name.lower() and needle not in (repo.description or "").lower():
                continue
            nodes.append(GRepo(repo, store))
        repository_count = len(nodes)
    elif kind == "USER":
        login = store.login
        if not needle or needle in login.lower():
            nodes.append(_user(store))
        user_count = len(nodes)
    else:
        repos = store.list_repos()
        for repo in repos:
            if owner and owner.lower() != store.login.lower():
                continue
            if repo_name and repo.name.lower() != repo_name.lower():
                continue
            if want_pr:
                for pull in repo.pulls.values():
                    if state == "open" and pull.state != "open":
                        continue
                    if state == "closed" and pull.state == "open":
                        continue
                    hay = f"{pull.title}\n{pull.body or ''}".lower()
                    if needle and needle not in hay:
                        continue
                    nodes.append(GPull(repo, pull, store))
            else:
                for issue in store.list_issues(repo, state or "all"):
                    hay = f"{issue.title}\n{issue.body or ''}".lower()
                    if needle and needle not in hay:
                        continue
                    nodes.append(GIssue(repo, issue, store))
        issue_count = len(nodes)
    total = len(nodes)
    first = args.get("first")
    if first is not None:
        nodes = nodes[: max(0, int(first))]
    return Conn(nodes, total, issue_count, repository_count, user_count)


def _intro_type_field(parent, field: str, args: dict):
    del args
    if isinstance(parent, dict):
        return parent.get(field)
    name = parent.name
    kind = parent.kind or (type_kind(name) if name else None)
    if field == "name":
        return name
    if field == "kind":
        return kind
    if field == "description":
        return name
    if field == "ofType":
        return None
    if field == "fields":
        spec = SCHEMA.get(name or "")
        if not spec or kind not in ("OBJECT", "INTERFACE"):
            return None
        fields = []
        for fname, (ftype, fargs) in spec.items():
            fields.append({
                "name": fname,
                "description": fname,
                "args": [{
                    "name": aname,
                    "description": aname,
                    "type": parse_type_name(atype),
                    "defaultValue": None,
                } for aname, atype in fargs.items()],
                "type": parse_type_name(ftype),
                "isDeprecated": False,
            })
        return fields
    if field == "enumValues":
        values = ENUMS.get(name or "")
        if not values:
            return None
        return [{"name": value, "description": value, "isDeprecated": False} for value in values]
    if field == "interfaces":
        if name == "User":
            return [Intro("RepositoryOwner"), Intro("Node")]
        if name in ("Repository", "Issue", "PullRequest"):
            return [Intro("Node")]
        if name == "Commit":
            return [Intro("GitObject")]
        return []
    if field == "possibleTypes":
        members = UNIONS.get(name or "")
        if name == "RepositoryOwner":
            members = ["User"]
        if not members:
            return None
        return [Intro(member) for member in members]
    if field == "inputFields":
        spec = INPUTS.get(name or "")
        if not spec:
            return None
        return [{
            "name": aname,
            "description": aname,
            "type": parse_type_name(atype),
            "defaultValue": None,
        } for aname, atype in spec.items()]
    return None


def _iter_fields(selections, schema_type: str, runtime: str, frags: dict[str, Frag], variables: dict):
    for sel in selections:
        if _skipped(sel, variables):
            continue
        if sel.kind == "field":
            yield sel
        elif sel.kind == "spread":
            frag = frags.get(sel.name)
            if frag and _applies(frag.on_type, schema_type, runtime):
                yield from _iter_fields(frag.selection, schema_type, runtime, frags, variables)
        elif sel.kind == "inline":
            if _applies(sel.on_type, schema_type, runtime):
                yield from _iter_fields(sel.selection, schema_type, runtime, frags, variables)


def _validate(selections, type_name: str, frags, errors: list) -> None:
    if type_name not in SCHEMA and type_name in UNIONS:
        for sel in selections:
            if sel.kind == "field" and sel.name != "__typename":
                errors.append({
                    "message": f'Cannot query field "{sel.name}" on type "{type_name}".',
                    "locations": [{"line": sel.line, "column": sel.col}],
                })
            elif sel.kind == "inline" and sel.on_type:
                _validate(sel.selection, sel.on_type, frags, errors)
            elif sel.kind == "spread":
                frag = frags.get(sel.name)
                if frag:
                    _validate(frag.selection, frag.on_type, frags, errors)
        return
    spec = SCHEMA.get(type_name)
    for sel in selections:
        if sel.kind == "spread":
            frag = frags.get(sel.name)
            if frag is None:
                errors.append({
                    "message": f'Unknown fragment "{sel.name}".',
                    "locations": [{"line": sel.line, "column": sel.col}],
                })
            else:
                _validate(frag.selection, frag.on_type, frags, errors)
            continue
        if sel.kind == "inline":
            _validate(sel.selection, sel.on_type or type_name, frags, errors)
            continue
        if sel.name == "__typename":
            continue
        if spec is None or sel.name not in spec:
            errors.append({
                "message": f'Cannot query field "{sel.name}" on type "{type_name}".',
                "locations": [{"line": sel.line, "column": sel.col}],
            })
            continue
        declared, arg_spec = spec[sel.name]
        provided = {name for name, _value in sel.args}
        for aname, atype in arg_spec.items():
            if atype.endswith("!") and aname not in provided:
                errors.append({
                    "message": f'Field "{sel.name}" argument "{aname}" of type "{atype}" is required.',
                    "locations": [{"line": sel.line, "column": sel.col}],
                })
        if sel.selection:
            inner = unwrap(declared)
            if inner in SCHEMA or inner in UNIONS:
                _validate(sel.selection, inner, frags, errors)


def _exec(type_name: str, parent, selections, store, variables, frags) -> dict:
    runtime = _runtime(parent, type_name)
    if type_name == "RepositoryOwner":
        runtime = "User"
        schema_type = "User"
    elif type_name not in SCHEMA and type_name in UNIONS:
        schema_type = runtime
    else:
        schema_type = type_name
    out = {}
    spec = SCHEMA.get(schema_type, SCHEMA.get(type_name, {}))
    for sel in _iter_fields(selections, type_name, runtime, frags, variables):
        key = sel.alias or sel.name
        if sel.name == "__typename":
            if type_name == "RepositoryOwner" or schema_type == "User" and type_name != "User":
                out[key] = "User"
            elif type_name in UNIONS:
                out[key] = runtime
            else:
                out[key] = schema_type
            continue
        args = _args(sel, variables)
        value = _resolve(schema_type, sel.name, parent, args, store)
        if isinstance(value, dict) and "_error" in value and sel.name == "createPullRequest":
            value = {item_key: item for item_key, item in value.items() if item_key != "_error"}
        declared = spec.get(sel.name, ("String", {}))[0]
        out[key] = _exec_value(declared, value, sel.selection, store, variables, frags)
    return out


def _exec_value(declared: str, value, selections, store, variables, frags):
    if value is None:
        return None
    text = declared.strip()
    if text.endswith("!"):
        return _exec_value(text[:-1], value, selections, store, variables, frags)
    if text.startswith("["):
        inner = text[1:-1]
        return [_exec_value(inner, item, selections, store, variables, frags) for item in value]
    named = unwrap(text)
    if named in SCALARS or named in ENUMS:
        return value
    if selections and (named in SCHEMA or named in UNIONS):
        return _exec(named, value, selections, store, variables, frags)
    return value


def _run(op: Op, frags, variables: dict) -> dict:
    merged = dict(variables or {})
    for name, default in op.defaults.items():
        if name not in merged:
            merged[name] = _eval(default, merged)
    root = "Mutation" if op.kind == "mutation" else "Query"
    errors: list = []
    _validate(op.selection, root, frags, errors)
    if errors:
        return {"data": None, "errors": errors}
    data = _exec(root, None, op.selection, get_store(), merged, frags)
    return {"data": data}


def execute_document(source: str, variables: dict | None, operation_name: str | None):
    ops, frags = Parser(source).parse()
    variables = variables or {}
    if operation_name:
        chosen = [op for op in ops if op.name == operation_name]
        if not chosen:
            return {"errors": [{"message": f'Unknown operation named "{operation_name}".'}]}
        return _run(chosen[0], frags, variables)
    if len(ops) > 1:
        return [_run(op, frags, variables) for op in ops]
    return _run(ops[0], frags, variables)


def handle(body: bytes) -> tuple:
    if not body or not body.strip():
        return 400, dumps({"message": "Problems parsing JSON"}), JSON, ()
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return 400, dumps({"message": "Problems parsing JSON"}), JSON, ()
    try:
        if isinstance(payload, list):
            return 200, dumps([_one(item) for item in payload]), JSON, ()
        if not isinstance(payload, dict):
            return 400, dumps({"message": "Problems parsing JSON"}), JSON, ()
        result = _one(payload)
        return 200, dumps(result), JSON, ()
    except Exception as exc:
        return 200, dumps({"data": None, "errors": [{"message": f"GraphQL error: {exc}"}]}), JSON, ()


def _one(payload) -> dict | list:
    if not isinstance(payload, dict):
        return {"errors": [{"message": "Problems parsing JSON"}]}
    query = payload.get("query")
    if not isinstance(query, str) or not query.strip():
        return {"errors": [{"message": "Must provide query string."}]}
    variables = payload.get("variables") or {}
    if not isinstance(variables, dict):
        variables = {}
    operation = payload.get("operationName")
    if operation is not None and not isinstance(operation, str):
        operation = None
    return execute_document(query, variables, operation)
