"""Write to an archive that is a folder. ``rails49-r49`` only reads (org ADR-0022)."""

import hashlib
import os
import secrets
import shutil
import threading
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

import r49
from r49.manifest import stays_in_archive

from occupancy_service.railroad import PLACEHOLDER, archive_for

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
    """Saves to the archive at ``archive()``, and calls ``reload`` once a new manifest is in place.
    A save that finds no archive there is refused with 412, as one from an older copy is.

    One lock covers each look at the current manifest and the rename that depends on it, so that of
    two saves from one copy only the first succeeds. A body is taken before the lock, so a slow
    client holds up no other. An ``OSError`` or ``R49Error`` raised before a rename leaves the old
    file in place. One raised by ``reload`` comes after the rename: the new manifest stays on disk,
    and the save still answers 500.
    """

    def __init__(
        self, archive: Callable[[], str | os.PathLike[str] | None], reload: Callable[[], None]
    ) -> None:
        self._archive = archive
        self._reload = reload
        self._lock = threading.Lock()

    def offered(self) -> bool:
        """Whether there is an archive, and it is a folder this process can write to, as of now."""
        archive = self._archive()
        if archive is None:
            return False
        return Path(archive).is_dir() and os.access(archive, os.W_OK | os.X_OK)

    def etag(self) -> str:
        return etag_of((self._folder() / MANIFEST).read_bytes())

    def check_copy(self, if_match: str) -> None:
        """Refuse a save based on anything but the current manifest."""
        if if_match != self.etag():
            raise Refused(412)

    def put_image(self, if_match: str, name: str, body: Iterable[bytes]) -> None:
        """Store ``body`` at ``name``, replacing a file already there."""
        target = self._folder() / name
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
        folder = self._folder()
        target = folder / MANIFEST
        with self._lock:
            self.check_copy(if_match)
            current = r49.load(folder)
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
        folder = self._folder()
        target = folder / name
        with self._lock:
            self.check_copy(if_match)
            named = {image.filename for image in r49.load(folder).manifest.images}
            if name == MANIFEST or name in named:
                raise Refused(409)
            if not target.is_file():
                raise Refused(404)
            target.unlink()

    def _folder(self) -> Path:
        archive = self._archive()
        if archive is None:
            raise Refused(412)
        return Path(archive)


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


class ArchiveCreator:
    """Creates the archive at ``pattern``, with the name ``railroad()`` gives in place of any
    ``{railroad}``, and calls ``opened`` with its path once it is in place.

    The zip is checked as a saved manifest is, unpacked into a dot-named folder beside the
    archive's path, each file flushed to disk, and renamed to that path. An ``OSError`` or
    ``R49Error`` raised before the rename leaves neither behind. One raised by ``opened`` comes
    after it: the archive stays on disk, and the create still answers 500. One lock covers the
    look for an archive at the path and the rename, so that of two creates only the first
    succeeds.
    """

    def __init__(
        self, pattern: str, railroad: Callable[[], str | None], opened: Callable[[Path], None]
    ) -> None:
        self._pattern = pattern
        self._railroad = railroad
        self._opened = opened
        self._lock = threading.Lock()

    def offered(self) -> bool:
        """Whether the archive's folder is one this process can write to, as of now."""
        folder = Path(self._pattern).parent
        return folder.is_dir() and os.access(folder, os.W_OK | os.X_OK)

    def check_absent(self) -> Path:
        """The archive's path. Refused with 409 when it needs a railroad and none is loaded, and
        with 412 while something is at it."""
        target = self._target()
        if target is None:
            raise Refused(409)
        if os.path.lexists(target):
            raise Refused(412)
        return target

    def create(self, body: Iterable[bytes]) -> str:
        """Put the zip ``body`` in place as the archive, and open it. Its manifest's ETag."""
        target = self.check_absent()
        unpacked = target.parent / f".{secrets.token_hex(8)}.new"
        try:
            with _temp_beside(target, body) as zipped:
                try:
                    _unpack(r49.load(zipped), unpacked)
                    mismatches = r49.load(unpacked).image_mismatches()
                except r49.R49Error as error:
                    raise Refused(422, error.rule) from None
            if any(mismatch.rule == _IMAGE_MISSING for mismatch in mismatches):
                raise Refused(422, _IMAGE_MISSING)
            with self._lock:
                self.check_absent()
                unpacked.rename(target)
        finally:
            shutil.rmtree(unpacked, ignore_errors=True)
        etag = etag_of((target / MANIFEST).read_bytes())
        if self._target() == target:  # Unless another railroad was loaded meanwhile.
            self._opened(target)
        return etag

    def _target(self) -> Path | None:
        if PLACEHOLDER not in self._pattern:
            return Path(self._pattern)
        name = self._railroad()
        path = None if name is None else archive_for(self._pattern, name)
        return None if path is None else Path(path)


def _unpack(archive: r49.R49Archive, folder: Path) -> None:
    """Write each of ``archive``'s files that a folder can hold into ``folder``, made here."""
    folder.mkdir()
    for name in archive.entries():
        if can_name(name):
            body = archive.image(name)
            assert body is not None  # It is one of the entries.
            (folder / name).parent.mkdir(parents=True, exist_ok=True)
            with (folder / name).open("xb") as out:
                out.write(body)
                out.flush()
                os.fsync(out.fileno())
