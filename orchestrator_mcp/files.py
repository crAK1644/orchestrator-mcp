"""Files this server reads on a caller's behalf, and the rules for doing so.

Shared by review (`context_paths`) and consult (`context_paths`), which differ only in
which configured roots apply and whether a secret-shaped file is refused or previewed.
The reader lives beside neither because review imports consult and not the reverse.
"""

from __future__ import annotations

import os
import stat
from contextlib import suppress
from pathlib import Path

from .contract import secret_lines

# The most line numbers a refusal names: enough to find the credential, few enough that
# a file full of them does not turn the error into a listing.
MAX_REFUSED_LINES = 5


def read_files(
    paths: list[str],
    roots: list[Path],
    *,
    setting: str,
    max_items: int,
    limit: int,
    refuse_secrets: bool = False,
) -> tuple[str, list[tuple[str, int]]]:
    """Assemble one text from files on disk, and `(path as given, characters)` for each.

    The consulted agent never sees a path. Every adapter runs it with no filesystem at
    all, so a path handed onward would be a string it cannot open, and an answer about
    a file nobody read. Reading here is also what makes a preview honest: `scan_secrets`
    can only report what is leaving because this process holds the exact bytes.

    Refusals are `ValueError`, and each one names the path it refused. `setting` is the
    config key that holds `roots`, quoted back in the sentences that mention it.

    The allowlist is checked after strict resolution, so symlinks cannot move a path
    outside the configured tree. An MCP caller may run with less filesystem authority
    than this server; `context_paths` must not turn that difference into a read primitive.

    `refuse_secrets` is for callers with no preview: consult sends at once, so a file
    holding something credential-shaped is refused, naming the lines and never the value.
    """
    if len(paths) > max_items:
        raise ValueError(f"at most {max_items} paths, got {len(paths)}")
    if not roots:
        raise ValueError(
            f"`context_paths` is disabled until `{setting}:` names the "
            "directories files may be read from"
        )
    allowed: list[Path] = []
    for root in roots:
        try:
            resolved_root = root.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"root `{root}` cannot be read: {type(exc).__name__}") from exc
        if not resolved_root.is_dir():
            raise ValueError(f"root `{root}` is not a directory")
        allowed.append(resolved_root)

    parts: list[str] = []
    files: list[tuple[str, int]] = []
    total = 0
    for raw in paths:
        path = Path(raw).expanduser()
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"`{raw}` cannot be read: {type(exc).__name__}") from exc
        if not any(resolved.is_relative_to(root) for root in allowed):
            choices = ", ".join(f"`{root}`" for root in allowed)
            raise ValueError(
                f"`{raw}` resolves outside `{setting}`; allowed roots: "
                f"{choices}. Add its directory explicitly or pass its contents "
                "through `context`"
            )
        root = next(root for root in allowed if resolved.is_relative_to(root))
        data = _read_beneath(root, resolved, raw, limit - total, limit)
        total += len(data)
        # `replace` rather than a raise: a stray byte in one file should cost a
        # character, not the whole request.
        text = data.decode("utf-8", errors="replace")
        if refuse_secrets and (lines := secret_lines(text)):
            shown = ", ".join(map(str, lines[:MAX_REFUSED_LINES]))
            more = f" and {len(lines) - MAX_REFUSED_LINES} more" if len(lines) > MAX_REFUSED_LINES else ""
            raise ValueError(
                f"`{raw}` has something credential-shaped on line {shown}{more}, and "
                "this call would send it without a preview. Pass a masked excerpt "
                "through `context` instead"
            )
        parts.append(f"===== {raw} =====\n{text}")
        files.append((raw, len(text)))

    context = "\n\n".join(parts)
    if len(context) > limit:
        # Reachable when the bytes fit but the decoded characters plus the headers do
        # not. Same refusal, stated against the number that actually applies.
        raise ValueError(
            f"the assembled material is {len(context)} characters, over the "
            f"{limit} limit; send fewer or smaller files"
        )
    return context, files


def _read_beneath(root: Path, target: Path, raw: str, remaining: int, limit: int) -> bytes:
    """Open one regular file beneath an already-resolved root without path races."""
    relative = target.relative_to(root)
    if not relative.parts:
        raise ValueError(f"`{raw}` is not a regular file; name files, not a directory")

    directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    directory_flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    # A FIFO opened read-only waits for a writer before we can reach ``fstat`` and
    # reject it. Non-blocking makes the type check authoritative without letting an
    # allowed path stall the MCP request indefinitely. It has no effect on regular
    # file reads.
    file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    file_flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    directory_fd: int | None = None
    file_fd: int | None = None
    try:
        directory_fd = os.open(root, directory_flags)
        for part in relative.parts[:-1]:
            next_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        file_fd = os.open(relative.parts[-1], file_flags, dir_fd=directory_fd)
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"`{raw}` is not a regular file; name files, not a directory")
        handle = os.fdopen(file_fd, "rb")
        file_fd = None
        with handle:
            data = handle.read(remaining + 1)
        if len(data) > remaining:
            raise ValueError(
                f"the material is over the {limit} character limit by `{raw}`; "
                "send fewer or smaller files"
            )
        return data
    except ValueError:
        raise
    except OSError as exc:
        raise ValueError(f"`{raw}` cannot be read safely: {type(exc).__name__}") from exc
    finally:
        if file_fd is not None:
            with suppress(OSError):
                os.close(file_fd)
        if directory_fd is not None:
            with suppress(OSError):
                os.close(directory_fd)
