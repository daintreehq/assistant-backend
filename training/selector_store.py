#!/usr/bin/env python3
"""Keep the selector's model files in a bucket, not in Git.

The weights (`*.onnx`), the tokenizer vocabularies and `vectors.npz` under
`src/daintree_assistant_server/runbooks/local/artifacts/selector/` used to be Git LFS, and
every retrain added ~225 MB to history that nothing ever needs again. They now live in a
private GCS bucket as objects named by their SHA-256, and Git carries only `selector.lock`:
the bucket plus each file's path, hash and size. The lock is the artifact's identity. A
commit names exactly one model, the build downloads exactly that and checks every byte
against it, and a retrain that forgets to publish fails `test_selector_lock.py`.

    selector_store.py fetch [--no-cache]   put the files the lock names in place (skips any
                                           that already match; a local cache serves worktrees)
    selector_store.py verify               check the files on disk against the lock
    selector_store.py publish [--bucket B] hash what is on disk, upload what the bucket lacks,
                                           rewrite the lock (run after `bench local` passes)
    selector_store.py prune [--delete]     list, or delete, bucket objects no lock names

Standard library only, so the Cloud Build step and a bare worktree can run it with whatever
`python3` they have; `gcloud storage` does the transfers with the caller's own credentials.
"""

from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "selector.lock"
ARTIFACT = (
    ROOT / "src" / "daintree_assistant_server" / "runbooks" / "local" / "artifacts" / "selector"
)
CACHE = Path(
    os.environ.get("DAINTREE_SELECTOR_CACHE", Path.home() / ".cache" / "daintree-selector")
)

#: The fp32 ranking graph exists only so the exporter can measure what int8 quantisation
#: cost; the server never loads it, so it is neither stored nor locked.
NOT_SHIPPED = {"ranking/model.onnx"}
#: Objects live under this prefix, one per distinct file content.
BLOBS = "blobs/"
CHUNK = 1 << 20


def is_stored(rel: str) -> bool:
    """Whether a file under the artifact directory belongs to the store rather than to Git."""
    name = rel.rsplit("/", 1)[-1]
    return rel not in NOT_SHIPPED and (
        name.endswith(".onnx") or name.endswith(".npz") or name == "tokenizer.json"
    )


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stored_files(directory: Path) -> list[str]:
    found = (p.relative_to(directory).as_posix() for p in directory.rglob("*") if p.is_file())
    return sorted(rel for rel in found if is_stored(rel))


def read_lock(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def lock_hashes(lock: dict[str, object]) -> set[str]:
    files = lock["files"]
    assert isinstance(files, dict)
    return {entry["sha256"] for entry in files.values()}


def write_lock(lock: dict[str, object], path: Path) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def check(lock: dict[str, object], directory: Path) -> list[str]:
    """Everything wrong with `directory` against `lock`; an empty list means it is the artifact."""
    problems: list[str] = []
    files = lock["files"]
    assert isinstance(files, dict)
    for rel, entry in sorted(files.items()):
        path = directory / rel
        if not path.is_file():
            problems.append(f"missing: {rel}")
        elif path.stat().st_size != entry["size"]:
            problems.append(f"size {path.stat().st_size}, lock says {entry['size']}: {rel}")
        elif sha256_of(path) != entry["sha256"]:
            problems.append(f"content differs from the lock: {rel}")
    return problems


class Store:
    """A bucket (`gs://name`) or, for tests, a plain directory with the same layout."""

    def __init__(self, bucket: str) -> None:
        self.bucket = bucket.rstrip("/")
        self.local = not bucket.startswith("gs://")

    def _url(self, sha: str) -> str:
        return f"{self.bucket}/{BLOBS}{sha}"

    def _gcloud(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["gcloud", "storage", *args], check=False, capture_output=True, text=True
        )

    def _must(self, *args: str) -> str:
        done = self._gcloud(*args)
        if done.returncode != 0:
            raise SystemExit(
                "gcloud storage {} failed:\n{}".format(" ".join(args), done.stderr.strip())
            )
        return done.stdout

    def has(self, sha: str) -> bool:
        if self.local:
            return (Path(self._url(sha))).is_file()
        return self._gcloud("ls", self._url(sha)).returncode == 0

    def put(self, source: Path, sha: str) -> None:
        if self.local:
            target = Path(self._url(sha))
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        else:
            self._must("cp", "--no-clobber", str(source), self._url(sha))

    def get(self, sha: str, destination: Path) -> None:
        if self.local:
            shutil.copyfile(self._url(sha), destination)
        else:
            self._must("cp", self._url(sha), str(destination))

    def objects(self) -> dict[str, float]:
        """Every stored object's hash with its age in seconds."""
        now = time.time()
        found: dict[str, float] = {}
        if self.local:
            root = Path(self.bucket) / BLOBS
            for path in root.glob("*") if root.is_dir() else []:
                found[path.name] = now - path.stat().st_mtime
            return found
        for line in self._must("ls", "-l", f"{self.bucket}/{BLOBS}").splitlines():
            parts = line.split()
            if len(parts) == 3 and parts[2].startswith("gs://"):
                created = calendar.timegm(time.strptime(parts[1], "%Y-%m-%dT%H:%M:%SZ"))
                found[parts[2].rsplit("/", 1)[-1]] = now - created
        return found

    def delete(self, sha: str) -> None:
        if self.local:
            Path(self._url(sha)).unlink()
        else:
            self._must("rm", self._url(sha))


def _clone(source: Path, target: Path) -> None:
    """Copy-on-write where the filesystem has it (APFS), a plain copy otherwise."""
    if (
        sys.platform == "darwin"
        and subprocess.run(
            ["cp", "-c", str(source), str(target)], check=False, capture_output=True
        ).returncode
        == 0
    ):
        return
    shutil.copyfile(source, target)


def fetch(lock: dict[str, object], directory: Path, use_cache: bool) -> int:
    store = Store(str(lock["bucket"]))
    files = lock["files"]
    assert isinstance(files, dict)
    for rel, entry in sorted(files.items()):
        target = directory / rel
        if target.is_file() and not check({"files": {rel: entry}}, directory):
            print(f"ok       {rel}")
            continue
        sha = entry["sha256"]
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = target.with_name(target.name + ".partial")
        cached = CACHE / sha
        if use_cache and cached.is_file() and cached.stat().st_size == entry["size"]:
            _clone(cached, staging)
            source = "cache"
        else:
            store.get(sha, staging)
            source = "bucket"
        if sha256_of(staging) != sha or staging.stat().st_size != entry["size"]:
            staging.unlink()
            raise SystemExit(f"{rel} from the {source} does not match the lock (hash or size)")
        if use_cache and source == "bucket":
            CACHE.mkdir(parents=True, exist_ok=True)
            _clone(staging, CACHE / (sha + ".partial"))
            (CACHE / (sha + ".partial")).replace(cached)
        staging.replace(target)
        print(f"fetched  {rel} ({source}, {entry['size']} bytes)")
    problems = check(lock, directory)
    if problems:
        raise SystemExit("\n".join(problems))
    return 0


def publish(bucket: str | None, directory: Path, lock_path: Path) -> int:
    existing = read_lock(lock_path) if lock_path.exists() else None
    bucket = bucket or (str(existing["bucket"]) if existing else None)
    if not bucket:
        raise SystemExit("no selector.lock yet: pass --bucket gs://<name>")
    store = Store(bucket)
    files: dict[str, dict[str, object]] = {}
    for rel in stored_files(directory):
        path = directory / rel
        files[rel] = {"sha256": sha256_of(path), "size": path.stat().st_size}
    if not files:
        raise SystemExit(f"no model files under {directory}")
    for rel, entry in files.items():
        sha = str(entry["sha256"])
        if store.has(sha):
            print(f"present  {rel}")
        else:
            store.put(directory / rel, sha)
            print("uploaded {} ({})".format(rel, entry["size"]))
    write_lock({"bucket": bucket, "files": files}, lock_path)
    print(f"wrote {lock_path.name}: commit it with the change that retrained the model")
    return 0


def _git_locks(lock_path: Path, keep: int) -> list[dict[str, object]]:
    """The lock at origin/main and at the `keep` commits before the latest that changed it."""
    revisions = ["origin/main"]
    log = subprocess.run(
        ["git", "log", "-n", str(keep + 1), "--format=%H", "--", lock_path.name],
        cwd=lock_path.parent,
        check=False,
        capture_output=True,
        text=True,
    )
    revisions += log.stdout.split()
    locks: list[dict[str, object]] = []
    for revision in revisions:
        shown = subprocess.run(
            ["git", "show", f"{revision}:{lock_path.name}"],
            cwd=lock_path.parent,
            check=False,
            capture_output=True,
            text=True,
        )
        if shown.returncode == 0:
            locks.append(json.loads(shown.stdout))
    return locks


def prune(delete: bool, keep_locks: int, min_age_hours: float, lock_path: Path) -> int:
    lock = read_lock(lock_path)
    keep = lock_hashes(lock)
    for older in _git_locks(lock_path, keep_locks):
        keep |= lock_hashes(older)
    store = Store(str(lock["bucket"]))
    doomed = []
    for sha, age in sorted(store.objects().items()):
        if sha in keep:
            continue
        if age < min_age_hours * 3600:
            print(f"young    {sha} (kept: under {min_age_hours:g} h old)")
            continue
        doomed.append(sha)
    for sha in doomed:
        if delete:
            store.delete(sha)
        print("{} {}".format("deleted " if delete else "would delete", sha))
    verdict = "deleted" if delete else "unreferenced (pass --delete)"
    print(f"{len(keep)} referenced, {len(doomed)} {verdict}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--dir", type=Path, default=ARTIFACT, help="the selector directory")
    parser.add_argument("--lock", type=Path, default=LOCK, help="the lock file")
    sub = parser.add_subparsers(dest="command", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--no-cache", action="store_true", help="skip the local download cache")
    sub.add_parser("verify")
    p = sub.add_parser("publish")
    p.add_argument("--bucket", help="gs://<name>; only needed before the first lock exists")
    r = sub.add_parser("prune")
    r.add_argument("--delete", action="store_true")
    r.add_argument(
        "--keep-previous",
        type=int,
        default=3,
        help="also keep this many models before the current one",
    )
    r.add_argument("--min-age-hours", type=float, default=24, help="never delete anything younger")
    args = parser.parse_args(argv)
    if args.command == "fetch":
        return fetch(read_lock(args.lock), args.dir, not args.no_cache)
    if args.command == "verify":
        problems = check(read_lock(args.lock), args.dir)
        print("\n".join(problems) if problems else "the files match selector.lock")
        return 1 if problems else 0
    if args.command == "publish":
        return publish(args.bucket, args.dir, args.lock)
    return prune(args.delete, args.keep_previous, args.min_age_hours, args.lock)


if __name__ == "__main__":
    sys.exit(main())
