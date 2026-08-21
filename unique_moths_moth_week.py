import requests
import time
import csv

API_URL = "https://api.inaturalist.org/v2/observations/species_counts"

PLACE_IDS = "6815,9753"

DATE_FROM = "2026-07-18"
DATE_TO   = "2026-07-26"

LEPIDOPTERA_ID = 47157
BUTTERFLIES_ID = 47224

OUTPUT_CSV = "israel_moths_unique_2026-07-18_to_2026-07-26.csv"

session = requests.Session()
session.headers.update({
    "User-Agent": "IsraelMothDateComparison/1.0"
})


def get_species_counts(params):
    """Get all species counts from iNaturalist with pagination."""

    all_results = []
    page = 1

    while True:
        p = dict(params)
        p["page"] = page
        p["per_page"] = 500

        for attempt in range(5):
            try:
                response = session.get(
                    API_URL,
                    params=p,
                    timeout=120
                )

                if response.status_code == 429:
                    wait = 30 * (attempt + 1)
                    print(
                        f"Rate limited. Waiting {wait} seconds..."
                    )
                    time.sleep(wait)
                    continue

                response.raise_for_status()
                break

            except requests.RequestException as e:
                if attempt == 4:
                    raise

                wait = 10 * (attempt + 1)

                print(
                    f"Request failed: {e}. "
                    f"Retrying in {wait} seconds..."
                )

                time.sleep(wait)

        data = response.json()
        results = data.get("results", [])

        all_results.extend(results)

        print(
            f"  Page {page}: {len(results)} species "
            f"(total {len(all_results)})"
        )

        # No more pages
        if len(results) < 500:
            break

        page += 1
        time.sleep(1.0)

    # Return the same structure as before
    data["results"] = all_results

    return data

def make_params(place_ids=None):

    params = {
        "taxon_id": LEPIDOPTERA_ID,
        "without_taxon_id": BUTTERFLIES_ID,

        "d1": DATE_FROM,
        "d2": DATE_TO,

        "fields": (
            "taxon.id,"
            "taxon.name,"
            "taxon.rank,"
            "taxon.preferred_common_name,"
            "count"
        ),

        "hrank": "species",

        # Do NOT restrict to Research Grade.
        #
        # We want all observations that iNat includes
        # in the query.
    }

    if place_ids is not None:
        params["place_id"] = place_ids

    return params


def extract_species(data):

    species = {}

    for item in data.get("results", []):

        taxon = item.get("taxon") or {}

        taxon_id = taxon.get("id")
        rank = taxon.get("rank")

        # Only actual species
        if rank != "species":
            continue

        if taxon_id is None:
            continue

        species[taxon_id] = {
            "taxon_id": taxon_id,
            "scientific_name": taxon.get("name", ""),
            "common_name":
                taxon.get("preferred_common_name", ""),
            "count": item.get("count", 0),
        }

    return species

def export_species_csv(species, filename):
    """Export species/count dictionary to CSV."""

    rows = sorted(
        species.values(),
        key=lambda x: x["scientific_name"].lower()
    )

    with open(
        filename,
        "w",
        newline="",
        encoding="utf-8-sig"
    ) as f:

        writer = csv.DictWriter(
            f,
            fieldnames=[
                "taxon_id",
                "scientific_name",
                "common_name",
                "count",
            ]
        )

        writer.writeheader()

        for row in rows:
            writer.writerow({
                "taxon_id":
                    row["taxon_id"],
                "scientific_name":
                    row["scientific_name"],
                "common_name":
                    row["common_name"],
                "count":
                    row["count"],
            })

def main():

    print()
    print("=" * 70)
    print("ISRAEL")
    print("=" * 70)

    israel_params = make_params(PLACE_IDS)

    israel_data = get_species_counts(israel_params)

    israel_species = extract_species(israel_data)

    print(
        f"Species found in Israel: "
        f"{len(israel_species):,}"
    )


    # --------------------------------------------------------
    # Export Israeli species
    # --------------------------------------------------------

    israel_file = (
        f"israel_moths_"
        f"{DATE_FROM}_to_{DATE_TO}.csv"
    )

    export_species_csv(
        israel_species,
        israel_file
    )

    print(f"Israeli list written to: {israel_file}")


    print()
    print("=" * 70)
    print("WORLD")
    print("=" * 70)

    world_params = make_params()

    world_data = get_species_counts(world_params)

    world_species = extract_species(world_data)

    print(
        f"Species found worldwide: "
        f"{len(world_species):,}"
    )


    # --------------------------------------------------------
    # Export worldwide species
    # --------------------------------------------------------

    world_file = (
        f"world_moths_"
        f"{DATE_FROM}_to_{DATE_TO}.csv"
    )

    export_species_csv(
        world_species,
        world_file
    )

    print(f"Worldwide list written to: {world_file}")


    # --------------------------------------------------------
    # Compare Israel vs world
    # --------------------------------------------------------

    unique_species = []

    for taxon_id, israel in israel_species.items():

        world = world_species.get(taxon_id)

        world_count = (
            world["count"]
            if world is not None
            else 0
        )

        if israel["count"] == world_count:

            unique_species.append({
                "taxon_id": taxon_id,
                "scientific_name":
                    israel["scientific_name"],
                "common_name":
                    israel["common_name"],
                "israel_count":
                    israel["count"],
                "world_count":
                    world_count,
            })


    unique_species.sort(
        key=lambda x: x["scientific_name"].lower()
    )


    # --------------------------------------------------------
    # Export comparison result
    # --------------------------------------------------------

    result_file = (
        f"israel_only_moths_"
        f"{DATE_FROM}_to_{DATE_TO}.csv"
    )

    with open(
        result_file,
        "w",
        newline="",
        encoding="utf-8-sig"
    ) as f:

        writer = csv.DictWriter(
            f,
            fieldnames=[
                "taxon_id",
                "scientific_name",
                "common_name",
                "israel_count",
                "world_count",
            ]
        )

        writer.writeheader()
        writer.writerows(unique_species)


    print()
    print("=" * 70)
    print("RESULT")
    print("=" * 70)

    print(
        f"Species observed in Israel but nowhere else "
        f"worldwide during {DATE_FROM}–{DATE_TO}: "
        f"{len(unique_species):,}"
    )

    print()
    print(f"Israel CSV: {israel_file}")
    print(f"World CSV:  {world_file}")
    print(f"Result CSV: {result_file}")

if __name__ == "__main__":
    main()