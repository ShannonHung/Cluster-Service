"""
app/check_users.py

Validate a users file (accounts + access policy) before deploying it — the
same checks the service runs at startup, offline::

    make check-users f=data/users.json
    python -m app.check_users data/users.json

Prints every problem with its location; exits non-zero when there is any.
"""

from __future__ import annotations

import sys
from pathlib import Path

from app.domain.users_document import UsersDocumentError
from app.repositories.json_users_file import read_users_document


def main(argv: list[str]) -> int:
    path = Path(argv[0] if argv else "data/users.json")
    try:
        doc = read_users_document(path)
    except UsersDocumentError as exc:
        print(f"INVALID {path}")
        for problem in exc.problems:
            print(f"  {problem}")
        return 1

    print(
        f"OK {path}: {len(doc.users)} user(s), {len(doc.policies)} global rule(s), "
        f"{sum(len(u.policies) for u in doc.users)} user rule(s)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
