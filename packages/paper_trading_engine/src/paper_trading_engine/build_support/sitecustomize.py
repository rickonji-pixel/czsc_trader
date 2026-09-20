"""Keep Python build temporary directories writable inside the Windows sandbox."""

from __future__ import annotations

import errno
import os
import sys
import tempfile


def _inherited_acl_mkdtemp(suffix=None, prefix=None, dir=None):
    """Create a unique directory that inherits its controlled parent's ACL."""
    prefix, suffix, dir, output_type = tempfile._sanitize_params(prefix, suffix, dir)
    names = tempfile._get_candidate_names()
    if output_type is bytes:
        names = map(os.fsencode, names)

    for _ in range(tempfile.TMP_MAX):
        name = next(names)
        path = os.path.join(dir, prefix + name + suffix)
        sys.audit("tempfile.mkdtemp", path)
        try:
            os.mkdir(path, 0o777)
        except FileExistsError:
            continue
        except PermissionError:
            if os.name == "nt" and os.path.isdir(dir) and os.access(dir, os.W_OK):
                continue
            raise
        return os.path.abspath(path)

    raise FileExistsError(errno.EEXIST, "No usable temporary directory name found")


if os.name == "nt" and os.environ.get("PTE_INHERITED_TEMP_ACL") == "1":
    tempfile.mkdtemp = _inherited_acl_mkdtemp
