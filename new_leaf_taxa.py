#!/usr/bin/env python3

import argparse
import json
import os
import sys
import time
import random
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import requests


# ============================================================
# Configuration
# ============================================================

API_BASE = "https://api.inaturalist.org/v2"

OBSERVATIONS_FILE = "observations.json"
TAXA_FILE = "taxa.json"

# Number of observations returned by each API request
PER_PAGE = 200

# Normal delay between API requests.
# Keep this conservative.
REQUEST_DELAY = 1.0

# Maximum number of retries after rate limiting / temporary errors
MAX_RETRIES = 8

# Initial retry delay
INITIAL_BACKOFF = 10

# Maximum retry delay
MAX_BACKOFF = 300

# Timeout for an individual API request
REQUEST_TIMEOUT = 60

# Number of taxa in batch
TAXA_BATCH_SIZE = 30


# ============================================================
# HTTP / rate limiting
# ============================================================

session = requests.Session()

session.headers.update({
    "User-Agent": "iNaturalist-life-first-taxa/1.0"
})

def load_api_token():
    token_file = Path("inaturalist_token.txt")
    if not token_file.exists():
        return None
    try:
        with open(token_file, "r", encoding="utf-8") as f:
            token = f.read().strip()
        return token if token else None
    except Exception as e:
        print(f"Warning: could not read inaturalist_token.txt: {e}")
        return None

_token = load_api_token()
if _token:
    session.headers["Authorization"] = f"Bearer {_token}"


def api_get(url, params=None):
    """
    GET request with conservative rate limiting and retry handling.

    In particular, don't repeatedly hammer iNaturalist after 403/429.
    """

    for attempt in range(MAX_RETRIES):
        try:
            response = session.get(
                url,
                params=params,
                timeout=REQUEST_TIMEOUT
            )

        except requests.RequestException as e:
            wait = min(
                MAX_BACKOFF,
                INITIAL_BACKOFF * (2 ** attempt)
            )

            print(
                f"\nRequest error: {e}\n"
                f"Waiting {wait} seconds before retry..."
            )

            time.sleep(wait)
            continue

        # Success
        if response.status_code == 200:
            time.sleep(REQUEST_DELAY)
            return response.json()

        # Rate limiting / forbidden
        if response.status_code in (403, 429):

            retry_after = response.headers.get("Retry-After")

            if retry_after:
                try:
                    wait = int(retry_after)
                except ValueError:
                    wait = INITIAL_BACKOFF
            else:
                wait = min(
                    MAX_BACKOFF,
                    INITIAL_BACKOFF * (2 ** attempt)
                )

            # Add a little randomness so repeated clients don't
            # wake up at exactly the same time.
            wait += random.uniform(0, 3)

            print(
                f"\nHTTP {response.status_code} from iNaturalist."
            )
            print(
                f"Waiting {wait:.1f} seconds before retry "
                f"(attempt {attempt + 1}/{MAX_RETRIES})..."
            )

            time.sleep(wait)
            continue

        # Temporary server errors
        if response.status_code >= 500:

            wait = min(
                MAX_BACKOFF,
                INITIAL_BACKOFF * (2 ** attempt)
            )

            print(
                f"\nHTTP {response.status_code} from iNaturalist."
            )
            print(
                f"Waiting {wait} seconds before retry..."
            )

            time.sleep(wait)
            continue

        # Other errors should not be silently retried
        print(
            f"\nHTTP {response.status_code}: "
            f"{response.text[:500]}"
        )

        response.raise_for_status()

    raise RuntimeError(
        "iNaturalist API request failed after "
        f"{MAX_RETRIES} retries."
    )


# ============================================================
# JSON helpers
# ============================================================

def load_json(filename, default):
    path = Path(filename)

    if not path.exists():
        return default

    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(filename, data):
    """
    Write atomically so a failed/interrupted run doesn't leave
    a half-written JSON file.
    """

    path = Path(filename)
    tmp = path.with_suffix(path.suffix + ".tmp")

    with tmp.open("w", encoding="utf-8") as f:
        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2
        )

    tmp.replace(path)


# ============================================================
# Observation handling
# ============================================================

def observation_id_set(observations):
    return {
        int(obs["id"])
        for obs in observations
        if "id" in obs
    }


def highest_observation_id(observations):
    if not observations:
        return 0

    return max(
        int(obs["id"])
        for obs in observations
        if "id" in obs
    )


def update_observations(observations, user_login):
    """
    Download observations after the highest observation ID
    already present in observations.json.

    Uses id_above rather than page pagination.
    """

    existing_ids = observation_id_set(observations)

    current_max_id = highest_observation_id(observations)

    print(
        f"Existing observations: {len(observations):,}"
    )
    print(
        f"Highest existing observation ID: {current_max_id}"
    )

    id_above = current_max_id
    total_added = 0

    if "Authorization" not in session.headers:
        raise RuntimeError(
            "\nCannot update observations.json without authentication.\n"
            "inaturalist_token.txt is missing or empty. Please create inaturalist_token.txt with a valid iNaturalist API token\n"
            "so that original_filename is retrieved and observations.json is not corrupted."
        )

    while True:

        params = {
            "user_login": user_login,
            "id_above": id_above,
            "order": "asc",
            "order_by": "id",
            "per_page": PER_PAGE,

            # Only request fields that are actually stored.
            "fields": (
                "id,"
                "user.login,"
                "time_observed_at,"
                "photos.id,"
                "photos.original_filename,"
                "taxon.id"
            )
        }

        print(
            f"Downloading observations with "
            f"id_above={id_above}..."
        )

        data = api_get(
            f"{API_BASE}/observations",
            params=params
        )

        results = data.get("results", [])

        if not results:
            print("No more new observations.")
            break

        max_returned_id = id_above

        added_this_request = 0

        for obs in results:
            for photo in obs.get("photos", []):
                if not photo.get("original_filename"):
                    raise RuntimeError(
                        f"\nMissing original_filename for observation {obs.get('id')}, photo {photo.get('id')}.\n"
                        f"inaturalist_token.txt exists but appears to be outdated or invalid.\n"
                        f"Please update inaturalist_token.txt with a valid, non-expired API token from iNaturalist."
                    )

            obs_id = obs.get("id")

            if obs_id is None:
                continue

            obs_id = int(obs_id)

            if obs_id not in existing_ids:
                observations.append(obs)
                existing_ids.add(obs_id)

                added_this_request += 1
                total_added += 1

            if obs_id > max_returned_id:
                max_returned_id = obs_id

        print(
            f"  Received: {len(results):,}"
            f" | Added: {added_this_request:,}"
        )

        # Critical guard against an API response that doesn't
        # advance the ID.
        if max_returned_id <= id_above:
            print(
                "WARNING: id_above did not advance. "
                "Stopping to avoid an infinite loop."
            )
            break

        id_above = max_returned_id

        # If fewer than PER_PAGE came back, we've probably
        # reached the end.
        if len(results) < PER_PAGE:
            print("Reached the end of available observations.")
            break

    if total_added:
        print(
            f"\nAdded {total_added:,} new observations."
        )

        # Keep the JSON reasonably ordered.
        observations.sort(
            key=lambda x: int(x.get("id", 0))
        )

        save_json(
            OBSERVATIONS_FILE,
            observations
        )

        print(
            f"Updated {OBSERVATIONS_FILE}"
        )
    else:
        print("\nNo new observations were added.")

    return observations


# ============================================================
# Taxonomy
# ============================================================

def get_taxa_batch(taxon_ids):
    """
    Get multiple taxa from the iNaturalist v2 API.

    The v2 API accepts multiple taxon IDs in the URL path:

        /v2/taxa/123,456,789

    """

    if not taxon_ids:
        return []

    ids_string = ",".join(
        str(int(taxon_id))
        for taxon_id in taxon_ids
    )

    fields = (
        "id,"
        "name,"
        "rank,"
        "rank_level,"
        "ancestor_ids,"
        "parent_id"
    )

    params = {
        "fields": fields
    }

    data = api_get(
        f"{API_BASE}/taxa/{ids_string}",
        params=params
    )

    return data.get("results", [])

def update_taxa(observations, taxa):
    """
    Find taxon IDs used by observations but absent from taxa.json,
    then download them in batches of TAXA_BATCH_SIZE.

    Taxonomy is cached permanently in taxa.json.
    """

    observation_taxon_ids = set()

    for obs in observations:

        taxon = obs.get("taxon")

        if not taxon:
            continue

        taxon_id = taxon.get("id")

        if taxon_id is not None:
            observation_taxon_ids.add(str(taxon_id))

    missing = sorted(
        observation_taxon_ids - set(taxa.keys()),
        key=int
    )

    print(
        f"\nUnique taxon IDs in observations: "
        f"{len(observation_taxon_ids):,}"
    )

    print(
        f"Already cached in taxa.json: "
        f"{len(taxa):,}"
    )

    print(
        f"Taxa requiring API requests: "
        f"{len(missing):,}"
    )

    if not missing:
        print("taxa.json is already up to date.")
        return taxa

    total_batches = (
        len(missing) + TAXA_BATCH_SIZE - 1
    ) // TAXA_BATCH_SIZE

    for batch_number, start in enumerate(
        range(0, len(missing), TAXA_BATCH_SIZE),
        1
    ):

        batch_ids = missing[
            start:start + TAXA_BATCH_SIZE
        ]

        print(
            f"[Taxa batch {batch_number:,}/"
            f"{total_batches:,}] "
            f"Downloading {len(batch_ids)} taxa..."
        )

        try:
            results = get_taxa_batch(batch_ids)

        except Exception as e:

            print(
                "\nERROR retrieving taxonomy batch:"
            )
            print(e)

            print(
                "\nThe successful batches have already "
                "been saved to taxa.json."
            )

            print(
                "Stopping so the next run can continue "
                "with the remaining taxa."
            )

            break

        returned_ids = set()

        for taxon in results:

            taxon_id = taxon.get("id")

            if taxon_id is None:
                continue

            taxon_id = str(taxon_id)

            taxa[taxon_id] = {
                "name": taxon.get("name"),
                "rank": taxon.get("rank"),
                "rank_level": taxon.get("rank_level"),
                "ancestor_ids": taxon.get(
                    "ancestor_ids",
                    []
                ),
                "parent_id": taxon.get(
                    "parent_id"
                )
            }

            returned_ids.add(taxon_id)

        print(
            f"  Requested: {len(batch_ids):,}"
            f" | Returned: {len(returned_ids):,}"
        )

        # Normally every requested taxon should be returned.
        # Don't silently ignore missing IDs.
        missing_from_response = (
            set(batch_ids) - returned_ids
        )

        if missing_from_response:
            print(
                "  WARNING: these taxon IDs were not "
                "returned by the API:"
            )

            print(
                "  " + ", ".join(
                    sorted(
                        missing_from_response,
                        key=int
                    )
                )
            )

        # Save after every successful batch.
        save_json(
            TAXA_FILE,
            taxa
        )

        print(
            f"  Saved taxa.json "
            f"({len(taxa):,} taxa cached)"
        )

    return taxa

# ============================================================
# Taxon hierarchy helpers
# ============================================================

def taxon_lineage(taxon_id, taxa):
    """
    Return the taxon itself plus all of its ancestors.

    ancestor_ids from the API do NOT contain the taxon itself.
    """

    taxon = taxa.get(str(taxon_id))

    if not taxon:
        return {int(taxon_id)}

    result = set(
        int(x)
        for x in taxon.get("ancestor_ids", [])
    )

    result.add(int(taxon_id))

    return result


def is_descendant_or_same(taxon_id, ancestor_id, taxa):
    """
    True if taxon_id is the same as, or a descendant of,
    ancestor_id.
    """

    return int(ancestor_id) in taxon_lineage(
        int(taxon_id),
        taxa
    )


# ============================================================
# New leaf taxa calculation
# ============================================================

def find_new_leaf_taxa(
    observations,
    taxa,
    target_date
):
    """
    Find taxa first encountered by the user on target_date.

    Definition:

    A taxon observed today is NOT new if the user had already
    observed that taxon OR any taxon more specific than it
    before today.

    Conversely, observing an ancestor previously does NOT make
    a descendant taxon non-new.

    Example:

        Previous: Genus A
        Today:    Species A1

        Species A1 is NEW.

        Previous: Species A2
        Today:    Genus A

        Genus A is NOT NEW.
    """

    # Parse target date.
    target_date = datetime.strptime(
        target_date,
        "%Y-%m-%d"
    ).date()

    today_observations = []
    previous_observations = []

    for obs in observations:

        timestamp = obs.get("time_observed_at")

        if not timestamp:
            continue

        try:
            dt = datetime.fromisoformat(
                timestamp.replace("Z", "+00:00")
            )
        except ValueError:
            continue

        obs_date = dt.date()

        if obs_date == target_date:
            today_observations.append(obs)

        elif obs_date < target_date:
            previous_observations.append(obs)

    print(
        f"\nObservations on {target_date}: "
        f"{len(today_observations):,}"
    )

    print(
        f"Previous observations: "
        f"{len(previous_observations):,}"
    )

    # Taxa previously encountered.
    previous_taxon_ids = set()

    for obs in previous_observations:

        taxon = obs.get("taxon")

        if not taxon:
            continue

        taxon_id = taxon.get("id")

        if taxon_id is not None:
            previous_taxon_ids.add(
                int(taxon_id)
            )

    # We need the set of ALL taxa that were previously
    # encountered, not just the exact IDs.
    #
    # If we previously saw Species A, then Genus A is also
    # considered encountered.
    previous_encountered = set()

    for taxon_id in previous_taxon_ids:
        previous_encountered.update(
            taxon_lineage(
                taxon_id,
                taxa
            )
        )

    # Unique taxa observed today.
    today_taxon_ids = set()

    for obs in today_observations:

        taxon = obs.get("taxon")

        if not taxon:
            continue

        taxon_id = taxon.get("id")

        if taxon_id is not None:
            today_taxon_ids.add(
                int(taxon_id)
            )

    print(
        f"Unique taxa today: "
        f"{len(today_taxon_ids):,}"
    )

    # First pass:
    #
    # A taxon is new if it wasn't previously encountered.
    #
    # But we also want the LOWER-LEVEL / leaf taxa, so if today
    # contains both a genus and a species under that genus, we
    # prefer the species.
    new_taxon_ids = []

    for taxon_id in today_taxon_ids:

        if taxon_id in previous_encountered:
            continue

        new_taxon_ids.append(taxon_id)

    # Remove new ancestors when a more specific NEW taxon exists
    # today.
    #
    # Example:
    #
    # Today:
    #   Genus A
    #   Species A1
    #
    # Result:
    #   Species A1
    #
    # not both.
    leaf_taxon_ids = []

    for taxon_id in new_taxon_ids:

        has_new_descendant_today = False

        for other_id in new_taxon_ids:

            if other_id == taxon_id:
                continue

            if is_descendant_or_same(
                other_id,
                taxon_id,
                taxa
            ):
                has_new_descendant_today = True
                break

        if not has_new_descendant_today:
            leaf_taxon_ids.append(taxon_id)

    # Build detailed result objects.
    results = []

    for taxon_id in leaf_taxon_ids:

        taxon_info = taxa.get(
            str(taxon_id),
            {}
        )

        obs_for_taxon = []

        for obs in today_observations:

            taxon = obs.get("taxon")

            if not taxon:
                continue

            obs_taxon_id = taxon.get("id")

            if obs_taxon_id is None:
                continue

            if is_descendant_or_same(
                int(obs_taxon_id),
                taxon_id,
                taxa
            ):
                obs_for_taxon.append(obs)

        results.append({
            "taxon_id": taxon_id,
            "name": taxon_info.get(
                "name",
                f"Taxon {taxon_id}"
            ),
            "rank": taxon_info.get("rank"),
            "rank_level": taxon_info.get(
                "rank_level"
            ),
            "observations": obs_for_taxon
        })

    # Most specific first.
    results.sort(
        key=lambda x: (
            x["rank_level"]
            if x["rank_level"] is not None
            else 999,
            x["name"] or ""
        )
    )

    return results

def find_top_days(
    observations,
    taxa,
    limit=30
):
    """
    Process all observation dates chronologically and find
    the dates on which the user encountered the most NEW
    leaf taxa.

    A taxon becomes "encountered" together with all of its
    ancestors.

    Thus:

        Previously: Genus A
        Today:      Species A1

    Species A1 is NEW.

    But:

        Previously: Species A1
        Today:      Genus A

    Genus A is NOT new.

    If multiple new taxa occur on the same day, only the
    most-specific (leaf) taxa are counted.
    """

    # --------------------------------------------------------
    # Group observations by date
    # --------------------------------------------------------

    observations_by_date = {}

    for obs in observations:

        timestamp = obs.get("time_observed_at")

        if not timestamp:
            continue

        try:
            dt = datetime.fromisoformat(
                timestamp.replace("Z", "+00:00")
            )
        except ValueError:
            continue

        date = dt.date().isoformat()

        observations_by_date.setdefault(
            date,
            []
        ).append(obs)

    dates = sorted(observations_by_date)

    print(
        f"\nProcessing {len(dates):,} observation dates..."
    )

    # --------------------------------------------------------
    # Taxa encountered before the current date
    # --------------------------------------------------------

    previously_encountered = set()

    daily_results = []

    # --------------------------------------------------------
    # Process chronologically
    # --------------------------------------------------------

    for date in dates:

        day_observations = observations_by_date[date]

        # Taxa actually identified in observations on this day.
        today_taxon_ids = set()

        for obs in day_observations:

            taxon = obs.get("taxon")

            if not taxon:
                continue

            taxon_id = taxon.get("id")

            if taxon_id is not None:
                today_taxon_ids.add(
                    int(taxon_id)
                )

        # ----------------------------------------------------
        # First identify taxa that are genuinely new.
        #
        # If the exact taxon is already in
        # previously_encountered, it isn't new.
        #
        # IMPORTANT:
        # previously_encountered contains ancestors of
        # previously observed taxa, but NOT descendants.
        #
        # Therefore previously seeing Genus A does NOT
        # prevent Species A1 from being new.
        # ----------------------------------------------------

        new_taxon_ids = []

        for taxon_id in today_taxon_ids:

            if taxon_id not in previously_encountered:
                new_taxon_ids.append(taxon_id)

        # ----------------------------------------------------
        # Keep only the leaf/new-most-specific taxa.
        #
        # If today's new taxa are:
        #
        #   Genus A
        #   Species A1
        #
        # count only Species A1.
        # ----------------------------------------------------

        leaf_taxon_ids = []

        for taxon_id in new_taxon_ids:

            has_new_descendant = False

            for other_id in new_taxon_ids:

                if other_id == taxon_id:
                    continue

                if is_descendant_or_same(
                    other_id,
                    taxon_id,
                    taxa
                ):
                    has_new_descendant = True
                    break

            if not has_new_descendant:
                leaf_taxon_ids.append(
                    taxon_id
                )

        # ----------------------------------------------------
        # Add the newly observed taxa AND their ancestors to
        # the historical state.
        #
        # This must happen AFTER determining today's new taxa.
        # Otherwise a taxon could incorrectly make itself
        # non-new.
        # ----------------------------------------------------

        today_encountered = set()

        for taxon_id in today_taxon_ids:

            today_encountered.update(
                taxon_lineage(
                    taxon_id,
                    taxa
                )
            )

        previously_encountered.update(
            today_encountered
        )

        # ----------------------------------------------------
        # Store daily result
        # ----------------------------------------------------

        daily_results.append({
            "date": date,
            "new_leaf_taxon_ids": leaf_taxon_ids,
            "new_leaf_count": len(
                leaf_taxon_ids
            ),
            "observation_count": len(
                day_observations
            )
        })

    # --------------------------------------------------------
    # Sort by number of new leaf taxa, descending.
    #
    # Date is used as a secondary key so ties are stable.
    # --------------------------------------------------------

    daily_results.sort(
        key=lambda x: (
            -x["new_leaf_count"],
            x["date"]
        )
    )

    return daily_results[:limit]


# ============================================================
# HTML report
# ============================================================

def html_escape(value):
    if value is None:
        return ""

    text = str(value)

    return (
        text
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def observation_url(obs_id):
    return (
        f"https://www.inaturalist.org/observations/{obs_id}"
    )


def taxon_url(taxon_id):
    return (
        f"https://www.inaturalist.org/taxa/{taxon_id}"
    )


def make_html_report(
    results,
    target_date,
    filename
):

    total_observations = sum(
        len(r["observations"])
        for r in results
    )

    rows = []

    for result in results:

        taxon_id = result["taxon_id"]
        name = result["name"]
        rank = result["rank"] or ""

        obs_links = []

        for obs in result["observations"]:

            obs_id = obs["id"]

            timestamp = (
                obs.get("time_observed_at")
                or ""
            )

            photos = obs.get("photos") or []

            photo_html = ""

            if photos:
                photo_id = photos[0].get("id")

                if photo_id:
                    photo_url = (
                        "https://static.inaturalist.org/"
                        f"photos/{photo_id}/medium.jpg"
                    )

                    photo_html = (
                        f'<img src="{photo_url}" '
                        f'class="thumb" loading="lazy">'
                    )

            obs_links.append(
                f"""
                <div class="observation">
                    {photo_html}
                    <div>
                        <a href="{observation_url(obs_id)}"
                           target="_blank">
                            Observation {obs_id}
                        </a>
                        <br>
                        <span class="date">
                            {html_escape(timestamp)}
                        </span>
                    </div>
                </div>
                """
            )

        rows.append(
            f"""
            <tr>
                <td>
                    <a href="{taxon_url(taxon_id)}"
                       target="_blank">
                        <strong>
                            {html_escape(name)}
                        </strong>
                    </a>
                </td>

                <td>
                    {html_escape(rank)}
                </td>

                <td>
                    {taxon_id}
                </td>

                <td>
                    {"".join(obs_links)}
                </td>
            </tr>
            """
        )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">

<title>
Life first taxa for {html_escape(target_date)}
</title>

<style>

body {{
    font-family: Arial, sans-serif;
    margin: 30px;
    line-height: 1.4;
}}

h1 {{
    margin-bottom: 5px;
}}

.summary {{
    margin-bottom: 25px;
    color: #555;
}}

table {{
    border-collapse: collapse;
    width: 100%;
}}

th, td {{
    border: 1px solid #ccc;
    padding: 8px;
    vertical-align: top;
}}

th {{
    text-align: left;
}}

.observation {{
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 8px;
}}

.thumb {{
    width: 80px;
    height: 80px;
    object-fit: cover;
}}

.date {{
    color: #666;
    font-size: 0.9em;
}}

</style>
</head>

<body>

<h1>
Life first taxa for {html_escape(target_date)}
</h1>

<div class="summary">

    <strong>
        {len(results)}
    </strong>
    new leaf taxa,

    from
    <strong>
        {total_observations}
    </strong>
    observations.

</div>

<table>

<thead>
<tr>
    <th>Taxon</th>
    <th>Rank</th>
    <th>Taxon ID</th>
    <th>Today's observations</th>
</tr>
</thead>

<tbody>

{"".join(rows)}

</tbody>

</table>

</body>
</html>
"""

    with open(
        filename,
        "w",
        encoding="utf-8"
    ) as f:
        f.write(html)


def make_comparison_html_report(
    date_results,
    filename
):
    """Generate a comparison report for selected dates."""

    rows = []

    for target_date, results in date_results:
        observation_count = sum(
            len(result["observations"])
            for result in results
        )

        taxon_names = []

        for result in results:
            taxon_names.append(
                f'<a href="{taxon_url(result["taxon_id"])}" '
                f'target="_blank">'
                f'{html_escape(result["name"])}'
                f'</a>'
            )

        rows.append(
            f"""
            <tr>
                <td><strong>{html_escape(target_date)}</strong></td>
                <td>{len(results)}</td>
                <td>{observation_count}</td>
                <td>{"<br>".join(taxon_names)}</td>
            </tr>
            """
        )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Life first taxa comparison</title>
<style>
body {{
    font-family: Arial, sans-serif;
    margin: 30px;
    line-height: 1.4;
}}
table {{
    border-collapse: collapse;
    width: 100%;
}}
th, td {{
    border: 1px solid #ccc;
    padding: 8px;
    vertical-align: top;
}}
th {{
    text-align: left;
}}
</style>
</head>
<body>
<h1>Life first taxa comparison</h1>
<table>
<thead>
<tr>
    <th>Date</th>
    <th>New leaf taxa</th>
    <th>Observations</th>
    <th>Taxa</th>
</tr>
</thead>
<tbody>
{"".join(rows)}
</tbody>
</table>
</body>
</html>
"""

    with open(filename, "w", encoding="utf-8") as f:
        f.write(html)

def make_top_days_html_report(
    top_days,
    observations,
    taxa,
    filename
):
    """
    Generate the top-days HTML report.
    """

    rows = []

    for rank, day in enumerate(
        top_days,
        1
    ):

        date = day["date"]
        count = day["new_leaf_count"]
        observation_count = day["observation_count"]

        # Get taxon information for this day.
        taxon_names = []

        for taxon_id in day[
            "new_leaf_taxon_ids"
        ]:

            taxon_info = taxa.get(
                str(taxon_id),
                {}
            )

            name = taxon_info.get(
                "name",
                f"Taxon {taxon_id}"
            )

            rank_name = taxon_info.get(
                "rank",
                ""
            )

            taxon_names.append(
                f"""
                <a href="{taxon_url(taxon_id)}"
                   target="_blank">
                    {html_escape(name)}
                </a>
                <span class="rank">
                    ({html_escape(rank_name)})
                </span>
                """
            )

        rows.append(
            f"""
            <tr>
                <td class="position">
                    {rank}
                </td>

                <td>
                    <strong>{date}</strong>
                </td>

                <td class="count">
                    <strong>{count}</strong>
                </td>

                <td>
                    {observation_count}
                </td>

                <td>
                    {"<br>".join(taxon_names)}
                </td>
            </tr>
            """
        )

    html = f"""<!DOCTYPE html>
<html lang="en">

<head>

<meta charset="UTF-8">

<title>
Top 30 days - new leaf taxa
</title>

<style>

body {{
    font-family: Arial, sans-serif;
    margin: 30px;
    line-height: 1.4;
}}

h1 {{
    margin-bottom: 5px;
}}

.summary {{
    color: #555;
    margin-bottom: 25px;
}}

table {{
    border-collapse: collapse;
    width: 100%;
}}

th,
td {{
    border: 1px solid #ccc;
    padding: 8px;
    vertical-align: top;
}}

th {{
    text-align: left;
}}

.position {{
    text-align: center;
    font-weight: bold;
}}

.count {{
    text-align: center;
    font-size: 1.1em;
}}

.rank {{
    color: #666;
    font-size: 0.9em;
}}

</style>

</head>

<body>

<h1>
Top 30 days for new leaf taxa
</h1>

<div class="summary">

The days on which the most new leaf taxa were
encountered for the first time.

</div>

<table>

<thead>

<tr>
    <th>#</th>
    <th>Date</th>
    <th>New leaf taxa</th>
    <th>Observations</th>
    <th>New taxa</th>
</tr>

</thead>

<tbody>

{"".join(rows)}

</tbody>

</table>

</body>

</html>
"""

    with open(
        filename,
        "w",
        encoding="utf-8"
    ) as f:
        f.write(html)


# ============================================================
# Main
# ============================================================

def main():

    parser = argparse.ArgumentParser(
        description=(
            "Update iNaturalist observations/taxonomy "
            "and find new leaf taxa for a date."
        )
    )

    parser.add_argument(
        "--user",
        required=True,
        help="iNaturalist username"
    )

    parser.add_argument(
        "--date",
        required=False,
        nargs="+",
        help=(
            "One or more dates in YYYY-MM-DD format. If omitted, "
            "generate the top-30 days report."
        )
    )

    args = parser.parse_args()

    # Validate date.
    if args.date:
        for target_date in args.date:
            try:
                datetime.strptime(
                    target_date,
                    "%Y-%m-%d"
                )
            except ValueError:
                print(
                    "ERROR: date must be YYYY-MM-DD"
                )
                sys.exit(1)

    print("=" * 70)
    print("iNaturalist new leaf taxa")
    print("=" * 70)

    # --------------------------------------------------------
    # Load observations
    # --------------------------------------------------------

    observations = load_json(
        OBSERVATIONS_FILE,
        []
    )

    if not isinstance(observations, list):
        raise RuntimeError(
            f"{OBSERVATIONS_FILE} must contain a JSON list."
        )

    # --------------------------------------------------------
    # Update observations
    # --------------------------------------------------------

    observations = update_observations(
        observations,
        args.user
    )

    # --------------------------------------------------------
    # Load taxonomy
    # --------------------------------------------------------

    taxa = load_json(
        TAXA_FILE,
        {}
    )

    if not isinstance(taxa, dict):
        raise RuntimeError(
            f"{TAXA_FILE} must contain a JSON object."
        )

    # --------------------------------------------------------
    # Update taxonomy cache
    # --------------------------------------------------------

    taxa = update_taxa(
        observations,
        taxa
    )

        # --------------------------------------------------------
    # Generate report
    # --------------------------------------------------------

    if args.date:

        # ====================================================
        # Specific date or selected dates
        # ====================================================

        date_results = [
            (
                target_date,
                find_new_leaf_taxa(
                    observations,
                    taxa,
                    target_date
                )
            )
            for target_date in args.date
        ]

        if len(date_results) == 1:
            target_date, results = date_results[0]
            output_file = (
                f"life_first_taxa_for_{target_date}.html"
            )
            make_html_report(
                results,
                target_date,
                output_file
            )
        else:
            output_file = (
                "life_first_taxa_for_selected_dates.html"
            )
            make_comparison_html_report(
                date_results,
                output_file
            )

        print()
        print("=" * 70)
        for target_date, results in date_results:
            print(
                f"NEW LEAF TAXA ON {target_date}: "
                f"{len(results):,}"
            )
        print(
            f"HTML report: {output_file}"
        )
        print("=" * 70)

        for target_date, results in date_results:
            if len(date_results) > 1:
                print(f"\n{target_date}")

            for result in results:

                print(
                    f"{result['name']} "
                    f"({result['rank']}, "
                    f"id={result['taxon_id']})"
                )

    else:

        # ====================================================
        # Top 30 days
        # ====================================================

        top_days = find_top_days(
            observations,
            taxa,
            limit=30
        )

        output_file = (
            "life_first_taxa_top_30.html"
        )

        make_top_days_html_report(
            top_days,
            observations,
            taxa,
            output_file
        )

        print()
        print("=" * 70)
        print("TOP 30 DAYS")
        print("=" * 70)

        for rank, day in enumerate(
            top_days,
            1
        ):

            print(
                f"{rank:2}. "
                f"{day['date']}  "
                f"{day['new_leaf_count']:4} "
                f"new leaf taxa  "
                f"({day['observation_count']} observations)"
            )

        print()
        print(
            f"HTML report: {output_file}"
        )


if __name__ == "__main__":
    main()