"""
Loading SQL that ships as `.sql` files instead of Python string literals.

The `sql/` directory beside this module holds the statements. Files are read as package
resources, so a statement is addressed the way its code is imported rather than by a path
relative to the working directory, and the files stay lintable (`sqlfluff`) as SQL.

Two conventions:

- Several statements share one file, each introduced by an aiosql-style `-- name: <name>`
  line; the address of a statement is `<file stem>.<name>`.
- Structure -- and ONLY structure -- is interpolated, through Jinja. Template variables
  are identifiers (relation names), supplied from a fixed catalog. Values NEVER reach a
  template: they stay bound as `$n` parameters at execution. `StrictUndefined` makes a
  variable the caller did not supply an error rather than an empty string.
"""

import re
from collections.abc import Generator
from functools import cache
from importlib.resources import files

from jinja2 import Environment, StrictUndefined

# An aiosql-style statement header, alone on its line: `-- name: create_work`.
_NAME_DIRECTIVE = re.compile(r"^--\s*name:\s*([a-z_][a-z0-9_]*)\s*$", re.MULTILINE)

_ENVIRONMENT = Environment(
    undefined=StrictUndefined, trim_blocks=True, lstrip_blocks=True
)


def _split_statements(text: str) -> Generator[tuple[str, str]]:
    """
    Yield each `-- name:`-introduced statement of one SQL file, as `(name, body)`.

    Anything before the first directive is the file's own header comment and belongs to no
    statement, so it is dropped.

    Args:
        text: Full contents of one `.sql` file

    Returns:
        Generator of `(statement name, statement body)` pairs, in file order
    """
    headers = tuple(_NAME_DIRECTIVE.finditer(text))

    for index, header in enumerate(headers):
        end = headers[index + 1].start() if index + 1 < len(headers) else len(text)

        yield header.group(1), text[header.end() : end].strip()


@cache
def read_catalog(package: str) -> dict[str, str]:
    """
    Read every statement of a `sql/` package, keyed by `<file stem>.<statement name>`.

    Args:
        package: Importable package holding the `.sql` files, e.g. `ires_fetch.sql`

    Returns:
        Unrendered statement bodies by address
    """
    catalog: dict[str, str] = {}

    for path in sorted(files(package).iterdir(), key=lambda item: item.name):
        if not path.name.endswith(".sql"):
            continue

        for name, body in _split_statements(path.read_text(encoding="utf-8")):
            address = f"{path.name.removesuffix('.sql')}.{name}"

            if address in catalog:
                raise ValueError(
                    f"Duplicate statement address '{address}' in {package}"
                )

            catalog[address] = body

    return catalog


def render(package: str, address: str, identifiers: dict[str, str]) -> str:
    """
    Render one catalog statement against the identifiers its template addresses.

    Args:
        package: Importable package holding the `.sql` files
        address: `<file stem>.<statement name>`
        identifiers: Template variables; relation names only, never run-time values

    Returns:
        Executable SQL
    """
    catalog = read_catalog(package)

    if address not in catalog:
        raise KeyError(
            f"no SQL statement {address!r} in {package}; "
            f"available: {', '.join(sorted(catalog))}"
        )

    return _ENVIRONMENT.from_string(catalog[address]).render(identifiers)
