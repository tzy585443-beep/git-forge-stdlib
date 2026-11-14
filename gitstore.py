"""Git object database and smart HTTP (upload-pack v2, receive-pack v0).

Objects and packfiles are built with hashlib and zlib. No git binary.
Protocol shapes match git's smart HTTP: a v2 advertisement opens with
"version 2", receive-pack keeps the "# service=git-receive-pack" preamble,
and a report-status payload is pkt-lines wrapped in side-band-64k.
"""

from __future__ import annotations

import hashlib
import struct
import threading
import zlib
from urllib.parse import parse_qs, unquote

ZERO = "0" * 40
JSON = "application/json; charset=utf-8"
NOT_FOUND = b'{"message":"Not Found"}'
TYPENAME = {1: "commit", 2: "tree", 3: "blob", 4: "tag"}
TYPEID = {"commit": 1, "tree": 2, "blob": 3, "tag": 4}
AGENT = "agent=git/2.43.0"
UPLOAD_AD = "application/x-git-upload-pack-advertisement"
RECEIVE_AD = "application/x-git-receive-pack-advertisement"
UPLOAD_RESULT = "application/x-git-upload-pack-result"
RECEIVE_RESULT = "application/x-git-receive-pack-result"
NO_CACHE = (("Cache-Control", "no-cache"),)
_PACK = struct.Struct(">4sII")


def sha1(*parts: bytes) -> bytes:
    digest = hashlib.sha1(usedforsecurity=False)
    for part in parts:
        digest.update(part)
    return digest.digest()


def object_id(typ: str, content: bytes) -> str:
    return sha1(f"{typ} {len(content)}\0".encode(), content).hex()


def pkt(data: bytes | str) -> bytes:
    if isinstance(data, str):
        data = data.encode()
    return f"{len(data) + 4:04x}".encode() + data


def sideband(data: bytes, band: int = 1, limit: int = 65515) -> bytes:
    """Wrap bytes as side-band pkt-lines. limit keeps each pkt at or under 65520."""
    if not data:
        return b""
    out = bytearray()
    band_byte = bytes((band,))
    view = memoryview(data)
    offset = 0
    total = len(view)
    while offset < total:
        take = limit if total - offset > limit else total - offset
        chunk = band_byte + view[offset : offset + take].tobytes()
        out += f"{len(chunk) + 4:04x}".encode()
        out += chunk
        offset += take
    return bytes(out)


def read_varint(data: bytes | memoryview, index: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while True:
        byte = data[index]
        index += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, index
        shift += 7


def read_ofs(data: bytes | memoryview, index: int) -> tuple[int, int]:
    byte = data[index]
    index += 1
    offset = byte & 0x7F
    while byte & 0x80:
        byte = data[index]
        index += 1
        offset = ((offset + 1) << 7) | (byte & 0x7F)
    return offset, index


def apply_delta(base: bytes, delta: bytes) -> bytes:
    index = 0
    source_size, index = read_varint(delta, index)
    target_size, index = read_varint(delta, index)
    if source_size != len(base):
        raise ValueError("delta base size mismatch")
    out = bytearray()
    delta_len = len(delta)
    while index < delta_len:
        cmd = delta[index]
        index += 1
        if cmd == 0:
            raise ValueError("bad delta opcode")
        if cmd & 0x80:
            offset = 0
            size = 0
            if cmd & 0x01:
                offset |= delta[index]
                index += 1
            if cmd & 0x02:
                offset |= delta[index] << 8
                index += 1
            if cmd & 0x04:
                offset |= delta[index] << 16
                index += 1
            if cmd & 0x08:
                offset |= delta[index] << 24
                index += 1
            if cmd & 0x10:
                size |= delta[index]
                index += 1
            if cmd & 0x20:
                size |= delta[index] << 8
                index += 1
            if cmd & 0x40:
                size |= delta[index] << 16
                index += 1
            if size == 0:
                size = 0x10000
            out += base[offset : offset + size]
        else:
            out += delta[index : index + cmd]
            index += cmd
    if len(out) != target_size:
        raise ValueError("delta target size mismatch")
    return bytes(out)


def _object_header(type_id: int, size: int) -> bytes:
    first = (type_id << 4) | (size & 0x0F)
    size >>= 4
    if not size:
        return bytes((first,))
    header = bytearray()
    while size:
        header.append(first | 0x80)
        first = size & 0x7F
        size >>= 7
    header.append(first)
    return bytes(header)


def build_pack(objects: list[tuple[str, bytes]]) -> bytes:
    out = bytearray()
    out += _PACK.pack(b"PACK", 2, len(objects))
    for typ, content in objects:
        out += _object_header(TYPEID[typ], len(content))
        out += zlib.compress(content, 1)
    out += sha1(out)
    return bytes(out)


def parse_pack(data: bytes, lookup) -> list[tuple[str, bytes]]:
    if len(data) < 32 or data[:4] != b"PACK":
        raise ValueError("not a pack")
    version = int.from_bytes(data[4:8], "big")
    if version not in (2, 3):
        raise ValueError(f"unsupported pack version {version}")
    count = int.from_bytes(data[8:12], "big")
    body = memoryview(data)[:-20]
    if sha1(data[:-20]) != data[-20:]:
        raise ValueError("pack checksum mismatch")
    entries = []
    index = 12
    body_len = len(body)
    for _ in range(count):
        start = index
        if index >= body_len:
            raise ValueError("truncated pack")
        byte = body[index]
        index += 1
        typ = (byte >> 4) & 7
        size = byte & 0x0F
        shift = 4
        while byte & 0x80:
            if index >= body_len:
                raise ValueError("truncated pack header")
            byte = body[index]
            index += 1
            size |= (byte & 0x7F) << shift
            shift += 7
        base_sha = None
        base_ofs = None
        if typ == 7:
            if index + 20 > body_len:
                raise ValueError("truncated ref delta")
            base_sha = bytes(body[index : index + 20]).hex()
            index += 20
        elif typ == 6:
            relative, index = read_ofs(body, index)
            base_ofs = start - relative
        elif typ not in TYPENAME:
            raise ValueError(f"unsupported object type {typ}")
        inflater = zlib.decompressobj()
        inflated = inflater.decompress(body[index:])
        if not inflater.eof:
            raise ValueError("incomplete zlib stream")
        unused = inflater.unused_data
        index = body_len - len(unused)
        if len(inflated) != size:
            raise ValueError("object size mismatch")
        entries.append((start, typ, inflated, base_sha, base_ofs))

    resolved: dict[int, tuple[str, bytes]] = {}
    by_sha: dict[str, tuple[str, bytes]] = {}
    output: list[tuple[str, bytes]] = []
    pending = []
    for start, typ, payload, base_sha, base_ofs in entries:
        if typ in TYPENAME:
            name = TYPENAME[typ]
            pair = (name, payload)
            resolved[start] = pair
            by_sha[object_id(name, payload)] = pair
            output.append(pair)
        else:
            pending.append((start, typ, payload, base_sha, base_ofs))
    guard = 0
    while pending:
        guard += 1
        if guard > len(entries) + 2:
            raise ValueError("unresolved delta")
        progress = False
        still = []
        for start, typ, payload, base_sha, base_ofs in pending:
            if typ == 6:
                base = resolved.get(base_ofs)
            else:
                base = by_sha.get(base_sha) if base_sha else None
                if base is None and base_sha:
                    base = lookup(base_sha)
            if base is None:
                still.append((start, typ, payload, base_sha, base_ofs))
                continue
            content = apply_delta(base[1], payload)
            pair = (base[0], content)
            resolved[start] = pair
            by_sha[object_id(pair[0], content)] = pair
            output.append(pair)
            progress = True
        if not progress:
            raise ValueError("unresolved delta")
        pending = still
    return output


def parse_commit(content: bytes) -> tuple[str, list[str]]:
    tree = ""
    parents: list[str] = []
    for line in content.split(b"\n"):
        if not line:
            break
        if line.startswith(b"tree "):
            tree = line[5:].decode()
        elif line.startswith(b"parent "):
            parents.append(line[7:].decode())
    return tree, parents


def parse_commit_full(content: bytes) -> dict:
    tree = ""
    parents: list[str] = []
    author = ""
    committer = ""
    lines = content.split(b"\n")
    message = ""
    for index, line in enumerate(lines):
        if line == b"":
            message = b"\n".join(lines[index + 1 :]).decode("utf-8", "replace")
            break
        if line.startswith(b"tree "):
            tree = line[5:].decode()
        elif line.startswith(b"parent "):
            parents.append(line[7:].decode())
        elif line.startswith(b"author "):
            author = line[7:].decode("utf-8", "replace")
        elif line.startswith(b"committer "):
            committer = line[10:].decode("utf-8", "replace")
    return {
        "tree": tree,
        "parents": parents,
        "author": author,
        "committer": committer,
        "message": message,
    }


def parse_tree(content: bytes) -> list[tuple[str, str, str]]:
    entries = []
    index = 0
    length = len(content)
    while index < length:
        space = content.index(b" ", index)
        mode = content[index:space].decode()
        index = space + 1
        null = content.index(b"\0", index)
        name = content[index:null].decode()
        index = null + 1
        entries.append((mode, name, content[index : index + 20].hex()))
        index += 20
    return entries


def encode_tree(entries: list[tuple[str, str, str]]) -> bytes:
    def sort_key(entry: tuple[str, str, str]) -> bytes:
        mode, name, _sha = entry
        raw = name.encode()
        return raw + b"/" if mode == "40000" else raw

    out = bytearray()
    for mode, name, sha in sorted(entries, key=sort_key):
        out += mode.encode()
        out += b" "
        out += name.encode()
        out += b"\0"
        out += bytes.fromhex(sha)
    return bytes(out)


def iso_to_unix(iso: str) -> int:
    from datetime import datetime, timezone

    text = iso[:-1] + "+00:00" if iso.endswith(("Z", "z")) else iso
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(moment.timestamp())


class PutResult:
    __slots__ = ("status", "commit", "blob", "tree", "parents", "message")

    def __init__(self, status: str, commit=None, blob=None, tree=None, parents=None, message=""):
        self.status = status
        self.commit = commit
        self.blob = blob
        self.tree = tree
        self.parents = parents or []
        self.message = message


class GitRepo:
    def __init__(self, default_branch: str = "main"):
        self.head_target = f"refs/heads/{default_branch}"
        self.objects: dict[str, tuple[str, bytes]] = {}
        self.refs: dict[str, str] = {}
        self.parents: dict[str, tuple[str, ...]] = {}
        self.commit_tree: dict[str, str] = {}
        self.lock = threading.RLock()
        # Framed upload-pack bodies keyed by the wants. Content-addressed, so a
        # push only has to drop the dict; identical shallow clones share one pack.
        self.resp_cache: dict[tuple, bytes] = {}
        self.inflight: dict[tuple, threading.Event] = {}

    def _bump(self) -> None:
        self.resp_cache.clear()
        self.inflight.clear()

    def add_object(self, typ: str, content: bytes) -> str:
        oid = object_id(typ, content)
        if oid not in self.objects:
            self.objects[oid] = (typ, content)
            if typ == "commit":
                tree, parents = parse_commit(content)
                self.parents[oid] = tuple(parents)
                self.commit_tree[oid] = tree
        return oid

    def lookup(self, oid: str):
        return self.objects.get(oid)

    def branch_sha(self, branch: str) -> str | None:
        return self.refs.get(f"refs/heads/{branch}")

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        if ancestor == descendant:
            return True
        stack = [descendant]
        seen: set[str] = set()
        parents = self.parents
        while stack:
            sha = stack.pop()
            if sha == ancestor:
                return True
            if sha in seen:
                continue
            seen.add(sha)
            stack.extend(parents.get(sha, ()))
        return False

    def merge_base(self, left: str, right: str) -> str | None:
        ancestors: set[str] = set()
        stack = [left]
        parents = self.parents
        while stack:
            sha = stack.pop()
            if sha in ancestors:
                continue
            ancestors.add(sha)
            stack.extend(parents.get(sha, ()))
        queue = [right]
        seen: set[str] = set()
        head = 0
        while head < len(queue):
            sha = queue[head]
            head += 1
            if sha in seen:
                continue
            seen.add(sha)
            if sha in ancestors:
                return sha
            queue.extend(parents.get(sha, ()))
        return None

    def write_tree(self, entries: list[tuple[str, str, str]]) -> str:
        return self.add_object("tree", encode_tree(entries))

    def walk(self, tree_sha: str | None, parts: list[str]):
        if tree_sha is None:
            return None
        node = tree_sha
        kind = "tree"
        objects = self.objects
        for depth, name in enumerate(parts):
            if kind != "tree":
                return None
            found = None
            for mode, entry_name, sha in parse_tree(objects[node][1]):
                if entry_name == name:
                    found = (mode, sha)
                    break
            if found is None:
                return None
            mode, sha = found
            kind = "tree" if mode == "40000" else "blob"
            node = sha
            if depth == len(parts) - 1:
                return kind, mode, sha
        return "tree", "40000", tree_sha

    def flatten(self, tree_sha: str | None, prefix: str = "") -> dict[str, tuple[str, str]]:
        if not tree_sha:
            return {}
        flat: dict[str, tuple[str, str]] = {}
        for mode, name, sha in parse_tree(self.objects[tree_sha][1]):
            path = f"{prefix}{name}"
            if mode == "40000":
                flat.update(self.flatten(sha, path + "/"))
            else:
                flat[path] = (mode, sha)
        return flat

    def upsert(self, tree_sha: str | None, parts: list[str], mode: str, blob_sha: str) -> str:
        entries = parse_tree(self.objects[tree_sha][1]) if tree_sha else []
        name = parts[0]
        if len(parts) == 1:
            kept = [entry for entry in entries if entry[1] != name]
            kept.append((mode, name, blob_sha))
            return self.write_tree(kept)
        sub = None
        kept = []
        for entry_mode, entry_name, sha in entries:
            if entry_name == name and entry_mode == "40000":
                sub = sha
            elif entry_name != name:
                kept.append((entry_mode, entry_name, sha))
        sub_sha = self.upsert(sub, parts[1:], mode, blob_sha)
        kept.append(("40000", name, sub_sha))
        return self.write_tree(kept)

    def delete_from_tree(self, tree_sha: str | None, parts: list[str]) -> tuple[str | None, bool]:
        if not tree_sha:
            return None, False
        entries = parse_tree(self.objects[tree_sha][1])
        name = parts[0]
        if len(parts) == 1:
            kept = [entry for entry in entries if entry[1] != name]
            if len(kept) == len(entries):
                return tree_sha, False
            if not kept:
                return None, True
            return self.write_tree(kept), True
        kept = []
        removed = False
        for entry_mode, entry_name, sha in entries:
            if entry_name == name and entry_mode == "40000":
                sub, ok = self.delete_from_tree(sha, parts[1:])
                removed = ok
                if sub is not None:
                    kept.append(("40000", entry_name, sub))
            else:
                kept.append((entry_mode, entry_name, sha))
        if not removed:
            return tree_sha, False
        if not kept:
            return None, True
        return self.write_tree(kept), True

    def make_commit(self, tree: str, parents: list[str], message: str, name: str, email: str, when_iso: str) -> str:
        if not message.endswith("\n"):
            message += "\n"
        stamp = iso_to_unix(when_iso)
        ident = f"{name} <{email}> {stamp} +0000"
        lines = [f"tree {tree}\n"]
        lines.extend(f"parent {parent}\n" for parent in parents)
        lines.append(f"author {ident}\n")
        lines.append(f"committer {ident}\n\n")
        content = "".join(lines).encode() + message.encode()
        return self.add_object("commit", content)

    def tip_tree(self, branch: str) -> tuple[str | None, str | None]:
        sha = self.branch_sha(branch)
        if sha is None:
            return None, None
        return sha, self.commit_tree.get(sha)

    def put_file(
        self,
        branch: str,
        path: str,
        content: bytes,
        message: str,
        name: str,
        email: str,
        when_iso: str,
        expected_sha: str | None,
    ) -> PutResult:
        with self.lock:
            ref = f"refs/heads/{branch}"
            parent = self.refs.get(ref)
            if parent is None and self.refs:
                return PutResult("missing_branch")
            tree = self.commit_tree.get(parent) if parent else None
            parts = [part for part in path.split("/") if part not in ("", ".")]
            if not parts or any(part == ".." for part in parts):
                return PutResult("invalid")
            current = self.walk(tree, parts) if tree else None
            current_blob = current[2] if current and current[0] == "blob" else None
            if current and current[0] == "tree":
                return PutResult("invalid")
            if current_blob is None:
                if expected_sha:
                    return PutResult("mismatch")
                created = True
                mode = "100644"
            else:
                if not expected_sha or expected_sha != current_blob:
                    return PutResult("mismatch")
                created = False
                mode = current[1]
            blob = self.add_object("blob", content)
            new_tree = self.upsert(tree, parts, mode, blob)
            parents = [parent] if parent else []
            commit = self.make_commit(new_tree, parents, message, name, email, when_iso)
            self.refs[ref] = commit
            self._bump()
            return PutResult("created" if created else "updated", commit, blob, new_tree, parents, message)

    def delete_file(
        self,
        branch: str,
        path: str,
        message: str,
        name: str,
        email: str,
        when_iso: str,
        expected_sha: str | None,
    ) -> PutResult:
        with self.lock:
            ref = f"refs/heads/{branch}"
            parent = self.refs.get(ref)
            if parent is None:
                return PutResult("missing")
            tree = self.commit_tree.get(parent)
            parts = [part for part in path.split("/") if part not in ("", ".")]
            current = self.walk(tree, parts)
            if not current or current[0] != "blob":
                return PutResult("missing")
            if not expected_sha or expected_sha != current[2]:
                return PutResult("mismatch")
            new_tree, removed = self.delete_from_tree(tree, parts)
            if not removed:
                return PutResult("missing")
            if new_tree is None:
                new_tree = self.write_tree([])
            commit = self.make_commit(new_tree, [parent], message, name, email, when_iso)
            self.refs[ref] = commit
            self._bump()
            return PutResult("deleted", commit, None, new_tree, [parent], message)

    def merged_tree(self, base_sha: str, head_sha: str) -> str | None:
        """Squash tree of base and head, or None when the two sides conflict."""
        if self.is_ancestor(base_sha, head_sha):
            return self.commit_tree.get(head_sha)
        if self.is_ancestor(head_sha, base_sha):
            return self.commit_tree.get(base_sha)
        base_commit = self.merge_base(base_sha, head_sha)
        if base_commit is None:
            return None
        base_flat = self.flatten(self.commit_tree.get(base_commit))
        ours = self.flatten(self.commit_tree.get(base_sha))
        theirs = self.flatten(self.commit_tree.get(head_sha))
        merged: dict[str, tuple[str, str]] = {}
        for path in set(base_flat) | set(ours) | set(theirs):
            base_entry = base_flat.get(path)
            our_entry = ours.get(path)
            their_entry = theirs.get(path)
            if our_entry == their_entry:
                if our_entry is not None:
                    merged[path] = our_entry
                continue
            if our_entry == base_entry:
                if their_entry is not None:
                    merged[path] = their_entry
                continue
            if their_entry == base_entry:
                if our_entry is not None:
                    merged[path] = our_entry
                continue
            return None
        return self._tree_from_flat(merged)

    def mergeable(self, base_sha: str, head_sha: str) -> bool:
        with self.lock:
            return self.merged_tree(base_sha, head_sha) is not None

    def squash(
        self,
        base_branch: str,
        head_branch: str,
        message: str,
        name: str,
        email: str,
        when_iso: str,
    ) -> str | None:
        with self.lock:
            base_sha = self.branch_sha(base_branch)
            head_sha = self.branch_sha(head_branch)
            if not base_sha or not head_sha:
                return None
            tree = self.merged_tree(base_sha, head_sha)
            if tree is None:
                return None
            commit = self.make_commit(tree, [base_sha], message, name, email, when_iso)
            self.refs[f"refs/heads/{base_branch}"] = commit
            self._bump()
            return commit

    def _tree_from_flat(self, flat: dict[str, tuple[str, str]]) -> str:
        root: dict = {}
        for path, (mode, sha) in flat.items():
            node = root
            parts = path.split("/")
            for name in parts[:-1]:
                node = node.setdefault(name, {})
            node[parts[-1]] = (mode, sha)

        def build(node: dict) -> str:
            entries = []
            for name, value in node.items():
                if isinstance(value, dict):
                    entries.append(("40000", name, build(value)))
                else:
                    entries.append((value[0], name, value[1]))
            return self.write_tree(entries)

        return build(root) if root else self.write_tree([])

    def diff(self, base_sha: str | None, head_sha: str | None) -> list[dict]:
        with self.lock:
            base_tree = self.commit_tree.get(base_sha) if base_sha else None
            head_tree = self.commit_tree.get(head_sha) if head_sha else None
            if base_sha and head_sha:
                ancestor = self.merge_base(base_sha, head_sha)
                if ancestor:
                    base_tree = self.commit_tree.get(ancestor)
            before = self.flatten(base_tree)
            after = self.flatten(head_tree)
        files = []
        objects = self.objects
        for path in sorted(set(before) | set(after)):
            left = before.get(path)
            right = after.get(path)
            if left == right:
                continue
            if left is None:
                status = "added"
                additions, deletions = _line_count(objects[right[1]][1]), 0
            elif right is None:
                status = "removed"
                additions, deletions = 0, _line_count(objects[left[1]][1])
            else:
                status = "modified"
                additions, deletions = _line_diff(objects[left[1]][1], objects[right[1]][1])
            files.append({
                "path": path,
                "status": status,
                "additions": additions,
                "deletions": deletions,
                "sha": None if right is None else right[1],
            })
        return files

    def ahead(self, base_sha: str, head_sha: str) -> int:
        ancestors: set[str] = set()
        stack = [base_sha]
        parents = self.parents
        while stack:
            sha = stack.pop()
            if sha in ancestors:
                continue
            ancestors.add(sha)
            stack.extend(parents.get(sha, ()))
        count = 0
        stack = [head_sha]
        seen: set[str] = set()
        while stack:
            sha = stack.pop()
            if sha in seen or sha in ancestors:
                continue
            seen.add(sha)
            count += 1
            stack.extend(parents.get(sha, ()))
        return count

    def history(self, sha: str | None, limit: int) -> list[str]:
        """First-parent walk, tip first. Used by the commits REST route."""
        out = []
        seen: set[str] = set()
        current = sha
        while current and current not in seen and len(out) < limit:
            seen.add(current)
            if current not in self.objects:
                break
            out.append(current)
            parents = self.parents.get(current, ())
            current = parents[0] if parents else None
        return out

    def advertise_upload(self, version2: bool) -> bytes:
        if version2:
            return b"".join((
                pkt("version 2\n"),
                pkt(f"{AGENT}\n"),
                pkt("ls-refs=unborn\n"),
                pkt("fetch=shallow\n"),
                pkt("object-format=sha1\n"),
                b"0000",
            ))
        with self.lock:
            head = self.refs.get(self.head_target)
            caps = (
                "multi_ack thin-pack side-band side-band-64k ofs-delta shallow "
                "no-progress include-tag multi_ack_detailed no-done "
                f"symref=HEAD:{self.head_target} object-format=sha1 {AGENT}"
            )
            body = [pkt("# service=git-upload-pack\n"), b"0000"]
            if head is None and not self.refs:
                body.append(pkt(f"{ZERO} capabilities^{{}}\0{caps}\n"))
            else:
                first = True
                if head is not None:
                    body.append(pkt(f"{head} HEAD\0{caps}\n"))
                    first = False
                for name in sorted(self.refs):
                    oid = self.refs[name]
                    if first:
                        body.append(pkt(f"{oid} {name}\0{caps}\n"))
                        first = False
                    else:
                        body.append(pkt(f"{oid} {name}\n"))
            body.append(b"0000")
            return b"".join(body)

    def advertise_receive(self) -> bytes:
        with self.lock:
            caps = (
                f"report-status report-status-v2 delete-refs side-band-64k "
                f"ofs-delta quiet object-format=sha1 {AGENT}"
            )
            body = [pkt("# service=git-receive-pack\n"), b"0000"]
            if not self.refs:
                body.append(pkt(f"{ZERO} capabilities^{{}}\0{caps}\n"))
            else:
                first = True
                for name in sorted(self.refs):
                    oid = self.refs[name]
                    if first:
                        body.append(pkt(f"{oid} {name}\0{caps}\n"))
                        first = False
                    else:
                        body.append(pkt(f"{oid} {name}\n"))
            body.append(b"0000")
            return b"".join(body)

    def upload_pack(self, body: bytes, version2: bool) -> bytes:
        if version2:
            return self._upload_v2(body)
        return self._upload_v0(body)

    def _upload_v2(self, body: bytes) -> bytes:
        command, args = _v2_command(body)
        if command == "ls-refs":
            return self._ls_refs(args)
        if command == "fetch":
            return self._fetch_v2(args)
        return pkt(f"ERR unknown command {command}\n") + b"0000"

    def _ls_refs(self, args: list[str]) -> bytes:
        symrefs = "symrefs" in args
        unborn = "unborn" in args
        peel = "peel" in args
        prefixes = [arg[len("ref-prefix ") :] for arg in args if arg.startswith("ref-prefix ")]

        def wanted(name: str) -> bool:
            if not prefixes:
                return True
            return any(name.startswith(prefix) for prefix in prefixes)

        with self.lock:
            lines = []
            target = self.head_target
            head_oid = self.refs.get(target)
            if wanted("HEAD"):
                if head_oid is not None:
                    extra = f" symref-target:{target}" if symrefs else ""
                    lines.append(f"{head_oid} HEAD{extra}\n")
                elif unborn:
                    lines.append(f"unborn HEAD symref-target:{target}\n")
            for name in sorted(self.refs):
                if not wanted(name):
                    continue
                oid = self.refs[name]
                suffix = ""
                if peel:
                    peeled = self._peel(oid)
                    if peeled and peeled != oid:
                        suffix = f" peeled:{peeled}"
                lines.append(f"{oid} {name}{suffix}\n")
        return b"".join(pkt(line) for line in lines) + b"0000"

    def _peel(self, oid: str) -> str | None:
        seen: set[str] = set()
        while oid and oid not in seen:
            seen.add(oid)
            pair = self.objects.get(oid)
            if not pair or pair[0] != "tag":
                return oid
            nxt = None
            for line in pair[1].split(b"\n"):
                if line.startswith(b"object "):
                    nxt = line[7:].decode()
                    break
            if nxt is None:
                return oid
            oid = nxt
        return oid

    def _fetch_v2(self, args: list[str]) -> bytes:
        wants: list[str] = []
        haves: list[str] = []
        depth = None
        include_tag = False
        seen_want: set[str] = set()
        for arg in args:
            if arg.startswith("want "):
                oid = arg.split()[1]
                if oid not in seen_want:
                    seen_want.add(oid)
                    wants.append(oid)
            elif arg.startswith("have "):
                haves.append(arg.split()[1])
            elif arg.startswith("deepen "):
                try:
                    depth = int(arg.split()[1])
                except ValueError:
                    depth = 1
            elif arg == "include-tag":
                include_tag = True
        key = (tuple(wants), tuple(haves), -1 if depth is None else depth, include_tag, 2)
        pack_body = self._cached(key, lambda: self._frame_v2(wants, haves, depth, include_tag))
        return pack_body

    def _frame_v2(self, wants: list[str], haves: list[str], depth: int | None, include_tag: bool) -> bytes:
        pack, shallow = self._collect(wants, haves, depth, include_tag)
        framed = build_pack(pack)
        chunks = []
        if depth is not None and shallow:
            chunks.append(pkt("shallow-info\n"))
            for oid in shallow:
                chunks.append(pkt(f"shallow {oid}\n"))
            chunks.append(b"0001")
        chunks.append(pkt("packfile\n"))
        chunks.append(sideband(framed))
        chunks.append(b"0000")
        return b"".join(chunks)

    def _upload_v0(self, body: bytes) -> bytes:
        wants, haves, depth, done, include_tag = _parse_v0(body)
        if not done:
            _pack, shallow = self._collect(wants, haves, depth, include_tag) if wants else ([], [])
            out = bytearray()
            for oid in shallow:
                out += pkt(f"shallow {oid}\n")
            out += pkt("NAK\n")
            out += b"0000"
            return bytes(out)
        key = (tuple(wants), tuple(haves), -1 if depth is None else depth, include_tag, 0)
        return self._cached(key, lambda: self._frame_v0(wants, haves, depth, include_tag))

    def _frame_v0(self, wants, haves, depth, include_tag) -> bytes:
        pack, shallow = self._collect(wants, haves, depth, include_tag)
        framed = build_pack(pack)
        out = bytearray()
        if depth is not None:
            for oid in shallow:
                out += pkt(f"shallow {oid}\n")
            out += b"0000"
        out += pkt("NAK\n")
        out += sideband(framed)
        out += b"0000"
        return bytes(out)

    def _cached(self, key: tuple, build) -> bytes:
        with self.lock:
            cached = self.resp_cache.get(key)
            if cached is not None:
                return cached
            event = self.inflight.get(key)
            if event is None:
                event = threading.Event()
                self.inflight[key] = event
                leader = True
            else:
                leader = False
        if not leader:
            event.wait()
            with self.lock:
                cached = self.resp_cache.get(key)
            if cached is not None:
                return cached
        try:
            body = build()
            with self.lock:
                # A push clears the cache and the inflight map. Only publish
                # when this build is still the one waiters are blocked on.
                if self.inflight.get(key) is event:
                    self.resp_cache[key] = body
            return body
        finally:
            with self.lock:
                if self.inflight.get(key) is event:
                    self.inflight.pop(key, None)
            event.set()

    def _collect(
        self,
        wants: list[str],
        haves: list[str],
        depth: int | None,
        include_tag: bool,
    ) -> tuple[list[tuple[str, bytes]], list[str]]:
        with self.lock:
            have_set: set[str] = set()
            stack = [oid for oid in haves if oid in self.objects]
            while stack:
                oid = stack.pop()
                if oid in have_set:
                    continue
                have_set.add(oid)
                stack.extend(self.parents.get(oid, ()))
            selected: list[str] = []
            shallow: list[str] = []
            seen: set[str] = set()
            queue: list[tuple[str, int]] = []
            for oid in wants:
                if oid in self.objects and oid not in have_set:
                    queue.append((oid, 1))
            head = 0
            while head < len(queue):
                oid, level = queue[head]
                head += 1
                if oid in seen or oid not in self.objects or oid in have_set:
                    continue
                seen.add(oid)
                if self.objects[oid][0] != "commit":
                    selected.append(oid)
                    continue
                selected.append(oid)
                parents = self.parents.get(oid, ())
                if depth is not None and level >= depth:
                    if any(parent not in have_set for parent in parents):
                        shallow.append(oid)
                    continue
                for parent in parents:
                    if parent not in have_set:
                        queue.append((parent, level + 1))
            objects: list[tuple[str, bytes]] = []
            added: set[str] = set()

            def add_sha(oid: str) -> None:
                if oid in added or oid not in self.objects or oid in have_set and self.objects[oid][0] == "commit":
                    return
                added.add(oid)
                typ, content = self.objects[oid]
                objects.append((typ, content))
                if typ == "commit":
                    tree = self.commit_tree.get(oid)
                    if tree:
                        add_tree(tree)
                elif typ == "tag":
                    peeled = self._peel(oid)
                    if peeled:
                        add_sha(peeled)
                elif typ == "tree":
                    add_tree(oid)

            def add_tree(oid: str) -> None:
                if oid in added or oid not in self.objects:
                    return
                added.add(oid)
                typ, content = self.objects[oid]
                objects.append((typ, content))
                if typ != "tree":
                    return
                for _mode, _name, child in parse_tree(content):
                    add_tree(child)

            for oid in selected:
                add_sha(oid)
            if include_tag:
                for oid in self.refs.values():
                    pair = self.objects.get(oid)
                    if pair and pair[0] == "tag":
                        peeled = self._peel(oid)
                        if peeled in added:
                            add_sha(oid)
            return objects, shallow

    def receive_pack(self, body: bytes) -> bytes:
        commands, caps, pack = _parse_receive(body)
        objects: list[tuple[str, bytes]] = []
        unpack_error = None
        if pack:
            try:
                objects = parse_pack(pack, self.lookup)
            except Exception as exc:
                unpack_error = str(exc) or "unpack failed"
        results: list[tuple[str, str | None]] = []
        if unpack_error is None:
            with self.lock:
                for typ, content in objects:
                    self.add_object(typ, content)
                for old, new, ref in commands:
                    if new != ZERO and new not in self.objects:
                        results.append((ref, "missing necessary objects"))
                        continue
                    current = self.refs.get(ref)
                    if new == ZERO:
                        if current is None or (old != ZERO and current != old):
                            results.append((ref, "failed to lock"))
                            continue
                        self.refs.pop(ref, None)
                        results.append((ref, None))
                        continue
                    if current is None:
                        if old != ZERO:
                            results.append((ref, "failed to lock"))
                            continue
                        self.refs[ref] = new
                        results.append((ref, None))
                        continue
                    if current != old:
                        results.append((ref, "failed to lock"))
                        continue
                    if new != current and not self.is_ancestor(current, new):
                        results.append((ref, "non-fast-forward"))
                        continue
                    self.refs[ref] = new
                    results.append((ref, None))
                self._bump()
        want_report = "report-status" in caps or "report-status-v2" in caps or not caps
        if not want_report:
            return b""
        lines = [f"unpack {unpack_error or 'ok'}\n"]
        if unpack_error:
            for _old, _new, ref in commands:
                lines.append(f"ng {ref} unpacker error\n")
        else:
            for ref, error in results:
                lines.append(f"ng {ref} {error}\n" if error else f"ok {ref}\n")
        report = b"".join(pkt(line) for line in lines) + b"0000"
        if "side-band-64k" in caps or not caps:
            return sideband(report, 1, 65515) + b"0000"
        if "side-band" in caps:
            return sideband(report, 1, 999) + b"0000"
        return report


def _v2_command(body: bytes) -> tuple[str, list[str]]:
    command = ""
    args: list[str] = []
    seen_delim = False
    index = 0
    length = len(body)
    while index + 4 <= length:
        try:
            size = int(body[index : index + 4], 16)
        except ValueError:
            break
        if size == 0:
            break
        if size == 1:
            seen_delim = True
            index += 4
            continue
        if size < 4 or index + size > length:
            break
        line = body[index + 4 : index + size].decode("utf-8", "replace").rstrip("\n")
        index += size
        if not seen_delim:
            if line.startswith("command="):
                command = line.split("=", 1)[1].split()[0]
        else:
            if line:
                args.append(line)
    return command, args


def _parse_v0(body: bytes) -> tuple[list[str], list[str], int | None, bool, bool]:
    wants: list[str] = []
    haves: list[str] = []
    depth = None
    done = False
    include_tag = False
    index = 0
    length = len(body)
    while index + 4 <= length:
        try:
            size = int(body[index : index + 4], 16)
        except ValueError:
            break
        if size == 0:
            index += 4
            continue
        if size < 4 or index + size > length:
            break
        line = body[index + 4 : index + size].decode("utf-8", "replace").strip()
        index += size
        if line.startswith("want "):
            parts = line.split()
            if len(parts) >= 2 and parts[1] not in wants:
                wants.append(parts[1])
            if "include-tag" in parts:
                include_tag = True
        elif line.startswith("have "):
            parts = line.split()
            if len(parts) >= 2:
                haves.append(parts[1])
        elif line.startswith("deepen "):
            try:
                depth = int(line.split()[1])
            except ValueError:
                depth = 1
        elif line == "done":
            done = True
    return wants, haves, depth, done, include_tag


def _parse_receive(body: bytes) -> tuple[list[tuple[str, str, str]], set[str], bytes]:
    commands = []
    caps: set[str] = set()
    index = 0
    length = len(body)
    while index + 4 <= length:
        try:
            size = int(body[index : index + 4], 16)
        except ValueError:
            break
        if size == 0:
            index += 4
            break
        if size < 4 or index + size > length:
            break
        raw = body[index + 4 : index + size]
        index += size
        if raw.endswith(b"\n"):
            raw = raw[:-1]
        if b"\0" in raw:
            raw, cap_bytes = raw.split(b"\0", 1)
            caps.update(token for token in cap_bytes.decode().split() if token)
        parts = raw.decode().split()
        if len(parts) < 3:
            continue
        commands.append((parts[0], parts[1], parts[2]))
    pack = body[index:]
    if pack.startswith(b"0000"):
        pack = pack[4:]
    if not pack.startswith(b"PACK"):
        pack = b""
    return commands, caps, pack


def ensure_git(repo, store) -> GitRepo:
    git = repo.git
    if git is None:
        with repo.lock:
            git = repo.git
            if git is None:
                git = GitRepo(repo.default_branch)
                repo.git = git
                init = repo.auto_init
            else:
                init = False
        if init:
            git.put_file(
                repo.default_branch,
                "README.md",
                f"# {repo.name}\n".encode(),
                "Initial commit",
                store.login,
                f"{store.login}@users.noreply.github.com",
                store.now,
                None,
            )
    git.head_target = f"refs/heads/{repo.default_branch}"
    return git


def parse_git_path(path: str) -> tuple[str, str, str] | None:
    if not path.startswith("/") or path.startswith("/api/"):
        return None
    parts = path.split("/")
    if len(parts) < 3:
        return None
    repo_part = parts[2]
    if not repo_part.endswith(".git"):
        return None
    owner = unquote(parts[1])
    name = unquote(repo_part[: -len(".git")])
    if not owner or not name:
        return None
    rest = "/".join(parts[3:])
    return owner, name, rest


def handle_git(method: str, path: str, raw_query: str, body: bytes, protocol: str) -> tuple | None:
    parsed = parse_git_path(path)
    if parsed is None:
        return None
    from store import get_store

    owner, name, rest = parsed
    store = get_store()
    repo = store.get_repo(owner, name)
    version2 = "version=2" in (protocol or "")
    service = (parse_qs(raw_query).get("service") or [""])[0]
    if repo is None:
        return 404, NOT_FOUND, JSON, ()
    git = ensure_git(repo, store)
    if method == "GET" and rest == "info/refs":
        if service == "git-upload-pack":
            return 200, git.advertise_upload(version2), UPLOAD_AD, NO_CACHE
        if service == "git-receive-pack":
            return 200, git.advertise_receive(), RECEIVE_AD, NO_CACHE
        return 404, NOT_FOUND, JSON, ()
    if method == "POST" and rest == "git-upload-pack":
        return 200, git.upload_pack(body, version2), UPLOAD_RESULT, NO_CACHE
    if method == "POST" and rest == "git-receive-pack":
        return 200, git.receive_pack(body), RECEIVE_RESULT, NO_CACHE
    return 404, NOT_FOUND, JSON, ()


def _line_count(content: bytes) -> int:
    if not content:
        return 0
    if b"\0" in content[:8192]:
        return 0
    return len(content.splitlines())


def _line_diff(before: bytes, after: bytes) -> tuple[int, int]:
    if before == after:
        return 0, 0
    if len(before) > 1_000_000 or len(after) > 1_000_000 or b"\0" in before[:8192] or b"\0" in after[:8192]:
        return 1, 1
    import difflib

    matcher = difflib.SequenceMatcher(a=before.splitlines(), b=after.splitlines(), autojunk=False)
    additions = deletions = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "insert":
            additions += j2 - j1
        elif tag == "delete":
            deletions += i2 - i1
        elif tag == "replace":
            deletions += i2 - i1
            additions += j2 - j1
    return additions, deletions
