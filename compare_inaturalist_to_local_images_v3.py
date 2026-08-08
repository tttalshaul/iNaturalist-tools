#!/usr/bin/env python3

import sys
import os
import json
import time
import argparse
import subprocess
import html
import shutil
from pathlib import Path

from datetime import datetime

import requests

from PIL import Image, ExifTags, ImageFile

from openpyxl import Workbook
from openpyxl.styles import Font, Alignment
from openpyxl.drawing.image import Image as XLImage


ImageFile.LOAD_TRUNCATED_IMAGES = True

# ======================================================
# Configuration
# ======================================================

INAT_API_URL = (
    "https://api.inaturalist.org/v2"
)

TOKEN_FILE = (
    "token.txt"
)

CHECKPOINT_FILE = (
    "inat_checkpoint.json"
)

LOCAL_CACHE_FILE = (
    "local_image_cache.json"
)


SUPPORTED_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png"
}


USER_AGENT = (
    "iNaturalist-local-image-comparator/3.0"
)


PAGE_SIZE = 200
DEBUG_MODE = False

API_DELAY_SECONDS = 3


THUMB_SIZE = (
    120,
    120
)



# ======================================================
# Token
# ======================================================


def load_api_token():

    if not os.path.exists(
        TOKEN_FILE
    ):

        raise RuntimeError(
            "Missing token.txt\n"
            "Create token.txt and paste "
            "your iNaturalist API token."
        )


    with open(
        TOKEN_FILE,
        "r",
        encoding="utf-8"
    ) as f:

        token = f.read().strip()


    if not token:

        raise RuntimeError(
            "token.txt is empty"
        )


    return token



# ======================================================
# API Client
# ======================================================


class INaturalistClient:


    def __init__(
            self,
            token):


        self.session = requests.Session()


        self.session.headers.update(
            {
                "Authorization":
                    f"Bearer {token}",

                "User-Agent":
                    USER_AGENT
            }
        )



    def get(
            self,
            endpoint,
            params=None,
            retries=8):


        url = (
            INAT_API_URL
            +
            endpoint
        )


        attempt = 0


        while True:


            try:


                response = self.session.get(
                    url,
                    params=params,
                    timeout=60
                )

                print("Response URL: ", response.url)

                if response.status_code in (
                    403,
                    429
                ):


                    wait = (
                        30
                        *
                        (attempt + 1)
                    )


                    print(
                        f"API {response.status_code}. "
                        f"Waiting {wait}s"
                    )


                    time.sleep(
                        wait
                    )


                    attempt += 1


                    if attempt >= retries:

                        raise RuntimeError(
                            "API retry limit reached"
                        )


                    continue



                response.raise_for_status()


                time.sleep(
                    API_DELAY_SECONDS
                )


                return response.json()



            except requests.exceptions.RequestException as e:


                attempt += 1


                if attempt >= retries:

                    raise


                wait = (
                    30
                    *
                    attempt
                )


                print(
                    f"Request failed: {e}"
                )

                print(
                    f"Retrying after {wait}s"
                )


                time.sleep(
                    wait
                )



# ======================================================
# JSON helpers
# ======================================================


def save_json(
        filename,
        data):


    with open(
        filename,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2
        )



def load_json(
        filename):


    with open(
        filename,
        encoding="utf-8"
    ) as f:

        return json.load(f)



# ======================================================
# Filename helpers
# ======================================================


def normalize_filename(
        filename):


    if not filename:

        return None


    return os.path.splitext(
        os.path.basename(filename)
    )[0].lower()



def timestamp_minute(
        dt):


    if not dt:

        return None


    return dt.strftime(
        "%Y-%m-%d %H:%M"
    )

from PIL import Image

def create_thumbnail_file(
        source,
        output,
        size=150):

    try:

        img = Image.open(source)
        img.load()

        img.thumbnail(
            (size, size)
        )

        if img.mode in ("RGBA", "LA", "P"):

            background = Image.new(
                "RGB",
                img.size,
                (255, 255, 255)
            )

            if img.mode == "P":
                img = img.convert("RGBA")

            background.paste(
                img,
                mask=img.split()[-1]
            )

            img = background

        elif img.mode != "RGB":

            img = img.convert("RGB")

        img.save(
            output,
            "JPEG",
            quality=65
        )

        return True

    except Exception as e:

        print(
            f"Thumbnail failed: {source} ({e})"
        )

        return False

def esc(value):

    if value is None:
        return ""

    return html.escape(
        str(value)
    )


def create_html_report(
        results,
        local_missing,
        output_dir):


    output_dir = Path(
        output_dir
    )

    thumb_dir = (
        output_dir /
        "thumbnails"
    )

    local_thumb_dir = (
        thumb_dir /
        "local"
    )


    for d in [
        output_dir,
        local_thumb_dir
    ]:
        d.mkdir(
            parents=True,
            exist_ok=True
        )


    html_file = (
        output_dir /
        "report.html"
    )


    with open(
        html_file,
        "w",
        encoding="utf-8"
    ) as f:


        f.write("""
<!DOCTYPE html>
<html>
<head>

<meta charset="utf-8">

<title>
iNaturalist comparison
</title>


<style>

body {
    font-family: Arial;
}

table {
    border-collapse: collapse;
    width:100%;
}

td,th {
    border:1px solid #ccc;
    padding:5px;
}

img {
    max-width:200px;
    max-height:200px;
}

input {
    width:400px;
    font-size:18px;
}

</style>

<script>
let currentPage = 0;
const pageSize = 100;


function showPage(page) {

    currentPage = page;

    let rows =
        document.querySelectorAll(
            "#results tbody tr"
        );


    let visibleRows = [];

    rows.forEach(function(row) {

        if (row.dataset.hidden !== "true") {
            visibleRows.push(row);
        }

        row.style.display = "none";

    });


    let start = page * pageSize;
    let end = start + pageSize;


    visibleRows
        .slice(start, end)
        .forEach(function(row) {

            row.style.display = "";

        });


    document.getElementById(
        "page_info"
    ).innerText =
        "Page "
        +
        (page + 1)
        +
        " / "
        +
        Math.max(
            1,
            Math.ceil(
                visibleRows.length / pageSize
            )
        );
}


function nextPage() {

    let rows =
        document.querySelectorAll(
            "#results tbody tr"
        );

    let count = 0;

    rows.forEach(function(row) {
        if (row.dataset.hidden !== "true") {
            count++;
        }
    });


    if (
        (currentPage + 1) * pageSize < count
    ) {
        showPage(currentPage + 1);
    }
}


function previousPage() {

    if (currentPage > 0) {
        showPage(currentPage - 1);
    }

}


function filterTable() {

    let text =
        document
        .getElementById("search")
        .value
        .toLowerCase();


    let rows =
        document.querySelectorAll(
            "#results tbody tr"
        );


    rows.forEach(function(row) {

        row.dataset.hidden =
            !row.innerText
            .toLowerCase()
            .includes(text);

    });


    showPage(0);
}


window.onload = function() {
    showPage(0);
};

</script>

</head>

<body>


<h1>
iNaturalist comparison
</h1>


<input
id="search"
onkeyup="filterTable()"
placeholder="Search filename, ID, date..."
>


""")


        f.write("""
<h2>
Matched observations
</h2>

<div>

<button onclick="previousPage()">
Previous
</button>

<span id="page_info"></span>

<button onclick="nextPage()">
Next
</button>

</div>

<table id="results">

<thead>
<tr>
<th>iNaturalist</th>
<th>Local</th>
<th>Thumbnail</th>
<th>Observation</th>
<th>Date</th>
</tr>
</thead>

<tbody>
""")


        for r in results:

            if r.get("status") != "MATCHED":
                continue

            local_images = r.get(
                "local_images",
                []
            )

            for local_path in local_images:
                local_path = Path(local_path)
                local_thumb_name = (
                    local_path.stem
                    +
                    ".thumb.jpg"
                )

                local_thumb_path = (
                    local_thumb_dir /
                    local_thumb_name
                )


                create_thumbnail_file(
                    local_path,
                    local_thumb_path
                )

            f.write(
                "<tr>"
            )


            f.write(
                "<td>"
            )

            f.write(
                esc(
                    r.get(
                        "inat_original_filename"
                    )
                )
            )

            f.write(
                "</td>"
            )


            f.write(
                "<td>"
            )

            local_names = [
                str(Path(p))
                for p in r.get(
                    "local_images",
                    []
                )
            ]

            f.write(
                "<br>".join(
                    esc(x)
                    for x in local_names
                )
            )

            f.write(
                "</td>"
            )


            f.write(
                "<td>"
            )

            for local_path in r.get(
                    "local_images",
                    []):

                local_path = Path(local_path)

                thumb_name = (
                    local_path.stem
                    +
                    ".thumb.jpg"
                )

                f.write(
                    f"""
                    <img src="thumbnails/local/{esc(thumb_name)}">
                    """
                )

            f.write(
                "</td>"
            )

            f.write(
                "<td>"
                +
                esc(
                    r.get(
                        "observation_id"
                    )
                )
                +
                "</td>"
            )


            f.write(
                "<td>"
                +
                esc(
                    r.get(
                        "timestamp"
                    )
                )
                +
                "</td>"
            )


            f.write(
                "</tr>"
            )


        f.write(
            "</tbody></table>"
        )

        f.write("""
<h2>
Local files not in iNaturalist
</h2>

<table>

<thead>
<tr>
<th>Filename</th>
<th>Thumbnail</th>
<th>Path</th>
<th>Timestamp</th>
</tr>
</thead>

<tbody>
""")


        for image in local_missing:
            thumb_name = (
                Path(
                    image["path"]
                ).stem
                +
                ".thumb.jpg"
            )


            thumb_path = (
                local_thumb_dir /
                thumb_name
            )


            create_thumbnail_file(
                image["path"],
                thumb_path
            )

            f.write(
                "<tr>"
            )

            f.write(
                "<td>"
                +
                esc(
                    image.get(
                        "original_filename",
                        ""
                    )
                )
                +
                "</td>"
            )

            f.write(
                "<td>"
            )

            f.write(
                f'<img src="thumbnails/local/{esc(thumb_name)}">'
            )

            f.write(
                "</td>"
            )

            f.write(
                "<td>"
                +
                esc(
                    image.get(
                        "path"
                    )
                )
                +
                "</td>"
            )

            f.write(
                "<td>"
                +
                esc(
                    image.get(
                        "timestamp"
                    )
                )
                +
                "</td>"
            )


            f.write(
                "</tr>"
            )


        f.write(
            """
</tbody>
</table>

</body>
</html>
"""
        )


def load_existing_observations(filename):

    if not os.path.exists(filename):
        return [], None

    with open(filename, "r", encoding="utf-8") as f:
        observations = json.load(f)

    if not observations:
        return [], None

    highest_id = max(obs["id"] for obs in observations)

    print(f"Loaded {len(observations)} observations")
    print(f"Continuing above observation ID {highest_id}")

    return observations, highest_id

# ======================================================
# Download observations
# ======================================================


def fetch_observations(
        client,
        username,
        existing_observations=None,
        id_above=None,
        limit=None):


    observations = list(existing_observations or [])

    first_request = True

    while True:


        print(
            f"Downloading iNaturalist "
        )


        request_page_size = (
            limit
            if limit and limit < PAGE_SIZE
            else PAGE_SIZE
        )

        params = {


            "user_login":
                username,


            "per_page":
                request_page_size,


            "order_by":
                "id",


            "order":
                "asc",


            # Explicit fields

            "fields":
                ",".join(
                    [

                        "id",

                        "user.login",

                        "time_observed_at",

                        "photos.id",

                        "photos.original_filename",

                    ]
                )

        }

        if id_above:
            params["id_above"] = id_above

        data = client.get(
            "/observations",
            params
        )

        if first_request:

            print(
                "iNaturalist total_results:",
                data.get("total_results")
            )

            first_request = False

        batch = data.get(
            "results",
            []
        )


        if not batch:
            print("BREAK: empty batch")
            break

        id_above = max(
            obs["id"]
            for obs in batch
        )

        for obs in batch:


            # Safety check:
            # never accept another user's observation

            obs_user = obs.get(
                "user",
                {}
            )


            if DEBUG_MODE:

                print("\n========== DEBUG OBSERVATION ==========")

                print(
                    json.dumps(
                        obs,
                        indent=2,
                        ensure_ascii=False
                    )
                )

                print(
                    "=======================================\n"
                )


            if (
                obs_user.get("login")
                !=
                username
            ):

                raise RuntimeError(

                    "\nReceived an observation "
                    "that does not belong to the authenticated user\n\n"

                    f"Requested username: {username}\n"

                    f"Returned user id: {obs_user.get('id')}\n"

                    f"Returned login: {obs_user.get('login')}\n"

                    f"Returned name: {obs_user.get('name')}\n"

                )


            # Mandatory filename check

            for photo in obs.get(
                "photos",
                []
            ):


                if not photo.get(
                    "original_filename"
                ):

                    raise RuntimeError(

                        "\nMissing original_filename\n"
                        f"Observation ID: {obs.get('id')}\n"
                        f"Photo ID: {photo.get('id')}\n\n"
                        "Cannot continue safely.\n"
                        "The script does not guess filenames "
                        "from URLs."
                    )



        observations.extend(
            batch
        )

        with open("observations.json", "w", encoding="utf-8") as f:
            json.dump(
                observations,
                f,
                ensure_ascii=False
            )

        print(
            "Downloaded:",
            len(observations),
            "last id:",
            batch[-1]["id"]
        )

        # Test mode

        if (
            limit
            and
            len(observations) >= limit
        ):

            observations = (
                observations[:limit]
            )

            print("BREAK: reached limit")
            break



        total = data.get(
            "total_results"
        )

    print(
        "Total observations:",
        len(observations)
    )


    return observations



# ======================================================
# Extract photo records
# ======================================================


def parse_inaturalist_photos(
        observations):


    photos = []



    for obs in observations:


        observation_id = obs.get(
            "id"
        )


        taxon = (
            obs.get("taxon")
            or {}
        ).get(
            "name"
        )


        observation_time = None


        raw_time = obs.get(
            "time_observed_at"
        )


        if raw_time:


            try:

                observation_time = (
                    datetime.fromisoformat(
                        raw_time.replace(
                            "Z",
                            "+00:00"
                        )
                    )
                    .replace(
                        tzinfo=None
                    )
                )


            except Exception:

                pass



        for photo in obs.get(
            "photos",
            []
        ):



            filename = photo.get(
                "original_filename"
            )


            # Double safety check

            if not filename:

                raise RuntimeError(
                    f"Missing filename for "
                    f"observation {observation_id}"
                )



            photos.append(
                {

                    "observation_id":
                        observation_id,


                    "taxon":
                        taxon,


                    "filename":
                        normalize_filename(
                            filename
                        ),

                    "inat_original_filename":
                        filename,

                    "timestamp":
                        observation_time,


                    "photo_url":
                        photo.get(
                            "original_url"
                        )

                }
            )



    print(
        "Total iNaturalist photos:",
        len(photos)
    )


    return photos

# ======================================================
# Local image scanning
# ======================================================


def is_supported_image(
        path):


    return (

        os.path.isfile(path)

        and

        os.path.splitext(path)[1].lower()
        in SUPPORTED_EXTENSIONS

    )



def get_file_signature(
        path):


    stat = os.stat(
        path
    )


    return {

        "size":
            stat.st_size,

        "mtime":
            stat.st_mtime

    }



def extract_exif_datetime(
        path):


    try:

        img = Image.open(
            path
        )


        exif = img.getexif()


        if not exif:

            return None



        for key, value in exif.items():


            tag = ExifTags.TAGS.get(
                key
            )


            if tag in (

                "DateTimeOriginal",

                "DateTime"

            ):


                return datetime.strptime(

                    value,

                    "%Y:%m:%d %H:%M:%S"

                )



    except Exception:

        pass



    return None



def scan_local_images(
        folders):


    cache = {}


    if os.path.exists(
        LOCAL_CACHE_FILE
    ):

        cache = load_json(
            LOCAL_CACHE_FILE
        )



    images = []


    updated = False



    for folder in folders:


        for root, _, files in os.walk(folder):


            for filename in files:


                path = os.path.join(
                    root,
                    filename
                )


                if not is_supported_image(
                    path
                ):

                    continue



                signature = get_file_signature(
                    path
                )


                cached = cache.get(
                    path
                )



                if (

                    cached

                    and

                    cached.get(
                        "signature"
                    )
                    ==
                    signature

                ):


                    images.append(
                        cached["data"]
                    )

                    continue



                # New or changed file


                dt = extract_exif_datetime(
                    path
                )


                data = {


                    "path":
                        path,


                    "filename":
                        normalize_filename(
                            filename
                        ),


                    "original_filename":
                        filename,


                    "timestamp":
                        dt.isoformat()
                        if dt
                        else None,


                    "timestamp_minute":
                        timestamp_minute(
                            dt
                        )

                }



                cache[path] = {


                    "signature":
                        signature,


                    "data":
                        data

                }



                images.append(
                    data
                )


                updated = True



    if updated:

        save_json(
            LOCAL_CACHE_FILE,
            cache
        )



    print(
        "Local images indexed:",
        len(images)
    )


    return images



# ======================================================
# Build filename index
# ======================================================


def build_local_indexes(
        local_images):


    filename_index = {}


    timestamp_index = {}



    for image in local_images:


        filename = image.get(
            "filename"
        )


        if filename:


            filename_index.setdefault(
                filename,
                []
            ).append(
                image
            )



        timestamp = image.get(
            "timestamp_minute"
        )


        if timestamp:


            timestamp_index.setdefault(
                timestamp,
                []
            ).append(
                image
            )



    return (
        filename_index,
        timestamp_index
    )

# ======================================================
# Matching engine
# ======================================================


def match_single_photo(
        inat_photo,
        filename_index):


    filename = inat_photo.get(
        "filename"
    )


    if not filename:


        raise RuntimeError(
            "Internal error: "
            "iNaturalist photo has no filename"
        )



    candidates = filename_index.get(
        filename,
        []
    )



    # --------------------------------------------------
    # No local file with this name
    # --------------------------------------------------

    if not candidates:


        return {

            "status":
                "NOT_FOUND",

            "local_image":
                None,

            "match_method":
                None

        }



    # --------------------------------------------------
    # Exactly one filename match
    # --------------------------------------------------

    if len(candidates) == 1:


        return {

            "status":
                "MATCHED",

            "local_image":
                candidates[0],

            "match_method":
                "filename"

        }



    # --------------------------------------------------
    # Multiple identical filenames
    # Use timestamp
    # --------------------------------------------------


    inat_time = timestamp_minute(
        inat_photo.get(
            "timestamp"
        )
    )



    if inat_time:


        timestamp_matches = []


        for candidate in candidates:


            if (
                candidate.get(
                    "timestamp_minute"
                )
                ==
                inat_time
            ):


                timestamp_matches.append(
                    candidate
                )



        if len(timestamp_matches) == 1:


            return {


                "status":
                    "MATCHED",


                "local_image":
                    timestamp_matches[0],


                "match_method":
                    "filename+timestamp"


            }



        elif len(timestamp_matches) > 1:


            return {


                "status":
                    "AMBIGUOUS_TIMESTAMP",


                "local_image":
                    timestamp_matches,


                "match_method":
                    None

            }



    # --------------------------------------------------
    # Filename duplicated but timestamp
    # did not resolve it
    # --------------------------------------------------

    return {


        "status":
            "AMBIGUOUS_FILENAME",


        "local_image":
            candidates,


        "match_method":
            None

    }



# ======================================================
# Match all iNaturalist photos
# ======================================================


def compare_photos(
        inat_photos,
        filename_index):


    results = []



    for photo in inat_photos:


        result = match_single_photo(
            photo,
            filename_index
        )



        local = result.get(
            "local_image"
        )



        # Convert image object(s)
        # to output-friendly values


        if isinstance(
            local,
            list
        ):


            local_paths = [

                x["path"]

                for x in local

            ]

        elif local:


            local_paths = [
                local["path"]
            ]

        else:

            local_paths = []



        results.append(
            {


                "observation_id":
                    photo["observation_id"],


                "taxon":
                    photo["taxon"],


                "inat_filename":
                    photo["filename"],

                "inat_original_filename":
                    photo["inat_original_filename"],

                "inat_timestamp":
                    timestamp_minute(
                        photo["timestamp"]
                    ),


                "inat_photo_url":
                    photo["photo_url"],


                "local_images":
                    local_paths,


                "status":
                    result["status"],


                "match_method":
                    result["match_method"]

            }
        )



    return results

# ======================================================
# XLSX generation
# ======================================================


def create_thumbnail(
        image_path,
        row_number):


    try:


        img = Image.open(
            image_path
        )


        img.thumbnail(
            THUMB_SIZE
        )


        thumb_path = (
            f"/tmp/"
            f"inat_thumbnail_{row_number}.jpg"
        )


        img.convert(
            "RGB"
        ).save(
            thumb_path,
            quality=90
        )


        return thumb_path



    except Exception as e:


        print(
            f"Cannot create thumbnail "
            f"for {image_path}: {e}"
        )


        return None


# ======================================================
# XLSX generation (two sheets)
# ======================================================


def create_thumbnail(
        image_path,
        row_number):

    try:

        img = Image.open(
            image_path
        )

        img.thumbnail(
            THUMB_SIZE
        )


        thumb_path = (
            f"/tmp/"
            f"inat_thumbnail_{row_number}.jpg"
        )


        img.convert(
            "RGB"
        ).save(
            thumb_path,
            quality=90
        )


        return thumb_path


    except Exception:

        return None



def create_xlsx(
        results,
        local_missing,
        output_file):


    wb = Workbook()



    # ==================================================
    # Sheet 1
    # ==================================================

    ws = wb.active

    ws.title = (
        "iNaturalist comparison"
    )


    headers = [

        "Observation ID",

        "Taxon",

        "iNaturalist Filename",

        "iNaturalist Timestamp",

        "Local Image(s)",

        "Status",

        "Match Method",

        "Thumbnail"

    ]


    ws.append(
        headers
    )


    for cell in ws[1]:

        cell.font = Font(
            bold=True
        )


    row = 2


    for result in results:


        local_images = result[
            "local_images"
        ]


        ws.append(
            [

                result["observation_id"],

                result["taxon"],

                result["inat_original_filename"],

                result["inat_timestamp"],

                "\n".join(
                    local_images
                ),

                result["status"],

                result["match_method"]
                or "",

                ""

            ]
        )



        if (

            result["status"]
            ==
            "MATCHED"

            and

            len(local_images)
            ==
            1

        ):


            thumb = create_thumbnail(

                local_images[0],

                row

            )


            if thumb:


                image = XLImage(
                    thumb
                )


                image.width = 120

                image.height = 120


                ws.add_image(

                    image,

                    f"H{row}"

                )


                ws.row_dimensions[
                    row
                ].height = 95



        row += 1



    for col, width in {

        "A":16,
        "B":35,
        "C":35,
        "D":22,
        "E":70,
        "F":22,
        "G":22,
        "H":18

    }.items():

        ws.column_dimensions[
            col
        ].width = width



    ws.freeze_panes = "A2"



    # ==================================================
    # Sheet 2
    # ==================================================

    ws2 = wb.create_sheet(
        "Local not in iNaturalist"
    )


    ws2.append(
        [
            "Filename",
            "Path",
            "Timestamp",
            "Thumbnail"
        ]
    )


    row = 2


    for image in local_missing:

        ws2.append(
            [
                image["original_filename"],
                image["path"],
                image["timestamp_minute"],
                ""
            ]
        )


        thumb = create_thumbnail(
            image["path"],
            row
        )


        if thumb:

            xl_image = XLImage(
                thumb
            )

            xl_image.width = 120
            xl_image.height = 120


            ws2.add_image(
                xl_image,
                f"D{row}"
            )


            ws2.row_dimensions[
                row
            ].height = 95


        row += 1

    ws2.column_dimensions["A"].width = 35
    ws2.column_dimensions["B"].width = 90
    ws2.column_dimensions["C"].width = 22
    ws2.column_dimensions["D"].width = 18

    wb.save(
        output_file
    )


    print(
        "Created:",
        output_file
    )


# ======================================================
# Convert XLSX to Apple Numbers
# ======================================================


def convert_to_numbers(
        xlsx_file):


    if not sys.platform.startswith(
        "darwin"
    ):

        print(
            "Numbers conversion "
            "requires macOS"
        )

        return



    numbers_file = (
        os.path.splitext(
            xlsx_file
        )[0]
        +
        ".numbers"
    )



    applescript = f'''

tell application "Numbers"

    activate

    open POSIX file "{os.path.abspath(xlsx_file)}"

    delay 5

    save front document as POSIX file "{os.path.abspath(numbers_file)}"

    close front document

end tell

'''



    subprocess.run(
        [
            "osascript",
            "-e",
            applescript
        ]
    )



    print(
        "Created Numbers file:",
        numbers_file
    )



# ======================================================
# Add local images not found in iNaturalist
# ======================================================


def find_local_not_in_inaturalist(
        local_images,
        results):


    matched_paths = set()



    for result in results:


        if (
            result["status"]
            ==
            "MATCHED"
        ):


            for path in result["local_images"]:

                matched_paths.add(
                    path
                )



    missing = []



    for image in local_images:


        if (
            image["path"]
            not in
            matched_paths
        ):


            missing.append(
                image
            )



    return missing


def load_observation_ids(filename):

    ids = set()

    with open(
        filename,
        "r",
        encoding="utf-8"
    ) as f:

        for line in f:

            line = line.strip()

            if not line:
                continue

            try:

                ids.add(
                    int(line)
                )

            except ValueError:

                # Skip header or non-ID lines
                continue


    return ids

# ======================================================
# Main
# ======================================================


def main():


    parser = argparse.ArgumentParser(

        description=
        "Compare iNaturalist photos "
        "with local images"

    )



    parser.add_argument(

        "--folders",

        nargs="+",

        required=True,

        help=
        "Folders containing local photos"

    )


    parser.add_argument(
        "--username",
        required=True,
        help="Your iNaturalist username"
    )



    parser.add_argument(

        "--limit",

        type=int,

        default=None,

        help=
        "Download only N observations "
        "for testing"

    )


    parser.add_argument(
        "--output-dir",
        default="inat_comparison"
    )

    parser.add_argument(

        "--numbers",

        action="store_true",

        help=
        "Convert XLSX to Apple Numbers"

    )


    parser.add_argument(
        "--id-export",
        help="iNaturalist exported observation IDs file"
    )


    args = parser.parse_args()
    global DEBUG_MODE

    if args.limit == 1:
        DEBUG_MODE = True



    # ------------------------------
    # Authentication
    # ------------------------------

    token = load_api_token()

    client = INaturalistClient(
        token
    )

    username = args.username


    # ------------------------------
    # Download observations
    # ------------------------------

    observations, id_above = load_existing_observations(
        "observations.json"
    )

    observations = fetch_observations(
        client,
        username,
        observations,
        id_above,
        args.limit,
    )

    if args.id_export:

        exported_ids = load_observation_ids(
            args.id_export
        )

        api_ids = {
            obs["id"]
            for obs in observations
        }


        missing = exported_ids - api_ids

        extra = api_ids - exported_ids


        print()
        print("========== ID comparison ==========")

        print(
            "Export IDs:",
            len(exported_ids)
        )

        print(
            "API IDs:",
            len(api_ids)
        )

        print(
            "Missing from API:",
            len(missing)
        )

        print(
            "Extra from API:",
            len(extra)
        )


        if missing:

            print(
                "First missing IDs:"
            )

            print(
                list(missing)[:20]
            )


        print(
            "=================================="
        )



    inat_photos = parse_inaturalist_photos(

        observations

    )



    # ------------------------------
    # Local files
    # ------------------------------


    local_images = scan_local_images(

        args.folders

    )



    filename_index, timestamp_index = build_local_indexes(

        local_images

    )



    # ------------------------------
    # Matching
    # ------------------------------


    results = compare_photos(

        inat_photos,

        filename_index

    )



    matched = sum(

        1

        for r in results

        if r["status"]
        ==
        "MATCHED"

    )



    print()

    print(
        "Results:"
    )

    print(
        "Total iNaturalist photos:",
        len(results)
    )

    print(
        "Matched:",
        matched
    )



    # ------------------------------
    # Output
    # ------------------------------


    local_missing = find_local_not_in_inaturalist(
        local_images,
        results
    )


    print(
        "Local files not in iNaturalist:",
        len(local_missing)
    )


    create_html_report(
        results,
        local_missing,
        args.output_dir
    )


    if args.numbers:


        convert_to_numbers(

            args.output

        )




if __name__ == "__main__":

    main()
