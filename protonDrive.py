from __future__ import annotations

import json
import hashlib
import tempfile
import os
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any

ROOT_PATH = PurePosixPath("/my-files")
CLI_ENVIRONMENT_VARIABLE = "PROTON_DRIVE_CLI"


class ProtonDriveCLIError(RuntimeError):
    """Raised when the Proton Drive CLI returns an error."""


def find_cli() -> str:
    """Find proton-drive in an explicit location, beside this script, or on PATH."""
    configured = os.environ.get(CLI_ENVIRONMENT_VARIABLE)
    if configured:
        path = Path(configured).expanduser()
        if path.is_file():
            return str(path.resolve())
        raise FileNotFoundError(
            f"{CLI_ENVIRONMENT_VARIABLE} points to a missing file: {path}"
        )

    script_directory = Path(__file__).resolve().parent
    for filename in ("proton-drive.exe", "proton-drive"):
        candidate = script_directory / filename
        if candidate.is_file():
            return str(candidate)

    executable = shutil.which("proton-drive")
    if executable:
        return executable

    raise FileNotFoundError(
        "Proton Drive CLI was not found. Download it from "
        "https://proton.me/download/drive/cli and either add it to PATH, "
        "place it beside this script, or set PROTON_DRIVE_CLI to its location."
    )


def run_cli(
    cli: str,
    *arguments: str,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run Proton Drive CLI safely without invoking a command shell."""
    result = subprocess.run(
        [cli, *arguments],
        text=True,
        capture_output=capture_output,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or f"CLI command {arguments!r} failed with exit code {result.returncode}; see console output.").strip()
        raise ProtonDriveCLIError(detail)
    return result


def parse_json_output(output: str) -> Any:
    try:
        return json.loads(output)
    except json.JSONDecodeError as error:
        raise ProtonDriveCLIError(
            "The Proton Drive CLI returned invalid JSON. "
            "Update the CLI and try again."
        ) from error


def list_items(cli: str, remote_path: PurePosixPath) -> list[dict[str, Any]]:
    """List the direct children of a Proton Drive folder."""
    result = run_cli(
        cli,
        "filesystem",
        "list",
        str(remote_path),
        "--json",
    )
    data = parse_json_output(result.stdout)
    if not isinstance(data, list):
        raise ProtonDriveCLIError("Unexpected response from filesystem list.")
    if any(not isinstance(item, dict) for item in data):
        raise ProtonDriveCLIError("Unexpected node in filesystem list response.")
    return data


def node_name(item: dict[str, Any]) -> str:
    """Read a node name from the Proton SDK result structure."""
    name = item.get("name")
    if isinstance(name, str):
        return name
    if isinstance(name, dict):
        value = name.get("value")
        if name.get("ok") is True and isinstance(value, str) and value:
            return value
    raise ProtonDriveCLIError("A Drive node has no readable name; refusing to treat its UID as a path.")


def is_folder(item: dict[str, Any]) -> bool:
    return str(item.get("type", "")).lower() == "folder"


def join_remote_path(parent: PurePosixPath, name: str) -> PurePosixPath:
    """Join a CLI path after rejecting ambiguous path separators."""
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise ValueError(f"Unsupported Proton Drive folder name: {name!r}")
    return parent / name


def verify_or_login(cli: str) -> None:
    """Test the saved session and launch browser login when necessary."""
    try:
        list_items(cli, ROOT_PATH)
    except ProtonDriveCLIError as error:
        print(f"Session check failed: {error}")
        answer = input("Sign in through the browser? [y/N]: ").strip().lower()
        if answer != "y":
            raise
        run_cli(cli, "auth", "login", capture_output=False)
        list_items(cli, ROOT_PATH)
    print("Connected to Proton Drive successfully.")


def create_folder(
    cli: str,
    parent: PurePosixPath,
) -> PurePosixPath:
    name = input("New folder name: ").strip()
    new_path = join_remote_path(parent, name)
    run_cli(
        cli,
        "filesystem",
        "create-folder",
        str(parent),
        name,
        capture_output=False,
    )
    print(f"Created: {new_path}")
    return new_path


def choose_destination(cli: str) -> PurePosixPath:
    """Browse folders and return the destination selected by the user."""
    current = ROOT_PATH

    while True:
        folders = sorted(
            (item for item in list_items(cli, current) if is_folder(item)),
            key=lambda item: node_name(item).casefold(),
        )
        print(f"\nProton Drive destination: {current}")
        print("-" * 68)
        if not folders:
            print("(No folders here.)")
        for number, folder in enumerate(folders, start=1):
            print(f"{number:>3}. [Folder] {node_name(folder)}")

        print("\nCommands: number=open | s=select here | n=new folder | b=back | q=quit")
        answer = input("> ").strip().lower()

        if answer == "q":
            raise KeyboardInterrupt
        if answer == "s":
            return current
        if answer == "n":
            try:
                current = create_folder(cli, current)
            except (ValueError, ProtonDriveCLIError) as error:
                print(f"Could not create folder: {error}")
            continue
        if answer == "b":
            if current != ROOT_PATH:
                current = current.parent
            else:
                print("Already at /my-files.")
            continue

        try:
            index = int(answer) - 1
            if index < 0:
                raise IndexError
            current = join_remote_path(current, node_name(folders[index]))
        except (ValueError, IndexError):
            print("Choose a listed number, s, n, b, or q.")


def upload_source(
    cli: str,
    destination: PurePosixPath,
    source: Path,
) -> None:
    """Upload a local file or folder to a selected Proton Drive folder."""
    source = source.expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"Local path does not exist: {source}")
    if not (source.is_file() or source.is_dir()):
        raise ValueError("The source must be a regular file or folder.")

    print(f"\nUploading {source} -> {destination}")
    run_cli(
        cli,
        "filesystem",
        "upload",
        str(source),
        str(destination),
        "--file-conflict-strategy", "skip",
        "--folder-conflict-strategy", "merge",
        capture_output=False,
    )
    print("Upload command completed; verification is still required.")


def ensure_folder(cli: str, parent: PurePosixPath, name: str) -> PurePosixPath:
    """Create or reuse an unambiguous remote folder."""
    path = join_remote_path(parent, name)
    matches = [item for item in list_items(cli, parent) if node_name(item) == name]
    if matches:
        if len(matches) != 1 or not is_folder(matches[0]):
            raise ProtonDriveCLIError(f"Destination name is ambiguous or not a folder: {path}")
        return path
    run_cli(cli, "filesystem", "create-folder", str(parent), name, capture_output=False)
    return path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_upload(cli: str, destination: PurePosixPath, source: Path) -> bool:
    """Download the exact remote file and compare SHA-256 before local deletion."""
    matches = [item for item in list_items(cli, destination) if node_name(item) == source.name]
    if len(matches) != 1 or is_folder(matches[0]):
        raise ProtonDriveCLIError("Uploaded file is missing or its remote name is ambiguous.")
    remote = join_remote_path(destination, source.name)
    with tempfile.TemporaryDirectory(prefix="proton-verify-") as folder:
        run_cli(cli, "filesystem", "download", str(remote), folder, capture_output=False)
        files = [path for path in Path(folder).rglob("*") if path.is_file()]
        if len(files) != 1:
            raise ProtonDriveCLIError("Verification did not download exactly one file.")
        return sha256_file(files[0]) == sha256_file(source)


def main() -> int:
    try:
        cli = find_cli()
        run_cli(cli, "version")
        verify_or_login(cli)
        destination = choose_destination(cli)
        print(f"\nSelected Proton Drive destination: {destination}")

        source_text = input(
            "Local file/folder to upload (press Enter to select only): "
        ).strip().strip('"')
        if source_text:
            upload_source(cli, destination, Path(source_text))
        return 0
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.")
    except (
        FileNotFoundError,
        ProtonDriveCLIError,
        ValueError,
        OSError,
    ) as error:
        print(f"Error: {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
