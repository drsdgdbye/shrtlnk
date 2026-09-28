#!/usr/bin/env python3
from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import selectors
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Generator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, cast

import yaml

SCHEMA_VERSION = 2
MAX_FINDINGS = 50
MAX_OUTPUT_BYTES = 256 * 1024
MAX_ARTIFACT_BYTES = 4 * 1024 * 1024
MAX_NATIVE_COMMAND_OUTPUT_BYTES = 4 * 1024 * 1024
MAX_SOURCE_STATUS_ENTRIES = 100
MAX_SOURCE_STATUS_BYTES = 32 * 1024
MAX_SOURCE_SCOPE_PATHS = 10_000
MAX_SOURCE_SCOPE_PATH_BYTES = 1024 * 1024
SOURCE_READ_CHUNK_BYTES = 64 * 1024
PROCESS_TERMINATION_GRACE_SECONDS = 0.25
INDEX_MTIME_SENTINEL = -1.0
SHA256_PATTERN = re.compile(r"(?:sha256:)?[0-9a-f]{64}")
GIT_REVISION_PATTERN = re.compile(r"[0-9a-f]{40}")
PROFILE_NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]*")
INDEX_SUMMARY_PATTERN = re.compile(
    r"Indexed (?P<units>[0-9]+) code units from (?P<files>[0-9]+) files "
    r"\((?P<unchanged>[0-9]+) unchanged, (?P<removed>[0-9]+) removed\)\."
)
REPORT_INDEX_CLUSTER_PATTERN = re.compile(r"\]\((cluster-[0-9]+\.md)\)")
EMPTY_ANALYZE_OUTCOMES = (
    "No similar pairs found.",
    "No similar code units found for configured similarity and rerank thresholds.",
    "No embedded code units found. Run `embed` first.",
)
ACCEPTED_CLASSIFICATIONS = {
    "accepted-existing-debt",
    "intentional-similarity",
    "low-signal",
}


class ContractError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "contract_error",
        evidence: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.evidence = evidence or {}


@dataclass(frozen=True)
class _ProcessResult:
    returncode: int | None
    output: bytes
    output_digest: str
    timed_out: bool
    overflow: bool
    launch_failed: bool


@dataclass(frozen=True)
class _LocalLock:
    repository_root: Path
    local_root: Path
    directory_fd: int
    lock_fd: int
    lock_device: int
    lock_inode: int


_ACTIVE_LOCAL_LOCK: ContextVar[_LocalLock | None] = ContextVar("active_local_lock", default=None)
_ACTIVE_PROFILE: ContextVar[str] = ContextVar("active_profile", default="backend-go")


def _profile_name() -> str:
    return _ACTIVE_PROFILE.get()


def _profile_from_path(path: Path, suffix: str) -> str | None:
    name = path.name
    if not name.endswith(suffix):
        return None
    profile = name[: -len(suffix)]
    return profile if PROFILE_NAME_PATTERN.fullmatch(profile) is not None else None


def _command_profile(args: argparse.Namespace) -> str:
    candidates: set[str] = set()
    path_suffixes = (
        ("main_config", ".yaml"),
        ("loose_config", "-loose.yaml"),
        ("toolchain", "-toolchain.json"),
        ("registry", ".accepted.json"),
    )
    for attribute, suffix in path_suffixes:
        value = getattr(args, attribute, None)
        if not isinstance(value, Path):
            continue
        profile = _profile_from_path(value, suffix)
        if profile is not None:
            candidates.add(profile)
    if len(candidates) > 1:
        raise ContractError(
            "Artifact names refer to different Slopo profiles",
            code="profile_path_mismatch",
        )
    return next(iter(candidates), "backend-go")


def _digest(*parts: str) -> str:
    canonical = "\0".join(parts).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ContractError(f"{label} does not exist: {path}") from error
    except (OSError, json.JSONDecodeError) as error:
        raise ContractError(f"{label} is not readable as JSON: {path}: {error}") from error
    if not isinstance(value, dict):
        raise ContractError(f"{label} must be a JSON object: {path}")
    return value


def _sha256_file(path: Path, label: str) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError as error:
        raise ContractError(f"{label} does not exist: {path}", code="profile_config_missing") from error
    except OSError as error:
        raise ContractError(f"{label} is not readable: {path}: {error}", code="profile_config_unreadable") from error


def _require_sha256(value: Any, label: str, *, prefixed: bool) -> str:
    if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
        raise ContractError(f"{label} must contain a SHA-256 value")
    if prefixed and not value.startswith("sha256:"):
        raise ContractError(f"{label} must start with sha256:")
    if not prefixed and value.startswith("sha256:"):
        raise ContractError(f"{label} must contain only a hexadecimal checksum")
    return value


def _require_git_revision(value: Any, label: str) -> str:
    if not isinstance(value, str) or GIT_REVISION_PATTERN.fullmatch(value) is None:
        raise ContractError(f"{label} must contain a full 40-character Git commit ID")
    return value


def _require_string(value: Any, label: str, *, max_length: int = 500) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{label} must be a non-empty string")
    if value != value.strip() or "\n" in value or "\r" in value:
        raise ContractError(f"{label} must be a single-line string without surrounding whitespace")
    if len(value) > max_length:
        raise ContractError(f"{label} exceeds the maximum length of {max_length} characters")
    return value


def _require_positive_int(value: Any, label: str, *, maximum: int = 3600) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1 or value > maximum:
        raise ContractError(f"{label} must be an integer from 1 to {maximum}")
    return value


def _require_path(value: Any, label: str) -> str:
    path = _require_string(value, label)
    pure_path = PurePosixPath(path)
    if pure_path.is_absolute() or ".." in pure_path.parts or "\\" in path:
        raise ContractError(f"{label} must be a relative POSIX path within the analysis scope")
    return path


def _validated_review_metadata(raw: dict[str, Any], label: str) -> dict[str, str]:
    raw_classification = raw.get("classification")
    if raw_classification not in ACCEPTED_CLASSIFICATIONS:
        allowed = ", ".join(sorted(ACCEPTED_CLASSIFICATIONS))
        raise ContractError(f"{label}.classification is unclassified; allowed values: {allowed}")
    classification = cast(str, raw_classification)
    reason = _require_string(raw.get("reason"), f"{label}.reason")
    if len(reason) < 20 or not any(character.isalpha() for character in reason):
        raise ContractError(f"{label}.reason must contain a substantive human-readable rationale")
    owner = _require_string(raw.get("owner"), f"{label}.owner", max_length=120)
    return {
        "classification": classification,
        "reason": reason,
        "owner": owner,
    }


def _normalize_member(raw: Any, label: str, *, require_fingerprint: bool) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ContractError(f"{label} must be a JSON object")
    path = _require_path(raw.get("path"), f"{label}.path")
    symbol = _require_string(raw.get("symbol"), f"{label}.symbol", max_length=200)
    body_hash = _require_sha256(raw.get("body_hash"), f"{label}.body_hash", prefixed=False)
    ordinal = raw.get("ordinal")
    if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 1:
        raise ContractError(f"{label}.ordinal must be a positive integer")
    fingerprint = _digest(
        "slopo-ratchet-occurrence-v1",
        path,
        symbol,
        body_hash,
        str(ordinal),
    )
    if require_fingerprint:
        stored_fingerprint = _require_sha256(
            raw.get("fingerprint"),
            f"{label}.fingerprint",
            prefixed=True,
        )
        if stored_fingerprint != fingerprint:
            raise ContractError(f"{label}.fingerprint does not match the member")
    return {
        "fingerprint": fingerprint,
        "path": path,
        "symbol": symbol,
        "body_hash": body_hash,
        "ordinal": ordinal,
    }


def _normalize_cluster(
    raw: Any,
    label: str,
    *,
    accepted: bool,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ContractError(f"{label} must be a JSON object")
    kind = _require_string(raw.get("kind"), f"{label}.kind")
    tier = _require_string(raw.get("tier"), f"{label}.tier")
    if kind not in {"exact", "semantic"}:
        raise ContractError(f"{label}.kind must be exact or semantic")
    if kind == "exact" and tier != "exact":
        raise ContractError(f"{label}.tier must be exact for an exact cluster")
    if kind == "semantic" and tier not in {"main", "loose"}:
        raise ContractError(f"{label}.tier must be main or loose for a semantic cluster")
    raw_members = raw.get("members")
    if not isinstance(raw_members, list) or len(raw_members) < 2:
        raise ContractError(f"{label}.members must contain at least two members")
    members = [
        _normalize_member(member, f"{label}.members[{index}]", require_fingerprint=accepted)
        for index, member in enumerate(raw_members)
    ]
    member_fingerprints = [member["fingerprint"] for member in members]
    if len(member_fingerprints) != len(set(member_fingerprints)):
        raise ContractError(f"{label}.members contains a duplicate occurrence fingerprint")
    body_hashes = sorted({member["body_hash"] for member in members})
    if kind == "exact" and len(body_hashes) != 1:
        raise ContractError(f"{label} must contain one body_hash for an exact cluster")
    if kind == "semantic" and len(body_hashes) < 2:
        raise ContractError(f"{label} must contain distinct body_hash values for a semantic cluster")
    fingerprint = _digest("slopo-ratchet-cluster-v1", kind, tier, *body_hashes)
    if accepted:
        stored_fingerprint = _require_sha256(
            raw.get("fingerprint"),
            f"{label}.fingerprint",
            prefixed=True,
        )
        if stored_fingerprint != fingerprint:
            raise ContractError(f"{label}.fingerprint does not match the cluster composition")
        _validated_review_metadata(raw, label)
    return {
        "kind": kind,
        "tier": tier,
        "fingerprint": fingerprint,
        "members": sorted(members, key=lambda member: member["fingerprint"]),
    }


def _validate_cross_profile_review_consistency(
    raw_clusters: list[Any],
    clusters: list[dict[str, Any]],
    label: str,
) -> None:
    reviewed_by_members: dict[tuple[str, tuple[str, ...]], tuple[str, str, str]] = {}
    for index, cluster in enumerate(clusters):
        if cluster["kind"] != "semantic":
            continue
        member_key = (
            cluster["kind"],
            tuple(member["fingerprint"] for member in cluster["members"]),
        )
        classification = cast(str, raw_clusters[index]["classification"])
        owner = cast(str, raw_clusters[index]["owner"])
        previous = reviewed_by_members.get(member_key)
        if previous is not None and previous[0] != classification:
            raise ContractError(
                f"{label}.accepted_clusters[{index}].classification differs from the classification "
                f"of the same member composition in the {previous[2]} profile"
            )
        if previous is not None and previous[1] != owner:
            raise ContractError(
                f"{label}.accepted_clusters[{index}].owner differs from the owner "
                f"of the same member composition in the {previous[2]} profile"
            )
        reviewed_by_members[member_key] = (classification, owner, cluster["tier"])


def _normalize_document(raw: dict[str, Any], *, accepted: bool, label: str) -> dict[str, Any]:
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise ContractError(f"{label}.schema_version must equal {SCHEMA_VERSION}")
    profile_fingerprint = _require_sha256(
        raw.get("profile_fingerprint"),
        f"{label}.profile_fingerprint",
        prefixed=True,
    )
    collection_key = "accepted_clusters" if accepted else "clusters"
    raw_clusters = raw.get(collection_key)
    if not isinstance(raw_clusters, list):
        raise ContractError(f"{label}.{collection_key} must be an array")
    clusters = [
        _normalize_cluster(cluster, f"{label}.{collection_key}[{index}]", accepted=accepted)
        for index, cluster in enumerate(raw_clusters)
    ]
    cluster_keys = [(cluster["kind"], cluster["tier"], cluster["fingerprint"]) for cluster in clusters]
    if len(cluster_keys) != len(set(cluster_keys)):
        raise ContractError(f"{label}.{collection_key} contains a duplicate cluster fingerprint")
    if accepted:
        _validate_cross_profile_review_consistency(raw_clusters, clusters, label)
    result: dict[str, Any] = {"profile_fingerprint": profile_fingerprint, "clusters": clusters}
    if not accepted and raw.get("source_revision") is not None:
        result.update(_validated_source_metadata(raw, label))
    return result


def _validated_source_metadata(raw: dict[str, Any], label: str) -> dict[str, Any]:
    revision = _require_git_revision(raw.get("source_revision"), f"{label}.source_revision")
    dirty = raw.get("source_dirty")
    if not isinstance(dirty, bool):
        raise ContractError(f"{label}.source_dirty must be a boolean")
    status = raw.get("source_status")
    if not isinstance(status, list) or not all(isinstance(item, str) for item in status):
        raise ContractError(f"{label}.source_status must be an array of strings")
    if len(status) > MAX_SOURCE_STATUS_ENTRIES:
        raise ContractError(f"{label}.source_status exceeds the entry limit")
    if sum(len(item.encode("utf-8")) for item in status) > MAX_SOURCE_STATUS_BYTES:
        raise ContractError(f"{label}.source_status exceeds the byte limit")
    status_digest = _require_sha256(
        raw.get("source_status_digest"),
        f"{label}.source_status_digest",
        prefixed=True,
    )
    status_truncated = raw.get("source_status_truncated")
    if not isinstance(status_truncated, bool):
        raise ContractError(f"{label}.source_status_truncated must be a boolean")
    scope_paths = raw.get("source_scope_paths")
    if not isinstance(scope_paths, list) or not all(isinstance(item, str) for item in scope_paths):
        raise ContractError(f"{label}.source_scope_paths must be an array of strings")
    normalized_scope_paths = [
        _require_path(path, f"{label}.source_scope_paths[{index}]") for index, path in enumerate(scope_paths)
    ]
    if normalized_scope_paths != sorted(set(normalized_scope_paths)):
        raise ContractError(f"{label}.source_scope_paths must contain unique paths in stable order")
    if len(normalized_scope_paths) > MAX_SOURCE_SCOPE_PATHS:
        raise ContractError(f"{label}.source_scope_paths exceeds the path limit")
    if sum(len(path.encode("utf-8")) for path in normalized_scope_paths) > MAX_SOURCE_SCOPE_PATH_BYTES:
        raise ContractError(f"{label}.source_scope_paths exceeds the byte limit")
    content_digest = _require_sha256(
        raw.get("source_content_digest"),
        f"{label}.source_content_digest",
        prefixed=True,
    )
    return {
        "source_revision": revision,
        "source_dirty": dirty,
        "source_status": status,
        "source_status_digest": status_digest,
        "source_status_truncated": status_truncated,
        "source_scope_paths": normalized_scope_paths,
        "source_content_digest": content_digest,
    }


def _attach_source_metadata(result: dict[str, Any], document: dict[str, Any]) -> None:
    for key in (
        "source_revision",
        "source_dirty",
        "source_status",
        "source_status_digest",
        "source_status_truncated",
        "source_content_digest",
    ):
        if key in document:
            result[key] = document[key]
    if "source_scope_paths" in document:
        result["source_file_count"] = len(document["source_scope_paths"])


def _required_source_metadata(raw: dict[str, Any], label: str) -> dict[str, Any]:
    if raw.get("source_revision") is None:
        raise ContractError(f"{label}.source_revision is missing")
    return _validated_source_metadata(raw, label)


def _source_identity(metadata: dict[str, Any]) -> tuple[str, str, str]:
    return (
        metadata["source_revision"],
        metadata["source_status_digest"],
        metadata["source_content_digest"],
    )


def _cluster_summary(cluster: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": cluster["kind"],
        "tier": cluster["tier"],
        "fingerprint": cluster["fingerprint"],
        "occurrences": [member["fingerprint"] for member in cluster["members"]],
        "paths": sorted({member["path"] for member in cluster["members"]}),
    }


def _lineage_cluster_summary(cluster: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": cluster["kind"],
        "tier": cluster["tier"],
        "fingerprint": cluster["fingerprint"],
    }


def _member_summary(member: dict[str, Any]) -> dict[str, Any]:
    return {
        "fingerprint": member["fingerprint"],
        "path": member["path"],
        "symbol": member["symbol"],
        "body_hash": member["body_hash"],
    }


def _review_key(cluster: dict[str, Any]) -> str:
    body_hashes = sorted({member["body_hash"] for member in cluster["members"]})
    return _digest("slopo-ratchet-review-v1", cluster["kind"], *body_hashes)


def _candidate_key(cluster: dict[str, Any]) -> str:
    member_fingerprints = sorted(member["fingerprint"] for member in cluster["members"])
    return _digest("slopo-ratchet-candidate-v1", cluster["kind"], *member_fingerprints)


def _member_lineage(
    current: dict[str, Any] | None,
    accepted: dict[str, Any] | None,
) -> dict[str, list[dict[str, Any]]]:
    current_members = {member["fingerprint"]: member for member in current["members"]} if current else {}
    accepted_members = {member["fingerprint"]: member for member in accepted["members"]} if accepted else {}
    unchanged_fingerprints = sorted(current_members.keys() & accepted_members.keys())
    unchanged_members = [_member_summary(current_members[fingerprint]) for fingerprint in unchanged_fingerprints]

    current_by_body: dict[str, list[dict[str, Any]]] = {}
    accepted_by_body: dict[str, list[dict[str, Any]]] = {}
    for fingerprint in sorted(current_members.keys() - accepted_members.keys()):
        member = current_members[fingerprint]
        current_by_body.setdefault(member["body_hash"], []).append(member)
    for fingerprint in sorted(accepted_members.keys() - current_members.keys()):
        member = accepted_members[fingerprint]
        accepted_by_body.setdefault(member["body_hash"], []).append(member)

    moved_members: list[dict[str, Any]] = []
    added_members: list[dict[str, Any]] = []
    removed_members: list[dict[str, Any]] = []
    for body_hash in sorted(current_by_body.keys() | accepted_by_body.keys()):
        current_occurrences = sorted(current_by_body.get(body_hash, []), key=lambda member: member["fingerprint"])
        accepted_occurrences = sorted(
            accepted_by_body.get(body_hash, []),
            key=lambda member: member["fingerprint"],
        )
        moved_count = min(len(current_occurrences), len(accepted_occurrences))
        moved_members.extend(
            {
                "body_hash": body_hash,
                "previous": _member_summary(previous),
                "current": _member_summary(current_member),
            }
            for previous, current_member in zip(
                accepted_occurrences[:moved_count],
                current_occurrences[:moved_count],
                strict=True,
            )
        )
        added_members.extend(_member_summary(member) for member in current_occurrences[moved_count:])
        removed_members.extend(_member_summary(member) for member in accepted_occurrences[moved_count:])

    return {
        "unchanged_members": unchanged_members,
        "moved_members": moved_members,
        "added_members": added_members,
        "removed_members": removed_members,
    }


def _cluster_lineage(
    current: dict[str, Any] | None,
    accepted: dict[str, Any] | None,
) -> dict[str, Any]:
    reference = current or accepted
    if reference is None:
        raise ValueError("Lineage requires a current or accepted cluster")
    current_candidate_key = _candidate_key(current) if current is not None else None
    accepted_candidate_key = _candidate_key(accepted) if accepted is not None else None
    current_review_key = _review_key(current) if current is not None else None
    accepted_review_key = _review_key(accepted) if accepted is not None else None
    return {
        "kind": reference["kind"],
        "candidate_key": current_candidate_key or accepted_candidate_key,
        "previous_candidate_key": accepted_candidate_key,
        "review_key": current_review_key or accepted_review_key,
        "previous_review_key": accepted_review_key,
        "current_cluster": _lineage_cluster_summary(current) if current is not None else None,
        "accepted_cluster": _lineage_cluster_summary(accepted) if accepted is not None else None,
        **_member_lineage(current, accepted),
    }


def _merge_lineages(lineages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for lineage in lineages:
        member_identity = json.dumps(
            {
                key: lineage[key]
                for key in (
                    "candidate_key",
                    "previous_candidate_key",
                    "review_key",
                    "previous_review_key",
                    "unchanged_members",
                    "moved_members",
                    "added_members",
                    "removed_members",
                )
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        merged = grouped.setdefault(
            member_identity,
            {
                "kind": lineage["kind"],
                "candidate_key": lineage["candidate_key"],
                "previous_candidate_key": lineage["previous_candidate_key"],
                "review_key": lineage["review_key"],
                "previous_review_key": lineage["previous_review_key"],
                "current_clusters": {},
                "accepted_clusters": {},
                "unchanged_members": lineage["unchanged_members"],
                "moved_members": lineage["moved_members"],
                "added_members": lineage["added_members"],
                "removed_members": lineage["removed_members"],
            },
        )
        current_cluster = lineage["current_cluster"]
        accepted_cluster = lineage["accepted_cluster"]
        if current_cluster is not None:
            merged["current_clusters"][(current_cluster["tier"], current_cluster["fingerprint"])] = current_cluster
        if accepted_cluster is not None:
            merged["accepted_clusters"][(accepted_cluster["tier"], accepted_cluster["fingerprint"])] = accepted_cluster

    result: list[dict[str, Any]] = []
    for member_identity in sorted(grouped):
        merged = grouped[member_identity]
        result.append(
            {
                **{key: value for key, value in merged.items() if key not in {"current_clusters", "accepted_clusters"}},
                "current_clusters": [merged["current_clusters"][key] for key in sorted(merged["current_clusters"])],
                "accepted_clusters": [merged["accepted_clusters"][key] for key in sorted(merged["accepted_clusters"])],
            }
        )
    return result


@dataclass(frozen=True)
class _ClusterMatches:
    matched: tuple[tuple[dict[str, Any], dict[str, Any]], ...]
    ambiguous: tuple[tuple[dict[str, Any], tuple[dict[str, Any], ...]], ...]
    current_only: tuple[dict[str, Any], ...]
    accepted_only: tuple[dict[str, Any], ...]


def _match_clusters(current: dict[str, Any], accepted: dict[str, Any]) -> _ClusterMatches:
    accepted_by_key = {
        (cluster["kind"], cluster["tier"], cluster["fingerprint"]): cluster for cluster in accepted["clusters"]
    }
    matched_accepted: set[tuple[str, str, str]] = set()
    matched: list[tuple[dict[str, Any], dict[str, Any]]] = []
    unmatched_current: list[dict[str, Any]] = []

    for cluster in current["clusters"]:
        key = (cluster["kind"], cluster["tier"], cluster["fingerprint"])
        baseline_cluster = accepted_by_key.get(key)
        if baseline_cluster is None:
            unmatched_current.append(cluster)
            continue
        matched_accepted.add(key)
        matched.append((cluster, baseline_cluster))

    unmatched_accepted = [cluster for key, cluster in accepted_by_key.items() if key not in matched_accepted]
    claimed_accepted: set[tuple[str, str, str]] = set()
    ambiguous: list[tuple[dict[str, Any], tuple[dict[str, Any], ...]]] = []
    current_only: list[dict[str, Any]] = []

    for cluster in unmatched_current:
        member_fingerprints = {member["fingerprint"] for member in cluster["members"]}
        body_hashes = {member["body_hash"] for member in cluster["members"]}
        overlaps: list[dict[str, Any]] = []
        for candidate in unmatched_accepted:
            candidate_key = (candidate["kind"], candidate["tier"], candidate["fingerprint"])
            if candidate_key in claimed_accepted:
                continue
            if (candidate["kind"], candidate["tier"]) != (cluster["kind"], cluster["tier"]):
                continue
            candidate_members = {member["fingerprint"] for member in candidate["members"]}
            candidate_bodies = {member["body_hash"] for member in candidate["members"]}
            if member_fingerprints & candidate_members or body_hashes & candidate_bodies:
                overlaps.append(candidate)
        if not overlaps:
            current_only.append(cluster)
            continue
        if len(overlaps) > 1:
            ambiguous.append((cluster, tuple(overlaps)))
            for candidate in overlaps:
                claimed_accepted.add((candidate["kind"], candidate["tier"], candidate["fingerprint"]))
            continue
        baseline_cluster = overlaps[0]
        claimed_accepted.add((baseline_cluster["kind"], baseline_cluster["tier"], baseline_cluster["fingerprint"]))
        matched.append((cluster, baseline_cluster))

    accepted_only = tuple(
        cluster
        for cluster in unmatched_accepted
        if (cluster["kind"], cluster["tier"], cluster["fingerprint"]) not in claimed_accepted
    )
    return _ClusterMatches(
        matched=tuple(matched),
        ambiguous=tuple(ambiguous),
        current_only=tuple(current_only),
        accepted_only=accepted_only,
    )


def _compare(current: dict[str, Any], accepted: dict[str, Any]) -> list[dict[str, Any]]:
    if current["profile_fingerprint"] != accepted["profile_fingerprint"]:
        return [
            {
                "code": "stale_profile",
                "current_profile": current["profile_fingerprint"],
                "accepted_profile": accepted["profile_fingerprint"],
            }
        ]

    findings: list[dict[str, Any]] = []
    matches = _match_clusters(current, accepted)
    for cluster, baseline_cluster in matches.matched:
        current_members = {member["fingerprint"] for member in cluster["members"]}
        baseline_members = {member["fingerprint"] for member in baseline_cluster["members"]}
        added = sorted(current_members - baseline_members)
        removed = sorted(baseline_members - current_members)
        if not added and not removed:
            continue
        lineage = _cluster_lineage(cluster, baseline_cluster)
        if cluster["fingerprint"] != baseline_cluster["fingerprint"]:
            findings.append(
                {
                    "code": "new_occurrence",
                    "cluster": _cluster_summary(cluster),
                    "previous_fingerprint": baseline_cluster["fingerprint"],
                    "added_occurrences": added,
                    "removed_occurrences": removed,
                    "lineage": lineage,
                }
            )
            continue
        if added:
            findings.append(
                {
                    "code": "new_occurrence",
                    "cluster": _cluster_summary(cluster),
                    "added_occurrences": added,
                    "lineage": lineage,
                }
            )
        if removed:
            findings.append(
                {
                    "code": "stale_baseline",
                    "cluster": _cluster_summary(baseline_cluster),
                    "removed_occurrences": removed,
                    "lineage": lineage,
                }
            )

    for cluster, overlaps in matches.ambiguous:
        findings.append(
            {
                "code": "ambiguous_cluster_change",
                "cluster": _cluster_summary(cluster),
                "accepted_fingerprints": sorted(candidate["fingerprint"] for candidate in overlaps),
                "lineage_candidates": [_cluster_lineage(cluster, candidate) for candidate in overlaps],
            }
        )

    for cluster in matches.current_only:
        findings.append(
            {
                "code": "new_cluster",
                "cluster": _cluster_summary(cluster),
                "lineage": _cluster_lineage(cluster, None),
            }
        )

    for cluster in matches.accepted_only:
        findings.append(
            {
                "code": "stale_baseline",
                "cluster": _cluster_summary(cluster),
                "lineage": _cluster_lineage(None, cluster),
            }
        )

    return findings


def _review_candidates_from_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for finding in findings:
        lineages = []
        if isinstance(finding.get("lineage"), dict):
            lineages.append(finding["lineage"])
        if isinstance(finding.get("lineage_candidates"), list):
            lineages.extend(finding["lineage_candidates"])
        for lineage in lineages:
            candidate_key = cast(str, lineage["candidate_key"])
            candidate = grouped.setdefault(
                candidate_key,
                {
                    "candidate_key": candidate_key,
                    "review_key": lineage["review_key"],
                    "kind": lineage["kind"],
                    "tiers": set(),
                    "current_fingerprints": set(),
                    "accepted_fingerprints": set(),
                    "finding_codes": set(),
                    "lineages": [],
                },
            )
            current_cluster = lineage["current_cluster"]
            accepted_cluster = lineage["accepted_cluster"]
            if current_cluster is not None:
                candidate["tiers"].add(current_cluster["tier"])
                candidate["current_fingerprints"].add(current_cluster["fingerprint"])
            if accepted_cluster is not None:
                candidate["tiers"].add(accepted_cluster["tier"])
                candidate["accepted_fingerprints"].add(accepted_cluster["fingerprint"])
            candidate["finding_codes"].add(finding["code"])
            candidate["lineages"].append(lineage)

    result: list[dict[str, Any]] = []
    for candidate_key in sorted(grouped):
        candidate = grouped[candidate_key]
        result.append(
            {
                "candidate_key": candidate["candidate_key"],
                "review_key": candidate["review_key"],
                "kind": candidate["kind"],
                "tiers": sorted(candidate["tiers"]),
                "current_fingerprints": sorted(candidate["current_fingerprints"]),
                "accepted_fingerprints": sorted(candidate["accepted_fingerprints"]),
                "finding_codes": sorted(candidate["finding_codes"]),
                "lineages": _merge_lineages(candidate["lineages"]),
            }
        )
    return result


def _bounded_result(status: str, findings: list[dict[str, Any]], profile_fingerprint: str | None) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "profile_fingerprint": profile_fingerprint,
        "finding_count": len(findings),
        "findings_truncated": len(findings) > MAX_FINDINGS,
        "findings": findings[:MAX_FINDINGS],
    }


def _encode_result(result: dict[str, Any]) -> bytes:
    encoded = (json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if len(encoded) > MAX_OUTPUT_BYTES:
        fallback = {
            "schema_version": SCHEMA_VERSION,
            "status": "error",
            "finding_count": result.get("finding_count", 0),
            "findings_truncated": True,
            "findings": [],
            "error": "bounded_evidence_limit_exceeded",
        }
        encoded = (json.dumps(fallback, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
    return encoded


def _write_atomic_at(directory_fd: int, name: str, content: bytes) -> None:
    # Publishing relative to the held directory_fd prevents replacement of the verified `.slopo/local`
    # between path validation and atomic file replacement.
    temporary_name = f".{name}.{secrets.token_hex(12)}"
    descriptor = os.open(
        temporary_name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory_fd,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(
            temporary_name,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary_name, dir_fd=directory_fd)


def _is_locked_local_path(path: Path, active_lock: _LocalLock) -> bool:
    return path.parent.resolve(strict=False) == active_lock.local_root


def _write_atomic(path: Path, content: bytes) -> None:
    active_lock = _ACTIVE_LOCAL_LOCK.get()
    if active_lock is not None and _is_locked_local_path(path, active_lock):
        _write_atomic_at(active_lock.directory_fd, path.name, content)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _append_private_file(path: Path, content: bytes) -> None:
    active_lock = _ACTIVE_LOCAL_LOCK.get()
    if active_lock is not None and _is_locked_local_path(path, active_lock):
        descriptor = os.open(
            path.name,
            os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=active_lock.directory_fd,
        )
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                raise ContractError("The local Slopo log must be a regular file", code="unsafe_output_path")
            with os.fdopen(descriptor, "ab") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        except Exception:
            with suppress(OSError):
                os.close(descriptor)
            raise
        return
    with path.open("ab") as stream:
        stream.write(content)


def _paths_alias(left: Path, right: Path) -> bool:
    if left.resolve(strict=False) == right.resolve(strict=False):
        return True
    try:
        return left.exists() and right.exists() and os.path.samefile(left, right)
    except OSError:
        return False


def _uses_symlink_below_repository(path: Path, repository_root: Path) -> bool:
    root = repository_root.resolve()
    current = path
    for _part in range(len(path.parts) + 1):
        if current.resolve(strict=False) == root:
            return False
        if current.is_symlink():
            return True
        parent = current.parent
        if parent == current:
            return False
        current = parent
    return False


def _versioned_input_paths(repository_root: Path) -> dict[str, Path]:
    if not (repository_root / ".git").exists():
        return {}
    raw = _run_process_bytes(
        ["git", "-C", str(repository_root), "ls-files", "-z"],
        max_output_bytes=MAX_ARTIFACT_BYTES,
    )
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ContractError("Git returned an invalid list of tracked files") from error
    relative_paths = [path for path in decoded.split("\0") if path]
    if len(relative_paths) > MAX_SOURCE_SCOPE_PATHS:
        raise ContractError("The tracked-file list exceeds the pinned limit")
    return {
        f"tracked input {index}": repository_root / _require_path(path, "tracked Git path")
        for index, path in enumerate(relative_paths)
    }


def _canonical_ratchet_inputs(repository_root: Path) -> dict[str, Path]:
    slopo_root = repository_root / ".slopo"
    profile = _profile_name()
    return {
        "pinned toolchain descriptor": slopo_root / f"{profile}-toolchain.json",
        "pinned main profile": slopo_root / f"{profile}.yaml",
        "pinned loose profile": slopo_root / f"{profile}-loose.yaml",
        "native ignore list": slopo_root / f"{profile}.ignore.txt",
    }


def _resolved_local_root(repository_root: Path) -> Path:
    root = repository_root.resolve()
    slopo_root = root / ".slopo"
    if slopo_root.is_symlink() or not slopo_root.is_dir():
        raise ContractError(
            "The .slopo directory must be a regular directory inside the analyzed repository",
            code="unsafe_output_path",
        )
    local_root = slopo_root / "local"
    if local_root.is_symlink() or (local_root.exists() and not local_root.is_dir()):
        raise ContractError(
            "The .slopo/local directory must be a regular directory without symbolic links",
            code="unsafe_output_path",
        )
    return local_root.resolve(strict=False)


def _same_open_path(directory_fd: int, name: str, descriptor: int) -> bool:
    opened = os.fstat(descriptor)
    try:
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return (opened.st_dev, opened.st_ino) == (current.st_dev, current.st_ino)


@contextmanager
def _exclusive_local_lock(repository_root: Path) -> Generator[_LocalLock]:
    root = repository_root.resolve()
    slopo_root = root / ".slopo"
    if slopo_root.is_symlink() or not slopo_root.is_dir():
        raise ContractError(
            "The .slopo directory must be a regular directory inside the analyzed repository",
            code="unsafe_lock_path",
        )
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        slopo_fd = os.open(slopo_root, directory_flags)
    except OSError as error:
        raise ContractError("Could not safely open the .slopo directory", code="unsafe_lock_path") from error
    local_fd: int | None = None
    lock_fd: int | None = None
    token = None
    try:
        with suppress(FileExistsError):
            os.mkdir("local", 0o700, dir_fd=slopo_fd)
        try:
            local_fd = os.open("local", directory_flags, dir_fd=slopo_fd)
        except OSError as error:
            raise ContractError(
                "Could not safely open the .slopo/local directory",
                code="unsafe_lock_path",
            ) from error
        local_stat = os.fstat(local_fd)
        if not stat.S_ISDIR(local_stat.st_mode) or not _same_open_path(slopo_fd, "local", local_fd):
            raise ContractError(
                "The .slopo/local directory was replaced while preparing the lock",
                code="unsafe_lock_path",
            )
        os.fchmod(local_fd, 0o700)
        lock_flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW
        # O_NOFOLLOW preserves a single mutual-exclusion point: a link to another inode must not let a
        # concurrent process acquire an independent lock for the same databases and reports.
        lock_name = f"{_profile_name()}.lock"
        try:
            lock_fd = os.open(lock_name, lock_flags, 0o600, dir_fd=local_fd)
        except OSError as error:
            if error.errno in {errno.ELOOP, errno.EMLINK}:
                raise ContractError(
                    "The .slopo/local lock file was replaced with a symbolic link",
                    code="unsafe_lock_path",
                ) from error
            raise ContractError("Could not safely open the lock file", code="unsafe_lock_path") from error
        lock_stat = os.fstat(lock_fd)
        if (
            not stat.S_ISREG(lock_stat.st_mode)
            or lock_stat.st_nlink != 1
            or not _same_open_path(local_fd, lock_name, lock_fd)
        ):
            raise ContractError(
                "The .slopo/local lock file is not a unique regular file",
                code="unsafe_lock_path",
            )
        os.fchmod(lock_fd, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ContractError(
                "Another process is already modifying local Slopo artifacts",
                code="ratchet_busy",
            ) from error
        active_lock = _LocalLock(
            repository_root=root,
            local_root=(slopo_root / "local").resolve(strict=False),
            directory_fd=local_fd,
            lock_fd=lock_fd,
            lock_device=lock_stat.st_dev,
            lock_inode=lock_stat.st_ino,
        )
        token = _ACTIVE_LOCAL_LOCK.set(active_lock)
        yield active_lock
        if not _same_open_path(local_fd, lock_name, lock_fd):
            raise ContractError(
                "The .slopo/local lock file was replaced during execution",
                code="unsafe_lock_path",
            )
    finally:
        if token is not None:
            _ACTIVE_LOCAL_LOCK.reset(token)
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)
        if local_fd is not None:
            os.close(local_fd)
        os.close(slopo_fd)


def _assert_active_local_root() -> None:
    active_lock = _ACTIVE_LOCAL_LOCK.get()
    if active_lock is None:
        return
    opened = os.fstat(active_lock.directory_fd)
    try:
        current = os.stat(active_lock.local_root, follow_symlinks=False)
    except FileNotFoundError as error:
        raise ContractError(
            "The .slopo/local directory disappeared during execution",
            code="unsafe_lock_path",
        ) from error
    if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
        raise ContractError(
            "The .slopo/local directory was replaced during execution",
            code="unsafe_lock_path",
        )


def _protected_local_artifacts(
    repository_root: Path,
    profile_artifacts: dict[str, Path] | None,
) -> dict[str, Path]:
    local_root = _resolved_local_root(repository_root)
    profile = _profile_name()
    databases = {local_root / f"{profile}.db"}
    reports = {
        local_root / f"{profile}-report",
        local_root / f"{profile}-report-loose",
    }
    if profile_artifacts is not None:
        databases.add(profile_artifacts["database"])
        reports.update({profile_artifacts["main_report"], profile_artifacts["loose_report"]})
    protected = {
        "local manifest": local_root / f"{profile}-current.json",
        "local evidence": local_root / f"{profile}-evidence.json",
        "Slopo log": local_root / f"{profile}-slopo.log",
        "lock file": local_root / f"{profile}.lock",
    }
    for index, database in enumerate(sorted(databases)):
        protected.update(
            {
                f"Slopo DB {index}": database,
                f"Slopo DB journal {index}": database.with_name(f"{database.name}-journal"),
                f"Slopo DB shared memory {index}": database.with_name(f"{database.name}-shm"),
                f"Slopo DB WAL {index}": database.with_name(f"{database.name}-wal"),
                f"index cache marker {index}": database.with_name(f"{database.name}.cache.json"),
            }
        )
    for index, report in enumerate(sorted(reports)):
        protected[f"Slopo report {index}"] = report
    return protected


def _configured_profile_artifacts(repository_root: Path) -> dict[str, Path] | None:
    canonical_inputs = _canonical_ratchet_inputs(repository_root)
    main_config = canonical_inputs["pinned main profile"]
    loose_config = canonical_inputs["pinned loose profile"]
    if not main_config.is_file() or not loose_config.is_file():
        return None
    return _profile_artifacts(repository_root, main_config, loose_config)


def _validated_ratchet_paths(
    repository_root: Path,
    *,
    local_paths: dict[str, tuple[Path, str]],
    protected_paths: dict[str, Path | None],
    allowed_artifact_roles: dict[str, set[str]],
    profile_artifacts: dict[str, Path] | None = None,
    registry_output: Path | None = None,
) -> dict[str, Path]:
    local_root = _resolved_local_root(repository_root)
    validated: dict[str, Path] = {}
    for label, (raw_path, access) in local_paths.items():
        if access not in {"input", "output"}:
            raise ContractError(f"Unknown local path role: {label}")
        expanded = Path(os.path.abspath(raw_path.expanduser()))
        resolved = expanded.resolve(strict=False)
        if resolved.parent != local_root or resolved.name in {"", ".", ".."}:
            raise ContractError(
                f"{label} must be a separate file directly inside .slopo/local",
                code="unsafe_input_path" if access == "input" else "unsafe_output_path",
            )
        if expanded.is_symlink():
            raise ContractError(
                f"{label} must not traverse a symbolic link",
                code="unsafe_input_path" if access == "input" else "unsafe_output_path",
            )
        if _uses_symlink_below_repository(expanded, repository_root):
            raise ContractError(
                f"{label} must not traverse a symbolic directory inside the repository",
                code="unsafe_input_path" if access == "input" else "unsafe_output_path",
            )
        validated[label] = resolved

    protected = {
        **_protected_local_artifacts(repository_root, profile_artifacts),
        **protected_paths,
    }
    local_items = list(validated.items())
    for index, (left_label, left_path) in enumerate(local_items):
        for right_label, right_path in local_items[index + 1 :]:
            if _paths_alias(left_path, right_path):
                raise ContractError(
                    f"{left_label} and {right_label} must be different files",
                    code="output_path_collision",
                )
        allowed_roles = allowed_artifact_roles.get(left_label, set())
        for protected_label, protected_path in protected.items():
            if protected_label in allowed_roles:
                continue
            if protected_path is not None and _paths_alias(left_path, protected_path):
                raise ContractError(
                    f"{left_label} must not match protected path {protected_label}",
                    code="output_path_collision",
                )

    if registry_output is not None:
        expanded_registry = Path(os.path.abspath(registry_output.expanduser()))
        profile = _profile_name()
        canonical_registry = repository_root.resolve() / ".slopo" / f"{profile}.accepted.json"
        resolved_registry = expanded_registry.resolve(strict=False)
        if (
            resolved_registry != canonical_registry
            or expanded_registry.is_symlink()
            or _uses_symlink_below_repository(expanded_registry, repository_root)
        ):
            raise ContractError(
                f"update-baseline may modify only .slopo/{profile}.accepted.json",
                code="unsafe_output_path",
            )
        if expanded_registry.exists():
            registry_stat = expanded_registry.stat(follow_symlinks=False)
            if not stat.S_ISREG(registry_stat.st_mode) or registry_stat.st_nlink != 1:
                raise ContractError(
                    "The versioned registry must be a separate regular file",
                    code="unsafe_output_path",
                )
        if any(_paths_alias(canonical_registry, local_path) for local_path in validated.values()):
            raise ContractError(
                "The versioned registry must not match a local input",
                code="output_path_collision",
            )
        validated["registry"] = canonical_registry
    return validated


def _validate_scan_outputs(
    args: argparse.Namespace,
    *,
    include_registry: bool,
    trust_evidence: bool = True,
) -> Path:
    repository_root = cast(Path, args.toolchain).resolve().parent.parent
    protected_inputs: dict[str, Path | None] = {
        "--toolchain": args.toolchain,
        "--main-config": args.main_config,
        "--loose-config": args.loose_config,
        **_canonical_ratchet_inputs(repository_root),
        **_versioned_input_paths(repository_root),
    }
    artifacts = _profile_artifacts(repository_root, args.main_config, args.loose_config)
    if include_registry:
        protected_inputs["registry"] = args.registry
    validated = _validated_ratchet_paths(
        repository_root,
        local_paths={"manifest": (args.manifest, "output"), "evidence": (args.evidence, "output")},
        protected_paths=protected_inputs,
        allowed_artifact_roles={
            "manifest": {"local manifest"},
            "evidence": {"local evidence"},
        },
        profile_artifacts=artifacts,
    )
    args.manifest = validated["manifest"]
    args.evidence = validated["evidence"]
    if trust_evidence:
        args._trusted_evidence = True
    return repository_root


def _validate_proposal_output(args: argparse.Namespace) -> Path:
    repository_root = cast(Path, args.repository_root).resolve()
    validated = _validated_ratchet_paths(
        repository_root,
        local_paths={
            "manifest": (args.manifest, "input"),
            "proposal output": (args.output, "output"),
        },
        protected_paths={
            "manifest input": args.manifest,
            "registry": args.registry,
            **_canonical_ratchet_inputs(repository_root),
            **_versioned_input_paths(repository_root),
        },
        allowed_artifact_roles={"manifest": {"local manifest", "manifest input"}},
        profile_artifacts=_configured_profile_artifacts(repository_root),
    )
    args.repository_root = repository_root
    args.manifest = validated["manifest"]
    args.output = validated["proposal output"]
    return repository_root


def _validate_update_paths(args: argparse.Namespace) -> Path:
    repository_root = cast(Path, args.repository_root).resolve()
    validated = _validated_ratchet_paths(
        repository_root,
        local_paths={
            "manifest": (args.manifest, "input"),
            "proposal": (args.proposal, "input"),
        },
        protected_paths={
            **_canonical_ratchet_inputs(repository_root),
            **_versioned_input_paths(repository_root),
        },
        allowed_artifact_roles={"manifest": {"local manifest"}},
        profile_artifacts=_configured_profile_artifacts(repository_root),
        registry_output=args.registry,
    )
    args.repository_root = repository_root
    args.manifest = validated["manifest"]
    args.proposal = validated["proposal"]
    args.registry = validated["registry"]
    return repository_root


def _comparison_evidence(
    current: dict[str, Any],
    accepted: dict[str, Any],
    *,
    native_commands: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], bytes, int]:
    findings = _compare(current, accepted)
    review_candidates = _review_candidates_from_findings(findings)
    bounded_findings = [
        {key: value for key, value in finding.items() if key not in {"lineage", "lineage_candidates"}}
        for finding in findings
    ]
    result = _bounded_result(
        "green" if not findings else "blocked",
        bounded_findings,
        current["profile_fingerprint"],
    )
    result["review_candidate_count"] = len(review_candidates)
    result["review_candidates"] = review_candidates
    _attach_source_metadata(result, current)
    if native_commands is not None:
        result["native_commands"] = native_commands
    return result, _encode_result(result), 0 if not findings else 1


def _validate_check_evidence(args: argparse.Namespace) -> None:
    if args.evidence is None:
        return
    evidence = Path(os.path.abspath(args.evidence.expanduser()))
    if evidence.is_symlink():
        raise ContractError(
            "The check evidence file must not be a symbolic link",
            code="unsafe_output_path",
        )
    if _paths_alias(evidence, args.manifest) or _paths_alias(evidence, args.registry):
        raise ContractError(
            "The check evidence file must not match the manifest or registry",
            code="output_path_collision",
        )
    args.evidence = evidence


def _run_check(args: argparse.Namespace) -> int:
    _validate_check_evidence(args)
    current = _normalize_document(_load_json(args.manifest, "manifest"), accepted=False, label="manifest")
    accepted = _normalize_document(_load_json(args.registry, "registry"), accepted=True, label="registry")
    _result, content, return_code = _comparison_evidence(current, accepted)
    if args.evidence is not None:
        _write_atomic(args.evidence, content)
    sys.stdout.buffer.write(content)
    return return_code


def _canonical_registry(raw: dict[str, Any], normalized: dict[str, Any]) -> dict[str, Any]:
    metadata_by_key: dict[tuple[str, str, str], dict[str, str]] = {}
    raw_clusters = raw["accepted_clusters"]
    for raw_cluster in raw_clusters:
        key = (raw_cluster["kind"], raw_cluster["tier"], raw_cluster["fingerprint"])
        metadata_by_key[key] = {
            "classification": raw_cluster["classification"],
            "reason": raw_cluster["reason"],
            "owner": raw_cluster["owner"],
        }
    clusters = []
    for cluster in sorted(
        normalized["clusters"],
        key=lambda item: (item["kind"], item["tier"], item["fingerprint"]),
    ):
        key = (cluster["kind"], cluster["tier"], cluster["fingerprint"])
        clusters.append({**cluster, **metadata_by_key[key]})
    return {
        "schema_version": SCHEMA_VERSION,
        "profile_fingerprint": normalized["profile_fingerprint"],
        **_required_source_metadata(raw, "proposal"),
        "accepted_clusters": clusters,
    }


def _materialize_reviewed_proposal(raw: dict[str, Any]) -> dict[str, Any]:
    raw_review_candidates = raw.get("review_candidates")
    if raw_review_candidates is None:
        return raw
    if not isinstance(raw_review_candidates, list):
        raise ContractError("proposal.review_candidates must be an array")
    raw_clusters = raw.get("accepted_clusters")
    if not isinstance(raw_clusters, list):
        raise ContractError("proposal.accepted_clusters must be an array")

    normalized_clusters: list[dict[str, Any]] = []
    for index, raw_cluster in enumerate(raw_clusters):
        if not isinstance(raw_cluster, dict):
            raise ContractError(f"proposal.accepted_clusters[{index}] must be a JSON object")
        structural_cluster = {
            **raw_cluster,
            "classification": "low-signal",
            "reason": "This internal decision validates only the structure of the local proposal.",
            "owner": "ratchet/structure",
        }
        normalized = _normalize_cluster(
            structural_cluster,
            f"proposal.accepted_clusters[{index}]",
            accepted=True,
        )
        expected_candidate_key = _candidate_key(normalized)
        expected_review_key = _review_key(normalized)
        if raw_cluster.get("candidate_key") != expected_candidate_key:
            raise ContractError(
                f"proposal.accepted_clusters[{index}].candidate_key does not match the member composition"
            )
        if raw_cluster.get("review_key") != expected_review_key:
            raise ContractError(f"proposal.accepted_clusters[{index}].review_key does not match the cluster shape")
        normalized_clusters.append(normalized)

    clusters_by_candidate: dict[str, list[dict[str, Any]]] = {}
    for cluster in normalized_clusters:
        clusters_by_candidate.setdefault(_candidate_key(cluster), []).append(cluster)

    review_metadata: dict[str, dict[str, str]] = {}
    for index, candidate in enumerate(raw_review_candidates):
        label = f"proposal.review_candidates[{index}]"
        if not isinstance(candidate, dict):
            raise ContractError(f"{label} must be a JSON object")
        candidate_key = _require_sha256(candidate.get("candidate_key"), f"{label}.candidate_key", prefixed=True)
        if candidate_key in review_metadata:
            raise ContractError("proposal.review_candidates contains a duplicate candidate_key")
        clusters = clusters_by_candidate.get(candidate_key)
        if clusters is None:
            raise ContractError(f"{label}.candidate_key does not reference a proposal cluster")
        expected_review_keys = {_review_key(cluster) for cluster in clusters}
        if len(expected_review_keys) != 1 or candidate.get("review_key") not in expected_review_keys:
            raise ContractError(f"{label}.review_key does not match the cluster shapes")
        expected_kind = clusters[0]["kind"]
        if candidate.get("kind") != expected_kind:
            raise ContractError(f"{label}.kind does not match the clusters")
        expected_tiers = sorted(cluster["tier"] for cluster in clusters)
        if candidate.get("tiers") != expected_tiers:
            raise ContractError(f"{label}.tiers does not match the tier-specific records")
        expected_fingerprints = sorted(cluster["fingerprint"] for cluster in clusters)
        if candidate.get("cluster_fingerprints") != expected_fingerprints:
            raise ContractError(f"{label}.cluster_fingerprints does not match the clusters")
        review_metadata[candidate_key] = _validated_review_metadata(candidate, label)

    if set(review_metadata) != set(clusters_by_candidate):
        raise ContractError("proposal.review_candidates must cover each member composition exactly once")

    materialized_clusters = []
    for raw_cluster, normalized in zip(raw_clusters, normalized_clusters, strict=True):
        metadata = review_metadata[_candidate_key(normalized)]
        materialized_clusters.append({**raw_cluster, **metadata})
    return {**raw, "accepted_clusters": materialized_clusters}


def _run_update_baseline(args: argparse.Namespace) -> int:
    _validate_update_paths(args)
    if args.proposal.resolve() == args.registry.resolve():
        raise ContractError("The proposal and registry must be different files")
    current_raw = _load_json(args.manifest, "manifest")
    current = _normalize_document(current_raw, accepted=False, label="manifest")
    manifest_source = _required_source_metadata(current_raw, "manifest")
    proposal_raw = _load_json(args.proposal, "proposal")
    materialized_proposal = _materialize_reviewed_proposal(proposal_raw)
    proposal = _normalize_document(materialized_proposal, accepted=True, label="proposal")
    proposal_source = _required_source_metadata(materialized_proposal, "proposal")
    if manifest_source["source_scope_paths"] != proposal_source["source_scope_paths"]:
        raise ContractError(
            "The source scope changed between proposal generation and manual review",
            code="source_identity_changed",
        )
    current_source = _source_metadata(args.repository_root, manifest_source["source_scope_paths"])
    if _source_identity(manifest_source) != _source_identity(proposal_source) or _source_identity(
        manifest_source
    ) != _source_identity(current_source):
        raise ContractError(
            "The source tree changed after proposal generation or manual review",
            code="source_identity_changed",
        )
    pinned_scope_paths, pinned_profile_fingerprint = _pinned_update_scope_paths(args.repository_root)
    if current["profile_fingerprint"] != pinned_profile_fingerprint:
        raise ContractError(
            "The manifest profile differs from the current pinned profile",
            code="profile_config_mismatch",
        )
    if manifest_source["source_scope_paths"] != sorted(pinned_scope_paths):
        raise ContractError(
            "The pinned source scope changed after proposal generation",
            code="source_identity_changed",
        )
    confirmed_profile = _require_sha256(args.confirm_profile, "--confirm-profile", prefixed=True)
    if confirmed_profile != current["profile_fingerprint"]:
        raise ContractError("--confirm-profile does not match the current manifest")
    findings = _compare(current, proposal)
    if findings:
        result = _bounded_result("blocked", findings, current["profile_fingerprint"])
        sys.stdout.buffer.write(_encode_result(result))
        return 1
    canonical = _canonical_registry(materialized_proposal, proposal)
    registry_content = (json.dumps(canonical, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    if len(registry_content) > MAX_ARTIFACT_BYTES:
        raise ContractError("The reviewed registry exceeds the 4 MiB limit")
    _write_atomic(args.registry, registry_content)
    result = _bounded_result("updated", [], current["profile_fingerprint"])
    _attach_source_metadata(result, current_source)
    sys.stdout.buffer.write(_encode_result(result))
    return 0


def _accepted_review_metadata(
    registry_raw: dict[str, Any],
    registry: dict[str, Any],
) -> dict[tuple[str, str, str], dict[str, str]]:
    normalized_by_key = {
        (cluster["kind"], cluster["tier"], cluster["fingerprint"]): cluster for cluster in registry["clusters"]
    }
    result: dict[tuple[str, str, str], dict[str, str]] = {}
    for raw_cluster in registry_raw["accepted_clusters"]:
        key = (raw_cluster["kind"], raw_cluster["tier"], raw_cluster["fingerprint"])
        if key not in normalized_by_key:
            raise ContractError("registry contains an inconsistent tier-specific record")
        result[key] = {
            "classification": raw_cluster["classification"],
            "reason": raw_cluster["reason"],
            "owner": raw_cluster["owner"],
        }
    return result


def _preserved_group_review(reviews: list[dict[str, str]]) -> dict[str, str] | None:
    """Preserve a shared profile decision without re-reviewing historical wording differences."""
    if not reviews:
        return None
    decisions = {(review["classification"], review["owner"]) for review in reviews}
    if len(decisions) != 1:
        return None
    selected_reason = min(
        reviews,
        key=lambda review: (
            0 if review["tier"] == "main" else 1,
            review["tier"],
            review["fingerprint"],
        ),
    )["reason"]
    classification, owner = next(iter(decisions))
    return {
        "classification": classification,
        "reason": selected_reason,
        "owner": owner,
    }


def _proposal_review_projection(
    current: dict[str, Any],
    accepted: dict[str, Any] | None,
    accepted_metadata: dict[tuple[str, str, str], dict[str, str]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if accepted is None or current["profile_fingerprint"] != accepted["profile_fingerprint"]:
        matches = _ClusterMatches(
            matched=(),
            ambiguous=(),
            current_only=tuple(current["clusters"]),
            accepted_only=tuple(accepted["clusters"]) if accepted is not None else (),
        )
    else:
        matches = _match_clusters(current, accepted)
    matched_by_id = {id(cluster): baseline for cluster, baseline in matches.matched}
    ambiguous_by_id = {id(cluster): baselines for cluster, baselines in matches.ambiguous}
    grouped: dict[str, dict[str, Any]] = {}

    for cluster in current["clusters"]:
        candidate_key = _candidate_key(cluster)
        candidate = grouped.setdefault(
            candidate_key,
            {
                "candidate_key": candidate_key,
                "review_key": _review_key(cluster),
                "kind": cluster["kind"],
                "tiers": set(),
                "cluster_fingerprints": set(),
                "lineages": [],
                "previous_reviews": {},
                "preserved_reviews": [],
                "requires_review": False,
            },
        )
        candidate["tiers"].add(cluster["tier"])
        candidate["cluster_fingerprints"].add(cluster["fingerprint"])
        baseline = matched_by_id.get(id(cluster))
        ambiguous_baselines = ambiguous_by_id.get(id(cluster), ())
        lineage_baselines = (baseline,) if baseline is not None else ambiguous_baselines
        if not lineage_baselines:
            candidate["lineages"].append(_cluster_lineage(cluster, None))
            candidate["requires_review"] = True
            continue
        for lineage_baseline in lineage_baselines:
            candidate["lineages"].append(_cluster_lineage(cluster, lineage_baseline))
            lineage_baseline_key = (
                lineage_baseline["kind"],
                lineage_baseline["tier"],
                lineage_baseline["fingerprint"],
            )
            previous_metadata = accepted_metadata.get(lineage_baseline_key)
            if previous_metadata is not None:
                candidate["previous_reviews"][lineage_baseline_key] = {
                    "tier": lineage_baseline["tier"],
                    "fingerprint": lineage_baseline["fingerprint"],
                    **previous_metadata,
                }
        current_members = {member["fingerprint"] for member in cluster["members"]}
        baseline_members = {member["fingerprint"] for member in baseline["members"]} if baseline is not None else set()
        unchanged = (
            baseline is not None
            and cluster["fingerprint"] == baseline["fingerprint"]
            and current_members == baseline_members
        )
        matched_baseline_key = (
            (
                baseline["kind"],
                baseline["tier"],
                baseline["fingerprint"],
            )
            if baseline is not None
            else None
        )
        if unchanged and baseline is not None and matched_baseline_key in accepted_metadata:
            candidate["preserved_reviews"].append(
                {
                    "tier": baseline["tier"],
                    "fingerprint": baseline["fingerprint"],
                    **accepted_metadata[matched_baseline_key],
                }
            )
        else:
            candidate["requires_review"] = True

    review_candidates: list[dict[str, Any]] = []
    for candidate_key in sorted(grouped):
        candidate = grouped[candidate_key]
        preserved = candidate.pop("preserved_reviews")
        requires_review = cast(bool, candidate.pop("requires_review"))
        previous_reviews = candidate.pop("previous_reviews")
        metadata = _preserved_group_review(preserved) if not requires_review else None
        if metadata is None:
            metadata = {"classification": "unclassified", "reason": "", "owner": ""}
        review_candidates.append(
            {
                "candidate_key": candidate["candidate_key"],
                "review_key": candidate["review_key"],
                "kind": candidate["kind"],
                "tiers": sorted(candidate["tiers"]),
                "cluster_fingerprints": sorted(candidate["cluster_fingerprints"]),
                **metadata,
                "previous_reviews": [previous_reviews[key] for key in sorted(previous_reviews)],
                "lineages": _merge_lineages(candidate["lineages"]),
            }
        )

    removed_candidates = _review_candidates_from_findings(
        [
            {
                "code": "stale_baseline",
                "lineage": _cluster_lineage(None, cluster),
            }
            for cluster in matches.accepted_only
        ]
    )
    return review_candidates, removed_candidates


def _run_propose_baseline(args: argparse.Namespace) -> int:
    _validate_proposal_output(args)
    if args.output.exists() and not args.replace:
        raise ContractError(f"The proposal already exists; review it before using --replace: {args.output}")
    if args.registry is not None and args.output.resolve() == args.registry.resolve():
        raise ContractError("The proposal cannot be written directly to the versioned registry")
    current_raw = _load_json(args.manifest, "manifest")
    current = _normalize_document(current_raw, accepted=False, label="manifest")
    source_metadata = _required_source_metadata(current_raw, "manifest")
    accepted_metadata: dict[tuple[str, str, str], dict[str, str]] = {}
    registry: dict[str, Any] | None = None
    if args.registry is not None and args.registry.is_file():
        registry_raw = _load_json(args.registry, "registry")
        registry = _normalize_document(registry_raw, accepted=True, label="registry")
        accepted_metadata = _accepted_review_metadata(registry_raw, registry)

    proposal_clusters: list[dict[str, Any]] = []
    for cluster in sorted(
        current["clusters"],
        key=lambda item: (item["kind"], item["tier"], item["fingerprint"]),
    ):
        proposal_clusters.append(
            {
                **cluster,
                "candidate_key": _candidate_key(cluster),
                "review_key": _review_key(cluster),
            }
        )
    review_candidates, removed_candidates = _proposal_review_projection(current, registry, accepted_metadata)
    unclassified_count = sum(candidate["classification"] == "unclassified" for candidate in review_candidates)
    proposal = {
        "schema_version": SCHEMA_VERSION,
        "profile_fingerprint": current["profile_fingerprint"],
        **source_metadata,
        "accepted_clusters": proposal_clusters,
        "review_candidates": review_candidates,
        "removed_candidates": removed_candidates,
    }
    content = (json.dumps(proposal, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    if len(content) > MAX_ARTIFACT_BYTES:
        raise ContractError("The baseline proposal exceeds the 4 MiB limit")
    _write_atomic(args.output, content)
    result = {
        "schema_version": SCHEMA_VERSION,
        "status": "proposed",
        "profile_fingerprint": current["profile_fingerprint"],
        "cluster_count": len(proposal_clusters),
        "review_candidate_count": len(review_candidates),
        "unclassified_count": unclassified_count,
    }
    _attach_source_metadata(result, source_metadata)
    sys.stdout.buffer.write(_encode_result(result))
    return 0


def _load_slopo_units(
    database: Path,
) -> tuple[list[dict[str, Any]], dict[tuple[str, int, int], dict[str, Any]], set[str]]:
    if not database.is_file():
        raise ContractError(f"Slopo database does not exist: {database}")
    try:
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
            file_rows = connection.execute("SELECT path FROM files ORDER BY path").fetchall()
            rows = connection.execute(
                """
                SELECT f.path, cu.name, cu.start_line, cu.end_line, cu.body_hash
                FROM code_units AS cu
                JOIN files AS f ON f.id = cu.file_id
                ORDER BY f.path, cu.name, cu.body_hash, cu.start_line, cu.end_line, cu.id
                """
            ).fetchall()
    except sqlite3.Error as error:
        raise ContractError(f"Slopo database does not match the expected schema: {database}: {error}") from error
    source_paths = {_require_path(row[0], "Slopo DB file path") for row in file_rows}
    if len(source_paths) != len(file_rows):
        raise ContractError("Slopo database contains duplicate source paths")
    ordinal_by_identity: dict[tuple[str, str, str], int] = {}
    units: list[dict[str, Any]] = []
    by_location: dict[tuple[str, int, int], dict[str, Any]] = {}
    for path_value, symbol_value, start_line, end_line, body_hash_value in rows:
        path = _require_path(path_value, "Slopo DB file path")
        symbol = _require_string(symbol_value, "Slopo DB symbol", max_length=200)
        body_hash = _require_sha256(body_hash_value, "Slopo DB body_hash", prefixed=False)
        identity = (path, symbol, body_hash)
        ordinal = ordinal_by_identity.get(identity, 0) + 1
        ordinal_by_identity[identity] = ordinal
        member = _normalize_member(
            {
                "path": path,
                "symbol": symbol,
                "body_hash": body_hash,
                "ordinal": ordinal,
            },
            "Slopo database occurrence",
            require_fingerprint=False,
        )
        location = (path, start_line, end_line)
        if location in by_location:
            raise ContractError(f"Slopo database contains an ambiguous location: {path}:{start_line}-{end_line}")
        units.append(member)
        by_location[location] = member
    return units, by_location, source_paths


def _exact_clusters(units: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_body_hash: dict[str, list[dict[str, Any]]] = {}
    for unit in units:
        by_body_hash.setdefault(unit["body_hash"], []).append(unit)
    return [
        _normalize_cluster(
            {"kind": "exact", "tier": "exact", "members": members},
            f"exact cluster {body_hash}",
            accepted=False,
        )
        for body_hash, members in sorted(by_body_hash.items())
        if len(members) > 1
    ]


_REPORT_OCCURRENCE_PATTERN = re.compile(r"^- `(?P<path>[^`]+)` lines (?P<start>[0-9]+)-(?P<end>[0-9]+)$", re.MULTILINE)


def _semantic_clusters(
    report_directory: Path,
    tier: str,
    units_by_location: dict[tuple[str, int, int], dict[str, Any]],
) -> list[dict[str, Any]]:
    index_path = report_directory / "index.md"
    if not index_path.is_file():
        raise ContractError(f"The {tier} Slopo report is incomplete: {index_path} is missing")
    clusters: list[dict[str, Any]] = []
    for cluster_path in sorted(report_directory.glob("cluster-*.md")):
        text = cluster_path.read_text(encoding="utf-8")
        members: list[dict[str, Any]] = []
        for match in _REPORT_OCCURRENCE_PATTERN.finditer(text):
            location = (match.group("path"), int(match.group("start")), int(match.group("end")))
            member = units_by_location.get(location)
            if member is None:
                raise ContractError(
                    "The Slopo report references an occurrence missing from the database: "
                    f"{location[0]}:{location[1]}-{location[2]}"
                )
            members.append(member)
        if len(members) < 2:
            raise ContractError(f"The Slopo cluster report does not contain two members: {cluster_path}")
        if len({member["body_hash"] for member in members}) < 2:
            continue
        clusters.append(
            _normalize_cluster(
                {"kind": "semantic", "tier": tier, "members": members},
                f"semantic cluster {cluster_path.name}",
                accepted=False,
            )
        )
    return clusters


def _run_collect_manifest(args: argparse.Namespace) -> int:
    profile_fingerprint = _require_sha256(
        args.profile_fingerprint,
        "--profile-fingerprint",
        prefixed=True,
    )
    units, units_by_location, indexed_paths = _load_slopo_units(args.db)
    clusters = [
        *_exact_clusters(units),
        *_semantic_clusters(args.main_report, "main", units_by_location),
        *_semantic_clusters(args.loose_report, "loose", units_by_location),
    ]
    source_paths = _repository_scope_paths(args.repository_root, args.source_directory, indexed_paths)
    source_metadata = _source_metadata(args.repository_root, source_paths)
    cluster_keys = [(cluster["kind"], cluster["tier"], cluster["fingerprint"]) for cluster in clusters]
    if len(cluster_keys) != len(set(cluster_keys)):
        raise ContractError("Slopo files contain a duplicate cluster fingerprint")
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "profile_fingerprint": profile_fingerprint,
        **source_metadata,
        "clusters": sorted(clusters, key=lambda cluster: (cluster["kind"], cluster["tier"], cluster["fingerprint"])),
    }
    content = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    if len(content) > MAX_ARTIFACT_BYTES:
        raise ContractError("The manifest exceeds the 4 MiB limit")
    _write_atomic(args.output, content)
    result = {
        "schema_version": SCHEMA_VERSION,
        "status": "collected",
        "profile_fingerprint": profile_fingerprint,
        "cluster_count": len(clusters),
        "exact_cluster_count": sum(cluster["kind"] == "exact" for cluster in clusters),
        "semantic_cluster_count": sum(cluster["kind"] == "semantic" for cluster in clusters),
    }
    _attach_source_metadata(result, source_metadata)
    sys.stdout.buffer.write(_encode_result(result))
    return 0


def _toolchain_value(raw: dict[str, Any], section: str, key: str) -> Any:
    section_value = raw.get(section)
    if not isinstance(section_value, dict) or key not in section_value:
        raise ContractError(f"toolchain.{section}.{key} is missing")
    return section_value[key]


def _canonical_ollama_model(value: Any, label: str, *, require_provider: bool) -> str:
    model = _require_string(value, label)
    provider_prefix = "ollama/"
    if require_provider:
        if not model.startswith(provider_prefix):
            raise ContractError(f"{label} must use the ollama provider", code="model_binding_mismatch")
        model = model.removeprefix(provider_prefix)
    elif model.startswith(provider_prefix):
        model = model.removeprefix(provider_prefix)
    if not model or any(character.isspace() for character in model) or model.startswith("/") or model.endswith("/"):
        raise ContractError(f"{label} contains an invalid Ollama model name", code="model_binding_mismatch")
    last_component = model.rsplit("/", 1)[-1]
    if ":" not in last_component:
        model = f"{model}:latest"
    return model.casefold()


def _validated_toolchain(path: Path, main_config: Path, loose_config: Path) -> tuple[dict[str, Any], str]:
    toolchain = _load_json(path, "toolchain")
    if toolchain.get("schema_version") != SCHEMA_VERSION:
        raise ContractError(f"toolchain.schema_version must equal {SCHEMA_VERSION}")
    profile_configs: dict[str, dict[str, Any]] = {}
    for profile_name, config_path in (("main", main_config), ("loose", loose_config)):
        expected_hash = _require_sha256(
            _toolchain_value(toolchain, "profiles", profile_name).get("sha256")
            if isinstance(_toolchain_value(toolchain, "profiles", profile_name), dict)
            else None,
            f"toolchain.profiles.{profile_name}.sha256",
            prefixed=False,
        )
        actual_hash = _sha256_file(config_path, f"Slopo {profile_name} config")
        if actual_hash != expected_hash:
            raise ContractError(
                f"The {profile_name} Slopo configuration differs from its pinned SHA-256",
                code="profile_config_mismatch",
            )
        profile_configs[profile_name] = _load_yaml_config(config_path, f"Slopo {profile_name} config")
    ollama = toolchain.get("ollama")
    if not isinstance(ollama, dict):
        raise ContractError("toolchain.ollama must be a JSON object")
    slopo_model = _require_string(ollama.get("slopo_model"), "toolchain.ollama.slopo_model")
    if any(config.get("embedding_model") != slopo_model for config in profile_configs.values()):
        raise ContractError(
            "toolchain.ollama.slopo_model does not match the Slopo profile embedding_model",
            code="model_profile_mismatch",
        )
    installed_model = _require_string(ollama.get("installed_model"), "toolchain.ollama.installed_model")
    if _canonical_ollama_model(
        slopo_model,
        "toolchain.ollama.slopo_model",
        require_provider=True,
    ) != _canonical_ollama_model(
        installed_model,
        "toolchain.ollama.installed_model",
        require_provider=False,
    ):
        raise ContractError(
            "The Slopo profile model does not match the Ollama model with the pinned checksum",
            code="model_binding_mismatch",
        )
    if _require_string(ollama.get("url_env"), "toolchain.ollama.url_env") != "OLLAMA_API_BASE":
        raise ContractError(
            "toolchain.ollama.url_env must equal OLLAMA_API_BASE",
            code="model_url_env_mismatch",
        )
    runtime = toolchain.get("runtime")
    if not isinstance(runtime, dict):
        raise ContractError("toolchain.runtime must be a JSON object")
    timeouts = runtime.get("native_timeouts_seconds")
    if not isinstance(timeouts, dict) or set(timeouts) != {"show-config", "index", "embed", "analyze"}:
        raise ContractError("toolchain.runtime.native_timeouts_seconds contains an incomplete command set")
    for command, timeout in timeouts.items():
        _require_positive_int(timeout, f"toolchain.runtime.native_timeouts_seconds.{command}")
    _require_positive_int(
        runtime.get("scope_probe_timeout_seconds"),
        "toolchain.runtime.scope_probe_timeout_seconds",
    )
    if runtime.get("native_command_output_max_bytes") != MAX_NATIVE_COMMAND_OUTPUT_BYTES:
        raise ContractError("toolchain.runtime.native_command_output_max_bytes does not match the output limit")
    evidence = toolchain.get("evidence")
    if (
        not isinstance(evidence, dict)
        or evidence.get("max_findings") != MAX_FINDINGS
        or evidence.get("max_bytes") != MAX_OUTPUT_BYTES
    ):
        raise ContractError("toolchain.evidence does not match the bounded-evidence policy")
    canonical_toolchain = json.dumps(toolchain, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    profile_fingerprint = _digest("slopo-ratchet-profile-v1", canonical_toolchain)
    return toolchain, profile_fingerprint


def _native_timeout(toolchain: dict[str, Any], command: str) -> int:
    runtime = toolchain["runtime"]
    timeouts = runtime["native_timeouts_seconds"]
    return _require_positive_int(
        timeouts.get(command),
        f"toolchain.runtime.native_timeouts_seconds.{command}",
    )


def _local_ollama_url(raw_url: str) -> str:
    parsed = urllib.parse.urlparse(raw_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ContractError("Ollama URL must point to a local HTTP address", code="remote_model_forbidden")
    if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        raise ContractError("Ollama URL must contain only a local origin", code="invalid_ollama_url")
    return raw_url.rstrip("/")


def _check_model(toolchain: dict[str, Any], ollama_url_override: str | None) -> tuple[str, str]:
    ollama = toolchain.get("ollama")
    if not isinstance(ollama, dict):
        raise ContractError("toolchain.ollama must be a JSON object")
    url_env = _require_string(ollama.get("url_env"), "toolchain.ollama.url_env")
    default_url = _require_string(ollama.get("default_url"), "toolchain.ollama.default_url")
    ollama_url = _local_ollama_url(ollama_url_override or os.environ.get(url_env) or default_url)
    endpoint = f"{ollama_url}/api/tags"
    try:
        with urllib.request.urlopen(endpoint, timeout=3) as response:
            payload = response.read(MAX_OUTPUT_BYTES + 1)
    except (OSError, urllib.error.URLError) as error:
        raise ContractError(
            f"Ollama is unavailable at the pinned local address: {ollama_url}. "
            f"Start Ollama manually; preflight does not start processes or download models: {error}",
            code="ollama_unavailable",
        ) from error
    if len(payload) > MAX_OUTPUT_BYTES:
        raise ContractError("The Ollama model list exceeds the 256 KiB limit", code="model_inventory_too_large")
    try:
        inventory = json.loads(payload)
    except json.JSONDecodeError as error:
        raise ContractError("Ollama returned an invalid model list", code="invalid_model_inventory") from error
    models = inventory.get("models") if isinstance(inventory, dict) else None
    if not isinstance(models, list):
        raise ContractError("The Ollama model list does not contain a models array", code="invalid_model_inventory")
    installed_model = _require_string(ollama.get("installed_model"), "toolchain.ollama.installed_model")
    canonical_installed_model = _canonical_ollama_model(
        installed_model,
        "toolchain.ollama.installed_model",
        require_provider=False,
    )
    expected_digest = _require_sha256(
        ollama.get("model_digest"),
        "toolchain.ollama.model_digest",
        prefixed=False,
    )
    matching = []
    for model in models:
        if not isinstance(model, dict):
            continue
        try:
            inventory_name = _canonical_ollama_model(
                model.get("name"),
                "Ollama models[].name",
                require_provider=False,
            )
        except ContractError:
            continue
        if inventory_name == canonical_installed_model:
            matching.append(model)
    if not matching:
        raise ContractError(
            f"The pinned Ollama model is not installed: {installed_model}. "
            "Install the approved model in advance; preflight does not run ollama pull",
            code="missing_model",
        )
    actual_digest = matching[0].get("digest")
    if actual_digest != expected_digest:
        raise ContractError(
            f"The Ollama model checksum differs from the pinned value: {installed_model}",
            code="model_digest_mismatch",
        )
    return expected_digest, ollama_url


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    # Terminating the whole process group prevents a child from continuing to run and holding the output
    # pipe after the time or output budget is exhausted.
    with suppress(PermissionError, ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=PROCESS_TERMINATION_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    with suppress(PermissionError, ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    try:
        process.wait(timeout=PROCESS_TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=PROCESS_TERMINATION_GRACE_SECONDS)


def _run_bounded_process(
    arguments: list[str],
    *,
    cwd: Path | None,
    timeout_seconds: int,
    max_output_bytes: int,
    environment: dict[str, str] | None = None,
) -> _ProcessResult:
    try:
        process = subprocess.Popen(
            arguments,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=environment,
            start_new_session=True,
        )
    except OSError:
        return _ProcessResult(None, b"", f"sha256:{hashlib.sha256().hexdigest()}", False, False, True)

    assert process.stdout is not None
    descriptor = process.stdout.fileno()
    os.set_blocking(descriptor, False)
    selector = selectors.DefaultSelector()
    selector.register(descriptor, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout_seconds
    output = bytearray()
    output_size = 0
    hasher = hashlib.sha256()
    timed_out = False
    overflow = False
    pipe_open = True
    try:
        while pipe_open:
            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= 0:
                timed_out = True
                _terminate_process_group(process)
                break
            events = selector.select(min(0.1, remaining_seconds))
            if not events:
                continue
            try:
                chunk = os.read(descriptor, 64 * 1024)
            except BlockingIOError:
                continue
            if not chunk:
                pipe_open = False
                break
            hasher.update(chunk)
            output_size += len(chunk)
            remaining_output = max_output_bytes - len(output)
            if remaining_output > 0:
                output.extend(chunk[:remaining_output])
            if output_size > max_output_bytes:
                overflow = True
                _terminate_process_group(process)
                break
    finally:
        selector.close()
        process.stdout.close()
        if process.poll() is None:
            try:
                process.wait(timeout=PROCESS_TERMINATION_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                _terminate_process_group(process)

    return _ProcessResult(
        process.poll(),
        bytes(output),
        f"sha256:{hasher.hexdigest()}",
        timed_out,
        overflow,
        False,
    )


def _run_process_bytes(
    arguments: list[str],
    *,
    cwd: Path | None = None,
    timeout_seconds: int = 30,
    max_output_bytes: int = MAX_OUTPUT_BYTES,
) -> bytes:
    result = _run_bounded_process(
        arguments,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
    )
    if result.launch_failed:
        raise ContractError(f"Could not run preflight command: {arguments[0]}")
    if result.overflow:
        raise ContractError(f"Preflight command output exceeded the limit: {arguments[0]}")
    if result.timed_out:
        raise ContractError(f"Preflight command exceeded the time limit: {arguments[0]}")
    if result.returncode != 0:
        details = result.output.decode("utf-8", errors="replace").strip()[-2000:]
        raise ContractError(f"Preflight command failed: {arguments[0]}: {details}")
    return result.output


def _run_process(
    arguments: list[str],
    *,
    cwd: Path | None = None,
    timeout_seconds: int = 30,
    max_output_bytes: int = MAX_OUTPUT_BYTES,
) -> str:
    return (
        _run_process_bytes(
            arguments,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
        )
        .decode("utf-8", errors="replace")
        .strip()
    )


def _repository_scope_paths(
    repository_root: Path,
    source_directory: Path,
    source_paths: set[str],
) -> set[str]:
    root = repository_root.resolve()
    source = source_directory.resolve()
    try:
        source.relative_to(root)
    except ValueError as error:
        raise ContractError("The source directory is outside the analyzed repository") from error
    repository_paths: set[str] = set()
    for source_path in source_paths:
        relative_source_path = _require_path(source_path, "Slopo source path")
        candidate = source / relative_source_path
        try:
            relative_repository_path = candidate.relative_to(root).as_posix()
        except ValueError as error:
            raise ContractError("The Slopo source path is outside the analyzed repository") from error
        repository_paths.add(_require_path(relative_repository_path, "repository source path"))
    if len(repository_paths) != len(source_paths):
        raise ContractError("The analysis scope contains ambiguous source paths")
    return repository_paths


def _source_content_digest(repository_root: Path, source_scope_paths: list[str]) -> str:
    root = repository_root.resolve()
    aggregate = hashlib.sha256(b"slopo-ratchet-source-content-v1\0")
    for relative_path in source_scope_paths:
        candidate = root / relative_path
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
        except (FileNotFoundError, OSError, ValueError) as error:
            raise ContractError(
                f"Pinned-scope source file is unavailable: {relative_path}",
                code="source_scope_changed",
            ) from error
        if resolved != candidate:
            raise ContractError(
                f"Pinned-scope source file traverses a symbolic link: {relative_path}",
                code="source_scope_changed",
            )
        file_digest = hashlib.sha256()
        try:
            with candidate.open("rb") as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise ContractError(
                        f"Pinned-scope path is not a regular file: {relative_path}",
                        code="source_scope_changed",
                    )
                while chunk := stream.read(SOURCE_READ_CHUNK_BYTES):
                    file_digest.update(chunk)
                after = os.fstat(stream.fileno())
        except OSError as error:
            raise ContractError(
                f"Pinned-scope source file is not readable: {relative_path}",
                code="source_scope_changed",
            ) from error
        identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if identity_before != identity_after:
            raise ContractError(
                f"Source file changed while its checksum was computed: {relative_path}",
                code="source_changed_during_scan",
            )
        path_bytes = relative_path.encode("utf-8")
        aggregate.update(len(path_bytes).to_bytes(8, "big"))
        aggregate.update(path_bytes)
        aggregate.update(file_digest.digest())
    return f"sha256:{aggregate.hexdigest()}"


def _source_metadata(repository_root: Path, source_scope_paths: set[str] | list[str]) -> dict[str, Any]:
    root = repository_root.resolve()
    if not root.is_dir():
        raise ContractError(f"The analyzed repository root does not exist: {root}")
    actual_root = Path(_run_process(["git", "-C", str(root), "rev-parse", "--show-toplevel"])).resolve()
    if actual_root != root:
        raise ContractError("--repository-root must point to the analyzed Git repository root")
    revision = _require_git_revision(
        _run_process(["git", "-C", str(root), "rev-parse", "HEAD"]),
        "source_revision",
    )
    raw_status = _run_process_bytes(
        ["git", "-C", str(root), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        timeout_seconds=30,
        max_output_bytes=MAX_ARTIFACT_BYTES,
    )
    all_entries = [entry.decode("utf-8", errors="replace") for entry in raw_status.split(b"\0") if entry]
    entries: list[str] = []
    byte_count = 0
    for entry in all_entries:
        entry_bytes = len(entry.encode("utf-8"))
        if len(entries) >= MAX_SOURCE_STATUS_ENTRIES or byte_count + entry_bytes > MAX_SOURCE_STATUS_BYTES:
            break
        entries.append(entry)
        byte_count += entry_bytes
    normalized_scope_paths = sorted({_require_path(path, "pinned-scope path") for path in source_scope_paths})
    if len(normalized_scope_paths) != len(source_scope_paths):
        raise ContractError("The pinned scope contains duplicate paths")
    if len(normalized_scope_paths) > MAX_SOURCE_SCOPE_PATHS:
        raise ContractError("The pinned scope exceeds the path limit")
    if sum(len(path.encode("utf-8")) for path in normalized_scope_paths) > MAX_SOURCE_SCOPE_PATH_BYTES:
        raise ContractError("The pinned scope exceeds the path-byte limit")
    return {
        "source_revision": revision,
        "source_dirty": bool(raw_status),
        "source_status": entries,
        "source_status_digest": f"sha256:{hashlib.sha256(raw_status).hexdigest()}",
        "source_status_truncated": len(entries) != len(all_entries),
        "source_scope_paths": normalized_scope_paths,
        "source_content_digest": _source_content_digest(root, normalized_scope_paths),
    }


def _secure_local_artifacts(local_root: Path) -> None:
    local_root.mkdir(parents=True, exist_ok=True)
    if local_root.is_symlink():
        raise ContractError(".slopo/local must not be a symbolic link")
    for path in [local_root, *local_root.rglob("*")]:
        if path.is_symlink():
            raise ContractError(f"Local Slopo file must not be a symbolic link: {path}")
        if path.is_dir():
            path.chmod(0o700)
        elif path.is_file():
            path.chmod(0o600)


def _check_slopo(
    toolchain: dict[str, Any],
    toolchain_path: Path,
    source_override: Path | None,
) -> tuple[str, str, Path]:
    slopo = toolchain.get("slopo")
    if not isinstance(slopo, dict):
        raise ContractError("toolchain.slopo must be a JSON object")
    source_env = _require_string(slopo.get("source_env"), "toolchain.slopo.source_env")
    repository_root = toolchain_path.resolve().parent.parent
    configured_source = source_override or (
        Path(os.environ[source_env])
        if os.environ.get(source_env)
        else repository_root
        / _require_string(
            slopo.get("default_source_relative_to_repository"),
            "toolchain.slopo.default_source_relative_to_repository",
        )
    )
    source = configured_source.expanduser().resolve()
    if not source.is_dir():
        raise ContractError(
            f"The pinned Slopo checkout does not exist: {source}. Set SLOPO_SOURCE to a prepared "
            "checkout; preflight does not download packages",
            code="missing_slopo_source",
        )
    expected_revision = _require_git_revision(slopo.get("git_revision"), "toolchain.slopo.git_revision")
    actual_revision = _run_process(["git", "-C", str(source), "rev-parse", "HEAD"])
    if actual_revision != expected_revision:
        raise ContractError("Slopo checkout commit differs from the pinned revision", code="slopo_revision_mismatch")
    expected_origin = _require_string(slopo.get("git_origin"), "toolchain.slopo.git_origin")
    actual_origin = _run_process(["git", "-C", str(source), "remote", "get-url", "origin"])
    if actual_origin != expected_origin:
        raise ContractError("Slopo checkout origin differs from the pinned origin", code="slopo_origin_mismatch")
    if _run_process(["git", "-C", str(source), "status", "--porcelain"]):
        raise ContractError("Slopo checkout contains uncommitted changes", code="slopo_source_dirty")
    version_output = _run_process(
        ["uv", "run", "--offline", "--locked", "--project", str(source), "slopo", "--version"]
    )
    expected_version = _require_string(slopo.get("version"), "toolchain.slopo.version")
    first_line = version_output.splitlines()[0] if version_output else ""
    if first_line != f"Slopo {expected_version}":
        raise ContractError("Running Slopo version differs from the pinned version", code="slopo_version_mismatch")
    return expected_version, actual_revision, source


def _preflight(args: argparse.Namespace) -> tuple[dict[str, Any], Path, str, dict[str, Any]]:
    toolchain, profile_fingerprint = _validated_toolchain(args.toolchain, args.main_config, args.loose_config)
    model_digest, ollama_url = _check_model(toolchain, args.ollama_url)
    slopo_version, slopo_revision, source = _check_slopo(toolchain, args.toolchain, args.slopo_source)
    return (
        {
            "schema_version": SCHEMA_VERSION,
            "status": "ready",
            "profile_fingerprint": profile_fingerprint,
            "slopo_version": slopo_version,
            "slopo_revision": slopo_revision,
            "model_digest": model_digest,
        },
        source,
        ollama_url,
        toolchain,
    )


def _run_preflight(args: argparse.Namespace) -> int:
    result, _source, _ollama_url, _toolchain = _preflight(args)
    sys.stdout.buffer.write(_encode_result(result))
    return 0


def _load_yaml_config(path: Path, label: str) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ContractError(f"{label} is not readable as YAML: {path}: {error}") from error
    if not isinstance(value, dict):
        raise ContractError(f"{label} must be a YAML object: {path}")
    return value


def _resolved_repository_path(repository_root: Path, raw_value: Any, label: str) -> Path:
    relative = _require_path(raw_value, label)
    resolved = (repository_root / relative).resolve()
    try:
        resolved.relative_to(repository_root)
    except ValueError as error:
        raise ContractError(f"{label} is outside the repository") from error
    return resolved


def _profile_artifacts(
    repository_root: Path,
    main_config_path: Path,
    loose_config_path: Path,
) -> dict[str, Path]:
    main = _load_yaml_config(main_config_path, "main config")
    loose = _load_yaml_config(loose_config_path, "loose config")
    shared_keys = (
        "source_dir",
        "source_dir_exclude",
        "source_extensions",
        "db_file",
        "ignore_file",
        "embedding_model",
        "embedding_dimensions",
        "body_node_count_threshold",
    )
    for key in shared_keys:
        if main.get(key) != loose.get(key):
            raise ContractError(f"The main and loose Slopo profiles differ on shared field: {key}")
    source_dir = _resolved_repository_path(repository_root, main.get("source_dir"), "source_dir")
    database = _resolved_repository_path(repository_root, main.get("db_file"), "db_file")
    ignore_file = _resolved_repository_path(repository_root, main.get("ignore_file"), "ignore_file")
    main_report = _resolved_repository_path(repository_root, main.get("report_dir"), "main report_dir")
    loose_report = _resolved_repository_path(repository_root, loose.get("report_dir"), "loose report_dir")
    local_root = (repository_root / ".slopo" / "local").resolve()
    for path, label in ((database, "db_file"), (main_report, "main report_dir"), (loose_report, "loose report_dir")):
        try:
            path.relative_to(local_root)
        except ValueError as error:
            raise ContractError(f"{label} must be located inside .slopo/local/") from error
    if not source_dir.is_dir():
        raise ContractError(f"Slopo source_dir does not exist: {source_dir}")
    if ignore_file.is_file():
        active_hashes = [
            line.split("#", 1)[0].strip()
            for line in ignore_file.read_text(encoding="utf-8").splitlines()
            if line.split("#", 1)[0].strip()
        ]
        if active_hashes:
            raise ContractError(
                "The native Slopo ignore list must remain empty so the accepted-similarity registry sees every "
                "cluster",
                code="native_ignore_not_empty",
            )
    return {
        "source_dir": source_dir,
        "database": database,
        "main_report": main_report,
        "loose_report": loose_report,
    }


def _pinned_scope_paths(
    slopo_source: Path,
    repository_root: Path,
    config_path: Path,
    timeout_seconds: int,
) -> set[str]:
    probe = """
import json
import sys
from pathlib import Path

from slopo.config import load_config
from slopo.indexing.scanner import scan_directory

repository_root = Path(sys.argv[1])
config = load_config(Path(sys.argv[2]))
source_dir = config.source_dir if config.source_dir.is_absolute() else repository_root / config.source_dir
paths = sorted(scan_directory(source_dir, config.source_dir_exclude, config.source_extensions))
print(json.dumps(paths, separators=(",", ":")))
"""
    output = _run_process(
        [
            "uv",
            "run",
            "--offline",
            "--locked",
            "--project",
            str(slopo_source),
            "python",
            "-c",
            probe,
            str(repository_root),
            str(config_path),
        ],
        cwd=repository_root,
        timeout_seconds=timeout_seconds,
        max_output_bytes=MAX_ARTIFACT_BYTES,
    )
    try:
        raw_paths = json.loads(output)
    except json.JSONDecodeError as error:
        raise ContractError("The pinned Slopo scanner returned an invalid file list") from error
    if not isinstance(raw_paths, list):
        raise ContractError("The pinned Slopo scanner must return an array of files")
    paths = {_require_path(path, "Slopo scanner path") for path in raw_paths}
    if len(paths) != len(raw_paths):
        raise ContractError("The pinned Slopo scanner returned duplicate paths")
    return paths


def _pinned_repository_scope_paths(
    slopo_source: Path,
    repository_root: Path,
    source_directory: Path,
    config_path: Path,
    timeout_seconds: int,
) -> tuple[set[str], set[str]]:
    source_paths = _pinned_scope_paths(
        slopo_source,
        repository_root,
        config_path,
        timeout_seconds,
    )
    return source_paths, _repository_scope_paths(repository_root, source_directory, source_paths)


def _pinned_update_scope_paths(repository_root: Path) -> tuple[set[str], str]:
    canonical_inputs = _canonical_ratchet_inputs(repository_root)
    toolchain_path = canonical_inputs["pinned toolchain descriptor"]
    main_config = canonical_inputs["pinned main profile"]
    loose_config = canonical_inputs["pinned loose profile"]
    toolchain, profile_fingerprint = _validated_toolchain(toolchain_path, main_config, loose_config)
    _slopo_version, _slopo_revision, slopo_source = _check_slopo(toolchain, toolchain_path, None)
    artifacts = _profile_artifacts(repository_root, main_config, loose_config)
    _source_paths, repository_paths = _pinned_repository_scope_paths(
        slopo_source,
        repository_root,
        artifacts["source_dir"],
        main_config,
        _require_positive_int(
            toolchain["runtime"].get("scope_probe_timeout_seconds"),
            "toolchain.runtime.scope_probe_timeout_seconds",
        ),
    )
    return repository_paths, profile_fingerprint


def _force_full_reparse(database: Path) -> None:
    if not database.exists():
        return
    try:
        with sqlite3.connect(database) as connection:
            connection.execute("UPDATE files SET mtime = ?", (INDEX_MTIME_SENTINEL,))
    except sqlite3.Error as error:
        raise ContractError(
            f"Could not prepare the Slopo database for a complete reparse: {database}",
            code="slopo_index_invalid",
        ) from error


def _remove_locked_local_file(path: Path) -> None:
    active_lock = _ACTIVE_LOCAL_LOCK.get()
    if active_lock is None or not _is_locked_local_path(path, active_lock):
        raise ContractError(
            "The local Slopo cache may be replaced only while holding the shared lock",
            code="unsafe_lock_path",
        )
    _assert_active_local_root()
    try:
        current = os.stat(path.name, dir_fd=active_lock.directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
        raise ContractError(
            "The local Slopo cache must consist of separate regular files",
            code="unsafe_output_path",
        )
    os.unlink(path.name, dir_fd=active_lock.directory_fd)
    os.fsync(active_lock.directory_fd)


def _read_index_cache_marker(path: Path) -> dict[str, Any] | None:
    active_lock = _ACTIVE_LOCAL_LOCK.get()
    if active_lock is None or not _is_locked_local_path(path, active_lock):
        raise ContractError(
            "The Slopo cache marker may be read only while holding the shared lock",
            code="unsafe_lock_path",
        )
    try:
        descriptor = os.open(
            path.name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=active_lock.directory_fd,
        )
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ContractError(
            "The Slopo cache marker cannot be opened safely",
            code="unsafe_output_path",
        ) from error
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or opened.st_size > MAX_OUTPUT_BYTES:
            raise ContractError(
                "The Slopo cache marker must be a separate bounded file",
                code="unsafe_output_path",
            )
        with os.fdopen(descriptor, "rb") as stream:
            raw = stream.read(MAX_OUTPUT_BYTES + 1)
        descriptor = -1
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _prepare_index_cache(
    database: Path,
    marker: Path,
    *,
    profile_fingerprint: str,
    slopo_revision: str,
) -> None:
    expected = {
        "schema_version": SCHEMA_VERSION,
        "profile_fingerprint": _require_sha256(
            profile_fingerprint,
            "Slopo cache profile_fingerprint",
            prefixed=True,
        ),
        "slopo_revision": _require_git_revision(
            slopo_revision,
            "Slopo cache slopo_revision",
        ),
    }
    cached = _read_index_cache_marker(marker)
    if cached == expected and database.is_file() and not database.is_symlink():
        return

    # A parser or profile change may alter the CodeUnit set while mtime and body_hash stay unchanged.
    # Replacing the database cold prevents results from two indexing contracts from being mixed.
    for suffix in ("", "-wal", "-shm", "-journal"):
        _remove_locked_local_file(database.with_name(f"{database.name}{suffix}"))
    _remove_locked_local_file(marker)


def _write_index_cache_marker(
    marker: Path,
    *,
    profile_fingerprint: str,
    slopo_revision: str,
) -> None:
    content = {
        "schema_version": SCHEMA_VERSION,
        "profile_fingerprint": _require_sha256(
            profile_fingerprint,
            "Slopo cache profile_fingerprint",
            prefixed=True,
        ),
        "slopo_revision": _require_git_revision(
            slopo_revision,
            "Slopo cache slopo_revision",
        ),
    }
    _write_atomic(
        marker,
        (json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode(),
    )


def _verify_index_completeness(
    database: Path,
    source_directory: Path,
    expected_paths: set[str],
    index_output: str,
) -> None:
    if "Skipping " in index_output:
        raise ContractError(
            "The pinned Slopo parser skipped a source file; details are available only in the local log",
            code="slopo_parser_skipped",
        )
    summary = INDEX_SUMMARY_PATTERN.search(index_output)
    if summary is None:
        raise ContractError(
            "Slopo index did not confirm a completed result",
            code="slopo_index_incomplete",
        )
    indexed_files = int(summary.group("files"))
    unchanged_files = int(summary.group("unchanged"))
    if unchanged_files != 0 or indexed_files != len(expected_paths):
        raise ContractError(
            "Slopo index did not perform a complete reparse of the analysis scope",
            code="slopo_index_incomplete",
        )
    try:
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
            rows = connection.execute("SELECT path, mtime FROM files ORDER BY path").fetchall()
    except sqlite3.Error as error:
        raise ContractError(
            f"The Slopo database does not contain a verifiable file index: {database}",
            code="slopo_index_invalid",
        ) from error
    indexed_mtimes = {_require_path(path, "Slopo DB file path"): mtime for path, mtime in rows}
    if len(indexed_mtimes) != len(rows):
        raise ContractError(
            "The Slopo index contains duplicate file paths",
            code="slopo_index_invalid",
        )
    indexed_paths = set(indexed_mtimes)
    missing = expected_paths - indexed_paths
    extra = indexed_paths - expected_paths
    if missing and not extra:
        raise ContractError(
            f"The Slopo index does not contain every analysis-scope file: missing_count={len(missing)}",
            code="slopo_index_incomplete",
        )
    if missing or extra:
        raise ContractError(
            f"The Slopo index differs from the analysis scope: missing_count={len(missing)}, extra_count={len(extra)}",
            code="slopo_index_scope_mismatch",
        )
    stale_count = sum(indexed_mtimes[path] != (source_directory / path).stat().st_mtime for path in expected_paths)
    if stale_count:
        raise ContractError(
            f"The Slopo index contains stale entries: stale_count={stale_count}",
            code="slopo_index_stale",
        )


def _reset_report_directory(report_directory: Path) -> None:
    report_directory.mkdir(parents=True, exist_ok=True)
    for cluster_path in report_directory.glob("cluster-*.md"):
        if cluster_path.is_file():
            cluster_path.unlink()
    index_path = report_directory / "index.md"
    index_path.unlink(missing_ok=True)


def _finalize_analyze_report(report_directory: Path, command_output: str) -> None:
    index_path = report_directory / "index.md"
    cluster_paths = list(report_directory.glob("cluster-*.md"))
    report_outcome_count = command_output.count("Report written to ")
    empty_outcome_count = sum(command_output.count(outcome) for outcome in EMPTY_ANALYZE_OUTCOMES)
    if report_outcome_count + empty_outcome_count != 1:
        raise ContractError(
            "Slopo analyze returned an ambiguous or incomplete outcome",
            code="slopo_analyze_incomplete",
        )
    if report_outcome_count == 1:
        if not index_path.is_file() or not cluster_paths or any(not path.is_file() for path in cluster_paths):
            raise ContractError(
                "Slopo analyze reported output but did not finish writing it",
                code="slopo_analyze_incomplete",
            )
        try:
            index_text = index_path.read_text(encoding="utf-8")
        except OSError as error:
            raise ContractError(
                "The Slopo report index is unavailable for validation",
                code="slopo_analyze_incomplete",
            ) from error
        referenced_names = REPORT_INDEX_CLUSTER_PATTERN.findall(index_text)
        actual_names = {path.name for path in cluster_paths}
        if (
            not referenced_names
            or len(referenced_names) != len(set(referenced_names))
            or set(referenced_names) != actual_names
        ):
            raise ContractError(
                "The Slopo report index differs from the cluster-file set",
                code="slopo_analyze_incomplete",
            )
        return
    if empty_outcome_count == 1:
        if index_path.exists() or cluster_paths:
            raise ContractError(
                "An empty Slopo analyze result contains inconsistent report files",
                code="slopo_analyze_incomplete",
            )
        # An empty index is created only after an explicit successful Slopo outcome, so it cannot mask an
        # incomplete analyze run.
        index_path.write_text("# Slopo report\n\nNo similar pairs found.\n", encoding="utf-8")
        return


def _run_native_slopo(
    slopo_source: Path,
    repository_root: Path,
    config_path: Path,
    command: str,
    stage: str,
    log_path: Path,
    timeout_seconds: int,
    ollama_url: str,
) -> tuple[dict[str, Any], str]:
    _assert_active_local_root()
    arguments = [
        "uv",
        "run",
        "--offline",
        "--locked",
        "--project",
        str(slopo_source),
        "slopo",
        "--config",
        str(config_path),
        command,
    ]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment["OLLAMA_API_BASE"] = ollama_url
    result = _run_bounded_process(
        arguments,
        cwd=repository_root,
        timeout_seconds=timeout_seconds,
        max_output_bytes=MAX_NATIVE_COMMAND_OUTPUT_BYTES,
        environment=environment,
    )
    _assert_active_local_root()
    try:
        relative_log_path = log_path.resolve().relative_to(repository_root.resolve()).as_posix()
    except ValueError:
        relative_log_path = str(log_path.resolve())
    evidence = {
        "command": stage,
        "exit_code": result.returncode,
        "output_digest": result.output_digest,
        "log_path": relative_log_path,
    }
    _append_private_file(log_path, f"\n[{stage}]\n".encode() + result.output)
    if result.overflow:
        raise ContractError(
            f"Slopo {stage} output exceeded the pinned limit; details are restricted to the local log",
            code="slopo_output_too_large",
            evidence=evidence,
        )
    if result.timed_out:
        raise ContractError(
            f"Slopo {stage} exceeded the pinned time limit of {timeout_seconds} seconds",
            code="slopo_execution_timeout",
            evidence=evidence,
        )
    if result.launch_failed:
        raise ContractError(
            f"Could not launch the native Slopo {stage} command",
            code="slopo_execution_failed",
            evidence=evidence,
        )
    if result.returncode != 0:
        raise ContractError(
            f"The native Slopo {stage} command failed; details are available only in the local log",
            code="slopo_execution_failed",
            evidence=evidence,
        )
    return evidence, result.output.decode("utf-8", errors="replace")


def _scan_current(args: argparse.Namespace) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    repository_root = args.toolchain.resolve().parent.parent
    local_root = repository_root / ".slopo" / "local"
    _secure_local_artifacts(local_root)
    preflight, slopo_source, ollama_url, toolchain = _preflight(args)
    artifacts = _profile_artifacts(repository_root, args.main_config, args.loose_config)
    expected_paths, repository_scope_paths = _pinned_repository_scope_paths(
        slopo_source,
        repository_root,
        artifacts["source_dir"],
        args.main_config,
        _require_positive_int(
            toolchain["runtime"].get("scope_probe_timeout_seconds"),
            "toolchain.runtime.scope_probe_timeout_seconds",
        ),
    )
    source_before = _source_metadata(repository_root, repository_scope_paths)
    log_path = repository_root / ".slopo" / "local" / f"{_profile_name()}-slopo.log"
    cache_marker = artifacts["database"].with_name(f"{artifacts['database'].name}.cache.json")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(log_path, b"")
    native_commands: list[dict[str, Any]] = []

    def run_native(config: Path, command: str, stage: str) -> str:
        evidence, output = _run_native_slopo(
            slopo_source,
            repository_root,
            config,
            command,
            stage,
            log_path,
            _native_timeout(toolchain, command),
            ollama_url,
        )
        native_commands.append(evidence)
        return output

    run_native(args.main_config, "show-config", "main:show-config")
    run_native(args.loose_config, "show-config", "loose:show-config")
    _prepare_index_cache(
        artifacts["database"],
        cache_marker,
        profile_fingerprint=preflight["profile_fingerprint"],
        slopo_revision=preflight["slopo_revision"],
    )
    _force_full_reparse(artifacts["database"])
    index_output = run_native(args.main_config, "index", "main:index")
    _verify_index_completeness(
        artifacts["database"],
        artifacts["source_dir"],
        expected_paths,
        index_output,
    )
    run_native(args.main_config, "embed", "main:embed")
    _reset_report_directory(artifacts["main_report"])
    main_output = run_native(args.main_config, "analyze", "main:analyze")
    _finalize_analyze_report(artifacts["main_report"], main_output)
    _reset_report_directory(artifacts["loose_report"])
    loose_output = run_native(args.loose_config, "analyze", "loose:analyze")
    _finalize_analyze_report(artifacts["loose_report"], loose_output)
    collect_args = argparse.Namespace(
        db=artifacts["database"],
        main_report=artifacts["main_report"],
        loose_report=artifacts["loose_report"],
        profile_fingerprint=preflight["profile_fingerprint"],
        repository_root=repository_root,
        source_directory=artifacts["source_dir"],
        output=args.manifest,
    )
    # Internal stages remain silent so CI receives one bounded final JSON document.
    original_stdout = sys.stdout
    try:
        with open(os.devnull, "w", encoding="utf-8") as devnull:
            sys.stdout = devnull
            _run_collect_manifest(collect_args)
    finally:
        sys.stdout = original_stdout
    _secure_local_artifacts(local_root)
    current = _normalize_document(_load_json(args.manifest, "manifest"), accepted=False, label="manifest")
    if _source_identity(source_before) != _source_identity(current):
        raise ContractError(
            "The source tree changed during Slopo analysis",
            code="source_changed_during_scan",
        )
    _write_index_cache_marker(
        cache_marker,
        profile_fingerprint=preflight["profile_fingerprint"],
        slopo_revision=preflight["slopo_revision"],
    )
    return current, native_commands


def _run_snapshot(args: argparse.Namespace) -> int:
    _validate_scan_outputs(args, include_registry=False)
    current, native_commands = _scan_current(args)
    result = _bounded_result("snapshot", [], current["profile_fingerprint"])
    _attach_source_metadata(result, current)
    result["native_commands"] = native_commands
    result["cluster_count"] = len(current["clusters"])
    result["exact_cluster_count"] = sum(cluster["kind"] == "exact" for cluster in current["clusters"])
    result["semantic_cluster_count"] = sum(cluster["kind"] == "semantic" for cluster in current["clusters"])
    content = _encode_result(result)
    _write_atomic(args.evidence, content)
    sys.stdout.buffer.write(content)
    return 0


def _run_gate(args: argparse.Namespace) -> int:
    repository_root = _validate_scan_outputs(args, include_registry=True)
    _secure_local_artifacts(repository_root / ".slopo" / "local")
    accepted = _normalize_document(_load_json(args.registry, "registry"), accepted=True, label="registry")
    _toolchain, pinned_profile = _validated_toolchain(args.toolchain, args.main_config, args.loose_config)
    if accepted["profile_fingerprint"] != pinned_profile:
        raise ContractError(
            "The working registry profile does not match the pinned analysis profile",
            code="stale_profile",
        )
    current, native_commands = _scan_current(args)
    _result, content, return_code = _comparison_evidence(
        current,
        accepted,
        native_commands=native_commands,
    )
    _write_atomic(args.evidence, content)
    sys.stdout.buffer.write(content)
    return return_code


def _add_scan_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--toolchain", type=Path, required=True)
    parser.add_argument("--main-config", type=Path, required=True)
    parser.add_argument("--loose-config", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--slopo-source", type=Path)
    parser.add_argument("--ollama-url")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Versioned Slopo backend-go ratchet.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    check_parser = subparsers.add_parser(
        "check",
        help="Compare the current manifest with the accepted registry without changing the baseline.",
    )
    check_parser.add_argument("--manifest", type=Path, required=True)
    check_parser.add_argument("--registry", type=Path, required=True)
    check_parser.add_argument("--evidence", type=Path)
    check_parser.set_defaults(handler=_run_check)
    update_parser = subparsers.add_parser(
        "update-baseline",
        help="Explicitly replace the registry with a fully classified proposal for the current manifest.",
    )
    update_parser.add_argument("--manifest", type=Path, required=True)
    update_parser.add_argument("--proposal", type=Path, required=True)
    update_parser.add_argument("--registry", type=Path, required=True)
    update_parser.add_argument("--repository-root", type=Path, required=True)
    update_parser.add_argument("--confirm-profile", required=True)
    update_parser.set_defaults(handler=_run_update_baseline)
    proposal_parser = subparsers.add_parser(
        "propose-baseline",
        help="Prepare a separate proposal; new and changed clusters remain unclassified.",
    )
    proposal_parser.add_argument("--repository-root", type=Path, required=True)
    proposal_parser.add_argument("--manifest", type=Path, required=True)
    proposal_parser.add_argument("--registry", type=Path)
    proposal_parser.add_argument("--output", type=Path, required=True)
    proposal_parser.add_argument("--replace", action="store_true")
    proposal_parser.set_defaults(handler=_run_propose_baseline)
    collect_parser = subparsers.add_parser(
        "collect-manifest",
        help="Collect stable fingerprints from the Slopo database and Markdown reports.",
    )
    collect_parser.add_argument("--db", type=Path, required=True)
    collect_parser.add_argument("--main-report", type=Path, required=True)
    collect_parser.add_argument("--loose-report", type=Path, required=True)
    collect_parser.add_argument("--profile-fingerprint", required=True)
    collect_parser.add_argument("--repository-root", type=Path, required=True)
    collect_parser.add_argument("--source-directory", type=Path, required=True)
    collect_parser.add_argument("--output", type=Path, required=True)
    collect_parser.set_defaults(handler=_run_collect_manifest)
    preflight_parser = subparsers.add_parser(
        "preflight",
        help="Verify the pinned profile, local model, and Slopo execution without network access.",
    )
    preflight_parser.add_argument("--toolchain", type=Path, required=True)
    preflight_parser.add_argument("--main-config", type=Path, required=True)
    preflight_parser.add_argument("--loose-config", type=Path, required=True)
    preflight_parser.add_argument("--slopo-source", type=Path)
    preflight_parser.add_argument("--ollama-url")
    preflight_parser.set_defaults(handler=_run_preflight)
    snapshot_parser = subparsers.add_parser(
        "snapshot",
        help="Capture the current Slopo analysis without reading or changing the accepted-similarity registry.",
    )
    _add_scan_arguments(snapshot_parser)
    snapshot_parser.set_defaults(handler=_run_snapshot)
    gate_parser = subparsers.add_parser(
        "gate",
        help="Run the pinned Slopo analysis and compare it with the versioned registry without changing it.",
    )
    _add_scan_arguments(gate_parser)
    gate_parser.add_argument("--registry", type=Path, required=True)
    gate_parser.set_defaults(handler=_run_gate)
    return parser


def _prevalidate_locked_command(args: argparse.Namespace) -> Path | None:
    if args.command == "snapshot":
        return _validate_scan_outputs(args, include_registry=False, trust_evidence=False)
    if args.command == "gate":
        return _validate_scan_outputs(args, include_registry=True, trust_evidence=False)
    if args.command == "propose-baseline":
        return _validate_proposal_output(args)
    if args.command == "update-baseline":
        return _validate_update_paths(args)
    return None


def _contract_error_result(args: argparse.Namespace, error: ContractError) -> int:
    finding = {"code": error.code, "message": str(error), **error.evidence}
    result = _bounded_result("error", [finding], None)
    content = _encode_result(result)
    evidence_path = getattr(args, "evidence", None)
    if isinstance(evidence_path, Path) and getattr(args, "_trusted_evidence", False):
        _write_atomic(evidence_path, content)
    sys.stdout.buffer.write(content)
    return 2


def main() -> int:
    os.umask(0o077)
    args = _parser().parse_args()
    try:
        profile = _command_profile(args)
    except ContractError as error:
        return _contract_error_result(args, error)
    profile_token = _ACTIVE_PROFILE.set(profile)
    try:
        try:
            repository_root = _prevalidate_locked_command(args)
        except ContractError as error:
            return _contract_error_result(args, error)
        if repository_root is None:
            try:
                handler = cast(Callable[[argparse.Namespace], int], args.handler)
                return handler(args)
            except ContractError as error:
                return _contract_error_result(args, error)
        try:
            with _exclusive_local_lock(repository_root):
                try:
                    handler = cast(Callable[[argparse.Namespace], int], args.handler)
                    return handler(args)
                except ContractError as error:
                    return _contract_error_result(args, error)
        except ContractError as error:
            return _contract_error_result(args, error)
    finally:
        _ACTIVE_PROFILE.reset(profile_token)


if __name__ == "__main__":
    raise SystemExit(main())
