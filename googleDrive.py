from pathlib import Path 

import sys
import re
import tempfile

from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

#Read-only acces to verify the connection and browse Drive files.
SCOPES = ['https://www.googleapis.com/auth/drive.readonly']

SCRIPT_DIR = Path(__file__).resolve().parent
CREDENTIALS_FILE = SCRIPT_DIR / 'credentials.json'
TOKEN_FILE = SCRIPT_DIR / 'token.json'

def authenticate() -> Credentials: 
    """Load saved credentials or run Google's OAuth 2.0 consent flow."""
    credentials = None 

    if TOKEN_FILE.exists():
        try:
            credentials = Credentials.from_authorized_user_file(
                str(TOKEN_FILE), SCOPES
            )
        except (ValueError, OSError) as error:
            print(f"Could not load {TOKEN_FILE.name}: {error}")
            print("A new sign-in will be started.")

    if credentials and credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
        except RefreshError:
            print("Saved authorization is no longer valid; signing in again.")
            credentials = None

    if not credentials or not credentials.valid:
        if not CREDENTIALS_FILE.exists():
            raise FileNotFoundError(
                f"Missing {CREDENTIALS_FILE.name}. Download the OAuth Desktop "
                "app credentials from Google Cloud Console and place the file "
                f"at: {CREDENTIALS_FILE}"
            )

        flow = InstalledAppFlow.from_client_secrets_file(
            str(CREDENTIALS_FILE), SCOPES
        )
        credentials = flow.run_local_server(port=0)

    TOKEN_FILE.write_text(credentials.to_json(), encoding="utf-8")
    return credentials

def connect_to_drive(): 
    """Authenticate the user and return a Google Drive API service object."""
    credentials = authenticate()
    return build("drive", "v3", credentials=credentials, cache_discovery=False)

def show_recent_files(drive_service, limit: int = 10) -> None:
    """List a few non-trashed files to confirm that the connection works."""
    if limit < 1 or limit > 1000:
        raise ValueError("limit must be between 1 and 1000")

    response = (
        drive_service.files()
        .list(
            pageSize=limit,
            q="trashed = false",
            fields="nextPageToken, files(id, name, mimeType, modifiedTime)",
            orderBy="modifiedTime desc",
        )
        .execute()
    )
    files = response.get("files", [])

    print("Connected to Google Drive successfully.")
    if not files:
        print("No files found.")
        return

    print(f"Most recently modified files (up to {limit}):")
    for item in files:
        print(
            f"- {item.get('name', '(unnamed)')}"
            f" | {item.get('mimeType', 'unknown type')}"
            f" | modified {item.get('modifiedTime', 'unknown')}"
            f" | ID: {item.get('id', 'unknown')}"
        )

# Google-native documents need an export format before they can be uploaded elsewhere.
EXPORT_FORMATS = {
    "application/vnd.google-apps.document": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"),
    "application/vnd.google-apps.spreadsheet": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    "application/vnd.google-apps.presentation": ("application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx"),
    "application/vnd.google-apps.drawing": ("application/pdf", ".pdf"),
}


FOLDER_MIME = "application/vnd.google-apps.folder"


def list_children(drive_service, parent):
    token = None
    escaped = parent.replace("\\", "\\\\").replace("'", "\\'")
    while True:
        response = drive_service.files().list(
            q=f"trashed = false and '{escaped}' in parents",
            pageSize=100, pageToken=token,
            fields="nextPageToken,files(id,name,mimeType)",
            orderBy="folder,name", supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute(num_retries=3)
        yield from response.get("files", [])
        token = response.get("nextPageToken")
        if not token:
            return


def select_files(drive_service):
    """Navigate My Drive; add multiple files/folders to a selection."""
    stack = [("root", "My Drive")]
    selected = {}
    page = 0
    while True:
        children = list(list_children(drive_service, stack[-1][0]))
        page = min(page, max(0, (len(children) - 1) // 20))
        visible = children[page * 20:(page + 1) * 20]
        print(f"\nGoogle Drive: {' / '.join(name for _, name in stack)} | page {page + 1}")
        for index, item in enumerate(visible, 1):
            kind = "Folder" if item["mimeType"] == FOLDER_MIME else "File"
            print(f"{index}. [{kind}] {item['name']}")
        print(f"Selected: {len(selected)}. number=open folder/add file; a 1,2=add files/folders")
        answer = input("n=next, p=previous, b=back, d=done, q=cancel: ").strip().lower()
        if answer == "q":
            return []
        if answer == "d":
            if selected:
                return list(selected.values())
            print("Select at least one file or folder.")
        elif answer == "n":
            if (page + 1) * 20 < len(children):
                page += 1
        elif answer == "p":
            page = max(0, page - 1)
        elif answer == "b":
            if len(stack) > 1:
                stack.pop()
            page = 0
        else:
            try:
                adding = answer.startswith("a ")
                numbers = answer[2:].split(",") if adding else [answer]
                indices = [int(value.strip()) - 1 for value in numbers]
                if any(index < 0 or index >= len(visible) for index in indices):
                    raise ValueError
                for index in indices:
                    item = visible[index]
                    if not adding and item["mimeType"] == FOLDER_MIME:
                        stack.append((item["id"], item["name"]))
                        page = 0
                    else:
                        selected[item["id"]] = item
            except ValueError:
                print("Enter a valid command or displayed number.")


def safe_filename(name):
    """Keep Drive names inside the chosen folder and usable on Windows/Linux."""
    name = name.translate(str.maketrans({"[": "_", "]": "_", "{": "_", "}": "_"}))
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip().rstrip(". ")
    if not name or name in {".", ".."}:
        name = "download"
    if re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]", name.split(".")[0], re.I):
        name = "_" + name
    return name


def download_file(drive_service, selected, destination):
    """Download one file to local staging for upload or retry."""
    metadata = drive_service.files().get(
        fileId=selected["id"],
        fields="id,name,mimeType,capabilities(canDownload)",
        supportsAllDrives=True,
    ).execute(num_retries=3)
    if metadata.get("capabilities", {}).get("canDownload") is False:
        raise ValueError("Google Drive does not allow downloading this file.")
    mime_type = metadata["mimeType"]
    filename = safe_filename(metadata["name"])
    if mime_type in EXPORT_FORMATS:
        export_type, extension = EXPORT_FORMATS[mime_type]
        request = drive_service.files().export_media(fileId=metadata["id"], mimeType=export_type)
        if not filename.lower().endswith(extension):
            filename += extension
        print(f"Exporting Google document as {extension}.")
    elif mime_type.startswith("application/vnd.google-apps."):
        raise ValueError("This Google-native file type (including shortcuts) is not supported. Select the original file instead.")
    else:
        request = drive_service.files().get_media(fileId=metadata["id"], supportsAllDrives=True)

    destination = Path(destination).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / filename
    # Never overwrite an existing file, including one created during the download.
    if target.exists():
        raise FileExistsError(f"Destination already exists: {target}. Choose another folder or rename the existing file.")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w+b", prefix=".drive-download-", suffix=".part", dir=destination, delete=False) as stream:
            temporary = Path(stream.name)
            downloader = MediaIoBaseDownload(stream, request, chunksize=8 * 1024 * 1024)
            done = False
            while not done:
                status, done = downloader.next_chunk(num_retries=3)
                if status:
                    print(f"Download: {status.progress():.0%}")
        # Exclusive creation prevents accidental replacement of another local file.
        import shutil
        created = False
        try:
            with target.open("xb") as output, temporary.open("rb") as source:
                created = True
                shutil.copyfileobj(source, output)
        except BaseException:
            if created:
                target.unlink(missing_ok=True)
            raise
        return target
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def load_proton_module():
    import importlib.util
    for name in ("protonDrive(1).py", "protonDrive.py"):
        path = SCRIPT_DIR / name
        if path.is_file():
            spec = importlib.util.spec_from_file_location("proton_migration", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    raise FileNotFoundError("Place protonDrive(1).py beside this Google script.")


def save_report(path, records):
    import json
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(records, indent=2), encoding="utf-8")
    temporary.replace(path)


def migrate(drive, proton, cli, selected, destination, run_folder):
    """Initial sequential pass, then at most three additional upload attempts."""
    records = []
    report = run_folder / "report.json"
    seen = set()
    used_names = {}

    def record(**values):
        records.append(values)
        save_report(report, records)
        return values

    def unique_name(parent, item):
        name = safe_filename(item["name"])
        if item["mimeType"] in EXPORT_FORMATS:
            extension = EXPORT_FORMATS[item["mimeType"]][1]
            if not name.lower().endswith(extension):
                name += extension
        key = (str(parent), name.casefold())
        if key in used_names and used_names[key] != item["id"]:
            path = Path(name)
            name = f"{path.stem}_{item['id']}{path.suffix}"
            key = (str(parent), name.casefold())
        used_names[key] = item["id"]
        return name

    def attempt(row):
        source = Path(row["local_file"])
        remote = proton.PurePosixPath(row["destination"])
        row["attempts"] += 1
        try:
            proton.upload_source(cli, remote, source)
            if not proton.verify_upload(cli, remote, source):
                raise proton.ProtonDriveCLIError("SHA-256 verification failed; local copy retained.")
            # Mark verified before cleanup so a cleanup failure never repeats an upload.
            row["status"] = "success"
            row["error"] = ""
            try:
                source.unlink()
                source.parent.rmdir()
                row["local_file"] = None
            except OSError as error:
                row["cleanup_warning"] = str(error)
        except (proton.ProtonDriveCLIError, OSError, ValueError) as error:
            row["status"] = "failed_upload"
            row["error"] = str(error)
            print(f"Saved for retry: {source}: {error}")
        finally:
            save_report(report, records)

    def visit(item, parent):
        if item["id"] in seen:
            return
        seen.add(item["id"])
        name = unique_name(parent, item)
        if item["mimeType"] == FOLDER_MIME:
            try:
                remote = proton.ensure_folder(cli, parent, name)
                for child in list_children(drive, item["id"]):
                    visit(child, remote)
            except (HttpError, TransportError, proton.ProtonDriveCLIError, OSError, ValueError) as error:
                record(google_id=item["id"], name=item["name"], status="failed_folder", error=str(error), attempts=0)
            return
        folder = Path(tempfile.mkdtemp(prefix="file-", dir=run_folder))
        row = record(google_id=item["id"], name=item["name"], destination=str(parent), status="downloading", local_file=None, attempts=0, error="")
        try:
            source = download_file(drive, item, folder)
            renamed = source.with_name(name)
            if renamed != source:
                source.rename(renamed)
            row["local_file"] = str(renamed)
            row["status"] = "pending_upload"
            save_report(report, records)
        except (HttpError, TransportError, OSError, ValueError) as error:
            row["status"] = "failed_download"
            row["error"] = str(error)
            save_report(report, records)
            return
        attempt(row)

    try:
        for item in selected:
            visit(item, destination)
        for retry in range(1, 4):
            failed = [row for row in records if row["status"] == "failed_upload"]
            if not failed:
                break
            print(f"\nRetry round {retry}/3: {len(failed)} file(s)")
            for row in failed:
                attempt(row)
    finally:
        save_report(report, records)
        successes = sum(row["status"] == "success" for row in records)
        print(f"\nReport: {successes} succeeded; {len(records) - successes} failed or unfinished.")
        print(f"Report and retained retry files: {run_folder}")
    return all(row["status"] == "success" for row in records)


def main() -> int:
    try:
        drive = connect_to_drive()
        print("Connected to Google Drive.")
        proton = load_proton_module()
        cli = proton.find_cli()
        proton.run_cli(cli, "version")
        proton.verify_or_login(cli)
        selected = select_files(drive)
        if not selected:
            print("Cancelled.")
            return 0
        destination = proton.choose_destination(cli)
        print(f"Selected {len(selected)} Google item(s) -> {destination}")
        run_folder = Path(tempfile.mkdtemp(prefix="google-proton-", dir=SCRIPT_DIR))
        return 0 if migrate(drive, proton, cli, selected, destination, run_folder) else 1
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled. Any downloaded retry files and report are retained.", file=sys.stderr)
    except Exception as error:
        print(f"Migration error: {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
