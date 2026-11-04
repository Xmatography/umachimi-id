import json
import os
from pathlib import Path, PurePosixPath

import pygit2
from blake3 import blake3


INDEX_PATH = Path("index.json")
LOCALIZED_DATA_DIR = Path("localized_data")
BASE_REF = os.environ.get("INDEX_BASE_REF", "refs/remotes/origin/release")


def ls_tree(tree: pygit2.Tree, parent=Path(""), skip_trees=False):
    for entry in tree:
        path = parent / entry.name
        if isinstance(entry, pygit2.Tree):
            if not skip_trees:
                yield path
            yield from ls_tree(entry, path, skip_trees)
        else:
            yield path


def find_localized_tree(tree: pygit2.Tree):
    for entry in tree:
        if entry.name == LOCALIZED_DATA_DIR.name and isinstance(entry, pygit2.Tree):
            return entry
    return None


def is_indexed_path(path: str):
    return PurePosixPath(path).name != ".gitignore"


def tracked_localized_paths(tree: pygit2.Tree):
    return {
        path.as_posix()
        for path in ls_tree(tree, skip_trees=True)
        if is_indexed_path(path.as_posix())
    }


def load_cached_records(base_commit: pygit2.Commit, index_base: dict):
    """Load the index from the baseline commit, not from the merged worktree."""
    try:
        index_blob = base_commit.tree["index.json"].to_object()
        cached_index = json.loads(index_blob.data.decode("utf-8"))
    except (KeyError, AttributeError, UnicodeDecodeError, json.JSONDecodeError) as error:
        print(f"[Index] No usable index.json at baseline ({error}); doing a full rebuild.")
        return None

    if any(cached_index.get(key) != value for key, value in index_base.items()):
        print("[Index] Baseline index metadata differs; doing a full rebuild.")
        return None

    files = cached_index.get("files")
    if not isinstance(files, list):
        print("[Index] Baseline index has no valid files list; doing a full rebuild.")
        return None

    records = {}
    for record in files:
        if (
            not isinstance(record, dict)
            or not isinstance(record.get("path"), str)
            or not isinstance(record.get("hash"), str)
            or not isinstance(record.get("size"), int)
            or record["path"] in records
        ):
            print("[Index] Baseline index contains invalid or duplicate records; doing a full rebuild.")
            return None
        records[record["path"]] = record

    baseline_tree = find_localized_tree(base_commit.tree)
    if baseline_tree is None or set(records) != tracked_localized_paths(baseline_tree):
        print("[Index] Baseline index does not match its localized_data tree; doing a full rebuild.")
        return None

    return records


def changed_localized_paths(old_tree: pygit2.Tree, new_tree: pygit2.Tree):
    diff = old_tree.diff_to_tree(new_tree)
    diff.find_similar()
    changed = set()

    for delta in diff.deltas:
        # Include both names so renames and deletions remove stale index entries.
        for file in (delta.old_file, delta.new_file):
            if is_indexed_path(file.path):
                changed.add(file.path)

    return changed


def make_record(relative_path: str):
    file_path = LOCALIZED_DATA_DIR / Path(*PurePosixPath(relative_path).parts)
    hasher = blake3(max_threads=blake3.AUTO)
    hasher.update_mmap(file_path)
    return {
        "path": relative_path,
        "hash": hasher.hexdigest(),
        "size": file_path.stat().st_size,
    }


def write_index(index: dict, records: dict):
    index["files"] = [records[path] for path in sorted(records)]
    with INDEX_PATH.open("w", encoding="utf-8", newline="\n") as file:
        json.dump(index, file, ensure_ascii=False, indent=2)
        file.write("\n")


def main():
    with Path("index_base.json").open(encoding="utf-8") as file:
        index = json.load(file)

    repo = pygit2.Repository(".")
    head_commit = repo.revparse_single("HEAD")
    current_tree = find_localized_tree(head_commit.tree)
    if current_tree is None:
        raise SystemExit("[Error] localized_data tree not found")

    print(f"[Branch] Indexing commit: {head_commit.id}")

    try:
        base_commit = repo.revparse_single(BASE_REF)
        print(f"[Branch] Incremental baseline ({BASE_REF}): {base_commit.id}")
    except (KeyError, pygit2.GitError):
        base_commit = None
        print(f"[Index] Baseline ref {BASE_REF!r} is unavailable; doing a full rebuild.")

    cached_records = None
    baseline_tree = None
    if base_commit is not None:
        baseline_tree = find_localized_tree(base_commit.tree)
        if baseline_tree is not None:
            cached_records = load_cached_records(base_commit, index)
        else:
            print("[Index] Baseline has no localized_data tree; doing a full rebuild.")

    if cached_records is None:
        records = {}
        paths_to_process = tracked_localized_paths(current_tree)
        print(f"[Diff] Full rebuild: {len(paths_to_process)} localized file(s) to hash.")
    else:
        records = cached_records
        paths_to_process = changed_localized_paths(baseline_tree, current_tree)
        print(f"[Diff] {len(paths_to_process)} localized file(s) changed since baseline.")

    updated_count = 0
    deleted_count = 0
    for relative_path in sorted(paths_to_process):
        file_path = LOCALIZED_DATA_DIR / Path(*PurePosixPath(relative_path).parts)
        if file_path.is_file():
            records[relative_path] = make_record(relative_path)
            updated_count += 1
            print(f"[Index] Updated: {relative_path}")
        elif relative_path in records:
            del records[relative_path]
            deleted_count += 1
            print(f"[Index] Removed: {relative_path}")

    write_index(index, records)
    print(
        f"[Index] Complete: {updated_count} updated, {deleted_count} removed, "
        f"{len(records)} total."
    )


if __name__ == "__main__":
    main()
