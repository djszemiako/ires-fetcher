"""
What the API documentation says each endpoint exports.

The documentation page is a Swagger UI shell whose content is a JSON spec. Within that
spec, the `POST /recalls/` description is an HTML fragment: each option is introduced by a
`<b>` heading and, where the option takes column names, the permitted names follow in the
first `<i>` after it. The by-id `GET` descriptions are plain sentences ending in
"including the following columns: a, b, c."
"""

import json
import re
from html.parser import HTMLParser
from typing import NamedTuple, Optional, cast
from urllib.parse import urljoin

import httpx

from ires_fetch.constants import API_DOCS_URL, IresRequestTypes

# The spec URL is assigned in the page's bootstrap script: `url = "src/main/ires.json";`.
_SPEC_URL_PATTERN = re.compile(r"""\burl\s*=\s*["']([^"']+\.json)["']""")

# A `GET` description's column list runs from the colon to the sentence's full stop.
_COLUMN_SENTENCE_PATTERN = re.compile(r"following columns:\s*(?P<columns>[^.]+)")

_COLUMN_SEPARATOR_PATTERN = re.compile(r"\s*,\s*")

_WHITESPACE_PATTERN = re.compile(r"\s+")

# The `<b>` headings of the `POST /recalls/` description that a column list follows.
DISPLAY_COLUMNS_HEADING = "Display columns"

SORT_HEADING = "Sort"

FILTER_HEADING = "Filter"

# The elements the description is read through.
HEADING_TAG = "b"

COLUMN_LIST_TAG = "i"

SwaggerSpec = dict[str, object]


class EndpointDocs(NamedTuple):
    """
    The documented vocabulary of one endpoint.

    `display_columns` are the names the endpoint can export; for `POST /recalls/` they
    are also what `displaycolumns` accepts. `sort_columns` and `filter_columns` are empty
    for every endpoint that takes no payload.
    """

    request_type: IresRequestTypes
    description: str
    display_columns: tuple[str, ...]
    sort_columns: tuple[str, ...]
    filter_columns: tuple[str, ...]


class _InlineElements(HTMLParser):
    """
    Records the text of every `<b>` and `<i>` of a fragment, in document order, as
    `(tag, text)`. Neither nests anything in the descriptions, so a flat scan is enough.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)

        self.elements: tuple[tuple[str, str], ...] = ()

        self._open_tag: Optional[str] = None

        self._text = ""

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in (HEADING_TAG, COLUMN_LIST_TAG):
            self._open_tag = tag

            self._text = ""

    def handle_data(self, data: str) -> None:
        if self._open_tag is not None:
            self._text += data

    def handle_endtag(self, tag: str) -> None:
        if tag == self._open_tag:
            self.elements = (*self.elements, (tag, self._text))

            self._open_tag = None


def scan_inline_elements(fragment: str) -> tuple[tuple[str, str], ...]:
    """
    The `<b>` and `<i>` elements of an HTML fragment, in order.

    Args:
        fragment: HTML markup, not necessarily a whole document

    Returns:
        `(tag, text)` per element
    """
    parser = _InlineElements()

    parser.feed(fragment)

    parser.close()

    return parser.elements


def _normalize_space(text: str) -> str:
    return _WHITESPACE_PATTERN.sub(" ", text).strip()


def _split_columns(text: str) -> tuple[str, ...]:
    return tuple(
        column.lower()
        for column in _COLUMN_SEPARATOR_PATTERN.split(text.strip())
        if column
    )


def discover_spec_url(docs_html: str, docs_url: str = API_DOCS_URL) -> str:
    """
    Resolve the Swagger spec URL the documentation page loads.

    Args:
        docs_html: Markup of the documentation page
        docs_url: URL the page was fetched from; the spec URL is relative to it

    Returns:
        Absolute URL of the JSON spec
    """
    match = _SPEC_URL_PATTERN.search(docs_html)

    if match is None:
        raise ValueError(f"No Swagger spec URL found in the page at {docs_url}")

    return urljoin(docs_url, match.group(1))


def parse_heading_columns(
    elements: tuple[tuple[str, str], ...], heading: str
) -> tuple[str, ...]:
    """
    Read the column list that follows a `<b>` heading of the `POST /recalls/` description.

    Args:
        elements: The fragment's inline elements, from `scan_inline_elements`
        heading: Text of the `<b>` heading, compared whitespace-normalised

    Returns:
        Column names in documented order, lower-cased
    """
    headings = tuple(
        index
        for index, (tag, text) in enumerate(elements)
        if tag == HEADING_TAG and _normalize_space(text) == heading
    )

    if len(headings) != 1:
        raise ValueError(
            f"Expected exactly one <{HEADING_TAG}>{heading}</{HEADING_TAG}> heading, "
            f"found {len(headings)}"
        )

    column_lists = tuple(
        text for tag, text in elements[headings[0] + 1 :] if tag == COLUMN_LIST_TAG
    )

    if not column_lists:
        raise ValueError(f"No <{COLUMN_LIST_TAG}> column list follows '{heading}'")

    return _split_columns(column_lists[0])


def parse_sentence_columns(description: str) -> tuple[str, ...]:
    """
    Read the column list of a plain-sentence `GET` description.

    Args:
        description: The endpoint description

    Returns:
        Column names in documented order, lower-cased; empty if the sentence is absent
    """
    match = _COLUMN_SENTENCE_PATTERN.search(description)

    if match is None:
        return ()

    return _split_columns(match.group("columns"))


def parse_endpoint_docs(
    spec: SwaggerSpec, request_type: IresRequestTypes
) -> EndpointDocs:
    """
    Extract one endpoint's documented columns from the Swagger spec.

    Args:
        spec: The parsed Swagger document
        request_type: Endpoint to document

    Returns:
        The endpoint's documented vocabulary
    """
    paths = cast(dict[str, dict[str, dict[str, str]]], spec["paths"])

    description = paths[request_type.path][request_type.method.lower()]["description"]

    if not request_type.accepts_payload:
        return EndpointDocs(
            request_type=request_type,
            description=description,
            display_columns=parse_sentence_columns(description),
            sort_columns=(),
            filter_columns=(),
        )

    elements = scan_inline_elements(description)

    return EndpointDocs(
        request_type=request_type,
        description=description,
        display_columns=parse_heading_columns(elements, DISPLAY_COLUMNS_HEADING),
        sort_columns=parse_heading_columns(elements, SORT_HEADING),
        filter_columns=parse_heading_columns(elements, FILTER_HEADING),
    )


def fetch_spec(client: httpx.Client, docs_url: str = API_DOCS_URL) -> SwaggerSpec:
    """
    Fetch the documentation page, find the spec it loads, and fetch that.

    Args:
        client: Client to fetch with
        docs_url: URL of the Swagger UI page

    Returns:
        The parsed Swagger document
    """
    page = client.get(docs_url)

    page.raise_for_status()

    spec_url = discover_spec_url(page.text, docs_url)

    spec = client.get(spec_url)

    spec.raise_for_status()

    return json.loads(spec.content)
