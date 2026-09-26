"""Tree-level evidence: root identity, parent links, branch scope and node chains.

N3 of the branch-data contract (``docs/分支数据合同与实施计划.md``, issue #31).
A trajectory keeps its own content identity; placing it in a tree adds exactly
one manifest section, ``tree``. Manifests without that section stay valid and
are never upgraded, so every pre-tree recording remains readable as before.

Layering rule: ``trajectory_id`` covers everything except ``trajectory_id`` and
``tree``. A placement therefore cannot change a node's content identity, and it
can bind that identity into a parent-to-child hash chain without circularity.
Two verification levels exist on purpose: a single bundle checks its own link
and its own root reference, while a packaged tree re-derives the root identity
and every chain from the root down. A lone child cannot prove that the root
digest it records is the genuine tree identity; only full tree validation can.
"""

from __future__ import annotations

import copy
import hashlib
import re
import shutil
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .audit_compare import EvidenceError, canonical, read_json

TREE_SCHEMA = "lvz.evidence-tree.v1"
TREE_FILE = "tree.json"
NODES_DIRECTORY = "nodes"
SCOPE = "root identity plus per-node content ids and the parent-to-child hash chain; not a comparison of node states"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")
_SECTION_KEYS = {"schema", "tree_id", "branch_id", "trunk", "root", "parent", "chain"}
_ROOT_KEYS = {"trajectory_id", "chain_sha256", "sha256"}
_PARENT_KEYS = {"trajectory_id", "chain_sha256", "boundary"}
_NODE_KEYS = {"key", "path", "trajectory_id", "branch_id", "trunk", "parent_key"}


def digest(value, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise EvidenceError(f"{label} must be a lowercase SHA-256 value")
    return value


def digests(value, *, label: str) -> None:
    if not isinstance(value, dict) or not value:
        raise EvidenceError(f"nonempty {label} digests are required")
    for item in value.values():
        if isinstance(item, dict):
            digests(item, label=label)
        else:
            digest(item, f"{label} digest")


def branch_id(value) -> str:
    if not isinstance(value, str) or _BRANCH.fullmatch(value) is None:
        raise EvidenceError("branch_id must be 1-64 characters of [A-Za-z0-9._:-] starting alphanumeric")
    return value


def boundary(value) -> dict:
    """A boundary coordinate: the (epoch, tick, revision) a branch departs from."""
    if not isinstance(value, dict) or set(value) != {"epoch", "tick", "revision"}:
        raise EvidenceError("tree boundary requires exactly epoch, tick and revision")
    if any(type(value[key]) is not int or value[key] < 0 for key in value):
        raise EvidenceError("tree boundary values must be nonnegative integers")
    return dict(value)


def _hash(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def manifest_of(node) -> dict:
    """Accept a Trajectory, a bundle directory, a trajectory.json path or a manifest."""
    if isinstance(node, dict):
        return node
    manifest = getattr(node, "manifest", None)
    if isinstance(manifest, dict):
        return manifest
    path = Path(node)
    return read_json(path / "trajectory.json" if path.is_dir() else path)


def content_identity(manifest) -> str:
    """The trajectory id definition shared with engine_replay."""
    manifest = manifest_of(manifest)
    return _hash({key: value for key, value in manifest.items() if key not in ("trajectory_id", "tree")})


def root_identity(*, game, artifacts, init_recipe, image_sha256=None) -> dict:
    """Root identity: game/asset digests, the initialization recipe, optional image digest.

    The B(0) normalization table enters as ordered ``(field, target)`` pairs:
    ``reason`` is review text, and binding it here would split one logical root
    into two and reject the merge of worlds that declared the same targets with
    different wording.
    """
    from . import b0_normalization

    if not isinstance(game, dict) or not game:
        raise EvidenceError("root identity requires the game identity")
    digests(artifacts, label="root artifact")
    if not isinstance(init_recipe, dict) or not init_recipe:
        raise EvidenceError("root identity requires the initialization recipe")
    if image_sha256 is not None:
        digest(image_sha256, "root image digest")
    return {
        "game": copy.deepcopy(game),
        "artifacts": copy.deepcopy(artifacts),
        "init_recipe": b0_normalization.identity_recipe(init_recipe),
        "image_sha256": image_sha256,
    }


def normalize_identity(value) -> dict:
    if not isinstance(value, dict) or not {"game", "artifacts", "init_recipe"} <= value.keys():
        raise EvidenceError("root identity requires game, artifacts and the initialization recipe")
    if not value.keys() <= {"game", "artifacts", "init_recipe", "image_sha256"}:
        raise EvidenceError("root identity has unexpected fields")
    return root_identity(
        game=value["game"],
        artifacts=value["artifacts"],
        init_recipe=value["init_recipe"],
        image_sha256=value.get("image_sha256"),
    )


def end_boundary(node) -> dict:
    """The last verified boundary of a node; its initial boundary when it has no steps."""
    manifest = manifest_of(node)
    steps = manifest.get("steps")
    if isinstance(steps, list) and steps:
        step = steps[-1]
        version = (
            step["after_version"]
            if step["request"]["method"] == "capture_frame"
            else step["result"]["observation"]["version"]
        )
    else:
        version = manifest.get("initial", {}).get("observation", {}).get("version")
    return boundary(version)


def _section(manifest) -> dict:
    manifest = manifest_of(manifest)
    tree = manifest.get("tree")
    if not isinstance(tree, dict):
        raise EvidenceError("trajectory has no tree placement")
    return tree


def descriptor(node) -> dict:
    """The tree-facing identity of one placed node, usable as a placement parent."""
    manifest = manifest_of(node)
    validate_embedded_tree(manifest)
    tree = _section(manifest)
    return {
        "trajectory_id": digest(manifest.get("trajectory_id"), "trajectory id"),
        "chain_sha256": digest(tree["chain"]["sha256"], "tree chain digest"),
        "branch_id": branch_id(tree["branch_id"]),
        "trunk": tree["trunk"],
        "root": copy.deepcopy(tree["root"]),
    }


@dataclass(frozen=True)
class TreePlacement:
    """Where a trajectory sits in a tree. Build it with the two helpers below."""

    branch_id: str
    trunk: bool
    identity: dict | None = None
    root: dict | None = None
    parent: dict | None = None


def root_placement(name: str, identity: dict) -> TreePlacement:
    """Place a trajectory as the tree root: the trunk node with a real baseline."""
    return TreePlacement(branch_id=branch_id(name), trunk=True, identity=normalize_identity(identity))


def branch_placement(parent, name: str, *, at: dict | None = None, trunk: bool = False) -> TreePlacement:
    """Place a trajectory as a child of a placed node.

    ``at`` is the boundary the child departs from and defaults to the parent's
    last verified boundary. A continuation keeps the parent's branch_id and
    trunk flag; a new branch_id starts a counterfactual branch.
    """
    source = descriptor(parent)
    if type(trunk) is not bool:
        raise EvidenceError("tree trunk flag must be boolean")
    name = branch_id(name)
    if trunk and name != source["branch_id"]:
        raise EvidenceError("a trunk node must continue its parent's branch_id")
    point = boundary(at if at is not None else end_boundary(parent))
    return TreePlacement(
        branch_id=name,
        trunk=trunk,
        root=copy.deepcopy(source["root"]),
        parent={"trajectory_id": source["trajectory_id"], "chain_sha256": source["chain_sha256"], "boundary": point},
    )


def chain_sha256(
    *, trajectory_id: str, branch: str, trunk: bool, parent: dict | None, parent_sha256: str | None, tree_id: str | None
) -> str:
    """The single link formula. Everything it binds is recorded in the section."""
    return _hash(
        {
            "schema": TREE_SCHEMA,
            "trajectory_id": trajectory_id,
            "branch_id": branch,
            "trunk": trunk,
            "parent": parent,
            "parent_sha256": parent_sha256,
            "tree_id": tree_id,
        }
    )


def attach_tree(manifest, placement: TreePlacement) -> dict:
    """Build a manifest's ``tree`` section. Reads nothing from the host."""
    manifest = manifest_of(manifest)
    if "tree" in manifest:
        raise EvidenceError("trajectory already carries a tree placement")
    if not isinstance(placement, TreePlacement):
        raise EvidenceError("tree placement must be a TreePlacement")
    trajectory_id = digest(manifest.get("trajectory_id"), "trajectory id")
    branch = branch_id(placement.branch_id)
    if type(placement.trunk) is not bool:
        raise EvidenceError("tree trunk flag must be boolean")
    if placement.parent is None:
        if placement.root is not None or placement.identity is None:
            raise EvidenceError("a root placement requires the root identity and no parent")
        identity = normalize_identity(placement.identity)
        chain = chain_sha256(
            trajectory_id=trajectory_id, branch=branch, trunk=True, parent=None, parent_sha256=None, tree_id=None
        )
        tree_id = _hash(
            {
                "schema": TREE_SCHEMA,
                "root": {"trajectory_id": trajectory_id, "chain_sha256": chain, "identity": identity},
            }
        )
        return {
            "schema": TREE_SCHEMA,
            "tree_id": tree_id,
            "branch_id": branch,
            "trunk": True,
            "root": {"trajectory_id": trajectory_id, "chain_sha256": chain, "sha256": tree_id, "identity": identity},
            "parent": None,
            "chain": {"parent_sha256": None, "sha256": chain},
        }
    if placement.identity is not None:
        raise EvidenceError("only the tree root may carry the root identity")
    if not isinstance(placement.root, dict) or not placement.root.keys() >= _ROOT_KEYS:
        raise EvidenceError("a child placement requires the root reference")
    if not isinstance(placement.parent, dict) or not placement.parent.keys() >= _PARENT_KEYS:
        raise EvidenceError("a child placement requires the parent reference")
    root = {key: digest(placement.root[key], f"root {key}") for key in _ROOT_KEYS}
    parent = {
        "trajectory_id": digest(placement.parent["trajectory_id"], "parent trajectory id"),
        "chain_sha256": digest(placement.parent["chain_sha256"], "parent chain digest"),
        "boundary": boundary(placement.parent["boundary"]),
    }
    if parent["trajectory_id"] == trajectory_id:
        raise EvidenceError("a node cannot be its own parent")
    chain = chain_sha256(
        trajectory_id=trajectory_id,
        branch=branch,
        trunk=placement.trunk,
        parent=parent,
        parent_sha256=parent["chain_sha256"],
        tree_id=root["sha256"],
    )
    return {
        "schema": TREE_SCHEMA,
        "tree_id": root["sha256"],
        "branch_id": branch,
        "trunk": placement.trunk,
        "root": root,
        "parent": parent,
        "chain": {"parent_sha256": parent["chain_sha256"], "sha256": chain},
    }


def validate_embedded_tree(manifest) -> dict:
    """Verify one bundle's own placement: shape, self link and root reference."""
    manifest = manifest_of(manifest)
    if "tree" not in manifest:
        return {"present": False}
    tree = manifest["tree"]
    if not isinstance(tree, dict) or set(tree) != _SECTION_KEYS:
        raise EvidenceError("tree placement has an unexpected shape")
    if tree["schema"] != TREE_SCHEMA:
        raise EvidenceError("tree placement schema mismatch")
    trajectory_id = digest(manifest.get("trajectory_id"), "trajectory id")
    branch = branch_id(tree["branch_id"])
    trunk = tree["trunk"]
    if type(trunk) is not bool:
        raise EvidenceError("tree placement trunk flag must be boolean")
    root, parent, chain = tree["root"], tree["parent"], tree["chain"]
    if not isinstance(root, dict) or not root.keys() >= _ROOT_KEYS:
        raise EvidenceError("tree root reference is incomplete")
    if not isinstance(chain, dict) or set(chain) != {"parent_sha256", "sha256"}:
        raise EvidenceError("tree chain has an unexpected shape")
    for key in _ROOT_KEYS:
        digest(root[key], f"root {key}")
    if chain["parent_sha256"] is None:
        if parent is not None or trunk is not True:
            raise EvidenceError("the tree root cannot have a parent and must be the trunk")
        if set(root) != _ROOT_KEYS | {"identity"}:
            raise EvidenceError("the tree root must carry the full root identity")
        identity = normalize_identity(root["identity"])
        if root["trajectory_id"] != trajectory_id or root["chain_sha256"] != chain["sha256"]:
            raise EvidenceError("tree root reference does not describe this node")
        expected = _hash(
            {
                "schema": TREE_SCHEMA,
                "root": {"trajectory_id": trajectory_id, "chain_sha256": chain["sha256"], "identity": identity},
            }
        )
        if tree["tree_id"] != expected or root["sha256"] != expected:
            raise EvidenceError("tree identity does not match the recorded root identity")
        rebuilt = chain_sha256(
            trajectory_id=trajectory_id, branch=branch, trunk=True, parent=None, parent_sha256=None, tree_id=None
        )
        is_root = True
    else:
        if not isinstance(parent, dict) or set(parent) != _PARENT_KEYS:
            raise EvidenceError("a child placement requires a complete parent reference")
        if set(root) != _ROOT_KEYS:
            raise EvidenceError("a child root reference must not repeat the root identity")
        record = {
            "trajectory_id": digest(parent["trajectory_id"], "parent trajectory id"),
            "chain_sha256": digest(parent["chain_sha256"], "parent chain digest"),
            "boundary": boundary(parent["boundary"]),
        }
        if chain["parent_sha256"] != record["chain_sha256"]:
            raise EvidenceError("tree chain does not continue the recorded parent")
        if record["trajectory_id"] == trajectory_id:
            raise EvidenceError("a node cannot be its own parent")
        if tree["tree_id"] != root["sha256"]:
            raise EvidenceError("tree identity disagrees with the recorded root reference")
        rebuilt = chain_sha256(
            trajectory_id=trajectory_id,
            branch=branch,
            trunk=trunk,
            parent=record,
            parent_sha256=record["chain_sha256"],
            tree_id=root["sha256"],
        )
        is_root = False
    if chain["sha256"] != rebuilt:
        raise EvidenceError("tree node hash chain is broken")
    initial_parent = manifest.get("initial", {}).get("parent")
    if is_root and isinstance(initial_parent, dict) and initial_parent:
        raise EvidenceError("a tree root cannot be a continuation of another trajectory")
    if not is_root and isinstance(initial_parent, dict) and "trajectory_id" in initial_parent:
        if initial_parent["trajectory_id"] != parent["trajectory_id"]:
            raise EvidenceError("recorded takeover parent disagrees with the tree placement")
    return {
        "present": True,
        "root": is_root,
        "tree_id": tree["tree_id"],
        "branch_id": branch,
        "trunk": trunk,
        "parent": None if parent is None else parent["trajectory_id"],
    }


@dataclass
class EvidenceTree:
    directory: Path
    record: dict

    @property
    def tree_id(self) -> str:
        return self.record["tree_id"]

    @property
    def nodes(self) -> list[dict]:
        return self.record["nodes"]

    @classmethod
    def load(cls, path) -> EvidenceTree:
        directory = Path(path)
        if directory.is_file():
            directory = directory.parent
        return cls(directory, read_json(directory / TREE_FILE))

    def validate(self) -> dict:
        return validate_tree(self.directory, record=self.record)

    def export(self, output) -> Path:
        return export_tree(self.directory, output)


def _index_entries(directory: Path, record) -> list[dict]:
    if not isinstance(record, dict) or record.get("schema") != TREE_SCHEMA:
        raise EvidenceError("evidence tree schema mismatch")
    nodes, root_key = record.get("nodes"), record.get("root")
    if not isinstance(nodes, list) or not nodes:
        raise EvidenceError("evidence tree requires at least one node")
    keys, paths = set(), set()
    for node in nodes:
        if not isinstance(node, dict) or not node.keys() >= _NODE_KEYS:
            raise EvidenceError("evidence tree node entry is incomplete")
        if node["key"] in keys or node["path"] in paths:
            raise EvidenceError("evidence tree node keys and paths must be unique")
        keys.add(node["key"])
        paths.add(node["path"])
        if type(node["trunk"]) is not bool:
            raise EvidenceError("evidence tree node trunk flag must be boolean")
        branch_id(node["branch_id"])
        digest(node["trajectory_id"], "node trajectory id")
        if node["parent_key"] is not None and not isinstance(node["parent_key"], str):
            raise EvidenceError("evidence tree parent key must be a string or null")
        candidate = (directory / node["path"]).resolve()
        if Path(node["path"]).is_absolute() or not candidate.is_relative_to(directory.resolve()):
            raise EvidenceError(f"evidence tree node path escapes the tree: {node['path']}")
        if not candidate.is_dir():
            raise EvidenceError(f"evidence tree node directory is missing: {node['path']}")
    if root_key not in keys:
        raise EvidenceError("evidence tree root key is missing from its nodes")
    for node in nodes:
        if node["parent_key"] is not None and node["parent_key"] not in keys:
            raise EvidenceError(f"evidence tree parent node is missing: {node['parent_key']}")
        if node["key"] == root_key and node["parent_key"] is not None:
            raise EvidenceError("the root node cannot have a parent")
        if node["key"] != root_key and node["parent_key"] is None:
            raise EvidenceError("every non-root node requires a parent")
    return nodes


def _load_nodes(directory: Path, record) -> list[dict]:
    from .trajectory import Trajectory

    resolved = []
    for node in _index_entries(directory, record):
        trajectory = Trajectory.load(directory / node["path"])
        placement = validate_embedded_tree(trajectory.manifest)
        if not placement["present"]:
            raise EvidenceError(f"tree node has no tree placement: {node['key']}")
        if trajectory.manifest["trajectory_id"] != node["trajectory_id"]:
            raise EvidenceError(f"tree node content identity mismatch: {node['key']}")
        if placement["branch_id"] != node["branch_id"] or placement["trunk"] != node["trunk"]:
            raise EvidenceError(f"tree node placement disagrees with the tree index: {node['key']}")
        if placement["root"] != (node["key"] == record["root"]):
            raise EvidenceError(f"only the root node may carry the root identity: {node['key']}")
        resolved.append(
            {
                "key": node["key"],
                "path": node["path"],
                "trajectory_id": node["trajectory_id"],
                "branch_id": node["branch_id"],
                "trunk": node["trunk"],
                "parent_key": node["parent_key"],
                "manifest": trajectory.manifest,
                "section": trajectory.manifest["tree"],
            }
        )
    if len([node for node in resolved if node["section"]["chain"]["parent_sha256"] is None]) != 1:
        raise EvidenceError("evidence tree requires exactly one root node")
    return resolved


def _verify_tree(nodes: list[dict], record) -> dict:
    by_key = {node["key"]: node for node in nodes}
    root = by_key[record["root"]]
    if root["section"]["chain"]["parent_sha256"] is not None:
        raise EvidenceError("the indexed root node is not the tree root")
    tree_id = root["section"]["tree_id"]
    for node in nodes:
        section = node["section"]
        if section["tree_id"] != tree_id:
            raise EvidenceError(f"tree node carries a foreign tree identity: {node['key']}")
        if section["root"]["trajectory_id"] != root["trajectory_id"]:
            raise EvidenceError(f"tree node references a foreign root: {node['key']}")
        if section["root"]["chain_sha256"] != root["section"]["chain"]["sha256"]:
            raise EvidenceError(f"tree node references a foreign root chain: {node['key']}")
        if node["parent_key"] is None:
            if node["key"] != root["key"]:
                raise EvidenceError(f"only the root node may omit its parent: {node['key']}")
            continue
        parent = by_key[node["parent_key"]]
        recorded = section["parent"]
        if recorded["trajectory_id"] != parent["trajectory_id"]:
            raise EvidenceError(f"tree node parent disagrees with the tree index: {node['key']}")
        if recorded["chain_sha256"] != parent["section"]["chain"]["sha256"]:
            raise EvidenceError(f"tree node chain does not continue its actual parent: {node['key']}")
    branches: dict[str, list[dict]] = {}
    for node in nodes:
        branches.setdefault(node["branch_id"], []).append(node)
    for branch, members in branches.items():
        origins = [
            node for node in members if node["parent_key"] is None or by_key[node["parent_key"]]["branch_id"] != branch
        ]
        if len(origins) != 1:
            raise EvidenceError(f"branch_id {branch} is claimed by {len(origins)} separate paths in one tree")
        origin = origins[0]
        for node in members:
            children = [child for child in members if child["parent_key"] == node["key"]]
            if len(children) > 1:
                raise EvidenceError(f"branch_id {branch} forks inside one tree")
            if node is not origin and node["trunk"] != by_key[node["parent_key"]]["trunk"]:
                raise EvidenceError(f"branch_id {branch} changes its trunk flag mid-branch")
        if origin is not root and origin["trunk"]:
            raise EvidenceError(f"branch_id {branch} leaves the trunk but claims the real continuation baseline")
    for node in nodes:
        if not node["trunk"] or node["key"] == root["key"]:
            continue
        parent = by_key[node["parent_key"]]
        if not parent["trunk"] or parent["branch_id"] != node["branch_id"]:
            raise EvidenceError(f"node {node['key']} claims a real continuation baseline off the trunk")
    return {
        "branches": sorted(branches),
        "trunk_nodes": sorted(node["key"] for node in nodes if node["trunk"]),
        "branch_points": sorted(
            [
                {
                    "key": node["key"],
                    "parent": node["parent_key"],
                    "branch_id": node["branch_id"],
                    "boundary": node["section"]["parent"]["boundary"],
                }
                for node in nodes
                if node["parent_key"] is not None
            ],
            key=lambda item: item["key"],
        ),
    }


def validate_tree(path, *, record: dict | None = None) -> dict:
    """Fully re-derive a packaged tree: root identity, chains and branch scope."""
    directory = Path(path)
    record = read_json(directory / TREE_FILE) if record is None else record
    nodes = _load_nodes(directory, record)
    summary = _verify_tree(nodes, record)
    root = next(node for node in nodes if node["key"] == record["root"])
    tree_id = root["section"]["tree_id"]
    if record.get("tree_id") != tree_id:
        raise EvidenceError("tree index identity disagrees with the root node")
    return {
        "schema": TREE_SCHEMA,
        "tree_id": tree_id,
        "root": root["key"],
        "nodes": len(nodes),
        **summary,
        "scope": SCOPE,
    }


def _bundle_name(branch: str, used: set) -> str:
    if branch not in used:
        return branch
    index = 2
    while f"{branch}-{index}" in used:
        index += 1
    return f"{branch}-{index}"


def package_tree(output_directory, root, *, branches: Iterable = ()) -> EvidenceTree:
    """Copy placed bundles into one tree directory and write its index.

    ``root`` and every entry of ``branches`` must already carry their tree
    placement: this function never invents a parent or a branch identity. Each
    branch entry is indexed under its parent, so a multi-node branch is packaged
    by passing the continued node as another entry whose parent is in this tree.
    """
    output = Path(output_directory)
    sources = [root, *branches]
    output.mkdir(parents=True, exist_ok=False)
    (output / NODES_DIRECTORY).mkdir()
    nodes, used, root_section = [], set(), None
    try:
        for source in sources:
            manifest = manifest_of(source)
            placement = validate_embedded_tree(manifest)
            if not placement["present"]:
                raise EvidenceError("every packaged node requires a tree placement")
            key = "root" if placement["root"] else _bundle_name(placement["branch_id"], used)
            if placement["root"]:
                root_section = manifest["tree"]
            used.add(key)
            origin = Path(getattr(source, "directory", source))
            if not origin.is_dir():
                raise EvidenceError(f"trajectory bundle directory is missing: {origin}")
            shutil.copytree(origin, output / NODES_DIRECTORY / key)
            nodes.append(
                {
                    "key": key,
                    "path": f"{NODES_DIRECTORY}/{key}",
                    "trajectory_id": manifest["trajectory_id"],
                    "branch_id": placement["branch_id"],
                    "trunk": placement["trunk"],
                    "parent_key": None,
                }
            )
        for node, source in zip(nodes[1:], sources[1:]):
            parent_id = manifest_of(source)["tree"]["parent"]["trajectory_id"]
            matches = [candidate for candidate in nodes if candidate["trajectory_id"] == parent_id]
            if len(matches) != 1:
                raise EvidenceError(f"packaged node parent is not part of this tree: {node['key']}")
            node["parent_key"] = matches[0]["key"]
        if root_section is None:
            raise EvidenceError("a packaged tree requires exactly one node carrying the root identity")
        record = {"schema": TREE_SCHEMA, "tree_id": root_section["tree_id"], "root": "root", "nodes": nodes}
        (output / TREE_FILE).write_text(canonical(record).decode("utf-8") + "\n", encoding="utf-8", newline="\n")
        tree = EvidenceTree(output, record)
        tree.validate()
        return tree
    except BaseException:
        shutil.rmtree(output, ignore_errors=True)
        raise


def export_tree(path, output) -> Path:
    """Export a validated tree as a deterministic ZIP: fixed metadata, sorted names."""
    tree = EvidenceTree.load(path)
    tree.validate()
    destination = Path(output)
    if destination.exists():
        raise EvidenceError(f"export destination already exists: {destination}")
    if destination.parent and not destination.parent.exists():
        destination.parent.mkdir(parents=True)
    files = sorted(candidate for candidate in tree.directory.rglob("*") if candidate.is_file())
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for candidate in files:
            relative = candidate.relative_to(tree.directory).as_posix()
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o644 << 16
            archive.writestr(info, candidate.read_bytes())
    return destination
