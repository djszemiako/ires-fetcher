import httpx
import pytest

from ires_fetch.constants import API_DOCS_URL, IresRequestTypes
from ires_fetch.docs import (
    discover_spec_url,
    fetch_spec,
    parse_endpoint_docs,
    parse_heading_columns,
    parse_sentence_columns,
    scan_inline_elements,
)


def test_discover_spec_url_resolves_against_the_docs_page(docs_html):
    assert discover_spec_url(docs_html) == f"{API_DOCS_URL}src/main/ires.json"


def test_discover_spec_url_raises_without_a_spec_assignment():
    with pytest.raises(ValueError, match="No Swagger spec URL"):
        discover_spec_url("<html><script>var x = 'a.json';</script></html>")


def test_scan_inline_elements_keeps_document_order_and_decodes_entities():
    elements = scan_inline_elements(
        "<b>Sort </b>- text &#9679; more <i>a, b</i> tail <b>Filter</b> <i>c</i>"
    )

    assert elements == (("b", "Sort "), ("i", "a, b"), ("b", "Filter"), ("i", "c"))


def test_recalls_docs_list_every_exportable_column(spec):
    docs = parse_endpoint_docs(spec, IresRequestTypes.RECALLS)

    assert len(docs.display_columns) == 33

    assert docs.display_columns[0] == "productid"

    assert docs.display_columns[-2:] == ("rid", "codeinformation")

    assert len(set(docs.display_columns)) == len(docs.display_columns)


def test_recalls_docs_separate_sort_and_filter_vocabularies(spec):
    docs = parse_endpoint_docs(spec, IresRequestTypes.RECALLS)

    assert "productid" in docs.sort_columns

    assert "rid" not in docs.sort_columns

    assert "codeinformation" not in docs.sort_columns

    assert {"eventlmdfrom", "eventlmdto", "centercd"} <= set(docs.filter_columns)

    assert "eventlmd" not in docs.filter_columns


@pytest.mark.parametrize(
    "request_type, first_column, last_column, total",
    (
        (IresRequestTypes.PRODUCT_TYPES, "centercd", "producttypeshorttxt", 2),
        (IresRequestTypes.EVENT, "recalleventid", "firmsurvivingfei", 26),
        (IresRequestTypes.PRODUCT, "productid", "codeinfoshort", 18),
        (IresRequestTypes.EVENT_PRODUCTS, "productid", "codeinfoshort", 21),
        (IresRequestTypes.CODE_INFO, "productid", "codeinformation", 2),
        (IresRequestTypes.PRODUCT_HISTORY, "eventid", "oldvalue", 6),
        (IresRequestTypes.EVENT_PRODUCT_HISTORY, "eventid", "oldvalue", 6),
        (IresRequestTypes.PRESS_RELEASE_URLS, "recalleventid", "pressreleaseurl", 4),
    ),
)
def test_get_docs_list_the_columns_of_the_sentence(
    spec, request_type, first_column, last_column, total
):
    docs = parse_endpoint_docs(spec, request_type)

    assert docs.display_columns[0] == first_column

    assert docs.display_columns[-1] == last_column

    assert len(docs.display_columns) == total

    assert docs.sort_columns == ()

    assert docs.filter_columns == ()


def test_parse_sentence_columns_without_the_sentence_is_empty():
    assert parse_sentence_columns("Gets the list of product types.") == ()


def test_parse_heading_columns_lower_cases_and_trims():
    elements = scan_inline_elements(
        "<b>Sort </b>- the values: <i> ProductId,  recalleventid ,rid</i>"
    )

    assert parse_heading_columns(elements, "Sort") == (
        "productid",
        "recalleventid",
        "rid",
    )


@pytest.mark.parametrize(
    "fragment, heading, match",
    (
        ("<b>Sort</b><i>a</i>", "Rows", "exactly one"),
        ("<b>Sort</b><b>Sort</b><i>a</i>", "Sort", "exactly one"),
        ("<b>Sort</b> no list", "Sort", "No <i> column list"),
    ),
)
def test_parse_heading_columns_raises_when_the_shape_is_off(fragment, heading, match):
    with pytest.raises(ValueError, match=match):
        parse_heading_columns(scan_inline_elements(fragment), heading)


def test_fetch_spec_follows_the_page_to_the_spec(docs_html, spec_text):
    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/apidocs/"):
            return httpx.Response(200, text=docs_html)

        if request.url.path.endswith("/apidocs/src/main/ires.json"):
            return httpx.Response(200, content=spec_text)

        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(_handler)) as client:
        spec = fetch_spec(client)

    assert spec["basePath"] == "/rest/iresapi/"
