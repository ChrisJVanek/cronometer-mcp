"""MCP server for Cronometer, using the API the Android app uses."""

import importlib.metadata
import json
import logging
import os
import threading
import uuid
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import Icon

from . import paging
from .client import CronometerClient, CronometerError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

try:
    __version__ = importlib.metadata.version("cronometer-mcp")
except importlib.metadata.PackageNotFoundError:  # running from a source tree
    __version__ = "0.0.0"

# Advertised in the initialize response per the MCP icons spec, so a client can
# badge the connector with the Cronometer mark rather than guessing from the
# hostname. Three sizes because the spec has clients pick the one that fits
# their UI, and downscaling a single 256px asset to a 16px chip looks it.
# PNG throughout: it is one of the two types an icon-rendering client MUST
# support, and the URLs are same-origin, which the spec asks for so the icon
# cannot leak a request to a third party.
# Ref: https://github.com/modelcontextprotocol/modelcontextprotocol/issues/1040#issuecomment-3967699520
_ICON_BASE = os.getenv("MCP_PUBLIC_URL", "").rstrip("/")
_ICON_SIZES = (48, 96, 256)

# The SDK's 30-minute default kills sessions of chats left idle, and their next call fails.
SESSION_IDLE_TIMEOUT = 24 * 3600

mcp = FastMCP(
    "cronometer",
    session_idle_timeout=SESSION_IDLE_TIMEOUT,
    icons=(
        [
            Icon(
                src=f"{_ICON_BASE}/icon.png"
                if size == 256
                else f"{_ICON_BASE}/icon-{size}.png",
                mimeType="image/png",
                sizes=[f"{size}x{size}"],
            )
            for size in _ICON_SIZES
        ]
        if _ICON_BASE
        else None
    ),
    website_url=_ICON_BASE or None,
    instructions=(
        "Read and write a Cronometer food diary: search foods, add and change "
        "diary entries, read daily nutrients and goals, record measurements, "
        "exercise and fasts. Use search_foods to find a food, get_food_details "
        "for its nutrients and serving sizes, add_food_entry to log a meal, "
        "and get_food_log to see what was eaten. Measurements and exercise "
        "entries can be added, changed and deleted, and so can notes."
    ),
)

# FastMCP has no version argument, and the server underneath falls back to the
# MCP SDK's own version, so initialize was reporting the SDK's number as ours.
mcp._mcp_server.version = __version__
paging.register(mcp)
# A paged result belongs to the person whose call made it.
paging.owner = lambda: _subject()

_client: CronometerClient | None = None

# Multi-user mode (accounts.py): one client pair per signed-in person, made
# from their stored credentials the first time one of their tools runs.
_accounts = None
_user_clients: dict[str, CronometerClient] = {}
_user_web_clients: dict = {}
_pool_lock = threading.Lock()


def _subject() -> str:
    """Whose account this tool call is for: the token's subject, or the owner.

    stdio and an MCP_API_KEY call are the owner. The SDK binds every HTTP
    session to the token that opened it, so one person's session cannot carry
    another person's calls.
    """
    from mcp.server.auth.middleware.auth_context import get_access_token

    from .accounts import OWNER

    token = get_access_token()
    if token is None:
        try:
            user = mcp.get_context().request_context.request.scope.get("user")
            token = getattr(user, "access_token", None)
        except Exception:
            token = None
    return (token.subject if token else None) or OWNER


def _user_credentials(subject: str) -> tuple[str, str]:
    creds = _accounts.get(subject) if _accounts is not None else None
    if creds is None:
        raise CronometerError(
            "This connection's Cronometer account is no longer on the server. "
            "Disconnect and connect again."
        )
    return creds


def _get_client() -> CronometerClient:
    global _client
    from .accounts import OWNER, subject_dir

    subject = _subject()
    if subject != OWNER:
        with _pool_lock:
            client = _user_clients.get(subject)
            if client is None:
                email, password = _user_credentials(subject)
                client = CronometerClient(
                    username=email,
                    password=password,
                    session_path=subject_dir(subject) / "session.json",
                )
                _user_clients[subject] = client
        return client
    if _client is None:
        _client = CronometerClient()
    return _client


def _verify_cronometer(email: str, password: str) -> str:
    """Log in as this person; returns their subject. Raises WrongPassword."""
    import httpx

    from .accounts import subject_dir
    from .auth import WrongPassword
    from .client import TwoFactorRequired

    scratch = subject_dir("_signin") / f"{uuid.uuid4().hex}.json"
    client = CronometerClient(username=email, password=password, session_path=scratch)
    try:
        client.login()
    except TwoFactorRequired:
        raise
    except CronometerError as e:
        raise WrongPassword() from e
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (400, 401, 403):
            raise WrongPassword() from e
        raise
    finally:
        scratch.unlink(missing_ok=True)
    subject = f"cm-{client.user_id}"
    # Keep this login: the person's first tool call then needs no second one.
    client._session_path = subject_dir(subject) / "session.json"
    client._save_cached_session()
    with _pool_lock:
        old_web = _user_web_clients.pop(subject, None)
        _user_clients[subject] = client
    if old_web is not None:
        logger.info("Replaced the web session for %s after a new sign-in", subject)
    return subject


def _parse_date(d: str | None) -> date | None:
    if d is None:
        return None
    return date.fromisoformat(d)


def _parse_time(t: str | None) -> dtime | None:
    """Parse a diary timestamp given as HH:MM or HH:MM:SS."""
    if t is None:
        return None
    try:
        return dtime.fromisoformat(t)
    except ValueError:
        raise ValueError(
            f"Invalid time '{t}'. Use HH:MM or HH:MM:SS, e.g. 10:15."
        ) from None


def _parse_local_datetime(value: str, tz) -> datetime:
    """'YYYY-MM-DD HH:MM' (or ISO with a T and seconds) in the account's zone."""
    parsed = datetime.fromisoformat(value.strip().replace(" ", "T", 1))
    return parsed.replace(tzinfo=tz) if parsed.tzinfo is None else parsed.astimezone(tz)


def _ok(data: dict) -> str:
    """Wrap a successful response."""
    return paging.fit({"status": "success", **data})


_WRITE = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,
}
_DESTRUCTIVE = {**_WRITE, "destructiveHint": True}


def _err(e: Exception) -> str:
    """Wrap an error response with actionable messages."""
    import httpx

    if isinstance(e, httpx.HTTPStatusError):
        status = e.response.status_code
        if status == 401 or status == 403:
            msg = "Authentication failed. Cronometer session may have expired -- try again."
        elif status == 429:
            msg = "Rate limit exceeded. Wait a few minutes before retrying."
        elif status == 404:
            msg = f"Resource not found (HTTP {status})."
        else:
            msg = f"Cronometer API error (HTTP {status})."
    elif isinstance(e, httpx.TimeoutException):
        msg = "Request timed out. Cronometer may be slow -- try again."
    elif isinstance(e, httpx.ConnectError):
        msg = "Could not connect to Cronometer. Check network connectivity."
    else:
        msg = f"{type(e).__name__}: {e}"

    return json.dumps({"status": "error", "message": msg})


# ------------------------------------------------------------------
# Diary: read
# ------------------------------------------------------------------


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
def get_food_log(date: str | None = None, include_untracked: bool = True) -> str:
    """Get all diary entries for a given date.

    Returns every food entry logged for the day. Each "Serving" entry is
    enriched (best-effort) with the food's name, source, the serving measure
    (unit name and grams per unit), the number of servings, and that food's
    own nutrient profile scaled to the amount eaten. Non-food entries
    (exercise, biometrics) carry their own name.

    Note: the per-entry "nutrients" are each food's individual contribution,
    which is distinct from the day-level nutrition_summary aggregate below.

    Also returns a top-level energy_summary field with pre-computed
    values most relevant to the user:

      - total_target_kcal: daily calorie target dynamically adjusted
        for expenditure and weight goal (equivalent to Cronometer's
        "Total Target" in the Energy Summary screen)
      - consumed_kcal: total calories consumed
      - remaining_kcal: calories remaining to stay on target
        (total_target_kcal - consumed_kcal). Always report this
        when summarizing the user's day. Prefer this over manually
        deriving values from the burn breakdown fields.

    Also returns a nutrition_summary field with consumed totals for every
    nutrient eaten that day:

      - macros: flat macro totals (energy, protein, carbs, net_carbs, fat,
        fiber, alcohol)
      - nutrients: every nutrient with amount, unit and `tracked`, which says
        whether it has a target set in Cronometer. Untracked ones were still
        eaten; they just have nothing to measure against, so report them as
        plain figures and never as over or under target.

    Args:
        date: Date as YYYY-MM-DD (defaults to today).
        include_untracked: Leave true for everything eaten. False restricts the
            summary to nutrients with targets, matching the app's own summary.
    """
    try:
        client = _get_client()
        day = _parse_date(date)
        data = client.get_diary(day)
        data = client.enrich_diary_servings(data)

        summary = (data or {}).get("summary") or {}
        target = (summary.get("macros") or {}).get("energy")
        consumed = (summary.get("consumed") or {}).get("total")
        energy_summary: dict | None = None
        if target is not None and consumed is not None:
            energy_summary = {
                "total_target_kcal": target,
                "consumed_kcal": consumed,
                "remaining_kcal": round(target - consumed),
            }

        nutrition_summary = client.get_consumed_nutrients(
            day, include_untracked=include_untracked
        )

        return _ok(
            {
                "date": date or str(date_module_today()),
                "energy_summary": energy_summary,
                "nutrition_summary": nutrition_summary,
                "diary": data,
            }
        )
    except Exception as e:
        return _err(e)


# ------------------------------------------------------------------
# Diary: write
# ------------------------------------------------------------------


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
def add_food_entry(
    food_id: int,
    measure_id: int,
    grams: float,
    date: str | None = None,
    translation_id: int = 0,
    diary_group: str = "auto",
    time: str | None = None,
) -> str:
    """Add a food entry to the Cronometer diary.

    Use search_foods to find food_id and measure_id, then
    get_food_details to confirm serving sizes and gram weights.

    Args:
        food_id: Numeric food ID from search_foods results.
        measure_id: Measure/unit ID from get_food_details.
        grams: Weight of the serving in grams. Real grams for recipes too:
            the conversion to Cronometer's batch units happens here.
        date: Date to log as YYYY-MM-DD (defaults to today).
        translation_id: Translation ID from search results (usually 0).
        diary_group: Meal slot -- one of "auto", "breakfast", "lunch",
                     "dinner", "snacks" (case-insensitive, default "auto").
        time: Time of day as HH:MM or HH:MM:SS. Defaults to now. Pass the real
            eating time when logging after the fact; an "auto" diary_group
            then follows that hour instead of the current one.
    """
    try:
        group_map = {
            "auto": 0,
            "breakfast": 1,
            "lunch": 2,
            "dinner": 3,
            "snacks": 4,
        }
        group_key = diary_group.strip().lower()
        group_int = group_map.get(group_key)
        if group_int is None:
            return _err(
                ValueError(
                    f"Invalid diary_group '{diary_group}'. "
                    "Must be one of: auto, breakfast, lunch, dinner, snacks."
                )
            )

        client = _get_client()
        day = _parse_date(date)
        result = client.add_serving(
            food_id=food_id,
            measure_id=measure_id,
            grams=grams,
            translation_id=translation_id,
            day=day,
            diary_group=group_int,
            time=_parse_time(time),
        )
        return _ok(
            {
                "entry": result,
                "note": "Use the serving ID to remove this entry with remove_food_entry.",
            }
        )
    except Exception as e:
        return _err(e)


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
def remove_food_entry(
    entry_ids: list[str],
    date: str | None = None,
) -> str:
    """Remove diary entries of any kind: foods, exercise, biometrics, notes.

    Use get_food_log to find entry IDs (servingId, exerciseId, biometricId or
    noteId). Deleting cannot be undone.

    Args:
        entry_ids: List of entry IDs to remove.
        date: Date the entries belong to as YYYY-MM-DD (defaults to today).
    """
    try:
        client = _get_client()
        day = _parse_date(date)
        # The diary-entries endpoint rejects notes, so those go through the
        # web app's removeNote.
        wanted = {str(i) for i in entry_ids}
        note_ids = [
            str(e["noteId"])
            for e in client.get_diary(day).get("diary", [])
            if e.get("type") == "Note" and str(e.get("noteId")) in wanted
        ]
        removed = []
        others = [i for i in entry_ids if str(i) not in note_ids]
        if others:
            removed += client.delete_entries(others, day).get("removed", [])
        for note_id in note_ids:
            _get_web_client().remove_note(int(note_id))
            removed.append(note_id)
        return _ok(
            {
                "removed": removed,
                "count": len(removed),
                "date": date or str(date_module_today()),
            }
        )
    except Exception as e:
        return _err(e)


# ------------------------------------------------------------------
# Diary: management
# ------------------------------------------------------------------


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
def mark_day_complete(date: str, complete: bool = True) -> str:
    """Mark a diary day as complete or incomplete.

    Args:
        date: Date to mark as YYYY-MM-DD.
        complete: True to mark complete, False for incomplete.
    """
    try:
        client = _get_client()
        day = _parse_date(date)
        result = client.mark_day_complete(day, complete)
        status = "complete" if complete else "incomplete"
        return _ok(
            {
                "date": date,
                "marked": status,
                "result": result,
            }
        )
    except Exception as e:
        return _err(e)


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
def copy_day(date: str | None = None, source_date: str | None = None) -> str:
    """Copy all diary entries from one day onto another.

    Additive -- does not remove existing entries on the destination date, so
    running it twice duplicates them.

    Args:
        date: Destination date as YYYY-MM-DD (defaults to today).
        source_date: Day to copy from, as YYYY-MM-DD. Defaults to the day
            before the destination.
    """
    try:
        client = _get_client()
        day = _parse_date(date)
        if source_date:
            dst = day or client.today()
            src = _parse_date(source_date)
            if src != dst - timedelta(days=1):
                _get_web_client().copy_day(src, dst)
                return _ok({"source_date": str(src), "destination_date": str(dst)})
        result = client.copy_day(to_day=day)
        return _ok(
            {
                "destination_date": date or str(date_module_today()),
                "result": result,
            }
        )
    except Exception as e:
        return _err(e)


# ------------------------------------------------------------------
# Nutrition
# ------------------------------------------------------------------


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
def get_daily_nutrition(date: str | None = None, include_untracked: bool = True) -> str:
    """Get daily nutrition totals for every nutrient eaten that day.

    The response has:

      - summary: flat macro totals (energy, protein, carbs, net_carbs, fat,
        fiber, alcohol). A value is null if nothing that day contained it.
      - nutrients: each with id, name, amount, unit, category, confidence and
        `tracked`.
      - tracked_count and untracked_count.

    `tracked` says whether that nutrient has a target set in Cronometer.
    Untracked nutrients are still eaten and still counted here; they simply have
    nothing to measure against, and Cronometer's own summary leaves them out. So
    an absent nutrient means none was eaten, rather than none being tracked.

    Report an untracked amount as a plain figure. It has no target, so never
    describe it as over, under or on track, and offer set_nutrient_target if a
    target would be useful.

    Args:
        date: Date as YYYY-MM-DD (defaults to today).
        include_untracked: Leave true for everything eaten. False restricts the
            response to nutrients with targets, matching the app's own summary.
    """
    try:
        client = _get_client()
        day = _parse_date(date)
        data = client.get_consumed_nutrients(day, include_untracked=include_untracked)
        return _ok(
            {
                "date": date or str(date_module_today()),
                "summary": data["macros"],
                "nutrients": data["nutrients"],
                "tracked_count": data["tracked_count"],
                "untracked_count": data["untracked_count"],
            }
        )
    except Exception as e:
        return _err(e)


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
def get_nutrition_scores(date: str | None = None) -> str:
    """Get nutrition scores with per-nutrient consumed amounts and category grades.

    Returns category scores (All Targets, Vitamins, Minerals, Electrolytes,
    Antioxidants, Immune Support, Metabolism, Bone Health) with the actual
    consumed amount and confidence level for each nutrient.

    This is Cronometer's own scoring, so it covers only nutrients with a target;
    scoring one without a target would have nothing to score against. Use it to
    see how close each nutrient is to its target, and get_daily_nutrition when
    you need everything that was actually eaten.

    Args:
        date: Date as YYYY-MM-DD (defaults to today).
    """
    try:
        client = _get_client()
        day = _parse_date(date)
        data = client.get_nutrition_scores(day)
        return _ok(
            {
                "date": date or str(date_module_today()),
                "scores": data,
            }
        )
    except Exception as e:
        return _err(e)


# ------------------------------------------------------------------
# Food search and details
# ------------------------------------------------------------------


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
def search_foods(query: str) -> str:
    """Search Cronometer's food database by name.

    Returns matching foods with their IDs and source information.
    Use the food_id and measure_id from results with add_food_entry,
    or pass food_id to get_food_details for full nutrition info.

    Args:
        query: Food name or keyword (e.g. "eggs", "chicken breast").
    """
    try:
        client = _get_client()
        foods = client.search_food(query)

        # Slim down results to the most useful fields
        results = []
        for f in foods:
            results.append(
                {
                    "food_id": f.get("id"),
                    "name": f.get("name"),
                    "source": f.get("source"),
                    "measure_id": f.get("measureId"),
                    "translation_id": f.get("translationId"),
                    "measure_display": f.get("measureDisplayName"),
                    "score": f.get("score"),
                }
            )

        return _ok(
            {
                "query": query,
                "count": len(results),
                "foods": results,
            }
        )
    except Exception as e:
        return _err(e)


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
def get_food_details(food_id: int) -> str:
    """Get detailed food information including nutrition and serving sizes.

    Use this after search_foods to get the full nutrient profile and
    available measure_ids needed for add_food_entry.

    `nutrients` is for one serving of `serving_grams`, keyed and scaled exactly
    as add_custom_food and update_custom_food take them, so a value read here
    can be written straight back unchanged. Cronometer's own stored rows, per
    100 g and by numeric id, are alongside as `nutrients_per_100g`.

    Args:
        food_id: Food ID from search_foods results.
    """
    try:
        return _ok(_get_client().get_food_details(food_id))
    except Exception as e:
        return _err(e)


# ------------------------------------------------------------------
# Custom food creation
# ------------------------------------------------------------------


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
def add_custom_food(
    name: str,
    nutrients: dict[str, float],
    serving_name: str = "1 serving",
    serving_grams: float = 100.0,
    label_type: str = "AMERICAN_2016",
    notes: str | None = None,
) -> str:
    """Create a custom food with any nutrients you have, from 1 to all 94.

    Amounts are for one whole serving of serving_grams, not per 100 g, each in
    that nutrient's own unit. Call list_nutrients for the accepted names and
    their units. Only pass the
    nutrients you actually know: a nutrient you leave out stays blank in
    Cronometer, while passing 0 states the food contains none of it, and the
    app treats those differently. An unrecognised name is an error, so nothing
    is silently dropped from a food that then looks complete.

    Two label conveniences: energy_kj is converted to calories, salt_g to
    sodium. Pass either one or its underlying nutrient, not both.

    After creation, use the returned food_id with add_food_entry to log it.

    Args:
        name: Food name.
        nutrients: Nutrient name to amount per serving, e.g.
            {"energy": 250, "protein": 12.5, "vitamin_c": 30, "b12_cobalamin": 1.2}.
        serving_name: Name for the serving size (default "1 serving").
        serving_grams: Weight of one serving in grams (default 100).
        label_type: "AMERICAN_2016" or "EUROPEAN".
        notes: Free-text note stored on the food.
    """
    try:
        client = _get_client()
        result = client.create_custom_food(
            name,
            nutrients,
            serving_name=serving_name,
            serving_grams=serving_grams,
            label_type=label_type,
            notes=notes,
        )

        # Fetch back to get the server-assigned measure_id
        food_data = client.get_food(result["food_id"])
        result["measure_id"] = food_data.get("defaultMeasureId")

        return _ok(
            {
                "food_id": result["food_id"],
                "measure_id": result["measure_id"],
                "name": name,
                "nutrients_set": len(nutrients),
                "note": "Use food_id and measure_id with add_food_entry to log this food.",
            }
        )
    except Exception as e:
        return _err(e)


_READ = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,
}


@mcp.tool(annotations=_READ)
def get_recent_foods() -> str:
    """Recently logged foods with how often each was logged.

    The fastest route for "log my usual": find the food here, then
    add_food_entry. Recipes are flagged so a whole batch is not logged as one
    portion by mistake.
    """
    try:
        return _ok({"foods": _get_client().get_recent_foods()})
    except Exception as e:
        return _err(e)


@mcp.tool(annotations=_READ)
def get_streak(date: str | None = None) -> str:
    """Diary logging streaks: current run of fully logged days, and the record.

    Args:
        date: Day to count back from as YYYY-MM-DD (defaults to today).
    """
    try:
        return _ok(_get_client().get_streak(_parse_date(date)))
    except Exception as e:
        return _err(e)


@mcp.tool(annotations=_READ)
def get_profile() -> str:
    """The account profile: birthdate, gender, timezone and language."""
    try:
        profile = _get_client().get_profile()
        # The profile also carries an analytics JWT and attribute list; neither
        # belongs in a chat transcript.
        return _ok(
            {"profile": {k: v for k, v in profile.items() if k not in ("JWTSignature", "braze_attributes", "braze_events", "uuid", "result")}}
        )
    except Exception as e:
        return _err(e)


@mcp.tool(annotations=_WRITE)
def create_recipe(
    name: str,
    ingredients: list[dict],
    servings: float = 1.0,
    notes: str | None = None,
    recipe_id: int | None = None,
    serving_type: str = "weight",
    cooked_grams: float | None = None,
) -> str:
    """Create a recipe from ingredients, or update one by passing recipe_id.

    Cronometer sums the nutrients from the ingredients itself, so unlike
    add_custom_food you give foods and amounts, not nutrient values. Use
    search_foods to find each ingredient's food_id and measure_id first.

    Cronometer locks a recipe's serving type when it is created and it cannot
    be changed afterwards, so choose deliberately:

    - "weight" (default) measures the recipe in grams. Portions log as plain
      grams in every client, including the mobile app's own entry screen. The
      ingredient list is recorded in the notes rather than as editable
      ingredients, because Cronometer will not compute a correct weight from
      ingredients that use non-weight measures.
    - "servings" keeps an editable ingredient list in Cronometer, but the
      mobile app then offers only a 1 g serving and hides the amount field.
      Logging through this server is still correct in grams.

    cooked_grams is the weight of the finished dish. Give it whenever the food
    was baked or simmered: nutrients are stored per 100 g, and a dish that lost
    water is denser than its raw ingredients, so without it a portion weighed
    off the plate logs too little.

    Updating with recipe_id replaces the whole ingredient list, so pass every
    ingredient, not only new ones. Recipes are removed with retire_custom_food,
    the same as custom foods.

    Args:
        name: Recipe name.
        ingredients: List of {"food_id": int, "grams": float,
            "measure_id": int (optional)}.
        servings: How many portions the batch makes.
        notes: Free-text note stored on the recipe.
        recipe_id: Existing recipe to update in place.
        serving_type: "weight" or "servings". Cannot be changed later.
        cooked_grams: Weight of the finished dish, if it lost water in cooking.
    """
    try:
        client = _get_client()
        result = client.create_recipe(
            name,
            ingredients,
            servings=servings,
            notes=notes,
            recipe_id=recipe_id or 0,
            serving_type=serving_type,
            cooked_grams=cooked_grams,
        )
        food = client.get_food(result["food_id"])
        measures = [
            {"measure_id": m["id"], "name": m["name"], "grams": m["value"]}
            for m in food.get("measures", [])
        ]
        return _ok(
            {
                **result,
                "servings": servings,
                "measures": measures,
                "note": "Log one portion with add_food_entry using the serving measure.",
            }
        )
    except Exception as e:
        return _err(e)


@mcp.tool(annotations=_READ)
def list_custom_foods(query: str = "", include_retired: bool = False) -> str:
    """Every custom food and recipe you own, with no database results mixed in.

    Leave query empty for the whole library. This is the way to audit it: to
    find, say, every food whose name lost its Finnish characters, list them all
    and read the names, then fix each with update_custom_food.

    Retired foods are hidden by Cronometer's own library listing, so
    include_retired cannot bring them back; it only affects rows already
    returned.

    Args:
        query: Narrow the list by name. Empty lists everything.
        include_retired: Keep retired foods in the result when they appear.
    """
    try:
        foods = _get_client().list_own_foods(query, include_retired)
        return _ok({"count": len(foods), "foods": foods})
    except Exception as e:
        return _err(e)


@mcp.tool(annotations=_READ)
def list_recipes(include_retired: bool = False) -> str:
    """Every recipe you own, with its serving type.

    serving_type says which kind each one is: "weight" logs in grams
    everywhere, "servings" keeps an editable ingredient list but shows a 1 g
    serving in the mobile app. It is fixed at creation and cannot be changed.

    Args:
        include_retired: Keep retired recipes in the result when they appear.
    """
    try:
        foods = [
            f for f in _get_client().list_own_foods("", include_retired)
            if f["is_recipe"]
        ]
        return _ok({"count": len(foods), "recipes": foods})
    except Exception as e:
        return _err(e)


@mcp.tool(annotations=_READ)
def find_entries_by_food(
    food_id: int, start_date: str | None = None, end_date: str | None = None
) -> str:
    """Every diary entry that references a food, with dates and amounts.

    Use before replacing or retiring a food, so its entries can be moved rather
    than left pointing at something retired.

    Cronometer cannot search entries by food, so this reads the diary one day
    at a time and the range costs a request per day. It defaults to the last 30
    days; widen it deliberately.

    Args:
        food_id: The food to look for.
        start_date: First day as YYYY-MM-DD (defaults to 30 days back).
        end_date: Last day as YYYY-MM-DD (defaults to today).
    """
    try:
        client = _get_client()
        entries = client.find_entries_by_food(
            food_id, _parse_date(start_date), _parse_date(end_date)
        )
        return _ok(
            {
                "food_id": food_id,
                "count": len(entries),
                "total_grams": round(sum(e["grams"] or 0 for e in entries), 1),
                "entries": entries,
            }
        )
    except Exception as e:
        return _err(e)


@mcp.tool(annotations=_WRITE)
def update_custom_food(
    food_id: int,
    name: str | None = None,
    notes: str | None = None,
    nutrients: dict[str, float] | None = None,
    measures: list[dict] | None = None,
) -> str:
    """Edit one of your custom foods in place, keeping its diary entries.

    Only what you pass changes. Entries already logged stay attached to this
    same food and their nutrition follows the edit, so a typo or a wrong
    nutrient can be fixed without re-logging anything.

    Nutrients are merged into the existing profile, so correcting one value
    leaves the rest alone. Call list_nutrients for the accepted names.

    Amounts are for one serving, the same basis get_food_details reports and
    returns as `serving_grams`. That is the food's default measure, which is
    not always 100 g: a juice measured in millilitres takes per-millilitre
    values. Read the food first and the two always agree.

    Args:
        food_id: The custom food to edit.
        name: New name.
        notes: New note text.
        nutrients: Nutrient name to amount per serving, merged in.
        measures: [{"measure_id": int, "name": str, "grams": float}] to fix a
            wrongly weighted measure. name and grams are each optional.
            Leave measure_id out to ADD a serving size instead, giving name and
            grams: that is how a food gets a per-piece measure such as
            "1 karkki" or "1 viipale" alongside plain grams, so it can be logged
            by the count as well as by weight.
    """
    try:
        result = _get_client().update_custom_food(
            food_id, name=name, notes=notes, nutrients=nutrients, measures=measures
        )
        return _ok(result)
    except Exception as e:
        return _err(e)


@mcp.tool(annotations=_WRITE)
def update_recipe(
    food_id: int,
    name: str | None = None,
    notes: str | None = None,
    ingredients: list[dict] | None = None,
    cooked_grams: float | None = None,
) -> str:
    """Edit one of your recipes in place, keeping its diary entries.

    Only what you pass changes, and the serving type never does: Cronometer
    fixes it at creation. Passing ingredients replaces the whole list, so send
    every ingredient rather than only the new ones.

    Args:
        food_id: The recipe to edit.
        name: New name.
        notes: New note text.
        ingredients: Full replacement list of
            {"food_id": int, "grams": float, "measure_id": int (optional)}.
        cooked_grams: New finished weight. Weight-based recipes only, and it
            needs the ingredients too, since the nutrition is recomputed.
    """
    try:
        result = _get_client().update_recipe(
            food_id,
            name=name,
            notes=notes,
            ingredients=ingredients,
            cooked_grams=cooked_grams,
        )
        return _ok(result)
    except Exception as e:
        return _err(e)


@mcp.tool(annotations=_DESTRUCTIVE)
def retire_custom_food(food_id: int, retired: bool = True) -> str:
    """Retire a custom food so it stops being offered for new entries.

    This is how Cronometer removes a food; there is no delete. Diary entries
    that already use it keep working. Pass retired=False to bring it back.

    Args:
        food_id: The custom food's ID.
        retired: True to retire, False to restore.
    """
    try:
        _get_client().retire_custom_food(food_id, retired)
        return _ok({"food_id": food_id, "retired": retired})
    except Exception as e:
        return _err(e)


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
def list_nutrients() -> str:
    """Every nutrient add_custom_food accepts, with its unit and category.

    Read from the account's own catalog, so it stays right as Cronometer adds
    nutrients. Use the returned keys for the add_custom_food nutrients dict.
    """
    try:
        index = _get_client().nutrient_index()
        by_category: dict[str, list[dict]] = {}
        for key, meta in sorted(index.items()):
            by_category.setdefault(meta["category"] or "Other", []).append(
                {"key": key, "name": meta["name"], "unit": meta["unit"]}
            )
        return _ok(
            {
                "count": len(index),
                "categories": by_category,
                "conveniences": {
                    "energy_kj": "kJ, converted to calories",
                    "salt_g": "g of salt, converted to sodium in mg",
                },
            }
        )
    except Exception as e:
        return _err(e)


# ------------------------------------------------------------------
# Macro targets
# ------------------------------------------------------------------


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
def get_macro_targets() -> str:
    """Get current macro targets including weekly schedule and templates.

    Returns the weekly macro schedule (which template applies to each day)
    and all saved macro target templates with their values.
    """
    try:
        client = _get_client()
        schedules = client.get_macro_schedules()
        templates = client.get_macro_target_templates()
        return _ok(
            {
                "schedules": schedules,
                "templates": templates,
            }
        )
    except Exception as e:
        return _err(e)


# ------------------------------------------------------------------
# Fasting
# ------------------------------------------------------------------


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
def get_fasting_history(
    start_date: str | None = None,
    end_date: str | None = None,
) -> str:
    """Get fasting history from Cronometer.

    Returns fasts within the date range including status, timestamps,
    and duration.

    Args:
        start_date: Start date as YYYY-MM-DD (defaults to 30 days ago).
        end_date: End date as YYYY-MM-DD (defaults to today).
    """
    try:
        client = _get_client()
        start = _parse_date(start_date)
        end = _parse_date(end_date)
        data = client.get_fasting_with_date_range(start, end)
        return _ok(
            {
                "start_date": start_date
                or str(date_module_today() - timedelta(days=30)),
                "end_date": end_date or str(date_module_today()),
                "fasting": data,
            }
        )
    except Exception as e:
        return _err(e)


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
def get_fasting_stats() -> str:
    """Get aggregate fasting statistics.

    Returns total fasting hours, longest fast, average fast duration,
    and completed fast count.
    """
    try:
        client = _get_client()
        data = client.get_fasting_stats()
        return _ok({"stats": data})
    except Exception as e:
        return _err(e)


# ------------------------------------------------------------------
# Biometrics
# ------------------------------------------------------------------


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
def list_biometrics() -> str:
    """List the biometric metrics tracked in Cronometer.

    Returns every metric type the account can record (Weight, Body Fat,
    Heart Rate, Blood Glucose, Waist Size, Sleep, blood panels, body
    measurements, etc.). Use the metric_id and a unit_id from the results
    with get_biometrics.
    """
    try:
        client = _get_client()
        metrics = client.get_metrics()

        # Slim down results to the fields needed to call get_biometrics
        results = []
        for m in metrics:
            results.append(
                {
                    "metric_id": m.get("id"),
                    "name": m.get("name"),
                    "units": [
                        {"unit_id": u.get("id"), "name": u.get("name")}
                        for u in m.get("units", [])
                    ],
                }
            )

        return _ok({"count": len(results), "metrics": results})
    except Exception as e:
        return _err(e)


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
def get_biometrics(
    metric_id: int,
    unit_id: int,
    start_date: str | None = None,
    end_date: str | None = None,
) -> str:
    """Get a biometric time series such as weight or body fat from Cronometer.

    Returns the recorded values over the date range as a list of
    {day, value} points.

    Use list_biometrics to find metric_id and unit_id (e.g. Weight is
    metric_id 1, with unit_id 1 for kg or 2 for lbs).

    Args:
        metric_id: Numeric metric ID from list_biometrics.
        unit_id: Numeric unit ID from the metric's units in list_biometrics.
        start_date: Start date as YYYY-MM-DD (defaults to 30 days ago).
        end_date: End date as YYYY-MM-DD (defaults to today).
    """
    try:
        client = _get_client()
        data = client.get_biometrics(
            metric_id,
            unit_id,
            start=_parse_date(start_date),
            end=_parse_date(end_date),
        )
        return _ok(
            {
                "metric_id": metric_id,
                "unit_id": unit_id,
                "start_date": start_date
                or str(date_module_today() - timedelta(days=30)),
                "end_date": end_date or str(date_module_today()),
                "biometrics": data,
            }
        )
    except Exception as e:
        return _err(e)


# ------------------------------------------------------------------
# Diary: servings
# ------------------------------------------------------------------


@mcp.tool(annotations=_WRITE)
def edit_food_entry(
    entry_id: int,
    grams: float | None = None,
    time: str | None = None,
    date: str | None = None,
) -> str:
    """Change the amount or time of an already logged food entry.

    Use get_food_log to find entry IDs.

    Args:
        entry_id: The serving ID of the entry to change.
        grams: New amount in grams.
        time: New time as HH:MM or HH:MM:SS.
        date: Date the entry is on as YYYY-MM-DD (defaults to today).
    """
    try:
        client = _get_client()
        stamp = _parse_time(time)
        client.edit_serving(
            entry_id,
            grams=grams,
            time=None if stamp is None else f"{stamp.hour}:{stamp.minute}:{stamp.second}",
            day=_parse_date(date),
        )
        entry = client.find_entry(entry_id, "Serving", _parse_date(date))
        return _ok({"entry_id": entry_id, "grams": entry["grams"], "time": entry["time"]})
    except Exception as e:
        return _err(e)


# ------------------------------------------------------------------
# Diary: notes
# ------------------------------------------------------------------


@mcp.tool(annotations=_WRITE)
def add_note(text: str, date: str | None = None) -> str:
    """Add a note to a day in the Cronometer diary.

    Cronometer cannot delete notes, only rewrite them, so a note added here can
    be changed but only removed in the Cronometer app.

    Args:
        text: The note text.
        date: Date as YYYY-MM-DD (defaults to today).
    """
    try:
        result = _get_client().add_note(text, _parse_date(date))
        return _ok({"note_id": result.get("id"), "date": date or str(date_module_today())})
    except Exception as e:
        return _err(e)


@mcp.tool(annotations=_WRITE)
def edit_note(note_id: int, text: str, date: str | None = None) -> str:
    """Replace the text of an existing diary note.

    Args:
        note_id: The note ID, from get_food_log.
        text: The replacement text.
        date: Date the note is on as YYYY-MM-DD (defaults to today).
    """
    try:
        _get_client().edit_note(note_id, text, _parse_date(date))
        return _ok({"note_id": note_id, "text": text})
    except Exception as e:
        return _err(e)


# ------------------------------------------------------------------
# Diary: biometrics
# ------------------------------------------------------------------


@mcp.tool(annotations=_DESTRUCTIVE)
def remove_note(note_id: int, date: str | None = None) -> str:
    """Delete a note from the diary.

    Args:
        note_id: The noteId, from get_food_log.
        date: The note's day, YYYY-MM-DD (defaults to today); used to confirm it went.
    """
    try:
        _get_web_client().remove_note(note_id)
        day = _parse_date(date)
        left = [
            e
            for e in _get_client().get_diary(day).get("diary", [])
            if e.get("type") == "Note" and e.get("noteId") == note_id
        ]
        if left:
            raise RuntimeError(f"Cronometer accepted the delete but note {note_id} is still there")
        return _ok({"deleted": note_id})
    except Exception as e:
        return _err(e)


@mcp.tool(annotations=_WRITE)
def add_biometric(
    metric_id: int,
    unit_id: int,
    amount: float,
    date: str | None = None,
) -> str:
    """Record a biometric measurement, such as weight or body fat.

    Use list_biometrics to find metric IDs and their valid unit IDs. Correct a
    wrong value with edit_biometric, or drop the reading entirely with
    remove_biometric when the measurement never happened.

    Args:
        metric_id: Metric to record, from list_biometrics.
        unit_id: Unit the amount is in, from that metric's units.
        amount: The measured value.
        date: Date as YYYY-MM-DD (defaults to today).
    """
    try:
        result = _get_client().add_biometric(
            metric_id, unit_id, amount, _parse_date(date)
        )
        return _ok(
            {
                "biometric_id": result.get("id"),
                "metric_id": metric_id,
                "amount": amount,
                "date": date or str(date_module_today()),
            }
        )
    except Exception as e:
        return _err(e)


@mcp.tool(annotations=_WRITE)
def edit_biometric(biometric_id: int, amount: float, date: str | None = None) -> str:
    """Change the value of a recorded biometric.

    Args:
        biometric_id: The biometric ID, from get_food_log.
        amount: The corrected value, in the unit the entry already uses.
        date: Date the entry is on as YYYY-MM-DD (defaults to today).
    """
    try:
        _get_client().edit_biometric(biometric_id, amount, _parse_date(date))
        return _ok({"biometric_id": biometric_id, "amount": amount})
    except Exception as e:
        return _err(e)


@mcp.tool(annotations=_DESTRUCTIVE)
def remove_biometric(biometric_ids: list[str], date: str | None = None) -> str:
    """Delete recorded biometrics, such as a reading from a misbehaving scale.

    Deleting is right when the measurement never happened or is impossible;
    edit_biometric is for a value that is merely wrong, since editing keeps the
    day's entry and its history.

    Args:
        biometric_ids: List of biometric IDs to remove, from get_food_log.
        date: Date the entries belong to as YYYY-MM-DD (defaults to today).
    """
    try:
        client = _get_client()
        result = client.delete_entries(
            biometric_ids, _parse_date(date), entry_type="Biometric"
        )
        return _ok(
            {
                "removed": result.get("removed", []),
                "count": result.get("count", 0),
                "date": date or str(date_module_today()),
            }
        )
    except Exception as e:
        return _err(e)


# ------------------------------------------------------------------
# Diary: exercise
# ------------------------------------------------------------------


@mcp.tool(annotations=_WRITE)
def add_exercise(
    name: str,
    minutes: int,
    calories_burned: float,
    date: str | None = None,
    activity_id: int = 0,
) -> str:
    """Log an exercise entry.

    A wrong entry can be corrected with edit_exercise or deleted outright with
    remove_exercise.

    Args:
        name: What the exercise was called.
        minutes: Duration in minutes.
        calories_burned: Calories burned, as a positive number.
        date: Date as YYYY-MM-DD (defaults to today).
        activity_id: Optional Cronometer activity, from search_activities.
            Leave 0 for a free-form entry named `name`.
    """
    try:
        result = _get_client().add_exercise(
            name, minutes, calories_burned, _parse_date(date), activity_id=activity_id
        )
        return _ok(
            {
                "exercise_id": result.get("id"),
                "name": name,
                "minutes": minutes,
                "calories_burned": calories_burned,
                "date": date or str(date_module_today()),
            }
        )
    except Exception as e:
        return _err(e)


@mcp.tool(annotations=_WRITE)
def edit_exercise(
    exercise_id: int,
    minutes: int | None = None,
    calories_burned: float | None = None,
    date: str | None = None,
) -> str:
    """Change the duration or calorie burn of a logged exercise.

    Args:
        exercise_id: The exercise ID, from get_food_log.
        minutes: New duration in minutes.
        calories_burned: New burn, as a positive number.
        date: Date the entry is on as YYYY-MM-DD (defaults to today).
    """
    try:
        _get_client().edit_exercise(
            exercise_id,
            minutes=minutes,
            calories_burned=calories_burned,
            day=_parse_date(date),
        )
        return _ok({"exercise_id": exercise_id})
    except Exception as e:
        return _err(e)


@mcp.tool(annotations=_DESTRUCTIVE)
def remove_exercise(exercise_ids: list[str], date: str | None = None) -> str:
    """Delete logged exercise entries.

    Use this for an entry that should not be there at all, such as a duplicate
    from a tracker sync. edit_exercise is for one whose duration or burn is
    merely wrong.

    Args:
        exercise_ids: List of exercise IDs to remove, from get_food_log.
        date: Date the entries belong to as YYYY-MM-DD (defaults to today).
    """
    try:
        client = _get_client()
        result = client.delete_entries(
            exercise_ids, _parse_date(date), entry_type="Exercise"
        )
        return _ok(
            {
                "removed": result.get("removed", []),
                "count": result.get("count", 0),
                "date": date or str(date_module_today()),
            }
        )
    except Exception as e:
        return _err(e)


# ------------------------------------------------------------------
# Fasting
# ------------------------------------------------------------------


@mcp.tool(annotations=_WRITE)
def add_fast(start: str, end: str | None = None, goal_hours: float = 16) -> str:
    """Record a fast.

    Args:
        start: When the fast started, as YYYY-MM-DD HH:MM.
        end: When it ended, same format. Omit for an ongoing fast.
        goal_hours: Target length in hours.
    """
    try:
        client = _get_client()
        tz = client._tzinfo()
        started = _parse_local_datetime(start, tz)
        ended = _parse_local_datetime(end, tz) if end else None
        result = client.add_fast(started, ended, goal_hours)
        return _ok({"fast_id": result.get("id"), "start": start, "end": end})
    except Exception as e:
        return _err(e)


@mcp.tool(annotations=_WRITE)
def edit_fast(
    fast_id: int,
    start: str | None = None,
    end: str | None = None,
    goal_hours: float | None = None,
) -> str:
    """Change a recorded fast, including ending one that is still open.

    Args:
        fast_id: The fast ID, from get_fasting_history.
        start: New start as YYYY-MM-DD HH:MM.
        end: New end as YYYY-MM-DD HH:MM.
        goal_hours: New target length in hours.
    """
    try:
        client = _get_client()
        tz = client._tzinfo()

        def parse(s: str) -> datetime:
            return _parse_local_datetime(s, tz)

        client.edit_fast(
            fast_id,
            start=parse(start) if start else None,
            end=parse(end) if end else None,
            goal_hours=goal_hours,
        )
        return _ok({"fast_id": fast_id})
    except Exception as e:
        return _err(e)


@mcp.tool(annotations=_DESTRUCTIVE)
def delete_fast(fast_id: int) -> str:
    """Remove a recorded fast.

    Args:
        fast_id: The fast ID, from get_fasting_history.
    """
    try:
        _get_client().delete_fast(fast_id)
        return _ok({"deleted": fast_id})
    except Exception as e:
        return _err(e)


# ------------------------------------------------------------------
# Targets
# ------------------------------------------------------------------


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
def get_targets() -> str:
    """Nutrient targets for the account, as shown beside the diary totals."""
    try:
        client = _get_client()
        defs = client.get_nutrient_definitions()
        rows = []
        for row in client.get_targets().get("targets", []):
            meta = defs.get(row.get("id"), {})
            rows.append({**row, "name": meta.get("name"), "unit": meta.get("unit")})
        return _ok({"targets": rows})
    except Exception as e:
        return _err(e)


@mcp.tool(annotations=_WRITE)
def set_nutrient_target(
    nutrient: str,
    minimum: float | None = None,
    maximum: float | None = None,
    visible: bool | None = None,
) -> str:
    """Set a nutrient's daily target, its upper limit, or whether it is tracked.

    Giving a minimum or maximum makes it a custom target, replacing the default
    Cronometer works out from the profile. Setting visible turns tracking of
    that nutrient on or off, which is how a micronutrient starts showing up in
    the diary at all.

    Only what you pass changes: the rest of the nutrient's settings are read
    first and kept, so turning on visibility never disturbs an existing target.

    Going back to Cronometer's own default is done in the app, under Settings
    then Targets. Report the current value from get_targets before overwriting
    one, so it can be put back by hand if wanted.

    Args:
        nutrient: Nutrient name, e.g. "protein", "iodine", "choline", "biotin".
            Call list_nutrients for the accepted names.
        minimum: Daily target, in that nutrient's own unit.
        maximum: Upper limit, in that nutrient's own unit.
        visible: True to track the nutrient, False to hide it.
    """
    try:
        client = _get_client()
        if minimum is None and maximum is None and visible is None:
            return _err(ValueError("Give at least one of minimum, maximum or visible"))
        previous = None
        index = client.nutrient_index()
        entry = index.get(nutrient.strip().lower().replace(" ", "_"))
        if entry:
            previous = client.find_target(entry["id"])
        result = client.set_target(
            nutrient, minimum=minimum, maximum=maximum, visible=visible
        )
        return _ok({**result, "previous": previous})
    except Exception as e:
        return _err(e)


# ------------------------------------------------------------------
# Barcodes, activities, recipe import, sleep
# ------------------------------------------------------------------


@mcp.tool(annotations=_READ)
def lookup_barcode(barcode: str) -> str:
    """Find a packaged food by its UPC/EAN barcode.

    Returns matching foods with the same fields as search_foods, so a hit can
    go straight to get_food_details or add_food_entry.

    Args:
        barcode: The digits printed under the barcode.
    """
    try:
        foods = _get_client().lookup_barcode(barcode)
        return _ok(
            {
                "barcode": barcode,
                "count": len(foods),
                "foods": [
                    {
                        "food_id": f.get("id"),
                        "name": f.get("name"),
                        "source": f.get("source"),
                        "measure_id": f.get("measureId") or f.get("defaultMeasureId"),
                        "measures": [
                            {"measure_id": m.get("id"), "name": m.get("name"), "grams": m.get("value")}
                            for m in f.get("measures") or []
                            if isinstance(m, dict)
                        ],
                        "barcodes": f.get("barcodes"),
                    }
                    for f in foods
                ],
            }
        )
    except Exception as e:
        return _err(e)


@mcp.tool(annotations=_READ)
def search_activities(query: str) -> str:
    """Search Cronometer's exercise activities by name, for add_exercise.

    Every word of the query must appear in the name, e.g. "running 6 mph" or
    "cycling". Returns up to 50 activity ids.

    Args:
        query: Words to look for in the activity name.
    """
    try:
        words = query.lower().split()
        matches = [
            a
            for a in _get_client().get_activities()
            if all(w in (a.get("name") or "").lower() for w in words)
        ]
        return _ok({"query": query, "count": len(matches), "activities": matches[:50]})
    except Exception as e:
        return _err(e)


@mcp.tool(annotations=_WRITE)
def import_recipe(
    ingredients: str,
    name: str | None = None,
    servings: float = 1.0,
    save: bool = True,
) -> str:
    """Create a recipe from a free-text ingredient list.

    Cronometer's own parser matches each line ("2 tbsp ketchup", "1 cup rice")
    to a database food and works out the grams, the same as Import Recipe in
    the app. Matching is fuzzy: show the matched ingredients to the user, and
    run with save=false first when unsure. create_recipe is the exact
    alternative when food ids and grams are already known.

    Args:
        ingredients: One ingredient per line.
        name: Recipe name. Defaults to the one Cronometer suggests.
        servings: How many portions the batch makes.
        save: False to only parse and preview the matches.
    """
    try:
        result = _get_client().import_recipe(
            ingredients, name=name, servings=servings, save=save
        )
        return _ok(result)
    except Exception as e:
        return _err(e)


def _metric_named(metrics: list[dict], *names: str) -> dict | None:
    wanted = {n.lower() for n in names}
    return next((m for m in metrics if (m.get("name") or "").lower() in wanted), None)


@mcp.tool(annotations=_WRITE)
def log_sleep(
    hours: float,
    date: str | None = None,
    deep_hours: float | None = None,
    light_hours: float | None = None,
    rem_hours: float | None = None,
    score: float | None = None,
) -> str:
    """Record a night's sleep, optionally with its stages and a sleep score.

    Each value is stored as its own biometric (Sleep, and the Deep, Light, REM
    and Score metrics when the account has them), so they show in Cronometer's
    trends. Fix or remove one with edit_biometric or remove_biometric.

    Args:
        hours: Total sleep in hours, e.g. 7.5.
        date: The diary day it belongs to, as YYYY-MM-DD (defaults to today).
        deep_hours: Deep sleep in hours.
        light_hours: Light sleep in hours.
        rem_hours: REM sleep in hours.
        score: Sleep score from 0 to 100.
    """
    try:
        if hours <= 0:
            raise ValueError("hours must be positive")
        client = _get_client()
        metrics = client.get_metrics()
        wanted = {
            "sleep": (hours, ("Sleep",)),
            "deep": (deep_hours, ("Sleep Deep", "Deep Sleep")),
            "light": (light_hours, ("Sleep Light", "Light Sleep")),
            "rem": (rem_hours, ("Sleep REM", "REM Sleep")),
            "score": (score, ("Sleep Score",)),
        }
        recorded, skipped = {}, {}
        for key, (amount, names) in wanted.items():
            if amount is None:
                continue
            metric = _metric_named(metrics, *names)
            if not metric or not metric.get("units"):
                skipped[key] = f"no {names[0]} metric on this account"
                continue
            units = metric["units"]
            # Hours where the metric offers them, else its first unit.
            unit = next(
                (u for u in units if (u.get("name") or "").lower() in ("h", "hr", "hrs", "hours")),
                units[0],
            )
            amount_in_unit = amount
            unit_name = (unit.get("name") or "").lower()
            if unit_name in ("min", "mins", "minutes"):
                amount_in_unit = amount * 60
            result = client.add_biometric(
                metric["id"], unit["id"], amount_in_unit, _parse_date(date)
            )
            recorded[key] = {
                "biometric_id": result.get("id"),
                "metric": metric.get("name"),
                "amount": amount_in_unit,
                "unit": unit.get("name"),
            }
        return _ok(
            {"date": date or str(date_module_today()), "recorded": recorded, "skipped": skipped}
        )
    except Exception as e:
        return _err(e)


@mcp.tool(annotations=_READ)
def get_nutrition_summary(
    start_date: str, end_date: str | None = None, nutrients: list[str] | None = None
) -> str:
    """Daily macro totals over a range of days, with averages.

    Each day carries energy, protein, carbs, net carbs, fat, fiber and alcohol.
    Days with nothing logged are listed but left out of the averages. Pass
    nutrient names (e.g. ["vitamin_d", "iron"]) to add those as well. At most
    62 days per call.

    Args:
        start_date: First day, YYYY-MM-DD.
        end_date: Last day, YYYY-MM-DD (defaults to today).
        nutrients: Extra nutrient names to include, as in list_nutrients.
    """
    try:
        client = _get_client()
        start = _parse_date(start_date)
        end = _parse_date(end_date) or client.today()
        if end < start:
            raise ValueError("end_date is before start_date")
        if (end - start).days > 61:
            raise ValueError("At most 62 days per call; split the range.")
        extra = [n.strip().lower().replace(" ", "_") for n in (nutrients or [])]
        days, totals, logged = [], {}, 0
        day = start
        while day <= end:
            data = client.get_consumed_nutrients(day, include_untracked=bool(extra))
            row = {"date": str(day), **data["macros"]}
            for name in extra:
                hit = next(
                    (
                        n
                        for n in data["nutrients"]
                        if (n.get("name") or "").lower().replace(" ", "_").startswith(name)
                    ),
                    None,
                )
                row[name] = hit.get("amount") if hit else None
            days.append(row)
            if row.get("energy"):
                logged += 1
                for k, v in row.items():
                    if k != "date" and isinstance(v, (int, float)):
                        totals[k] = totals.get(k, 0) + v
            day += timedelta(days=1)
        averages = {k: round(v / logged, 2) for k, v in totals.items()} if logged else {}
        return _ok(
            {
                "start_date": str(start),
                "end_date": str(end),
                "days_logged": logged,
                "averages": averages,
                "days": days,
            }
        )
    except Exception as e:
        return _err(e)


# ------------------------------------------------------------------
# Web-app features: repeated items, macro templates, exports
# ------------------------------------------------------------------
# These calls go through the Cronometer web app (GWT-RPC), the only place they
# exist. Same credentials, separate session.

_web_client = None


def _get_web_client():
    global _web_client
    from .accounts import OWNER, subject_dir
    from .web import CronometerWebClient

    subject = _subject()
    if subject != OWNER:
        with _pool_lock:
            web = _user_web_clients.get(subject)
            if web is None:
                email, password = _user_credentials(subject)
                web = CronometerWebClient(
                    email, password, cookie_path=subject_dir(subject) / ".session_cookies"
                )
                _user_web_clients[subject] = web
        return web
    if _web_client is None:
        _web_client = CronometerWebClient()
    return _web_client


def _web_today() -> date:
    try:
        return date_module_today()
    except Exception:
        return date.today()


_DAY_NAMES = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]


def _parse_days(days: str) -> list[int]:
    """'all', 'weekdays', 'weekends', or names/numbers (0=Sun) comma separated."""
    key = days.strip().lower()
    if key in ("all", "every day", "daily"):
        return list(range(7))
    if key == "weekdays":
        return [1, 2, 3, 4, 5]
    if key == "weekends":
        return [0, 6]
    out = []
    for part in key.split(","):
        part = part.strip()
        if not part:
            continue
        if part.isdigit() and 0 <= int(part) <= 6:
            out.append(int(part))
        elif part[:3] in _DAY_NAMES:
            out.append(_DAY_NAMES.index(part[:3]))
        else:
            raise ValueError(f"Unknown day '{part}'. Use names like Mon or numbers 0=Sun..6=Sat.")
    return sorted(set(out))


def _resolve_web_group(web, diary_group: str | int | None) -> int:
    """Web diary group slot (1-based) from a name or 1-based number."""
    groups = web.diary_groups
    if diary_group is None:
        return _meal_group_now()
    if isinstance(diary_group, int) or str(diary_group).isdigit():
        return int(diary_group)
    name = str(diary_group).strip().lower()
    for g in groups:
        if (g.get("name") or "").lower() == name:
            return int(g["wire_index"])
    raise ValueError(
        f"Unknown diary group '{diary_group}'. Groups: {[g.get('name') for g in groups]}"
    )


def _meal_group_now() -> int:
    from .client import _meal_group_for_hour

    return _meal_group_for_hour(_get_client().now().hour)


@mcp.tool(annotations=_READ)
def list_diary_groups() -> str:
    """The diary groups (meals) this account uses, e.g. Breakfast, Lunch."""
    try:
        return _ok({"groups": _get_web_client().diary_groups})
    except Exception as e:
        return _err(e)


@mcp.tool(annotations=_READ)
def get_repeated_items() -> str:
    """Foods set to log themselves automatically on chosen days of the week."""
    try:
        items = []
        for item in _get_web_client().get_repeated_items():
            # Rename the web ids to the ones every other tool uses.
            item = dict(item)
            item["food_id"], item["measure_id"] = item.pop("food_source_id"), item.pop("food_id")
            item["days"] = [_DAY_NAMES[d].title() for d in item.get("days_of_week") or []]
            items.append(item)
        return _ok({"count": len(items), "items": items})
    except Exception as e:
        return _err(e)


@mcp.tool(annotations=_WRITE)
def add_repeat_item(
    quantity: float,
    food_id: int | None = None,
    measure_id: int | None = None,
    food_name: str | None = None,
    days: str = "all",
    diary_group: str | None = None,
) -> str:
    """Make a food log itself automatically on chosen days of the week.

    Give food_id (from search_foods) and optionally measure_id; without a
    measure the food's default serving is used. Or give only food_name and
    the best search match is used: its name is echoed back, so check it.

    Args:
        quantity: How many of the measure, e.g. 1 for one medium banana.
        food_id: From search_foods.
        measure_id: A measure of that food, from get_food_details.
        food_name: Search text, when food_id is not known.
        days: "all", "weekdays", "weekends", or e.g. "Mon,Wed,Fri".
        diary_group: Meal to log into, e.g. "Breakfast" (defaults by time of day).
    """
    try:
        web = _get_web_client()
        client = _get_client()
        if food_id is None:
            if not food_name:
                raise ValueError("Give food_id (from search_foods) or food_name")
            hits = client.search_food(food_name)
            if not hits:
                raise ValueError(f"No food found for '{food_name}'")
            food_id = hits[0]["id"]
        food = client.get_food(food_id)
        name = food.get("name") or food_name or str(food_id)
        measure_id = measure_id or food.get("defaultMeasureId")
        day_list = _parse_days(days)
        group = _resolve_web_group(web, diary_group)
        # The web call names these differently: its "food source" is the food
        # and its "food id" is the measure.
        web.add_repeat_item(
            food_source_id=food_id,
            food_id=measure_id,
            quantity=quantity,
            food_name=name,
            diary_group=group,
            days_of_week=day_list,
        )
        return _ok(
            {
                "food_name": name,
                "food_id": food_id,
                "measure_id": measure_id,
                "quantity": quantity,
                "diary_group": group,
                "days": [_DAY_NAMES[d].title() for d in day_list],
            }
        )
    except Exception as e:
        return _err(e)


@mcp.tool(annotations=_DESTRUCTIVE)
def delete_repeat_item(repeat_item_id: int) -> str:
    """Stop a food from repeating. Entries it already logged are kept.

    Args:
        repeat_item_id: From get_repeated_items.
    """
    try:
        _get_web_client().delete_repeat_item(repeat_item_id)
        return _ok({"deleted": repeat_item_id})
    except Exception as e:
        return _err(e)


def _day_macro_targets(day: date) -> dict:
    """The macro targets in force on a day: its own override if it has one,
    else the template the weekly schedule gives it."""
    targets = _get_web_client().get_daily_macro_targets(day)
    if targets is not None:
        return {**targets, "override": True}
    template = _get_client().get_diary(day).get("template") or {}
    return {
        "protein_g": template.get("protein"),
        "fat_g": template.get("fat"),
        "carbs_g": template.get("carbs"),
        "calories": template.get("energy"),
        "template_name": template.get("name"),
        "override": False,
    }


@mcp.tool(annotations=_READ)
def get_daily_macro_targets(date: str | None = None) -> str:
    """Calorie, protein, carb and fat targets that apply on one day.

    Args:
        date: YYYY-MM-DD (defaults to today).
    """
    try:
        day = _parse_date(date) or _web_today()
        return _ok({"date": str(day), "targets": _day_macro_targets(day)})
    except Exception as e:
        return _err(e)


@mcp.tool(annotations=_WRITE)
def set_macro_targets(
    date: str | None = None,
    calories: float | None = None,
    protein_grams: float | None = None,
    carbs_grams: float | None = None,
    fat_grams: float | None = None,
    template_name: str | None = None,
) -> str:
    """Change the calorie and macro targets for one day.

    Only the values given change; the rest are read first and kept. For a
    lasting change across the week, use create_macro_template and
    set_weekly_macro_schedule instead.

    Args:
        date: YYYY-MM-DD (defaults to today).
        calories: Energy target in kcal.
        protein_grams: Protein target in grams.
        carbs_grams: Net carbs target in grams.
        fat_grams: Fat target in grams.
        template_name: Label for the day's targets (defaults to "Custom Targets").
    """
    try:
        web = _get_web_client()
        day = _parse_date(date) or _web_today()
        current = _day_macro_targets(day)
        new = {
            "protein_g": protein_grams if protein_grams is not None else current.get("protein_g"),
            "fat_g": fat_grams if fat_grams is not None else current.get("fat_g"),
            "carbs_g": carbs_grams if carbs_grams is not None else current.get("carbs_g"),
            "calories": calories if calories is not None else current.get("calories"),
        }
        unset = [k for k, v in new.items() if v is None]
        if unset:
            raise ValueError(f"No current target for {unset} on {day}; pass explicit values.")
        web.update_daily_targets(day=day, template_name=template_name or "Custom Targets", **new)
        return _ok(
            {"date": str(day), "previous": current, "updated": _day_macro_targets(day)}
        )
    except Exception as e:
        return _err(e)


@mcp.tool(annotations=_READ)
def list_macro_templates() -> str:
    """Saved macro target templates, with their ids and values."""
    try:
        templates = _get_web_client().get_macro_target_templates()
        return _ok({"count": len(templates), "templates": templates})
    except Exception as e:
        return _err(e)


@mcp.tool(annotations=_WRITE)
def create_macro_template(
    template_name: str,
    calories: float,
    protein_grams: float,
    carbs_grams: float,
    fat_grams: float,
    days: str | None = None,
) -> str:
    """Save a new macro target template, and optionally schedule it.

    Args:
        template_name: Name for the template; must not already exist.
        calories: Energy target in kcal.
        protein_grams: Protein in grams.
        carbs_grams: Net carbs in grams.
        fat_grams: Fat in grams.
        days: Also make it the weekly default on these days: "all",
            "weekdays", "weekends" or e.g. "Mon,Wed,Fri".
    """
    try:
        web = _get_web_client()
        for t in web.get_macro_target_templates():
            if t["template_name"] == template_name:
                raise ValueError(
                    f"Template '{template_name}' already exists (id={t['template_id']})."
                )
        template_id = web.save_macro_target_template(
            template_name=template_name,
            protein_g=protein_grams,
            fat_g=fat_grams,
            carbs_g=carbs_grams,
            calories=calories,
        )
        result = {"template_name": template_name, "template_id": template_id}
        if days and template_id:
            for dow in _parse_days(days):
                web.save_macro_schedule(dow, template_id)
            result["schedule"] = web.get_all_macro_schedules()
        return _ok(result)
    except Exception as e:
        return _err(e)


@mcp.tool(annotations=_DESTRUCTIVE)
def delete_macro_template(template_id: int) -> str:
    """Delete a saved macro target template.

    Args:
        template_id: From list_macro_templates.
    """
    try:
        _get_web_client().delete_macro_target_template(template_id)
        return _ok({"deleted": template_id})
    except Exception as e:
        return _err(e)


@mcp.tool(annotations=_WRITE)
def set_weekly_macro_schedule(template_name: str, days: str = "all") -> str:
    """Make a saved template the default targets on chosen days of the week.

    This changes the recurring schedule for every future week.

    Args:
        template_name: A template from list_macro_templates, or "default" to
            put those days back on the profile's default targets.
        days: "all", "weekdays", "weekends" or e.g. "Mon,Wed,Fri".
    """
    try:
        web = _get_web_client()
        templates = web.get_macro_target_templates()
        if template_name.strip().lower() == "default":
            # Template 0 is the profile's own default targets.
            templates = [{"template_id": 0, "template_name": "Default"}, *templates]
        match = next((t for t in templates if t["template_name"] == template_name), None)
        if match is None:
            match = next(
                (t for t in templates if t["template_name"].lower() == template_name.lower()),
                None,
            )
        if match is None:
            raise ValueError(
                f"Template '{template_name}' not found. "
                f"Available: {[t['template_name'] for t in templates]}"
            )
        day_list = _parse_days(days)
        for dow in day_list:
            web.save_macro_schedule(dow, match["template_id"])
        return _ok(
            {
                "template_name": match["template_name"],
                "days": [_DAY_NAMES[d].title() for d in day_list],
                "schedule": web.get_all_macro_schedules(),
            }
        )
    except Exception as e:
        return _err(e)


@mcp.tool(annotations=_DESTRUCTIVE)
def cancel_active_fast(fast_id: int) -> str:
    """Cancel a fast that is still running. A recurring fast keeps its
    schedule; a one-off fast is removed.

    To end it with the time fasted kept instead, use edit_fast with an end.

    Args:
        fast_id: The running fast's id, from get_fasting_history.
    """
    try:
        try:
            _get_web_client().cancel_fast_keep_series(fast_id)
            return _ok({"cancelled": fast_id, "series_kept": True})
        except Exception as e:
            # The web call only accepts fasts that belong to a recurring
            # series. A one-off fast has nothing to keep: cancelling it is
            # removing it, as the app does.
            logger.info("cancelFastAndKeepSeries refused %s (%s); deleting it", fast_id, e)
            fast = next(
                (f for f in _get_client().get_fasting() if f.get("fastId") == fast_id), None
            )
            if fast is None:
                raise ValueError(f"No fast with id {fast_id}") from None
            _get_client().delete_fast(fast_id)
            return _ok({"cancelled": fast_id, "series_kept": False, "removed": True})
    except Exception as e:
        return _err(e)


_EXPORT_TYPES = ("servings", "daily_summary", "exercises", "biometrics", "notes")


@mcp.tool(annotations=_READ)
def export_data(
    data_type: str,
    start_date: str,
    end_date: str | None = None,
    format: str = "json",
) -> str:
    """Cronometer's own data export, for long date ranges.

    data_type is one of:
      - servings: every food logged, with its full nutrients
      - daily_summary: one row of nutrient totals per day
      - exercises, biometrics, notes

    The rows are the columns of Cronometer's CSV export. Large results are
    paged; follow the get_result_page hint.

    Args:
        data_type: servings, daily_summary, exercises, biometrics or notes.
        start_date: First day, YYYY-MM-DD.
        end_date: Last day, YYYY-MM-DD (defaults to today).
        format: "json" for parsed rows or "csv" for the raw text.
    """
    try:
        if data_type not in _EXPORT_TYPES:
            raise ValueError(f"data_type must be one of {list(_EXPORT_TYPES)}")
        web = _get_web_client()
        start = _parse_date(start_date)
        end = _parse_date(end_date) or _web_today()
        if format == "csv":
            return _ok({"data_type": data_type, "csv": web.export_raw(data_type, start, end)})
        rows = web.export_parsed(data_type, start, end)
        return _ok(
            {
                "data_type": data_type,
                "start_date": str(start),
                "end_date": str(end),
                "count": len(rows),
                "rows": rows,
            }
        )
    except Exception as e:
        return _err(e)


# ------------------------------------------------------------------
# Analysis
# ------------------------------------------------------------------


@mcp.tool(annotations=_READ)
def connection_status() -> str:
    """Check the Cronometer login works, and which account it is."""
    try:
        client = _get_client()
        profile = client.get_profile()
        return _ok(
            {
                "connected": True,
                "user_id": client.user_id,
                "timezone": str(client._tzinfo()),
                "today": str(client.today()),
                "profile": {
                    k: profile.get(k) for k in ("birthdate", "gender", "timezone", "localization")
                },
                "server_version": __version__,
            }
        )
    except Exception as e:
        return _err(e)


def _target_bounds(row: dict) -> tuple[float | None, float | None]:
    lo = row.get("min", row.get("minimum"))
    hi = row.get("max", row.get("maximum"))
    return (lo if isinstance(lo, (int, float)) else None, hi if isinstance(hi, (int, float)) else None)


@mcp.tool(annotations=_READ)
def nutrient_report(date: str | None = None) -> str:
    """Every tracked nutrient for a day against its target: amount, target,
    upper limit and percent of target, lowest first.

    Good for spotting gaps (what to eat more of) and anything over a limit.

    Args:
        date: YYYY-MM-DD (defaults to today).
    """
    try:
        client = _get_client()
        day = _parse_date(date)
        consumed = {
            n["id"]: n
            for n in client.get_consumed_nutrients(day, include_untracked=True)["nutrients"]
        }
        defs = client.get_nutrient_definitions()
        rows = []
        for target in client.get_targets().get("targets", []):
            nid = target.get("id")
            lo, hi = _target_bounds(target)
            if lo is None and hi is None:
                continue
            meta = defs.get(nid, {})
            amount = (consumed.get(nid) or {}).get("amount") or 0
            rows.append(
                {
                    "nutrient": meta.get("name") or (consumed.get(nid) or {}).get("name"),
                    "unit": meta.get("unit"),
                    "amount": round(amount, 2),
                    "target": lo,
                    "limit": hi,
                    "percent_of_target": round(100 * amount / lo, 1) if lo else None,
                    "over_limit": bool(hi and amount > hi),
                }
            )
        rows.sort(key=lambda r: (r["percent_of_target"] is None, r["percent_of_target"] or 0))
        return _ok(
            {
                "date": date or str(date_module_today()),
                "below_target": [r["nutrient"] for r in rows if (r["percent_of_target"] or 100) < 100],
                "over_limit": [r["nutrient"] for r in rows if r["over_limit"]],
                "nutrients": rows,
            }
        )
    except Exception as e:
        return _err(e)


def _group_names() -> dict[int, str]:
    """Diary group slot -> name, from the web app when it answers."""
    names = {1: "Breakfast", 2: "Lunch", 3: "Dinner", 4: "Snacks"}
    try:
        for g in _get_web_client().diary_groups:
            names[int(g["wire_index"])] = g.get("name") or names.get(int(g["wire_index"]))
    except Exception as e:
        logger.info("Using default diary group names: %s", e)
    return names


@mcp.tool(annotations=_READ)
def analyze_day(date: str | None = None, top: int = 10) -> str:
    """A day at a glance: calories and macros per meal, and the foods that
    contributed the most energy and protein.

    Args:
        date: YYYY-MM-DD (defaults to today).
        top: How many top foods to list.
    """
    try:
        client = _get_client()
        day = _parse_date(date)
        diary = client.enrich_diary_servings(client.get_diary(day))
        macro_ids = {"energy": 208, "protein": 203, "carbs": 205, "fat": 204, "fiber": 291}
        names = _group_names()
        groups: dict[str, dict] = {}
        foods = []
        for entry in diary.get("diary") or []:
            if not isinstance(entry, dict) or entry.get("type") != "Serving":
                continue
            amounts = {}
            for n in entry.get("nutrients") or []:
                for key, nid in macro_ids.items():
                    if n.get("id") == nid:
                        amounts[key] = n.get("amount") or 0
            group = names.get((entry.get("order") or 0) >> 16, "Uncategorized")
            bucket = groups.setdefault(group, {k: 0 for k in macro_ids} | {"items": 0})
            bucket["items"] += 1
            for k, v in amounts.items():
                bucket[k] = round(bucket[k] + v, 1)
            foods.append(
                {"name": entry.get("name"), "grams": entry.get("grams"), "group": group, **amounts}
            )
        totals = client.get_consumed_nutrients(day, include_untracked=False)["macros"]
        return _ok(
            {
                "date": date or str(date_module_today()),
                "totals": totals,
                "by_group": groups,
                "top_energy": sorted(foods, key=lambda f: -(f.get("energy") or 0))[:top],
                "top_protein": sorted(foods, key=lambda f: -(f.get("protein") or 0))[:top],
            }
        )
    except Exception as e:
        return _err(e)


@mcp.tool(annotations=_READ)
def check_food_fit(
    food_id: int, grams: float, date: str | None = None
) -> str:
    """Would eating this food still fit today's targets? Shows the day's
    calories and macros now, what the food adds, and what would be left.

    Args:
        food_id: From search_foods.
        grams: Amount you plan to eat, in grams.
        date: YYYY-MM-DD (defaults to today).
    """
    try:
        client = _get_client()
        day = _parse_date(date)
        food = client.get_food(food_id)
        per100 = {
            n.get("id"): n.get("amount") or 0
            for n in food.get("nutrients") or []
            if isinstance(n, dict)
        }
        macro_ids = {"energy": 208, "protein": 203, "carbs": 205, "net_carbs": -1205, "fat": 204, "fiber": 291}
        adds = {k: round(per100.get(nid, 0) * grams / 100, 1) for k, nid in macro_ids.items()}
        now = client.get_consumed_nutrients(day, include_untracked=False)["macros"]
        targets = {}
        for row in client.get_targets().get("targets", []):
            for key, nid in macro_ids.items():
                if row.get("id") == nid:
                    targets[key] = _target_bounds(row)[0]
        # The day's macro template (what the diary's Targets panel shows) wins
        # over the generic nutrient targets for calories and the big three.
        diary = client.get_diary(day)
        template = diary.get("template") or {}
        for key in ("protein", "carbs", "fat"):
            if template.get("grams") and isinstance(template.get(key), (int, float)):
                targets["net_carbs" if key == "carbs" else key] = template[key]
        energy_target = ((diary.get("summary") or {}).get("macros") or {}).get("energy")
        if energy_target:
            targets["energy"] = energy_target
        after = {k: round((now.get(k) or 0) + adds[k], 1) for k in macro_ids}
        remaining = {
            k: round(targets[k] - after[k], 1) for k in macro_ids if targets.get(k) is not None
        }
        return _ok(
            {
                "food": food.get("name"),
                "grams": grams,
                "adds": adds,
                "consumed_now": now,
                "after_eating": after,
                "targets": targets,
                "remaining_after": remaining,
                "fits_calories": remaining.get("energy", 0) >= 0 if "energy" in remaining else None,
            }
        )
    except Exception as e:
        return _err(e)


# ------------------------------------------------------------------
# search / fetch (ChatGPT connectors and deep research)
# ------------------------------------------------------------------
# ChatGPT's connector and deep-research modes look for exactly these two
# tools: search returns {"results": [{id, title, url}]}, fetch returns one
# document {id, title, text, url, metadata}. Ids are "food:<id>" for a food
# and "diary:<YYYY-MM-DD>" for a day.


def _diary_text(day: date) -> tuple[str, dict]:
    client = _get_client()
    diary = client.enrich_diary_servings(client.get_diary(day))
    lines = []
    for entry in diary.get("diary") or []:
        if not isinstance(entry, dict) or not entry.get("name"):
            continue
        if entry.get("type") == "Serving":
            measure = (entry.get("measure") or {}).get("name") or ""
            amount = entry.get("servings") or entry.get("grams") or ""
            lines.append(f"- {entry['name']}: {amount} {measure} ({entry.get('grams')} g)")
        else:
            lines.append(f"- {entry.get('type')}: {entry['name']}")
    nutrition = client.get_consumed_nutrients(day, include_untracked=False)
    macros = ", ".join(f"{k}: {v}" for k, v in nutrition["macros"].items() if v is not None)
    text = f"Cronometer diary for {day}\n\nTotals: {macros or 'nothing logged'}\n\n"
    text += "\n".join(lines) if lines else "No foods logged."
    return text, {"macros": nutrition["macros"]}


@mcp.tool(annotations=_READ)
def search(query: str) -> str:
    """Search Cronometer: foods by name, or a diary day by date.

    A date (YYYY-MM-DD, "today", "yesterday") finds that day's diary; anything
    else searches the food database. Read a result in full with fetch.

    Args:
        query: Food name, or a date.
    """
    try:
        client = _get_client()
        q = query.strip()
        results = []
        day = None
        if q.lower() == "today":
            day = client.today()
        elif q.lower() == "yesterday":
            day = client.today() - timedelta(days=1)
        else:
            try:
                day = date.fromisoformat(q)
            except ValueError:
                day = None
        if day is not None:
            results.append(
                {
                    "id": f"diary:{day}",
                    "title": f"Cronometer diary {day}",
                    "url": "https://cronometer.com/#diary",
                }
            )
        else:
            for f in client.search_food(q)[:20]:
                results.append(
                    {
                        "id": f"food:{f.get('id')}",
                        "title": f"{f.get('name')} ({f.get('source')})",
                        "url": "https://cronometer.com/#foods",
                    }
                )
        return json.dumps({"results": results})
    except Exception as e:
        return _err(e)


@mcp.tool(annotations=_READ)
def fetch(id: str) -> str:
    """Read one search result in full: a food's nutrients, or a diary day.

    Args:
        id: An id from search, "food:<id>" or "diary:<YYYY-MM-DD>".
    """
    try:
        kind, _, value = id.partition(":")
        if kind == "food":
            details = _get_client().get_food_details(int(value))
            doc = {
                "id": id,
                "title": details.get("name") or f"Food {value}",
                "text": json.dumps(details, ensure_ascii=False, default=str),
                "url": "https://cronometer.com/#foods",
                "metadata": {"type": "food", "food_id": int(value)},
            }
        elif kind == "diary":
            day = date.fromisoformat(value)
            text, meta = _diary_text(day)
            doc = {
                "id": id,
                "title": f"Cronometer diary {day}",
                "text": text,
                "url": "https://cronometer.com/#diary",
                "metadata": {"type": "diary", "date": str(day), **meta},
            }
        else:
            raise ValueError("id must look like food:<id> or diary:<YYYY-MM-DD>")
        return json.dumps(doc, ensure_ascii=False, default=str)
    except Exception as e:
        return _err(e)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def date_module_today() -> date:
    """Return today's date in the account's timezone.

    Uses the authenticated client's timezone (resolved from the Cronometer
    account) so response echoes match the diary day an entry actually lands
    on, independent of the host clock. Extracted for easy mocking in tests.
    """
    return _get_client().today()


# ------------------------------------------------------------------
# Entrypoint
# ------------------------------------------------------------------


def _enable_remote(public_url: str, host: str, port: int) -> None:
    """Turn on OAuth and accept the public hostname, for ChatGPT and claude.ai."""
    from urllib.parse import urlparse

    from mcp.server.auth.provider import ProviderTokenVerifier
    from mcp.server.auth.settings import (
        AuthSettings,
        ClientRegistrationOptions,
        RevocationOptions,
    )
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.responses import FileResponse, Response

    from .auth import SCOPE, PasswordOAuthProvider

    global _accounts
    password = os.getenv("MCP_AUTH_PASSWORD", "")
    secret = os.getenv("MCP_SECRET_KEY", "")
    if secret:
        from .accounts import AccountStore, allowed_emails

        _accounts = AccountStore(secret)
        allowed = allowed_emails()
        logger.info(
            "Multi-user mode: people sign in with their own Cronometer account (%s)",
            "anyone" if allowed is None else f"owner + {len(allowed)} allowed emails",
        )
    elif not password:
        raise SystemExit(
            "MCP_PUBLIC_URL is set, so this server is reachable from the internet: "
            "set MCP_AUTH_PASSWORD too (8+ characters). It is the password you type "
            "when ChatGPT or Claude connects. Or set MCP_SECRET_KEY so each person "
            "signs in with their own Cronometer account."
        )
    provider = PasswordOAuthProvider(
        password,
        public_url,
        api_key=os.getenv("MCP_API_KEY") or None,
        local_url=f"http://localhost:{port}",
        accounts=_accounts,
        verify=_verify_cronometer if _accounts is not None else None,
    )
    mcp.settings.auth = AuthSettings(
        issuer_url=public_url,
        resource_server_url=f"{public_url}/mcp",
        client_registration_options=ClientRegistrationOptions(
            enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]
        ),
        revocation_options=RevocationOptions(enabled=True),
        required_scopes=[SCOPE],
        validate_token_resource=False,
    )
    mcp._auth_server_provider = provider
    mcp._token_verifier = ProviderTokenVerifier(provider)

    public_host = urlparse(public_url).netloc
    # Other names the server answers to: Railway's own *.up.railway.app domain
    # (it sets RAILWAY_PUBLIC_DOMAIN), plus any listed in MCP_ALLOWED_HOSTS.
    extra_hosts = [
        h.strip()
        for h in [os.getenv("RAILWAY_PUBLIC_DOMAIN", ""), *os.getenv("MCP_ALLOWED_HOSTS", "").split(",")]
        if h.strip()
    ]
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            public_host,
            *extra_hosts,
            f"127.0.0.1:{port}",
            f"localhost:{port}",
            f"{host}:{port}",
        ],
        allowed_origins=[
            public_url,
            "https://chatgpt.com",
            "https://chat.openai.com",
            "https://claude.ai",
            f"http://127.0.0.1:{port}",
            f"http://localhost:{port}",
        ],
    )

    mcp.custom_route("/login", methods=["GET", "POST"], include_in_schema=False)(
        provider.login_page
    )
    mcp.custom_route("/approve", methods=["GET", "POST"], include_in_schema=False)(
        provider.approve_page
    )

    async def _home(_request):
        from starlette.responses import HTMLResponse

        from .auth import _page

        return HTMLResponse(
            _page(
                f"""<h1>Cronometer MCP</h1>
<p>This is a Model Context Protocol server for a Cronometer account.</p>
<p>Add <code>{public_url}/mcp</code> as a custom connector in ChatGPT or Claude.</p>"""
            )
        )

    mcp.custom_route("/", methods=["GET"], include_in_schema=False)(_home)

    public_dir = Path(__file__).resolve().parents[2] / "public"

    async def _static(request):
        name = request.url.path.lstrip("/") or "icon.png"
        path = public_dir / name
        if path.is_file():
            return FileResponse(path)
        return Response(status_code=404)

    for name in ("icon.png", "icon-48.png", "icon-96.png", "favicon.ico"):
        mcp.custom_route(f"/{name}", methods=["GET"], include_in_schema=False)(_static)

    async def _health(_request):
        return Response("ok", media_type="text/plain")

    mcp.custom_route("/health", methods=["GET"], include_in_schema=False)(_health)


def main():
    """Run the server on stdin/stdout (Claude Desktop, Claude Code), or over
    HTTP (ChatGPT, claude.ai).

    Over HTTP with MCP_PUBLIC_URL set, the server is its own OAuth
    authorization server and asks for MCP_AUTH_PASSWORD when an app connects.
    Without MCP_PUBLIC_URL it has no login, so it only listens on the local
    machine.
    """
    import argparse

    # Load .env for local development (credentials). No-op if the file is
    # missing. override=False keeps real environment variables (systemd,
    # MCP client `env` blocks, etc.) authoritative over .env.
    from dotenv import find_dotenv, load_dotenv

    dotenv_path = find_dotenv(usecwd=True) or str(Path(__file__).resolve().parents[2] / ".env")
    if dotenv_path and load_dotenv(dotenv_path, override=False):
        logger.info("Loaded .env from %s", dotenv_path)

    parser = argparse.ArgumentParser(prog="cronometer-mcp")
    parser.add_argument(
        "--transport",
        choices=("stdio", "http"),
        default=os.getenv("MCP_TRANSPORT", "stdio"),
    )
    parser.add_argument("--host", default=os.getenv("MCP_HOST", "127.0.0.1"))
    # PORT is what Railway, Render and similar hosts assign.
    parser.add_argument(
        "--port", type=int, default=int(os.getenv("MCP_PORT") or os.getenv("PORT") or "8430")
    )
    parser.add_argument(
        "--public-url",
        default=os.getenv("MCP_PUBLIC_URL", ""),
        help="Public https origin (e.g. a tunnel); turns on OAuth.",
    )
    args = parser.parse_args()

    if args.transport == "stdio":
        mcp.run(transport="stdio")
        return

    public_url = args.public_url.rstrip("/")
    if public_url:
        _enable_remote(public_url, args.host, args.port)
    elif args.host not in ("127.0.0.1", "::1", "localhost"):
        raise SystemExit(
            f"refusing to listen on {args.host} without a login. Set MCP_PUBLIC_URL "
            "and MCP_AUTH_PASSWORD to turn on OAuth."
        )

    mcp.settings.host = args.host
    mcp.settings.port = args.port
    logger.info(
        "Listening on http://%s:%d/mcp%s",
        args.host,
        args.port,
        f" (public: {public_url}/mcp, OAuth on)" if public_url else "",
    )
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
