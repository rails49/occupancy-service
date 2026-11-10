"""Write to an archive that is a folder. ``rails49-r49`` only reads (org ADR-0022)."""

import hashlib
import os
import secrets
import threading
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

import r49
from r49.manifest import stays_in_archive

MANIFEST = "manifest.json"
_ID_CHANGED = "manifest/id"
_IMAGE_MISSING = "images/missing"


class Refused(Exception):
    """A write that is not done. ``status`` is its HTTP status, and ``rule`` the rule it broke when
    the contract names one."""

    def __init__(self, status: int, rule: str | None = None) -> None:
        super().__init__(f"{status} {rule}")
        self.status = status
        self.rule = rule


def can_name(name: str) -> bool:
    """Whether a file may have this name: one ``manifest/filename`` lets through, which a folder
    does not ignore as a dot path, and which a path can hold."""
    return (
        stays_in_archive(name)
        and not any(segment.startswith(".") for segment in name.split("/"))
        and "\0" not in name
    )


def etag_of(manifest: bytes) -> str:
    return f'"{hashlib.sha256(manifest).hexdigest()}"'


class ArchiveWriter:
    """Saves to ``archive``, and calls ``reload`` once a new manifest is in place.

    One lock covers each look at the current manifest and the rename that depends on it, so that of
    two saves from one copy only the first succeeds. A body is taken before the lock, so a slow
    client holds up no other. An ``OSError`` or ``R49Error`` raised before a rename leaves the old
    file in place. One raised by ``reload`` comes after the rename: the new manifest stays on disk,
    and the save still answers 500.
    """

    def __init__(self, archive: str | os.PathLike[str], reload: Callable[[], None]) -> None:
        self._archive = Path(archive)
        self._reload = reload
        self._lock = threading.Lock()

    def offered(self) -> bool:
        """Whether the archive is a folder this process can write to, as of now."""
        return self._archive.is_dir() and os.access(self._archive, os.W_OK | os.X_OK)

    def etag(self) -> str:
        return etag_of((self._archive / MANIFEST).read_bytes())

    def check_copy(self, if_match: str) -> None:
        """Refuse a save based on anything but the current manifest."""
        if if_match != self.etag():
            raise Refused(412)

    def put_image(self, if_match: str, name: str, body: Iterable[bytes]) -> None:
        """Store ``body`` at ``name``, replacing a file already there."""
        target = self._archive / name
        try:
            with _temp_beside(target, body) as temp:
                with self._lock:
                    self.check_copy(if_match)
                    os.replace(temp, target)
        except (FileExistsError, IsADirectoryError, NotADirectoryError):
            raise Refused(409) from None  # A folder is at the name, or a file on the way to it.

    def put_manifest(self, if_match: str, body: bytes) -> str:
        """Put ``body`` in place as the manifest, and reload. The new manifest's ETag."""
        try:
            manifest = r49.parse_manifest(body)
        except r49.R49Error as error:
            raise Refused(422, error.rule) from None
        target = self._archive / MANIFEST
        with self._lock:
            self.check_copy(if_match)
            current = r49.load(self._archive)
            if manifest.id != current.manifest.id:
                raise Refused(422, _ID_CHANGED)
            held = set(current.entries())
            if any(image.filename not in held for image in manifest.images):
                raise Refused(422, _IMAGE_MISSING)
            with _temp_beside(target, [body]) as temp:
                os.replace(temp, target)
            self._reload()
        return etag_of(body)

    def delete(self, if_match: str, name: str) -> None:
        """Remove the file at ``name``, which the manifest must not name."""
        target = self._archive / name
        with self._lock:
            self.check_copy(if_match)
            named = {image.filename for image in r49.load(self._archive).manifest.images}
            if name == MANIFEST or name in named:
                raise Refused(409)
            if not target.is_file():
                raise Refused(404)
            target.unlink()


@contextmanager
def _temp_beside(target: Path, body: Iterable[bytes]) -> Iterator[Path]:
    """A dot-named file holding ``body``, on disk, in ``target``'s folder, which this makes if need
    be. It is gone afterwards, unless the caller has renamed it."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.parent / f".{secrets.token_hex(8)}.tmp"
    try:
        with temp.open("xb") as out:
            for chunk in body:
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        yield temp
    finally:
        temp.unlink(missing_ok=True)
