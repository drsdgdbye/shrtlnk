from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from unittest import mock

from tools.slopo_harness import ratchet

TOOLS_DIR = Path(__file__).resolve().parent
FIXTURE_DIR = TOOLS_DIR / "fixtures" / "semantic_duplication_ratchet"
RATCHET = TOOLS_DIR / "ratchet.py"
REPOSITORY_ROOT = TOOLS_DIR.parent.parent
TOOLCHAIN = REPOSITORY_ROOT / ".slopo" / "backend-go-toolchain.json"
MAIN_CONFIG = REPOSITORY_ROOT / ".slopo" / "backend-go.yaml"
LOOSE_CONFIG = REPOSITORY_ROOT / ".slopo" / "backend-go-loose.yaml"


def initialize_repository(repository: Path) -> dict[str, object]:
    repository.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repository)], check=True)
    (repository / "tracked.txt").write_text("initial\n", encoding="utf-8")
    (repository / ".gitignore").write_text(".slopo/local/\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", ".gitignore", "tracked.txt"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "-c",
            "user.name=Ratchet Contract",
            "-c",
            "user.email=ratchet@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "fixture",
        ],
        check=True,
    )
    return cast(dict[str, object], ratchet._source_metadata(repository, {"tracked.txt"}))


def write_index_database(database: Path, source_directory: Path, paths: list[str]) -> None:
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE files (
                id INTEGER PRIMARY KEY,
                path TEXT NOT NULL UNIQUE,
                mtime REAL NOT NULL
            );
            """
        )
        connection.executemany(
            "INSERT INTO files (path, mtime) VALUES (?, ?)",
            [(path, (source_directory / path).stat().st_mtime) for path in paths],
        )


def write_empty_slopo_database(database: Path, paths: list[str]) -> None:
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE files (
                id INTEGER PRIMARY KEY,
                path TEXT NOT NULL UNIQUE,
                mtime REAL NOT NULL
            );
            CREATE TABLE code_units (
                id INTEGER PRIMARY KEY,
                file_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                body_hash TEXT NOT NULL
            );
            """
        )
        connection.executemany(
            "INSERT INTO files (path, mtime) VALUES (?, 0)",
            [(path,) for path in paths],
        )


def write_source_files(repository: Path, paths: list[str]) -> None:
    for relative_path in paths:
        source_file = repository / relative_path
        source_file.parent.mkdir(parents=True, exist_ok=True)
        source_file.write_text(f"# fixture for {relative_path}\n", encoding="utf-8")


def write_profile_fixture(repository: Path) -> tuple[Path, Path, Path]:
    slopo_root = repository / ".slopo"
    slopo_root.mkdir(parents=True)
    main_config = slopo_root / "backend-go.yaml"
    loose_config = slopo_root / "backend-go-loose.yaml"
    toolchain = slopo_root / "backend-go-toolchain.json"
    shutil.copyfile(MAIN_CONFIG, main_config)
    shutil.copyfile(LOOSE_CONFIG, loose_config)
    shutil.copyfile(TOOLCHAIN, toolchain)
    (repository / "internal").mkdir(parents=True, exist_ok=True)
    return toolchain, main_config, loose_config


def write_executable(path: Path, source: str) -> None:
    path.write_text(f"#!{sys.executable}\n{source}", encoding="utf-8")
    path.chmod(0o700)


@contextmanager
def serve_model_inventory(inventory: Mapping[str, object]) -> Generator[str]:
    payload = json.dumps(inventory).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class SemanticDuplicationRatchetContractTest(unittest.TestCase):
    def run_ratchet(
        self,
        *arguments: str,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(RATCHET), *arguments],
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )

    @staticmethod
    def _registry_with_matching_loose_cluster() -> dict[str, Any]:
        registry = json.loads((FIXTURE_DIR / "accepted.json").read_text(encoding="utf-8"))
        loose_cluster = {
            **registry["accepted_clusters"][0],
            "tier": "loose",
            "fingerprint": "sha256:660b66bf9db95684db9c5b7ce080b45cacc596cd7c90f7ddd4bef6f6af5cd6fc",
        }
        registry["accepted_clusters"].append(loose_cluster)
        return cast(dict[str, Any], registry)

    def test_reviewed_clusters_are_green_and_registry_stays_unchanged(self) -> None:
        registry = FIXTURE_DIR / "accepted.json"
        before = registry.read_bytes()

        with tempfile.TemporaryDirectory() as temporary_directory:
            evidence = Path(temporary_directory) / "evidence.json"
            result = self.run_ratchet(
                "check",
                "--manifest",
                str(FIXTURE_DIR / "current.json"),
                "--registry",
                str(registry),
                "--evidence",
                str(evidence),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["status"], "green")
            self.assertEqual(json.loads(evidence.read_text(encoding="utf-8"))["status"], "green")

        self.assertEqual(registry.read_bytes(), before)

    def test_new_occurrence_blocks_with_bounded_machine_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            manifest = json.loads((FIXTURE_DIR / "current.json").read_text(encoding="utf-8"))
            manifest["clusters"][1]["members"].append(
                {
                    "path": "core/e.py",
                    "symbol": "same_copy_2",
                    "body_hash": "c" * 64,
                    "ordinal": 1,
                }
            )
            manifest_path = temporary_path / "current.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            result = self.run_ratchet(
                "check",
                "--manifest",
                str(manifest_path),
                "--registry",
                str(FIXTURE_DIR / "accepted.json"),
            )

            evidence = json.loads(result.stdout)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(evidence["status"], "blocked")
            self.assertEqual([finding["code"] for finding in evidence["findings"]], ["new_occurrence"])
            self.assertLessEqual(len(result.stdout.encode()), 256 * 1024)
            self.assertNotIn('"body":', result.stdout)

    def test_changed_cluster_evidence_separates_member_lineage(self) -> None:
        def member(path: str, symbol: str, body_marker: str) -> dict[str, object]:
            return {
                "path": path,
                "symbol": symbol,
                "body_hash": body_marker * 64,
                "ordinal": 1,
            }

        profile = "sha256:" + "d" * 64
        accepted_cluster = ratchet._normalize_cluster(
            {
                "kind": "semantic",
                "tier": "main",
                "members": [
                    member("core/unchanged.py", "unchanged_rule", "a"),
                    member("core/before_move.py", "moved_rule", "b"),
                    member("core/removed.py", "removed_rule", "c"),
                ],
            },
            "accepted fixture",
            accepted=False,
        )
        registry = {
            "schema_version": 2,
            "profile_fingerprint": profile,
            "accepted_clusters": [
                {
                    **accepted_cluster,
                    "classification": "intentional-similarity",
                    "reason": "The three rules remain separate because different domain owners change them.",
                    "owner": "core/example",
                }
            ],
        }
        manifest = {
            "schema_version": 2,
            "profile_fingerprint": profile,
            "clusters": [
                {
                    "kind": "semantic",
                    "tier": "main",
                    "members": [
                        member("core/unchanged.py", "unchanged_rule", "a"),
                        member("core/after_move.py", "renamed_rule", "b"),
                        member("core/added.py", "added_rule", "d"),
                    ],
                }
            ],
        }

        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            manifest_path = temporary_path / "current.json"
            registry_path = temporary_path / "accepted.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            registry_path.write_text(json.dumps(registry), encoding="utf-8")

            result = self.run_ratchet(
                "check",
                "--manifest",
                str(manifest_path),
                "--registry",
                str(registry_path),
            )

        self.assertEqual(result.returncode, 1, result.stdout)
        evidence = json.loads(result.stdout)
        self.assertEqual(evidence["review_candidate_count"], 1)
        candidate = evidence["review_candidates"][0]
        self.assertRegex(candidate["candidate_key"], r"^sha256:[0-9a-f]{64}$")
        self.assertRegex(candidate["review_key"], r"^sha256:[0-9a-f]{64}$")
        lineage = candidate["lineages"][0]
        self.assertEqual(lineage["accepted_clusters"][0]["fingerprint"], accepted_cluster["fingerprint"])
        self.assertEqual(
            [item["path"] for item in lineage["unchanged_members"]],
            ["core/unchanged.py"],
        )
        self.assertEqual(
            [
                (item["previous"]["path"], item["current"]["path"], item["body_hash"])
                for item in lineage["moved_members"]
            ],
            [("core/before_move.py", "core/after_move.py", "b" * 64)],
        )
        self.assertEqual([item["path"] for item in lineage["added_members"]], ["core/added.py"])
        self.assertEqual([item["path"] for item in lineage["removed_members"]], ["core/removed.py"])

    def test_stale_accepted_cluster_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manifest = json.loads((FIXTURE_DIR / "current.json").read_text(encoding="utf-8"))
            manifest["clusters"] = manifest["clusters"][:1]
            manifest_path = Path(temporary_directory) / "current.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            result = self.run_ratchet(
                "check",
                "--manifest",
                str(manifest_path),
                "--registry",
                str(FIXTURE_DIR / "accepted.json"),
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("stale_baseline", {finding["code"] for finding in json.loads(result.stdout)["findings"]})

    def test_new_semantic_cluster_blocks_until_classification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manifest = json.loads((FIXTURE_DIR / "current.json").read_text(encoding="utf-8"))
            manifest["clusters"].append(
                {
                    "kind": "semantic",
                    "tier": "main",
                    "members": [
                        {
                            "path": "core/new_a.py",
                            "symbol": "calculate_a",
                            "body_hash": "d" * 64,
                            "ordinal": 1,
                        },
                        {
                            "path": "core/new_b.py",
                            "symbol": "calculate_b",
                            "body_hash": "e" * 64,
                            "ordinal": 1,
                        },
                    ],
                }
            )
            manifest_path = Path(temporary_directory) / "current.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            result = self.run_ratchet(
                "check",
                "--manifest",
                str(manifest_path),
                "--registry",
                str(FIXTURE_DIR / "accepted.json"),
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn(
                "new_cluster",
                {finding["code"] for finding in json.loads(result.stdout)["findings"]},
            )

    def test_registry_requires_review_classification_reason_and_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            registry = json.loads((FIXTURE_DIR / "accepted.json").read_text(encoding="utf-8"))
            registry["accepted_clusters"][0]["reason"] = "duplicate"
            registry["accepted_clusters"][0]["owner"] = ""
            registry_path = Path(temporary_directory) / "accepted.json"
            registry_path.write_text(json.dumps(registry), encoding="utf-8")

            result = self.run_ratchet(
                "check",
                "--manifest",
                str(FIXTURE_DIR / "current.json"),
                "--registry",
                str(registry_path),
            )

            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)["findings"][0]["code"], "contract_error")

    def test_registry_accepts_review_reason_in_repository_language(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            registry = json.loads((FIXTURE_DIR / "accepted.json").read_text(encoding="utf-8"))
            registry["accepted_clusters"][0]["reason"] = (
                "La similitud es intencional porque las funciones sirven contratos externos distintos."
            )
            registry_path = Path(temporary_directory) / "accepted.json"
            registry_path.write_text(json.dumps(registry), encoding="utf-8")

            result = self.run_ratchet(
                "check",
                "--manifest",
                str(FIXTURE_DIR / "current.json"),
                "--registry",
                str(registry_path),
            )

            self.assertEqual(result.returncode, 0, result.stdout)

    def test_registry_rejects_different_classifications_for_same_members_across_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            registry = self._registry_with_matching_loose_cluster()
            registry["accepted_clusters"][-1]["classification"] = "low-signal"
            registry_path = Path(temporary_directory) / "accepted.json"
            registry_path.write_text(json.dumps(registry), encoding="utf-8")

            result = self.run_ratchet(
                "check",
                "--manifest",
                str(FIXTURE_DIR / "current.json"),
                "--registry",
                str(registry_path),
            )

            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)["findings"][0]["code"], "contract_error")

    def test_registry_rejects_different_owners_for_same_members_across_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            registry = self._registry_with_matching_loose_cluster()
            registry["accepted_clusters"][-1]["owner"] = "core/other-owner"
            registry_path = Path(temporary_directory) / "accepted.json"
            registry_path.write_text(json.dumps(registry), encoding="utf-8")

            result = self.run_ratchet(
                "check",
                "--manifest",
                str(FIXTURE_DIR / "current.json"),
                "--registry",
                str(registry_path),
            )

            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)["findings"][0]["code"], "contract_error")

    def test_baseline_changes_only_through_explicit_reviewed_update_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            repository = temporary_path / "repository"
            initialize_repository(repository)
            source_file = repository / "internal" / "rule.py"
            source_file.parent.mkdir(parents=True)
            source_file.write_text("def rule():\n    return 'reviewed'\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(repository), "add", "internal/rule.py"],
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repository),
                    "-c",
                    "user.name=Ratchet Contract",
                    "-c",
                    "user.email=ratchet@example.invalid",
                    "commit",
                    "--quiet",
                    "-m",
                    "baseline fixture",
                ],
                check=True,
            )
            toolchain, main_config, loose_config = write_profile_fixture(repository)
            local_root = repository / ".slopo" / "local"
            local_root.mkdir(parents=True)
            registry_path = repository / ".slopo" / "backend-go.accepted.json"
            shutil.copyfile(FIXTURE_DIR / "accepted.json", registry_path)
            source_metadata = ratchet._source_metadata(repository, {"internal/rule.py"})
            manifest = json.loads((FIXTURE_DIR / "current.json").read_text(encoding="utf-8"))
            manifest.update(source_metadata)
            profile_fingerprint = ratchet._validated_toolchain(toolchain, main_config, loose_config)[1]
            manifest["profile_fingerprint"] = profile_fingerprint
            new_member = {
                "path": "core/e.py",
                "symbol": "same_copy_2",
                "body_hash": "c" * 64,
                "ordinal": 1,
            }
            manifest["clusters"][1]["members"].append(new_member)
            manifest_path = local_root / "current.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            before = registry_path.read_bytes()

            blocked = self.run_ratchet(
                "check",
                "--manifest",
                str(manifest_path),
                "--registry",
                str(registry_path),
            )
            self.assertEqual(blocked.returncode, 1)
            self.assertEqual(registry_path.read_bytes(), before)

            aliased_evidence = self.run_ratchet(
                "check",
                "--manifest",
                str(manifest_path),
                "--registry",
                str(registry_path),
                "--evidence",
                str(registry_path),
            )
            self.assertEqual(aliased_evidence.returncode, 2)
            self.assertEqual(
                json.loads(aliased_evidence.stdout)["findings"][0]["code"],
                "output_path_collision",
            )
            self.assertEqual(registry_path.read_bytes(), before)

            proposal_path = local_root / "reviewed-proposal.json"
            proposed = self.run_ratchet(
                "propose-baseline",
                "--repository-root",
                str(repository),
                "--manifest",
                str(manifest_path),
                "--registry",
                str(registry_path),
                "--output",
                str(proposal_path),
            )
            self.assertEqual(proposed.returncode, 0, proposed.stdout)
            proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
            exact_candidate = next(
                candidate for candidate in proposal["review_candidates"] if candidate["kind"] == "exact"
            )
            self.assertEqual(exact_candidate["classification"], "unclassified")
            exact_candidate.update(
                {
                    "classification": "accepted-existing-debt",
                    "reason": "The new exact copy was reviewed and temporarily accepted as existing backend debt.",
                    "owner": "backend",
                }
            )
            semantic_candidate = next(
                candidate for candidate in proposal["review_candidates"] if candidate["kind"] == "semantic"
            )
            semantic_candidate.update(
                {
                    "classification": "intentional-similarity",
                    "reason": "The functions serve different external contracts and were reconfirmed by a reviewer.",
                    "owner": "backend",
                }
            )
            proposal_path.write_text(json.dumps(proposal), encoding="utf-8")
            environment = os.environ.copy()
            environment["SLOPO_SOURCE"] = str(Path(os.environ["SLOPO_SOURCE"]))

            updated = self.run_ratchet(
                "update-baseline",
                "--manifest",
                str(manifest_path),
                "--proposal",
                str(proposal_path),
                "--registry",
                str(registry_path),
                "--repository-root",
                str(repository),
                "--confirm-profile",
                manifest["profile_fingerprint"],
                environment=environment,
            )

            self.assertEqual(updated.returncode, 0, updated.stdout)
            self.assertNotEqual(registry_path.read_bytes(), before)
            green = self.run_ratchet(
                "check",
                "--manifest",
                str(manifest_path),
                "--registry",
                str(registry_path),
            )
            self.assertEqual(green.returncode, 0, green.stdout)

    def test_update_baseline_rejects_noncanonical_registry_targets_without_modifying_them(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory) / "repository"
            initialize_repository(repository)
            local_root = repository / ".slopo" / "local"
            local_root.mkdir(parents=True)
            manifest_path = local_root / "current.json"
            proposal_path = local_root / "proposal.json"
            manifest_path.write_text("{}", encoding="utf-8")
            proposal_path.write_text("{}", encoding="utf-8")

            for target in (repository / "tracked.txt", repository / ".git" / "config"):
                with self.subTest(target=target.relative_to(repository)):
                    before = target.read_bytes()
                    result = self.run_ratchet(
                        "update-baseline",
                        "--manifest",
                        str(manifest_path),
                        "--proposal",
                        str(proposal_path),
                        "--registry",
                        str(target),
                        "--repository-root",
                        str(repository),
                        "--confirm-profile",
                        "sha256:" + "d" * 64,
                    )

                    self.assertEqual(result.returncode, 2, result.stdout)
                    self.assertEqual(json.loads(result.stdout)["findings"][0]["code"], "unsafe_output_path")
                    self.assertEqual(target.read_bytes(), before)

    def test_update_baseline_rejects_new_ignored_python_file_from_fresh_pinned_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory) / "repository"
            initialize_repository(repository)
            source_directory = repository / "internal"
            source_directory.mkdir(parents=True)
            source_file = source_directory / "rule.py"
            source_file.write_text("def rule():\n    return 'reviewed'\n", encoding="utf-8")
            (repository / ".gitignore").write_text(
                ".slopo/local/\ninternal/ignored.py\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "-C", str(repository), "add", ".gitignore", "internal/rule.py"],
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repository),
                    "-c",
                    "user.name=Ratchet Contract",
                    "-c",
                    "user.email=ratchet@example.invalid",
                    "commit",
                    "--quiet",
                    "-m",
                    "pinned scope fixture",
                ],
                check=True,
            )
            toolchain, main_config, loose_config = write_profile_fixture(repository)
            source_metadata = ratchet._source_metadata(repository, {"internal/rule.py"})
            local_root = repository / ".slopo" / "local"
            local_root.mkdir(parents=True)
            manifest = json.loads((FIXTURE_DIR / "current.json").read_text(encoding="utf-8"))
            manifest.update(source_metadata)
            profile_fingerprint = ratchet._validated_toolchain(toolchain, main_config, loose_config)[1]
            manifest["profile_fingerprint"] = profile_fingerprint
            manifest_path = local_root / "current.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            proposal = json.loads((FIXTURE_DIR / "accepted.json").read_text(encoding="utf-8"))
            proposal.update(source_metadata)
            proposal["profile_fingerprint"] = profile_fingerprint
            proposal_path = local_root / "proposal.json"
            proposal_path.write_text(json.dumps(proposal), encoding="utf-8")
            status_before = subprocess.check_output(
                ["git", "-C", str(repository), "status", "--porcelain=v1", "-z", "--untracked-files=all"]
            )
            (source_directory / "ignored.py").write_text(
                "def ignored_rule():\n    return 'new scope member'\n",
                encoding="utf-8",
            )
            status_after = subprocess.check_output(
                ["git", "-C", str(repository), "status", "--porcelain=v1", "-z", "--untracked-files=all"]
            )
            self.assertEqual(status_after, status_before)
            registry = repository / ".slopo" / "backend-go.accepted.json"
            environment = os.environ.copy()
            environment["SLOPO_SOURCE"] = str(Path(os.environ["SLOPO_SOURCE"]))

            updated = self.run_ratchet(
                "update-baseline",
                "--manifest",
                str(manifest_path),
                "--proposal",
                str(proposal_path),
                "--registry",
                str(registry),
                "--repository-root",
                str(repository),
                "--confirm-profile",
                manifest["profile_fingerprint"],
                environment=environment,
            )

            self.assertEqual(updated.returncode, 2, updated.stdout)
            self.assertEqual(
                json.loads(updated.stdout)["findings"][0]["code"],
                "source_identity_changed",
                updated.stdout,
            )
            self.assertFalse(registry.exists())

    def test_update_baseline_refuses_source_changed_after_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            repository = temporary_path / "repository"
            source_metadata = initialize_repository(repository)
            local_root = repository / ".slopo" / "local"
            local_root.mkdir(parents=True)
            manifest = json.loads((FIXTURE_DIR / "current.json").read_text(encoding="utf-8"))
            manifest.update(source_metadata)
            manifest_path = local_root / "current.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            proposal = json.loads((FIXTURE_DIR / "accepted.json").read_text(encoding="utf-8"))
            proposal.update(source_metadata)
            proposal_path = local_root / "proposal.json"
            proposal_path.write_text(json.dumps(proposal), encoding="utf-8")
            registry_path = repository / ".slopo" / "backend-go.accepted.json"
            (repository / "tracked.txt").write_text("changed after review\n", encoding="utf-8")

            result = self.run_ratchet(
                "update-baseline",
                "--manifest",
                str(manifest_path),
                "--proposal",
                str(proposal_path),
                "--registry",
                str(registry_path),
                "--repository-root",
                str(repository),
                "--confirm-profile",
                manifest["profile_fingerprint"],
            )

            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)["findings"][0]["code"], "source_identity_changed")
            self.assertFalse(registry_path.exists())

    def test_update_baseline_rejects_changed_source_bytes_when_git_status_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory) / "repository"
            initialize_repository(repository)
            source_directory = repository / "core"
            source_directory.mkdir()
            source_file = source_directory / "rule.py"
            source_file.write_text("def rule():\n    return 'committed'\n", encoding="utf-8")
            (repository / ".gitignore").write_text(".slopo/local/\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(repository), "add", ".gitignore", "core/rule.py"],
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repository),
                    "-c",
                    "user.name=Ratchet Contract",
                    "-c",
                    "user.email=ratchet@example.invalid",
                    "commit",
                    "--quiet",
                    "-m",
                    "scope fixture",
                ],
                check=True,
            )
            source_file.write_text("def rule():\n    return 'reviewed'\n", encoding="utf-8")
            status_before = subprocess.check_output(
                ["git", "-C", str(repository), "status", "--porcelain=v1", "-z", "--untracked-files=all"]
            )

            local_root = repository / ".slopo" / "local"
            local_root.mkdir(parents=True)
            database = local_root / "fixture.db"
            write_empty_slopo_database(database, ["rule.py"])
            main_report = local_root / "main-report"
            loose_report = local_root / "loose-report"
            for report in (main_report, loose_report):
                report.mkdir()
                (report / "index.md").write_text("# Slopo report\n", encoding="utf-8")
            manifest = local_root / "current.json"

            collected = self.run_ratchet(
                "collect-manifest",
                "--db",
                str(database),
                "--main-report",
                str(main_report),
                "--loose-report",
                str(loose_report),
                "--profile-fingerprint",
                "sha256:" + "d" * 64,
                "--repository-root",
                str(repository),
                "--source-directory",
                str(source_directory),
                "--output",
                str(manifest),
            )
            self.assertEqual(collected.returncode, 0, collected.stdout)
            collected_result = json.loads(collected.stdout)
            self.assertEqual(collected_result["source_file_count"], 1)
            self.assertNotIn("source_scope_paths", collected_result)

            proposal = local_root / "proposal.json"
            proposed = self.run_ratchet(
                "propose-baseline",
                "--repository-root",
                str(repository),
                "--manifest",
                str(manifest),
                "--output",
                str(proposal),
            )
            self.assertEqual(proposed.returncode, 0, proposed.stdout)
            proposed_result = json.loads(proposed.stdout)
            self.assertEqual(proposed_result["source_file_count"], 1)
            self.assertNotIn("source_scope_paths", proposed_result)

            source_file.write_text("def rule():\n    return 'changed later'\n", encoding="utf-8")
            status_after = subprocess.check_output(
                ["git", "-C", str(repository), "status", "--porcelain=v1", "-z", "--untracked-files=all"]
            )
            self.assertEqual(status_after, status_before)

            registry = repository / ".slopo" / "backend-go.accepted.json"
            updated = self.run_ratchet(
                "update-baseline",
                "--manifest",
                str(manifest),
                "--proposal",
                str(proposal),
                "--registry",
                str(registry),
                "--repository-root",
                str(repository),
                "--confirm-profile",
                "sha256:" + "d" * 64,
            )

            self.assertEqual(updated.returncode, 2, updated.stdout)
            self.assertEqual(json.loads(updated.stdout)["findings"][0]["code"], "source_identity_changed")
            self.assertFalse(registry.exists())

    def test_update_baseline_requires_source_metadata_in_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            repository = temporary_path / "repository"
            source_metadata = initialize_repository(repository)
            local_root = repository / ".slopo" / "local"
            local_root.mkdir(parents=True)
            manifest = json.loads((FIXTURE_DIR / "current.json").read_text(encoding="utf-8"))
            manifest.update(source_metadata)
            manifest_path = local_root / "current.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            proposal_path = local_root / "proposal.json"
            shutil.copyfile(FIXTURE_DIR / "accepted.json", proposal_path)

            result = self.run_ratchet(
                "update-baseline",
                "--manifest",
                str(manifest_path),
                "--proposal",
                str(proposal_path),
                "--registry",
                str(repository / ".slopo" / "backend-go.accepted.json"),
                "--repository-root",
                str(repository),
                "--confirm-profile",
                manifest["profile_fingerprint"],
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn("source_revision", json.loads(result.stdout)["findings"][0]["message"])

    def test_update_baseline_rejects_proposal_for_another_source_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            repository = temporary_path / "repository"
            source_metadata = initialize_repository(repository)
            local_root = repository / ".slopo" / "local"
            local_root.mkdir(parents=True)
            manifest = json.loads((FIXTURE_DIR / "current.json").read_text(encoding="utf-8"))
            manifest.update(source_metadata)
            manifest_path = local_root / "current.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            proposal = json.loads((FIXTURE_DIR / "accepted.json").read_text(encoding="utf-8"))
            proposal.update(source_metadata)
            proposal["source_status_digest"] = "sha256:" + "f" * 64
            proposal_path = local_root / "proposal.json"
            proposal_path.write_text(json.dumps(proposal), encoding="utf-8")

            result = self.run_ratchet(
                "update-baseline",
                "--manifest",
                str(manifest_path),
                "--proposal",
                str(proposal_path),
                "--registry",
                str(repository / ".slopo" / "backend-go.accepted.json"),
                "--repository-root",
                str(repository),
                "--confirm-profile",
                manifest["profile_fingerprint"],
            )

            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)["findings"][0]["code"], "source_identity_changed")

    def test_registry_source_metadata_is_informational_for_read_only_comparison(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            manifest = json.loads((FIXTURE_DIR / "current.json").read_text(encoding="utf-8"))
            manifest["source_revision"] = "b" * 40
            manifest["source_dirty"] = True
            manifest["source_status_digest"] = "sha256:" + "c" * 64
            manifest_path = temporary_path / "current.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            registry = json.loads((FIXTURE_DIR / "accepted.json").read_text(encoding="utf-8"))
            registry.update(
                {
                    "source_revision": "a" * 40,
                    "source_dirty": False,
                    "source_status": [],
                    "source_status_digest": "sha256:" + "d" * 64,
                    "source_status_truncated": False,
                }
            )
            registry_path = temporary_path / "registry.json"
            registry_path.write_text(json.dumps(registry), encoding="utf-8")

            result = self.run_ratchet(
                "check",
                "--manifest",
                str(manifest_path),
                "--registry",
                str(registry_path),
            )

            self.assertEqual(result.returncode, 0, result.stdout)

    def test_collects_exact_and_semantic_fingerprints_from_native_slopo_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            repository = temporary_path / "repository"
            initialize_repository(repository)
            (repository / "tracked.txt").write_text("dirty\n", encoding="utf-8")
            write_source_files(repository, ["core/a.py", "core/b.py", "core/c.py", "core/d.py"])
            database = temporary_path / "slopo.db"
            with sqlite3.connect(database) as connection:
                connection.executescript((FIXTURE_DIR / "native.sql").read_text(encoding="utf-8"))
            manifest = temporary_path / "private" / "current.json"

            collected = self.run_ratchet(
                "collect-manifest",
                "--db",
                str(database),
                "--main-report",
                str(FIXTURE_DIR / "main-report"),
                "--loose-report",
                str(FIXTURE_DIR / "loose-report"),
                "--profile-fingerprint",
                "sha256:" + "d" * 64,
                "--repository-root",
                str(repository),
                "--source-directory",
                str(repository),
                "--output",
                str(manifest),
            )

            self.assertEqual(collected.returncode, 0, collected.stdout)
            manifest_document = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertRegex(manifest_document["source_revision"], r"^[0-9a-f]{40}$")
            self.assertTrue(manifest_document["source_dirty"])
            self.assertIn(" M tracked.txt", manifest_document["source_status"])
            self.assertEqual(manifest.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(manifest.stat().st_mode & 0o777, 0o600)
            checked = self.run_ratchet(
                "check",
                "--manifest",
                str(manifest),
                "--registry",
                str(FIXTURE_DIR / "accepted.json"),
            )
            self.assertEqual(checked.returncode, 0, checked.stdout)
            evidence = json.loads(checked.stdout)
            self.assertEqual(evidence["source_revision"], manifest_document["source_revision"])
            self.assertTrue(evidence["source_dirty"])

    def test_proposal_keeps_reviewed_clusters_and_marks_changed_cluster_unclassified(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            repository = temporary_path / "repository"
            initialize_repository(repository)
            local_root = repository / ".slopo" / "local"
            local_root.mkdir(parents=True)
            manifest = json.loads((FIXTURE_DIR / "current.json").read_text(encoding="utf-8"))
            manifest["clusters"][1]["members"].append(
                {
                    "path": "core/e.py",
                    "symbol": "same_copy_2",
                    "body_hash": "c" * 64,
                    "ordinal": 1,
                }
            )
            manifest_path = local_root / "current.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            proposal_path = local_root / "proposal.json"
            registry_path = repository / ".slopo" / "accepted.json"
            shutil.copyfile(FIXTURE_DIR / "accepted.json", registry_path)

            proposed = self.run_ratchet(
                "propose-baseline",
                "--repository-root",
                str(repository),
                "--manifest",
                str(manifest_path),
                "--registry",
                str(registry_path),
                "--output",
                str(proposal_path),
            )

            self.assertEqual(proposed.returncode, 0, proposed.stdout)
            proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
            self.assertEqual(proposal["source_revision"], manifest["source_revision"])
            self.assertEqual(proposal["source_status_digest"], manifest["source_status_digest"])
            by_kind = {candidate["kind"]: candidate for candidate in proposal["review_candidates"]}
            self.assertEqual(by_kind["semantic"]["classification"], "intentional-similarity")
            self.assertEqual(by_kind["exact"]["classification"], "unclassified")
            self.assertEqual(by_kind["exact"]["reason"], "")
            self.assertEqual(by_kind["exact"]["owner"], "")
            self.assertEqual(len(by_kind["exact"]["lineages"][0]["added_members"]), 1)
            self.assertTrue(all("classification" not in cluster for cluster in proposal["accepted_clusters"]))

    def test_proposal_groups_same_member_set_across_main_and_loose_for_one_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            repository = temporary_path / "repository"
            initialize_repository(repository)
            local_root = repository / ".slopo" / "local"
            local_root.mkdir(parents=True)
            manifest = json.loads((FIXTURE_DIR / "current.json").read_text(encoding="utf-8"))
            semantic_main = manifest["clusters"][0]
            semantic_loose = json.loads(json.dumps(semantic_main))
            semantic_loose["tier"] = "loose"
            for cluster in (semantic_main, semantic_loose):
                cluster["members"][0]["path"] = "core/moved_a.py"
                cluster["members"][0]["symbol"] = "renamed_render"
            manifest["clusters"].append(semantic_loose)
            manifest_path = local_root / "current.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            proposal_path = local_root / "proposal.json"
            registry_path = repository / ".slopo" / "accepted.json"
            registry_path.write_text(json.dumps(self._registry_with_matching_loose_cluster()), encoding="utf-8")

            checked = self.run_ratchet(
                "check",
                "--manifest",
                str(manifest_path),
                "--registry",
                str(registry_path),
            )
            self.assertEqual(checked.returncode, 1, checked.stdout)
            evidence = json.loads(checked.stdout)
            semantic_evidence = [
                candidate for candidate in evidence["review_candidates"] if candidate["kind"] == "semantic"
            ]
            self.assertEqual(len(semantic_evidence), 1)
            self.assertEqual(semantic_evidence[0]["tiers"], ["loose", "main"])
            self.assertEqual(len(semantic_evidence[0]["lineages"]), 1)

            proposed = self.run_ratchet(
                "propose-baseline",
                "--repository-root",
                str(repository),
                "--manifest",
                str(manifest_path),
                "--registry",
                str(registry_path),
                "--output",
                str(proposal_path),
            )

            self.assertEqual(proposed.returncode, 0, proposed.stdout)
            proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
            semantic_reviews = [
                candidate for candidate in proposal["review_candidates"] if candidate["kind"] == "semantic"
            ]
            self.assertEqual(len(semantic_reviews), 1)
            candidate = semantic_reviews[0]
            self.assertEqual(candidate["tiers"], ["loose", "main"])
            self.assertEqual(candidate["classification"], "unclassified")
            self.assertEqual(len(candidate["cluster_fingerprints"]), 2)
            self.assertEqual(len(candidate["lineages"]), 1)
            self.assertEqual(
                [cluster["tier"] for cluster in candidate["lineages"][0]["current_clusters"]],
                ["loose", "main"],
            )
            semantic_clusters = [cluster for cluster in proposal["accepted_clusters"] if cluster["kind"] == "semantic"]
            self.assertEqual(len(semantic_clusters), 2)
            self.assertTrue(all("classification" not in cluster for cluster in semantic_clusters))
            with self.assertRaises(ratchet.ContractError):
                ratchet._materialize_reviewed_proposal(proposal)

            candidate.update(
                {
                    "classification": "intentional-similarity",
                    "reason": "The shared profile membership was reviewed once and remains with different domain owners.",
                    "owner": "core/example",
                }
            )
            materialized = ratchet._materialize_reviewed_proposal(proposal)
            semantic_materialized = [
                cluster for cluster in materialized["accepted_clusters"] if cluster["kind"] == "semantic"
            ]
            self.assertEqual(
                {cluster["classification"] for cluster in semantic_materialized},
                {"intentional-similarity"},
            )
            self.assertEqual({cluster["owner"] for cluster in semantic_materialized}, {"core/example"})

    def test_proposal_preserves_unchanged_cross_tier_decision_with_main_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            repository = temporary_path / "repository"
            initialize_repository(repository)
            local_root = repository / ".slopo" / "local"
            local_root.mkdir(parents=True)
            manifest = json.loads((FIXTURE_DIR / "current.json").read_text(encoding="utf-8"))
            semantic_loose = json.loads(json.dumps(manifest["clusters"][0]))
            semantic_loose["tier"] = "loose"
            manifest["clusters"].append(semantic_loose)
            manifest_path = local_root / "current.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            registry = self._registry_with_matching_loose_cluster()
            main_reason = registry["accepted_clusters"][0]["reason"]
            registry["accepted_clusters"][-1]["reason"] = (
                "The loose profile confirms the same membership but retains a different historical rationale."
            )
            registry_path = repository / ".slopo" / "accepted.json"
            registry_path.write_text(json.dumps(registry), encoding="utf-8")
            proposal_path = local_root / "proposal.json"

            proposed = self.run_ratchet(
                "propose-baseline",
                "--repository-root",
                str(repository),
                "--manifest",
                str(manifest_path),
                "--registry",
                str(registry_path),
                "--output",
                str(proposal_path),
            )

            self.assertEqual(proposed.returncode, 0, proposed.stdout)
            proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
            semantic_candidate = next(
                candidate for candidate in proposal["review_candidates"] if candidate["kind"] == "semantic"
            )
            self.assertEqual(semantic_candidate["classification"], "intentional-similarity")
            self.assertEqual(semantic_candidate["owner"], "backend")
            self.assertEqual(semantic_candidate["reason"], main_reason)

    def test_proposal_does_not_preserve_divergent_cross_tier_decision_or_owner(self) -> None:
        manifest_raw = json.loads((FIXTURE_DIR / "current.json").read_text(encoding="utf-8"))
        semantic_loose = json.loads(json.dumps(manifest_raw["clusters"][0]))
        semantic_loose["tier"] = "loose"
        manifest_raw["clusters"].append(semantic_loose)
        current = ratchet._normalize_document(manifest_raw, accepted=False, label="manifest fixture")
        registry_raw = self._registry_with_matching_loose_cluster()
        accepted = ratchet._normalize_document(registry_raw, accepted=True, label="registry fixture")
        metadata = ratchet._accepted_review_metadata(registry_raw, accepted)
        loose_key = next(key for key in metadata if key[0:2] == ("semantic", "loose"))

        for field, divergent_value in (
            ("classification", "low-signal"),
            ("owner", "core/other-owner"),
        ):
            with self.subTest(field=field):
                divergent_metadata = {key: dict(value) for key, value in metadata.items()}
                divergent_metadata[loose_key][field] = divergent_value

                review_candidates, _removed = ratchet._proposal_review_projection(
                    current,
                    accepted,
                    divergent_metadata,
                )

                semantic_candidate = next(
                    candidate for candidate in review_candidates if candidate["kind"] == "semantic"
                )
                self.assertEqual(semantic_candidate["classification"], "unclassified")
                self.assertEqual(semantic_candidate["reason"], "")
                self.assertEqual(semantic_candidate["owner"], "")

    def test_proposal_exposes_removed_candidate_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            repository = temporary_path / "repository"
            initialize_repository(repository)
            local_root = repository / ".slopo" / "local"
            local_root.mkdir(parents=True)
            manifest = json.loads((FIXTURE_DIR / "current.json").read_text(encoding="utf-8"))
            manifest["clusters"] = [cluster for cluster in manifest["clusters"] if cluster["kind"] == "exact"]
            manifest_path = local_root / "current.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            proposal_path = local_root / "proposal.json"
            registry_path = repository / ".slopo" / "accepted.json"
            shutil.copyfile(FIXTURE_DIR / "accepted.json", registry_path)

            proposed = self.run_ratchet(
                "propose-baseline",
                "--repository-root",
                str(repository),
                "--manifest",
                str(manifest_path),
                "--registry",
                str(registry_path),
                "--output",
                str(proposal_path),
            )

            self.assertEqual(proposed.returncode, 0, proposed.stdout)
            proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
            self.assertEqual(len(proposal["removed_candidates"]), 1)
            removed = proposal["removed_candidates"][0]
            self.assertEqual(removed["kind"], "semantic")
            lineage = removed["lineages"][0]
            self.assertEqual(lineage["current_clusters"], [])
            self.assertEqual(len(lineage["removed_members"]), 2)
            self.assertEqual(lineage["added_members"], [])

    def test_preflight_fails_deterministically_when_pinned_model_is_absent(self) -> None:
        with serve_model_inventory({"models": []}) as ollama_url:
            result = self.run_ratchet(
                "preflight",
                "--toolchain",
                str(TOOLCHAIN),
                "--main-config",
                str(MAIN_CONFIG),
                "--loose-config",
                str(LOOSE_CONFIG),
                "--slopo-source",
                "/definitely/missing/slopo",
                "--ollama-url",
                ollama_url,
            )

        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["findings"][0]["code"], "missing_model")

    def test_gate_replaces_evidence_with_preflight_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory) / "repository"
            toolchain, main_config, loose_config = write_profile_fixture(repository)
            local_root = repository / ".slopo" / "local"
            local_root.mkdir()
            evidence = local_root / "evidence.json"
            evidence.write_text('{"status":"green"}\n', encoding="utf-8")
            registry = json.loads((FIXTURE_DIR / "accepted.json").read_text(encoding="utf-8"))
            registry["profile_fingerprint"] = ratchet._validated_toolchain(
                TOOLCHAIN,
                MAIN_CONFIG,
                LOOSE_CONFIG,
            )[1]
            registry_path = repository / ".slopo" / "registry.json"
            registry_path.write_text(json.dumps(registry), encoding="utf-8")
            with serve_model_inventory({"models": []}) as ollama_url:
                result = self.run_ratchet(
                    "gate",
                    "--toolchain",
                    str(toolchain),
                    "--main-config",
                    str(main_config),
                    "--loose-config",
                    str(loose_config),
                    "--registry",
                    str(registry_path),
                    "--manifest",
                    str(local_root / "current.json"),
                    "--evidence",
                    str(evidence),
                    "--slopo-source",
                    "/definitely/missing/slopo",
                    "--ollama-url",
                    ollama_url,
                )

            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(evidence.read_text(encoding="utf-8"))["findings"][0]["code"], "missing_model")

    def test_gate_rejects_stale_registry_profile_before_model_access(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory) / "repository"
            toolchain, main_config, loose_config = write_profile_fixture(repository)
            local_root = repository / ".slopo" / "local"
            local_root.mkdir()
            result = self.run_ratchet(
                "gate",
                "--toolchain",
                str(toolchain),
                "--main-config",
                str(main_config),
                "--loose-config",
                str(loose_config),
                "--registry",
                str(FIXTURE_DIR / "accepted.json"),
                "--manifest",
                str(local_root / "current.json"),
                "--evidence",
                str(local_root / "evidence.json"),
                "--slopo-source",
                "/definitely/missing/slopo",
                "--ollama-url",
                "http://127.0.0.1:1",
            )

            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)["findings"][0]["code"], "stale_profile")

    def test_gate_validates_registry_before_preflight_or_native_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory) / "repository"
            toolchain, main_config, loose_config = write_profile_fixture(repository)
            local_root = repository / ".slopo" / "local"
            local_root.mkdir()
            evidence = local_root / "evidence.json"
            result = self.run_ratchet(
                "gate",
                "--toolchain",
                str(toolchain),
                "--main-config",
                str(main_config),
                "--loose-config",
                str(loose_config),
                "--registry",
                str(repository / ".slopo" / "missing-registry.json"),
                "--manifest",
                str(local_root / "current.json"),
                "--evidence",
                str(evidence),
                "--slopo-source",
                "/definitely/missing/slopo",
                "--ollama-url",
                "http://127.0.0.1:1",
            )

            finding = json.loads(result.stdout)["findings"][0]
            self.assertEqual(result.returncode, 2)
            self.assertEqual(finding["code"], "contract_error")
            self.assertIn("registry does not exist", finding["message"])

    def test_gate_rejects_invalid_registry_before_model_access(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory) / "repository"
            toolchain, main_config, loose_config = write_profile_fixture(repository)
            local_root = repository / ".slopo" / "local"
            local_root.mkdir()
            registry = repository / ".slopo" / "invalid-registry.json"
            registry.write_text('{"schema_version": 0}\n', encoding="utf-8")
            result = self.run_ratchet(
                "gate",
                "--toolchain",
                str(toolchain),
                "--main-config",
                str(main_config),
                "--loose-config",
                str(loose_config),
                "--registry",
                str(registry),
                "--manifest",
                str(local_root / "current.json"),
                "--evidence",
                str(local_root / "evidence.json"),
                "--slopo-source",
                "/definitely/missing/slopo",
                "--ollama-url",
                "http://127.0.0.1:1",
            )

            finding = json.loads(result.stdout)["findings"][0]
            self.assertEqual(result.returncode, 2)
            self.assertEqual(finding["code"], "contract_error")
            self.assertIn("registry.schema_version", finding["message"])

    def test_snapshot_command_keeps_initial_baseline_workflow_separate_from_gate(self) -> None:
        result = self.run_ratchet("snapshot", "--help")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("--registry", result.stdout)
        self.assertIn("--manifest", result.stdout)

    def test_snapshot_rejects_unsafe_and_aliased_outputs_before_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            repository = temporary_path / "repository"
            toolchain, main_config, loose_config = write_profile_fixture(repository)
            local_root = repository / ".slopo" / "local"
            local_root.mkdir()

            outside_manifest = temporary_path / "outside-current.json"
            outside_evidence = temporary_path / "outside-evidence.json"
            outside = self.run_ratchet(
                "snapshot",
                "--toolchain",
                str(toolchain),
                "--main-config",
                str(main_config),
                "--loose-config",
                str(loose_config),
                "--manifest",
                str(outside_manifest),
                "--evidence",
                str(outside_evidence),
                "--slopo-source",
                "/definitely/missing/slopo",
                "--ollama-url",
                "http://127.0.0.1:1",
            )
            self.assertEqual(outside.returncode, 2)
            self.assertEqual(json.loads(outside.stdout)["findings"][0]["code"], "unsafe_output_path")
            self.assertFalse(outside_manifest.exists())
            self.assertFalse(outside_evidence.exists())

            shared_output = local_root / "shared.json"
            collided = self.run_ratchet(
                "snapshot",
                "--toolchain",
                str(toolchain),
                "--main-config",
                str(main_config),
                "--loose-config",
                str(loose_config),
                "--manifest",
                str(shared_output),
                "--evidence",
                str(shared_output),
                "--slopo-source",
                "/definitely/missing/slopo",
                "--ollama-url",
                "http://127.0.0.1:1",
            )
            self.assertEqual(collided.returncode, 2)
            self.assertEqual(json.loads(collided.stdout)["findings"][0]["code"], "output_path_collision")
            self.assertFalse(shared_output.exists())

            protected_bytes = main_config.read_bytes()
            aliased_manifest = local_root / "aliased-current.json"
            aliased_manifest.symlink_to(main_config)
            aliased = self.run_ratchet(
                "snapshot",
                "--toolchain",
                str(toolchain),
                "--main-config",
                str(main_config),
                "--loose-config",
                str(loose_config),
                "--manifest",
                str(aliased_manifest),
                "--evidence",
                str(local_root / "safe-evidence.json"),
                "--slopo-source",
                "/definitely/missing/slopo",
                "--ollama-url",
                "http://127.0.0.1:1",
            )
            self.assertEqual(aliased.returncode, 2)
            self.assertEqual(json.loads(aliased.stdout)["findings"][0]["code"], "unsafe_output_path")
            self.assertEqual(main_config.read_bytes(), protected_bytes)

            symlink_target = temporary_path / "symlink-target"
            symlink_target.mkdir()
            (local_root / "linked-parent").symlink_to(symlink_target, target_is_directory=True)
            linked_parent = self.run_ratchet(
                "snapshot",
                "--toolchain",
                str(toolchain),
                "--main-config",
                str(main_config),
                "--loose-config",
                str(loose_config),
                "--manifest",
                str(local_root / "linked-parent" / "current.json"),
                "--evidence",
                str(local_root / "evidence.json"),
                "--slopo-source",
                "/definitely/missing/slopo",
                "--ollama-url",
                "http://127.0.0.1:1",
            )
            self.assertEqual(linked_parent.returncode, 2)
            self.assertEqual(json.loads(linked_parent.stdout)["findings"][0]["code"], "unsafe_output_path")
            self.assertFalse((symlink_target / "current.json").exists())

            local_alias = repository / "local-alias"
            local_alias.symlink_to(local_root, target_is_directory=True)
            aliased_parent = self.run_ratchet(
                "snapshot",
                "--toolchain",
                str(toolchain),
                "--main-config",
                str(main_config),
                "--loose-config",
                str(loose_config),
                "--manifest",
                str(local_alias / "current.json"),
                "--evidence",
                str(local_root / "evidence.json"),
                "--slopo-source",
                "/definitely/missing/slopo",
                "--ollama-url",
                "http://127.0.0.1:1",
            )
            self.assertEqual(aliased_parent.returncode, 2)
            self.assertEqual(json.loads(aliased_parent.stdout)["findings"][0]["code"], "unsafe_output_path")
            self.assertFalse((local_root / "current.json").exists())

            internal_collision = self.run_ratchet(
                "snapshot",
                "--toolchain",
                str(toolchain),
                "--main-config",
                str(main_config),
                "--loose-config",
                str(loose_config),
                "--manifest",
                str(local_root / "backend-go.db"),
                "--evidence",
                str(local_root / "evidence.json"),
                "--slopo-source",
                "/definitely/missing/slopo",
                "--ollama-url",
                "http://127.0.0.1:1",
            )
            self.assertEqual(internal_collision.returncode, 2)
            self.assertEqual(
                json.loads(internal_collision.stdout)["findings"][0]["code"],
                "output_path_collision",
            )
            self.assertFalse((local_root / "backend-go.db").exists())

    def test_snapshot_rejects_source_bytes_changed_during_the_native_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            repository = temporary_path / "repository"
            initialize_repository(repository)
            source_file = repository / "internal" / "rule.py"
            source_file.parent.mkdir(parents=True)
            source_file.write_text("def rule():\n    return 'committed'\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repository), "add", "internal/rule.py"], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repository),
                    "-c",
                    "user.name=Ratchet Contract",
                    "-c",
                    "user.email=ratchet@example.invalid",
                    "commit",
                    "--quiet",
                    "-m",
                    "scan fixture",
                ],
                check=True,
            )
            source_file.write_text("def rule():\n    return 'before scan'\n", encoding="utf-8")
            toolchain_path, main_config, loose_config = write_profile_fixture(repository)

            slopo_source = temporary_path / "slopo"
            slopo_source.mkdir()
            subprocess.run(["git", "init", "--quiet", str(slopo_source)], check=True)
            (slopo_source / "marker.txt").write_text("fixture\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(slopo_source), "add", "marker.txt"], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(slopo_source),
                    "-c",
                    "user.name=Ratchet Contract",
                    "-c",
                    "user.email=ratchet@example.invalid",
                    "commit",
                    "--quiet",
                    "-m",
                    "slopo fixture",
                ],
                check=True,
            )
            slopo_origin = "https://example.invalid/local-slopo.git"
            subprocess.run(["git", "-C", str(slopo_source), "remote", "add", "origin", slopo_origin], check=True)
            slopo_revision = subprocess.check_output(
                ["git", "-C", str(slopo_source), "rev-parse", "HEAD"],
                text=True,
            ).strip()
            toolchain = json.loads(toolchain_path.read_text(encoding="utf-8"))
            toolchain["slopo"]["git_revision"] = slopo_revision
            toolchain["slopo"]["git_origin"] = slopo_origin
            toolchain_path.write_text(json.dumps(toolchain), encoding="utf-8")

            fake_bin = temporary_path / "bin"
            fake_bin.mkdir()
            write_executable(
                fake_bin / "uv",
                '''
import json
import sqlite3
import sys
from pathlib import Path

arguments = sys.argv[1:]
if arguments[-1] == "--version":
    print("Slopo 0.5.1")
elif "python" in arguments:
    print(json.dumps(["rule.py"], separators=(",", ":")))
elif arguments[-1] == "index":
    root = Path.cwd()
    source = root / "internal" / "rule.py"
    database = root / ".slopo" / "local" / "backend-go.db"
    with sqlite3.connect(database) as connection:
        connection.executescript("""
        CREATE TABLE files (id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, mtime REAL NOT NULL);
        CREATE TABLE code_units (
            id INTEGER PRIMARY KEY,
            file_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            start_line INTEGER NOT NULL,
            end_line INTEGER NOT NULL,
            body_hash TEXT NOT NULL
        );
        """)
        connection.execute("INSERT INTO files (path, mtime) VALUES (?, ?)", ("rule.py", source.stat().st_mtime))
    print("Indexed 0 code units from 1 files (0 unchanged, 0 removed).")
elif arguments[-1] == "embed":
    source = Path.cwd() / "internal" / "rule.py"
    source.write_text("def rule():\\n    return 'during scan'\\n", encoding="utf-8")
elif arguments[-1] == "analyze":
    print("No similar pairs found.")
else:
    print("Configuration loaded.")
''',
            )
            inventory = {
                "models": [
                    {
                        "name": toolchain["ollama"]["installed_model"],
                        "digest": toolchain["ollama"]["model_digest"],
                    }
                ]
            }
            status_before = subprocess.check_output(
                ["git", "-C", str(repository), "status", "--porcelain=v1", "-z", "--untracked-files=all"]
            )
            local_root = repository / ".slopo" / "local"
            manifest = local_root / "current.json"
            evidence = local_root / "evidence.json"
            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
            with serve_model_inventory(inventory) as ollama_url:
                result = self.run_ratchet(
                    "snapshot",
                    "--toolchain",
                    str(toolchain_path),
                    "--main-config",
                    str(main_config),
                    "--loose-config",
                    str(loose_config),
                    "--manifest",
                    str(manifest),
                    "--evidence",
                    str(evidence),
                    "--slopo-source",
                    str(slopo_source),
                    "--ollama-url",
                    ollama_url,
                    environment=environment,
                )
            status_after = subprocess.check_output(
                ["git", "-C", str(repository), "status", "--porcelain=v1", "-z", "--untracked-files=all"]
            )

            self.assertEqual(status_after, status_before)
            self.assertEqual(result.returncode, 2, result.stdout)
            self.assertEqual(json.loads(result.stdout)["findings"][0]["code"], "source_changed_during_scan")
            self.assertEqual(json.loads(evidence.read_text(encoding="utf-8"))["status"], "error")
            self.assertNotEqual(source_file.read_text(encoding="utf-8"), "def rule():\n    return 'before scan'\n")
            self.assertFalse((local_root / "backend-go.db.cache.json").exists())

    def test_gate_and_proposal_outputs_cannot_alias_versioned_or_local_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory) / "repository"
            initialize_repository(repository)
            toolchain, main_config, loose_config = write_profile_fixture(repository)
            local_root = repository / ".slopo" / "local"
            local_root.mkdir()
            source_metadata = ratchet._source_metadata(repository, {"tracked.txt"})
            manifest_document = json.loads((FIXTURE_DIR / "current.json").read_text(encoding="utf-8"))
            manifest_document.update(source_metadata)
            manifest = local_root / "current.json"
            manifest.write_text(json.dumps(manifest_document), encoding="utf-8")
            registry = repository / ".slopo" / "accepted.json"
            shutil.copyfile(FIXTURE_DIR / "accepted.json", registry)

            aliased_manifest = local_root / "registry-alias.json"
            aliased_manifest.symlink_to(registry)
            gate = self.run_ratchet(
                "gate",
                "--toolchain",
                str(toolchain),
                "--main-config",
                str(main_config),
                "--loose-config",
                str(loose_config),
                "--registry",
                str(registry),
                "--manifest",
                str(aliased_manifest),
                "--evidence",
                str(local_root / "gate-evidence.json"),
                "--slopo-source",
                "/definitely/missing/slopo",
                "--ollama-url",
                "http://127.0.0.1:1",
            )
            self.assertEqual(gate.returncode, 2)
            self.assertEqual(json.loads(gate.stdout)["findings"][0]["code"], "unsafe_output_path")

            before = manifest.read_bytes()
            proposal = self.run_ratchet(
                "propose-baseline",
                "--repository-root",
                str(repository),
                "--manifest",
                str(manifest),
                "--registry",
                str(registry),
                "--output",
                str(manifest),
                "--replace",
            )
            self.assertEqual(proposal.returncode, 2)
            self.assertEqual(json.loads(proposal.stdout)["findings"][0]["code"], "output_path_collision")
            self.assertEqual(manifest.read_bytes(), before)

            outside_output = repository.parent / "outside-proposal.json"
            outside = self.run_ratchet(
                "propose-baseline",
                "--repository-root",
                str(repository),
                "--manifest",
                str(manifest),
                "--registry",
                str(registry),
                "--output",
                str(outside_output),
            )
            self.assertEqual(outside.returncode, 2)
            self.assertEqual(json.loads(outside.stdout)["findings"][0]["code"], "unsafe_output_path")
            self.assertFalse(outside_output.exists())

            toolchain_before = toolchain.read_bytes()
            toolchain_alias = local_root / "toolchain-alias.json"
            os.link(toolchain, toolchain_alias)
            protected = self.run_ratchet(
                "propose-baseline",
                "--repository-root",
                str(repository),
                "--manifest",
                str(manifest),
                "--registry",
                str(registry),
                "--output",
                str(toolchain_alias),
                "--replace",
            )
            self.assertEqual(protected.returncode, 2)
            self.assertEqual(json.loads(protected.stdout)["findings"][0]["code"], "output_path_collision")
            self.assertEqual(toolchain.read_bytes(), toolchain_before)

    def test_proposal_rejects_protected_local_artifacts_without_modifying_them(self) -> None:
        protected_names = (
            "backend-go-current.json",
            "backend-go-evidence.json",
            "backend-go.db",
            "backend-go.db.cache.json",
            "backend-go-report",
            "backend-go-report-loose",
            "backend-go-slopo.log",
            "backend-go.lock",
        )
        for protected_name in protected_names:
            with self.subTest(protected_name=protected_name), tempfile.TemporaryDirectory() as temporary_directory:
                repository = Path(temporary_directory) / "repository"
                initialize_repository(repository)
                local_root = repository / ".slopo" / "local"
                local_root.mkdir(parents=True)
                manifest = local_root / "current.json"
                manifest.write_text("{}", encoding="utf-8")
                protected = local_root / protected_name
                protected.write_bytes(b"protected ratchet artifact")
                before = protected.read_bytes()

                result = self.run_ratchet(
                    "propose-baseline",
                    "--repository-root",
                    str(repository),
                    "--manifest",
                    str(manifest),
                    "--output",
                    str(protected),
                    "--replace",
                )

                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertEqual(json.loads(result.stdout)["findings"][0]["code"], "output_path_collision")
                self.assertEqual(protected.read_bytes(), before)

    def test_parallel_cli_commands_cannot_mix_local_ratchet_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory) / "repository"
            source_metadata = initialize_repository(repository)
            local_root = repository / ".slopo" / "local"
            local_root.mkdir(parents=True)
            manifest_document = json.loads((FIXTURE_DIR / "current.json").read_text(encoding="utf-8"))
            manifest_document.update(source_metadata)
            manifest_pipe = local_root / "current.pipe"
            os.mkfifo(manifest_pipe, 0o600)

            def command(output_name: str) -> list[str]:
                return [
                    sys.executable,
                    str(RATCHET),
                    "propose-baseline",
                    "--repository-root",
                    str(repository),
                    "--manifest",
                    str(manifest_pipe),
                    "--output",
                    str(local_root / output_name),
                ]

            first = subprocess.Popen(
                command("proposal-first.json"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            lock_path = local_root / "backend-go.lock"
            for _attempt in range(100):
                if lock_path.exists() or first.poll() is not None:
                    break
                time.sleep(0.01)

            second = subprocess.Popen(
                command("proposal-second.json"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            second_timed_out = False
            try:
                second_stdout, second_stderr = second.communicate(timeout=1)
            except subprocess.TimeoutExpired:
                second_timed_out = True
                second.kill()
                second_stdout, second_stderr = second.communicate(timeout=1)

            writer = os.open(manifest_pipe, os.O_WRONLY | os.O_NONBLOCK)
            try:
                os.write(writer, json.dumps(manifest_document).encode("utf-8"))
            finally:
                os.close(writer)
            first_stdout, first_stderr = first.communicate(timeout=3)

            self.assertFalse(second_timed_out, second_stderr)
            self.assertEqual(second.returncode, 2, second_stdout)
            self.assertEqual(json.loads(second_stdout)["findings"][0]["code"], "ratchet_busy")
            self.assertEqual(first.returncode, 0, first_stderr)
            self.assertEqual(json.loads(first_stdout)["status"], "proposed")
            self.assertTrue((local_root / "proposal-first.json").is_file())
            self.assertFalse((local_root / "proposal-second.json").exists())
            json.loads((local_root / "proposal-first.json").read_text(encoding="utf-8"))
            self.assertEqual(local_root.stat().st_mode & 0o777, 0o700)
            self.assertEqual(lock_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual((local_root / "proposal-first.json").stat().st_mode & 0o777, 0o600)

    def test_cli_rejects_a_symlink_in_place_of_the_shared_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory) / "repository"
            source_metadata = initialize_repository(repository)
            local_root = repository / ".slopo" / "local"
            local_root.mkdir(parents=True)
            manifest_document = json.loads((FIXTURE_DIR / "current.json").read_text(encoding="utf-8"))
            manifest_document.update(source_metadata)
            manifest = local_root / "current.json"
            manifest.write_text(json.dumps(manifest_document), encoding="utf-8")
            protected = repository / "protected.txt"
            protected.write_text("must stay intact\n", encoding="utf-8")
            (local_root / "backend-go.lock").symlink_to(protected)

            result = self.run_ratchet(
                "propose-baseline",
                "--repository-root",
                str(repository),
                "--manifest",
                str(manifest),
                "--output",
                str(local_root / "proposal.json"),
            )

            self.assertEqual(result.returncode, 2, result.stdout)
            self.assertEqual(json.loads(result.stdout)["findings"][0]["code"], "unsafe_lock_path")
            self.assertEqual(protected.read_text(encoding="utf-8"), "must stay intact\n")
            self.assertFalse((local_root / "proposal.json").exists())

    def test_line_shift_does_not_change_exact_or_occurrence_fingerprints(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            repository = temporary_path / "repository"
            initialize_repository(repository)
            write_source_files(repository, ["core/a.py", "core/b.py", "core/c.py", "core/d.py"])
            reports = temporary_path / "reports"
            reports.mkdir()
            (reports / "index.md").write_text("# Slopo report\n", encoding="utf-8")
            fingerprints: list[tuple[str, tuple[str, ...]]] = []
            for shift in (0, 100):
                database = temporary_path / f"slopo-{shift}.db"
                with sqlite3.connect(database) as connection:
                    connection.executescript((FIXTURE_DIR / "native.sql").read_text(encoding="utf-8"))
                    connection.execute(
                        "UPDATE code_units SET start_line = start_line + ?, end_line = end_line + ?",
                        (shift, shift),
                    )
                manifest_path = temporary_path / f"manifest-{shift}.json"
                result = self.run_ratchet(
                    "collect-manifest",
                    "--db",
                    str(database),
                    "--main-report",
                    str(reports),
                    "--loose-report",
                    str(reports),
                    "--profile-fingerprint",
                    "sha256:" + "d" * 64,
                    "--repository-root",
                    str(repository),
                    "--source-directory",
                    str(repository),
                    "--output",
                    str(manifest_path),
                )
                self.assertEqual(result.returncode, 0, result.stdout)
                exact = next(
                    cluster
                    for cluster in json.loads(manifest_path.read_text(encoding="utf-8"))["clusters"]
                    if cluster["kind"] == "exact"
                )
                fingerprints.append(
                    (
                        exact["fingerprint"],
                        tuple(sorted(member["fingerprint"] for member in exact["members"])),
                    )
                )

            self.assertEqual(fingerprints[0], fingerprints[1])

    def test_merge_and_split_cluster_changes_stay_blocked(self) -> None:
        def semantic_cluster(body_hashes: list[str]) -> dict[str, object]:
            members = [
                {
                    "path": f"core/{body_hash}.py",
                    "symbol": f"rule_{body_hash}",
                    "body_hash": body_hash * 64,
                    "ordinal": 1,
                }
                for body_hash in body_hashes
            ]
            return cast(
                dict[str, object],
                ratchet._normalize_cluster(
                    {"kind": "semantic", "tier": "main", "members": members},
                    "test cluster",
                    accepted=False,
                ),
            )

        profile = "sha256:" + "d" * 64
        left = semantic_cluster(["a", "b"])
        right = semantic_cluster(["c", "d"])
        merged = semantic_cluster(["a", "b", "c", "d"])

        merge_findings = ratchet._compare(
            {"profile_fingerprint": profile, "clusters": [merged]},
            {"profile_fingerprint": profile, "clusters": [left, right]},
        )
        split_findings = ratchet._compare(
            {"profile_fingerprint": profile, "clusters": [left, right]},
            {"profile_fingerprint": profile, "clusters": [merged]},
        )

        self.assertTrue(merge_findings)
        self.assertTrue(split_findings)
        self.assertIn("ambiguous_cluster_change", {finding["code"] for finding in merge_findings})

    def test_analyzer_requires_explicit_terminal_outcome_before_empty_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            report = Path(temporary_directory) / "report"
            report.mkdir()

            with self.assertRaises(ratchet.ContractError) as incomplete:
                ratchet._finalize_analyze_report(report, "Calculating similarity...\n")
            self.assertEqual(incomplete.exception.code, "slopo_analyze_incomplete")
            self.assertFalse((report / "index.md").exists())

            ratchet._finalize_analyze_report(report, "No similar pairs found.\n")
            self.assertTrue((report / "index.md").is_file())

            (report / "index.md").unlink()
            ratchet._finalize_analyze_report(report, "No embedded code units found. Run `embed` first.\n")
            self.assertTrue((report / "index.md").is_file())

            (report / "index.md").unlink()
            with self.assertRaises(ratchet.ContractError) as missing_report:
                ratchet._finalize_analyze_report(
                    report,
                    "Report written to .slopo/local/missing directory.\n",
                )
            self.assertEqual(missing_report.exception.code, "slopo_analyze_incomplete")

    def test_analyzer_report_index_matches_files_and_has_one_terminal_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            report = Path(temporary_directory) / "report"
            report.mkdir()
            index = report / "index.md"
            index.write_text(
                "| Cluster | Hash |\n|---|---|\n| [Cluster 1](cluster-001.md) | abc |\n",
                encoding="utf-8",
            )
            (report / "cluster-001.md").write_text("# Cluster 1\n", encoding="utf-8")
            completed = "Report written to .slopo/local/report directory.\n"

            ratchet._finalize_analyze_report(report, completed)

            (report / "cluster-002.md").write_text("# Unreferenced\n", encoding="utf-8")
            with self.assertRaises(ratchet.ContractError) as extra:
                ratchet._finalize_analyze_report(report, completed)
            self.assertEqual(extra.exception.code, "slopo_analyze_incomplete")

            (report / "cluster-002.md").unlink()
            index.write_text(
                index.read_text(encoding="utf-8") + "| [Cluster 2](cluster-002.md) | def |\n",
                encoding="utf-8",
            )
            with self.assertRaises(ratchet.ContractError) as missing:
                ratchet._finalize_analyze_report(report, completed)
            self.assertEqual(missing.exception.code, "slopo_analyze_incomplete")

            index.write_text(
                "| Cluster | Hash |\n|---|---|\n| [Cluster 1](cluster-001.md) | abc |\n",
                encoding="utf-8",
            )
            with self.assertRaises(ratchet.ContractError) as ambiguous:
                ratchet._finalize_analyze_report(report, completed + "No similar pairs found.\n")
            self.assertEqual(ambiguous.exception.code, "slopo_analyze_incomplete")

    def test_index_completeness_rejects_parser_skip_and_missing_or_stale_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            source = temporary_path / "source"
            source.mkdir()
            (source / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
            (source / "b.py").write_text("def b():\n    return 2\n", encoding="utf-8")

            parser_database = temporary_path / "parser.db"
            write_index_database(parser_database, source, ["a.py", "b.py"])
            with self.assertRaises(ratchet.ContractError) as parser_skip:
                ratchet._verify_index_completeness(
                    parser_database,
                    source,
                    {"a.py", "b.py"},
                    "Skipping a.py: invalid syntax\nIndexed 0 code units from 2 files (0 unchanged, 0 removed).\n",
                )
            self.assertEqual(parser_skip.exception.code, "slopo_parser_skipped")

            incomplete_database = temporary_path / "incomplete.db"
            write_index_database(incomplete_database, source, ["a.py"])
            with self.assertRaises(ratchet.ContractError) as incomplete:
                ratchet._verify_index_completeness(
                    incomplete_database,
                    source,
                    {"a.py", "b.py"},
                    "Indexed 0 code units from 2 files (0 unchanged, 0 removed).\n",
                )
            self.assertEqual(incomplete.exception.code, "slopo_index_incomplete")

            (source / "stale.py").write_text("def stale():\n    return 3\n", encoding="utf-8")
            stale_database = temporary_path / "stale.db"
            write_index_database(stale_database, source, ["a.py", "stale.py"])
            with self.assertRaises(ratchet.ContractError) as stale:
                ratchet._verify_index_completeness(
                    stale_database,
                    source,
                    {"a.py"},
                    "Indexed 0 code units from 1 files (0 unchanged, 1 removed).\n",
                )
            self.assertEqual(stale.exception.code, "slopo_index_scope_mismatch")

            mtime_database = temporary_path / "mtime.db"
            write_index_database(mtime_database, source, ["a.py"])
            with sqlite3.connect(mtime_database) as connection:
                connection.execute("UPDATE files SET mtime = -1")
            with self.assertRaises(ratchet.ContractError) as stale_mtime:
                ratchet._verify_index_completeness(
                    mtime_database,
                    source,
                    {"a.py"},
                    "Indexed 0 code units from 1 files (0 unchanged, 0 removed).\n",
                )
            self.assertEqual(stale_mtime.exception.code, "slopo_index_stale")

    def test_existing_index_is_forced_to_reparse_before_completeness_check(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            source = temporary_path / "source"
            source.mkdir()
            (source / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
            database = temporary_path / "slopo.db"
            write_index_database(database, source, ["a.py"])

            ratchet._force_full_reparse(database)

            with sqlite3.connect(database) as connection:
                self.assertEqual(connection.execute("SELECT mtime FROM files").fetchone()[0], -1.0)

    def test_changed_slopo_revision_discards_the_previous_index_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory) / "repository"
            repository.mkdir()
            (repository / ".slopo").mkdir()
            local_root = repository / ".slopo" / "local"
            local_root.mkdir()
            database = local_root / "backend-go.db"
            marker = local_root / "backend-go.db.cache.json"
            database.write_bytes(b"previous parser database")
            marker.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "profile_fingerprint": "sha256:" + "a" * 64,
                        "slopo_revision": "1" * 40,
                    }
                ),
                encoding="utf-8",
            )

            with ratchet._exclusive_local_lock(repository):
                ratchet._prepare_index_cache(
                    database,
                    marker,
                    profile_fingerprint="sha256:" + "a" * 64,
                    slopo_revision="2" * 40,
                )

            self.assertFalse(database.exists())
            self.assertFalse(marker.exists())

            database.write_bytes(b"current parser database")
            with ratchet._exclusive_local_lock(repository):
                ratchet._write_index_cache_marker(
                    marker,
                    profile_fingerprint="sha256:" + "a" * 64,
                    slopo_revision="2" * 40,
                )
                ratchet._prepare_index_cache(
                    database,
                    marker,
                    profile_fingerprint="sha256:" + "a" * 64,
                    slopo_revision="2" * 40,
                )

            self.assertEqual(database.read_bytes(), b"current parser database")
            self.assertEqual(marker.stat().st_mode & 0o777, 0o600)

    def test_native_command_has_timeout_local_model_env_and_safe_bounded_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            log_path = temporary_path / "native.log"
            config_path = temporary_path / "config.yaml"
            config_path.write_text("source_dir: core\n", encoding="utf-8")
            write_executable(
                temporary_path / "uv",
                """
import os
import sys

print(os.environ.get("OLLAMA_API_BASE", "missing"))
print(" ".join(sys.argv[1:]))
print("SECRET_SOURCE_BODY", file=sys.stderr)
raise SystemExit(3)
""",
            )

            with (
                mock.patch.dict(os.environ, {"PATH": f"{temporary_path}:{os.environ['PATH']}"}),
                self.assertRaises(ratchet.ContractError) as failed,
            ):
                ratchet._run_native_slopo(
                    temporary_path / "slopo",
                    temporary_path,
                    config_path,
                    "index",
                    "index",
                    log_path,
                    7,
                    "http://127.0.0.1:11434",
                )

            self.assertEqual(failed.exception.code, "slopo_execution_failed")
            self.assertIn("--offline --locked --project", log_path.read_text(encoding="utf-8"))
            self.assertEqual(
                set(failed.exception.evidence),
                {"command", "exit_code", "output_digest", "log_path"},
            )
            self.assertIn("http://127.0.0.1:11434", log_path.read_text(encoding="utf-8"))
            self.assertNotIn("SECRET_SOURCE_BODY", str(failed.exception))
            self.assertNotIn("SECRET_SOURCE_BODY", json.dumps(failed.exception.evidence))

    def test_native_command_output_overflow_fails_without_exposing_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            log_path = temporary_path / "native.log"
            config_path = temporary_path / "config.yaml"
            config_path.write_text("source_dir: core\n", encoding="utf-8")
            sentinel = temporary_path / "child-finished"
            write_executable(
                temporary_path / "uv",
                """
import os
import sys
import time
from pathlib import Path

sys.stdout.buffer.write(b"x" * 2048)
sys.stdout.buffer.flush()
time.sleep(1.5)
Path(os.environ["RATCHET_SENTINEL"]).write_text("finished", encoding="utf-8")
""",
            )

            with (
                mock.patch.object(ratchet, "MAX_NATIVE_COMMAND_OUTPUT_BYTES", 1024),
                mock.patch.dict(
                    os.environ,
                    {
                        "PATH": f"{temporary_path}:{os.environ['PATH']}",
                        "RATCHET_SENTINEL": str(sentinel),
                    },
                ),
                self.assertRaises(ratchet.ContractError) as overflow,
            ):
                started = time.monotonic()
                ratchet._run_native_slopo(
                    temporary_path / "slopo",
                    temporary_path,
                    config_path,
                    "index",
                    "index",
                    log_path,
                    7,
                    "http://127.0.0.1:11434",
                )
            elapsed = time.monotonic() - started

            self.assertEqual(overflow.exception.code, "slopo_output_too_large")
            self.assertLess(elapsed, 1.0)
            self.assertFalse(sentinel.exists())
            self.assertLessEqual(log_path.stat().st_size, 1200)
            self.assertNotIn("xxx", str(overflow.exception))

    def test_native_command_timeout_terminates_the_real_child(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            sentinel = temporary_path / "child-finished"
            log_path = temporary_path / "native.log"
            config_path = temporary_path / "config.yaml"
            config_path.write_text("source_dir: core\n", encoding="utf-8")
            write_executable(
                temporary_path / "uv",
                """
import os
import time
from pathlib import Path

time.sleep(2)
Path(os.environ["RATCHET_SENTINEL"]).write_text("finished", encoding="utf-8")
""",
            )
            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "PATH": f"{temporary_path}:{os.environ['PATH']}",
                        "RATCHET_SENTINEL": str(sentinel),
                    },
                ),
                self.assertRaises(ratchet.ContractError) as timed_out,
            ):
                started = time.monotonic()
                ratchet._run_native_slopo(
                    temporary_path / "slopo",
                    temporary_path,
                    config_path,
                    "index",
                    "index",
                    log_path,
                    1,
                    "http://127.0.0.1:11434",
                )
            elapsed = time.monotonic() - started

            self.assertEqual(timed_out.exception.code, "slopo_execution_timeout")
            self.assertLess(elapsed, 1.5)
            self.assertFalse(sentinel.exists())

    def test_preflight_process_output_is_bounded_while_the_child_is_running(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            sentinel = temporary_path / "child-finished"
            script = (
                "import sys,time; from pathlib import Path; "
                "sys.stdout.buffer.write(b'x'*2048); sys.stdout.buffer.flush(); "
                f"time.sleep(1.5); Path({str(sentinel)!r}).write_text('finished')"
            )

            started = time.monotonic()
            with self.assertRaises(ratchet.ContractError) as overflow:
                ratchet._run_process(
                    [sys.executable, "-c", script],
                    timeout_seconds=5,
                    max_output_bytes=1024,
                )
            elapsed = time.monotonic() - started

            self.assertIn("exceeded the limit", str(overflow.exception))
            self.assertLess(elapsed, 1.0)
            self.assertFalse(sentinel.exists())

    def test_toolchain_model_must_match_profile_before_runtime_access(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            toolchain = json.loads(TOOLCHAIN.read_text(encoding="utf-8"))
            toolchain["ollama"]["slopo_model"] = "ollama/another-model"
            changed_toolchain = Path(temporary_directory) / "toolchain.json"
            changed_toolchain.write_text(json.dumps(toolchain), encoding="utf-8")

            result = self.run_ratchet(
                "preflight",
                "--toolchain",
                str(changed_toolchain),
                "--main-config",
                str(MAIN_CONFIG),
                "--loose-config",
                str(LOOSE_CONFIG),
                "--slopo-source",
                "/definitely/missing/slopo",
                "--ollama-url",
                "http://127.0.0.1:1",
            )

            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)["findings"][0]["code"], "model_profile_mismatch")

    def test_profile_model_must_match_the_installed_model_bound_to_the_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            main_config = temporary_path / "main.yaml"
            loose_config = temporary_path / "loose.yaml"
            for source, target in ((MAIN_CONFIG, main_config), (LOOSE_CONFIG, loose_config)):
                target.write_text(
                    source.read_text(encoding="utf-8").replace(
                        "ollama/unclemusclez/jina-embeddings-v2-base-code",
                        "ollama/example/profile-a",
                    ),
                    encoding="utf-8",
                )
            toolchain = json.loads(TOOLCHAIN.read_text(encoding="utf-8"))
            toolchain["ollama"]["slopo_model"] = "ollama/example/profile-a"
            toolchain["ollama"]["installed_model"] = "example/model-b:latest"
            toolchain["profiles"]["main"]["sha256"] = ratchet._sha256_file(main_config, "main config")
            toolchain["profiles"]["loose"]["sha256"] = ratchet._sha256_file(loose_config, "loose config")
            toolchain_path = temporary_path / "toolchain.json"
            toolchain_path.write_text(json.dumps(toolchain), encoding="utf-8")

            result = self.run_ratchet(
                "preflight",
                "--toolchain",
                str(toolchain_path),
                "--main-config",
                str(main_config),
                "--loose-config",
                str(loose_config),
                "--slopo-source",
                "/definitely/missing/slopo",
                "--ollama-url",
                "http://127.0.0.1:1",
            )

            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)["findings"][0]["code"], "model_binding_mismatch")

    def test_ollama_latest_tag_is_normalized_before_digest_verification(self) -> None:
        toolchain = json.loads(TOOLCHAIN.read_text(encoding="utf-8"))
        inventory = {
            "models": [
                {
                    "name": "unclemusclez/jina-embeddings-v2-base-code",
                    "digest": toolchain["ollama"]["model_digest"],
                }
            ]
        }
        with serve_model_inventory(inventory) as ollama_url:
            result = self.run_ratchet(
                "preflight",
                "--toolchain",
                str(TOOLCHAIN),
                "--main-config",
                str(MAIN_CONFIG),
                "--loose-config",
                str(LOOSE_CONFIG),
                "--slopo-source",
                "/definitely/missing/slopo",
                "--ollama-url",
                ollama_url,
            )

        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["findings"][0]["code"], "missing_slopo_source")

    def test_toolchain_requires_named_finite_native_timeouts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            toolchain = json.loads(TOOLCHAIN.read_text(encoding="utf-8"))
            toolchain["runtime"]["native_timeouts_seconds"]["index"] = 0
            changed_toolchain = Path(temporary_directory) / "toolchain.json"
            changed_toolchain.write_text(json.dumps(toolchain), encoding="utf-8")

            result = self.run_ratchet(
                "preflight",
                "--toolchain",
                str(changed_toolchain),
                "--main-config",
                str(MAIN_CONFIG),
                "--loose-config",
                str(LOOSE_CONFIG),
                "--slopo-source",
                "/definitely/missing/slopo",
                "--ollama-url",
                "http://127.0.0.1:1",
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn(
                "native_timeouts_seconds.index",
                json.loads(result.stdout)["findings"][0]["message"],
            )

    def test_native_slopo_indexes_short_helpers_and_data_only_dataclasses_at_pinned_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory) / "repository"
            initialize_repository(repository)
            source_directory = repository / "core"
            source_directory.mkdir()
            short_helper = (
                "MONEY_SCALE = 100\n\n"
                "def _rub_to_kopecks(amount_rub: int) -> int:\n"
                "    return int(amount_rub * MONEY_SCALE)\n"
            )
            (source_directory / "first.py").write_text(short_helper, encoding="utf-8")
            (source_directory / "second.py").write_text(short_helper, encoding="utf-8")
            data_shape = "@dataclass\nclass AgeGroupSpec:\n    minimum_age: int\n    maximum_age: int = 99\n"
            (source_directory / "first_spec.py").write_text(data_shape, encoding="utf-8")
            (source_directory / "second_spec.py").write_text(data_shape, encoding="utf-8")
            slopo_root = repository / ".slopo"
            local_root = slopo_root / "local"
            local_root.mkdir(parents=True)
            config = slopo_root / "short-helpers.yaml"
            config.write_text(
                MAIN_CONFIG.read_text(encoding="utf-8")
                .replace('source_dir: "internal"', 'source_dir: "core"')
                .replace(
                    'db_file: ".slopo/local/backend-go.db"',
                    'db_file: ".slopo/local/short-helpers.db"',
                )
                .replace(
                    'report_dir: ".slopo/local/backend-go-report"',
                    'report_dir: ".slopo/local/short-helpers-report"',
                )
                .replace(
                    'ignore_file: ".slopo/backend-go.ignore.txt"',
                    'ignore_file: ".slopo/short-helpers.ignore.txt"',
                ),
                encoding="utf-8",
            )
            (slopo_root / "short-helpers.ignore.txt").write_text("", encoding="utf-8")
            slopo_source = Path(os.environ.get("SLOPO_SOURCE", str(Path(os.environ["SLOPO_SOURCE"]))))

            indexed = subprocess.run(
                [
                    "uv",
                    "run",
                    "--offline",
                    "--locked",
                    "--project",
                    str(slopo_source),
                    "slopo",
                    "--config",
                    str(config),
                    "index",
                ],
                cwd=repository,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            self.assertEqual(indexed.returncode, 0, indexed.stderr)
            database = local_root / "short-helpers.db"
            with sqlite3.connect(database) as connection:
                indexed_units = connection.execute(
                    "SELECT name, body_node_count, body_hash FROM code_units ORDER BY name, id"
                ).fetchall()
            self.assertEqual(len(indexed_units), 4)
            units_by_name: dict[str, list[tuple[str, int, str]]] = {}
            for row in indexed_units:
                units_by_name.setdefault(row[0], []).append(row)
            self.assertEqual(set(units_by_name), {"AgeGroupSpec", "_rub_to_kopecks"})
            self.assertEqual({row[1] for row in units_by_name["_rub_to_kopecks"]}, {8})
            self.assertGreaterEqual(min(row[1] for row in units_by_name["AgeGroupSpec"]), 8)
            self.assertTrue(all(len({row[2] for row in rows}) == 1 for rows in units_by_name.values()))

            reports = []
            for name in ("main-report", "loose-report"):
                report = local_root / name
                report.mkdir()
                (report / "index.md").write_text("# Slopo report\n", encoding="utf-8")
                reports.append(report)
            manifest = local_root / "current.json"
            collected = self.run_ratchet(
                "collect-manifest",
                "--db",
                str(database),
                "--main-report",
                str(reports[0]),
                "--loose-report",
                str(reports[1]),
                "--profile-fingerprint",
                "sha256:" + "d" * 64,
                "--repository-root",
                str(repository),
                "--source-directory",
                str(source_directory),
                "--output",
                str(manifest),
            )
            self.assertEqual(collected.returncode, 0, collected.stdout)
            self.assertEqual(json.loads(collected.stdout)["exact_cluster_count"], 2)
            registry = slopo_root / "empty-accepted.json"
            registry.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "profile_fingerprint": "sha256:" + "d" * 64,
                        "accepted_clusters": [],
                    }
                ),
                encoding="utf-8",
            )
            checked = self.run_ratchet(
                "check",
                "--manifest",
                str(manifest),
                "--registry",
                str(registry),
            )

            self.assertEqual(checked.returncode, 1, checked.stdout)
            check_result = json.loads(checked.stdout)
            self.assertEqual(check_result["finding_count"], 2)
            self.assertEqual({finding["code"] for finding in check_result["findings"]}, {"new_cluster"})

    def test_preflight_rejects_unpinned_profile_before_runtime_access(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            changed_config = Path(temporary_directory) / "backend-go.yaml"
            changed_config.write_text(
                MAIN_CONFIG.read_text(encoding="utf-8").replace(
                    "similarity_threshold: 0.92",
                    "similarity_threshold: 0.91",
                ),
                encoding="utf-8",
            )
            result = self.run_ratchet(
                "preflight",
                "--toolchain",
                str(TOOLCHAIN),
                "--main-config",
                str(changed_config),
                "--loose-config",
                str(LOOSE_CONFIG),
                "--slopo-source",
                "/definitely/missing/slopo",
                "--ollama-url",
                "http://127.0.0.1:1",
            )

        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["findings"][0]["code"], "profile_config_mismatch")


if __name__ == "__main__":
    unittest.main()
