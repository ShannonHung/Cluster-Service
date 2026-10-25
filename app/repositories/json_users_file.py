"""
app/repositories/json_users_file.py

Loads and validates the USERS_JSON_PATH file (schema in
``app/domain/users_document.py``) for both readers of it —
``JsonUserRepository`` (accounts) and ``JsonPolicyRepository`` (access policy)
— so the two can never disagree about what the file says.

The validated document is cached against the file's identity and change
stamps (inode, mtime, ctime, size): an edit takes effect on the next request
without a restart, and an unchanged file is not re-parsed per request. The
inode catches the usual atomic replace (write a temp file, rename it over)
even when the new content has the same size and lands within one tick of the
filesystem clock. A file that is missing or invalid raises
``UsersFileInvalidException`` every time — the last good version is never
served in its place.
"""

from __future__ import annotations

import json
import logging
import threading
from functools import lru_cache
from pathlib import Path

from app.core.exceptions import UsersFileInvalidException
from app.domain.users_document import UsersDocument, UsersDocumentError, parse_users_document

_logger = logging.getLogger(__name__)

_Stamp = tuple[int, int, int, int]


def read_users_document(path: Path) -> UsersDocument:
    """Read and validate one users file, uncached.

    Raises ``UsersDocumentError`` for any problem — unreadable, not JSON, or
    invalid — and never with the path in the text: callers decide whether the
    path may be shown (the CLI does; an HTTP response must not).
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        # str(exc) would embed the filename; strerror does not.
        raise UsersDocumentError([f"<file>: cannot read users file: {exc.strerror}"]) from exc
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise UsersDocumentError([f"<file>: not valid JSON: {exc}"]) from exc
    return parse_users_document(raw)


class JsonUsersFile:

    def __init__(self, json_path: str | Path) -> None:
        self._path = Path(json_path)
        self._lock = threading.Lock()
        self._stamp: _Stamp | None = None
        self._doc: UsersDocument | None = None

    def load(self) -> UsersDocument:
        try:
            st = self._path.stat()
        except OSError as exc:
            raise self._invalid(f"cannot read users file: {exc.strerror}") from exc
        stamp = (st.st_ino, st.st_mtime_ns, st.st_ctime_ns, st.st_size)

        with self._lock:
            if stamp != self._stamp or self._doc is None:
                self._doc = self._parse()
                self._stamp = stamp
            return self._doc

    def _parse(self) -> UsersDocument:
        try:
            doc = read_users_document(self._path)
        except UsersDocumentError as exc:
            raise self._invalid(str(exc)) from exc

        _logger.info(
            "Users file loaded | path=%s | users=%d | global_rules=%d | users_with_rules=%d",
            self._path, len(doc.users), len(doc.policies),
            sum(1 for u in doc.users if u.policies),
        )
        return doc

    def _invalid(self, reason: str) -> UsersFileInvalidException:
        # The path stays in the log; the response only says what is wrong.
        _logger.error("Users file invalid | path=%s | reason=%s", self._path, reason)
        return UsersFileInvalidException(
            "Users file is invalid; login and policed writes are refused until "
            f"it is fixed: {reason}",
        )


@lru_cache
def users_file(path: str) -> JsonUsersFile:
    """One loader per path, so its cache survives across requests."""
    return JsonUsersFile(path)
