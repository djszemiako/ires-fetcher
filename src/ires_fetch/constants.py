from enum import StrEnum
from typing import Literal, NamedTuple, Optional

# The Swagger UI shell. Its spec URL is discovered from the page rather than pinned, so a
# relocation of the spec (currently `src/main/ires.json`) costs nothing here.
API_DOCS_URL = "https://www.accessdata.fda.gov/scripts/ires/apidocs/"

API_BASE_URL = "https://www.accessdata.fda.gov/rest/iresapi"

# Browser defaults. `Accept-Encoding` is left to httpx, which advertises only the codings
# it can decode.
DEFAULT_HEADERS: dict[str, str] = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Priority": "u=0, i",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
}

DOCS_REQUEST_HEADERS: dict[str, str] = {
    **DEFAULT_HEADERS,
    "Host": "www.accessdata.fda.gov",
}

# The API is an XHR target rather than a navigated document, so `Accept` is the one
# browser default that has to change; the credentials are added per request.
API_REQUEST_HEADERS: dict[str, str] = {
    **DOCS_REQUEST_HEADERS,
    "Accept": "application/json, text/plain, */*",
}

# `POST /recalls/` reads its payload from a form-encoded body.
POST_CONTENT_TYPE = "application/x-www-form-urlencoded"

AUTHORIZATION_USER_HEADER = "Authorization-User"

AUTHORIZATION_KEY_HEADER = "Authorization-Key"

# Every run restricts `POST /recalls/` to the two drug and biologic centers. The by-id
# endpoints accept no filter; they inherit the restriction through the ids they are fed.
DEFAULT_FILTERS: tuple[dict[str, list[str]], ...] = ({"centercd": ["CBER", "CDER"]},)

DEFAULT_SORT = "productid"

DEFAULT_SORT_ORDER: Literal["asc"] = "asc"

# Documented page caps for `POST /recalls/`: 5000 rows, or 2500 once the text-BLOB column
# `codeinformation` is requested. Confirmed live: `rows=5000` with every column answers
# HTTP 200 carrying `STATUSCODE` 417 in the body.
MAX_ROWS_PER_PAGE = 5000

MAX_ROWS_PER_PAGE_WITH_CODE_INFORMATION = 2500

CODE_INFORMATION_COLUMN = "codeinformation"

# `start` is a 1-based record offset, not a page number: `start=3` skips the first two rows
# of the sorted result and `start=0` is rejected.
FIRST_RECORD_OFFSET = 1

# Requests are fetched, staged and appended one batch at a time, so an interrupted run
# keeps whatever the batches before it wrote and a later run resumes from that parquet.
DEFAULT_BATCH_SIZE = 1000

# Politeness window each worker sleeps through before every fetch.
FETCH_COOLDOWN_SECONDS: tuple[float, float] = (1.0, 3.0)

REQUEST_TIMEOUT_SECONDS = 120.0

# The API answers HTTP 200 to every well-formed call and reports the outcome in the body:
# `MESSAGE` is `success` on success (alongside `STATUSCODE` 400, sic), or a reason
# otherwise, e.g. "The payload rows should be less than or equal to 2500 ...".
SUCCESS_MESSAGE = "success"


class IresEndpoint(NamedTuple):
    """
    One endpoint of the spec, addressed the way `paths` in the Swagger document spells it.

    `path` keeps the spec's placeholder (`{eventid}` / `{productid}`) so the entry doubles
    as the key into the documentation; `id_column` names the `POST /recalls/` column whose
    distinct values fill that placeholder.
    """

    method: Literal["GET", "POST"]
    path: str
    id_column: Optional[str] = None


class IresRequestTypes(StrEnum):
    """
    Our names for the endpoints. String-valued so a CLI can offer them as choices.
    """

    RECALLS = "RECALLS"
    PRODUCT_TYPES = "PRODUCT_TYPES"
    EVENT = "EVENT"
    PRODUCT = "PRODUCT"
    EVENT_PRODUCTS = "EVENT_PRODUCTS"
    CODE_INFO = "CODE_INFO"
    PRODUCT_HISTORY = "PRODUCT_HISTORY"
    EVENT_PRODUCT_HISTORY = "EVENT_PRODUCT_HISTORY"
    PRESS_RELEASE_URLS = "PRESS_RELEASE_URLS"

    @property
    def endpoint(self) -> IresEndpoint:
        return ENDPOINTS[self]

    @property
    def method(self) -> Literal["GET", "POST"]:
        return self.endpoint.method

    @property
    def path(self) -> str:
        return self.endpoint.path

    @property
    def id_column(self) -> Optional[str]:
        return self.endpoint.id_column

    @property
    def accepts_payload(self) -> bool:
        """
        Whether the endpoint takes `displaycolumns` / `filter` / `sort` / paging at all.
        Only `POST /recalls/` does; the rest are keyed by a path id or take nothing.
        """
        return self.method == "POST"

    @property
    def needs_ids(self) -> bool:
        return self.id_column is not None


ENDPOINTS: dict[IresRequestTypes, IresEndpoint] = {
    IresRequestTypes.RECALLS: IresEndpoint("POST", "/recalls/"),
    IresRequestTypes.PRODUCT_TYPES: IresEndpoint("GET", "/search/producttypes"),
    IresRequestTypes.EVENT: IresEndpoint(
        "GET", "/recalls/event/{eventid}", "recalleventid"
    ),
    IresRequestTypes.PRODUCT: IresEndpoint(
        "GET", "/recalls/product/{productid}", "productid"
    ),
    IresRequestTypes.EVENT_PRODUCTS: IresEndpoint(
        "GET", "/recalls/eventproducts/{eventid}", "recalleventid"
    ),
    IresRequestTypes.CODE_INFO: IresEndpoint(
        "GET", "/search/codeinfo/{productid}", "productid"
    ),
    IresRequestTypes.PRODUCT_HISTORY: IresEndpoint(
        "GET", "/search/producthistory/{productid}", "productid"
    ),
    IresRequestTypes.EVENT_PRODUCT_HISTORY: IresEndpoint(
        "GET", "/search/eventproducthistory/{eventid}", "recalleventid"
    ),
    IresRequestTypes.PRESS_RELEASE_URLS: IresEndpoint(
        "GET", "/search/pressreleaseurls/{eventid}", "recalleventid"
    ),
}
