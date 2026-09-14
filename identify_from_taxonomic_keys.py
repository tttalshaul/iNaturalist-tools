#!/usr/bin/env python3

"""Explainable, evidence-based identification from taxonomic articles/keys.

This tool is intentionally conservative: it ranks candidates from evidence
written in the supplied key/article and observation metadata, but it does not
claim that a photograph alone proves an identification.
"""

import argparse
import html
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin

import requests

try:
    from PIL import ExifTags, Image
except ImportError:  # pragma: no cover - Pillow is optional
    ExifTags = None
    Image = None


INAT_API = "https://api.inaturalist.org/v2"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"}
STOP_WORDS = {
    "the", "and", "with", "from", "this", "that", "very", "more",
    "than", "usually", "often", "body", "photo", "species", "likely",
    "present", "absent", "male", "female", "adult", "larva", "unknown",
}


@dataclass
class Candidate:
    name: str
    rank: str = "unknown"
    evidence: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    score: float = 0.0


def read_text(path: Path) -> str:
    if path.suffix.lower() in {".html", ".htm"}:
        text = re.sub(r"<script.*?</script>|<style.*?</style>", " ",
                      path.read_text(encoding="utf-8", errors="replace"),
                      flags=re.IGNORECASE | re.DOTALL)
        return re.sub(r"<[^>]+>", " ", html.unescape(text))
    return path.read_text(encoding="utf-8", errors="replace")


def normalise(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def words(value: str) -> set[str]:
    return {word for word in re.findall(r"[a-z][a-z-]{2,}", value.lower())
            if word not in STOP_WORDS}


def parse_key_file(path: Path) -> list[Candidate]:
    """Parse JSON keys or simple heading/paragraph taxonomic documents.

    JSON accepts either [{"name": ..., "features": [...]}] or
    {"taxa": [{"name": ..., "features": [...]}]}.
    Text files use a heading such as '# Genus species' followed by evidence
    lines. Lines beginning with Evidence:, Features:, Missing:, or Notes: are
    especially useful but ordinary paragraphs are accepted too.
    """
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        entries = data.get("taxa", data) if isinstance(data, dict) else data
        candidates = []
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("name"):
                continue
            features = entry.get("features", entry.get("evidence", []))
            if isinstance(features, str):
                features = [features]
            missing = entry.get("missing", [])
            if isinstance(missing, str):
                missing = [missing]
            candidates.append(Candidate(str(entry["name"]),
                                        str(entry.get("rank", "unknown")),
                                        [str(item) for item in features],
                                        [str(item) for item in missing]))
        return candidates

    candidates: list[Candidate] = []
    current: Candidate | None = None
    for raw_line in read_text(path).splitlines():
        line = normalise(re.sub(r"^\s*(?:[-*]|\d+[.)])\s*", "", raw_line))
        if not line:
            continue
        heading = re.sub(r"^#+\s*", "", line)
        if raw_line.lstrip().startswith("#") or re.match(r"^[A-Z][^:]{2,80}$", line):
            if current:
                candidates.append(current)
            current = Candidate(heading)
            continue
        if current is None:
            continue
        label, separator, value = line.partition(":")
        if separator and label.lower() in {"missing", "unknown", "not seen"}:
            current.missing.append(value.strip())
        else:
            current.evidence.append(line)
    if current:
        candidates.append(current)
    return candidates


def rationalise_gps(value: Any) -> float:
    if isinstance(value, tuple):
        return float(value[0]) / float(value[1]) if value[1] else 0.0
    return float(value)


def exif_value(exif: Any, name: str) -> Any:
    if ExifTags is None:
        return None
    names = {value: key for key, value in ExifTags.TAGS.items()}
    return exif.get(names.get(name))


def image_observation(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {"path": str(path), "filename": path.name}
    if Image is None:
        result["metadata_note"] = "Install Pillow to read EXIF date/time and GPS."
        return result
    try:
        with Image.open(path) as image:
            exif = image.getexif()
            captured = exif_value(exif, "DateTimeOriginal") or exif_value(exif, "DateTime")
            if captured:
                result["observed_at"] = str(captured).replace(":", "-", 2)
            gps = exif_value(exif, "GPSInfo")
            if gps:
                gps_names = {value: key for key, value in ExifTags.GPSTAGS.items()}
                latitude = gps.get(gps_names.get("GPSLatitude"))
                longitude = gps.get(gps_names.get("GPSLongitude"))
                if latitude and longitude:
                    lat = sum(rationalise_gps(item) / (60 ** index)
                              for index, item in enumerate(latitude))
                    lon = sum(rationalise_gps(item) / (60 ** index)
                              for index, item in enumerate(longitude))
                    if gps.get(gps_names.get("GPSLatitudeRef")) == "S":
                        lat = -lat
                    if gps.get(gps_names.get("GPSLongitudeRef")) == "W":
                        lon = -lon
                    result["location"] = {"latitude": lat, "longitude": lon}
    except Exception as error:  # metadata should never prevent identification
        result["metadata_note"] = f"Could not read image metadata: {error}"
    return result


def observation_from_inat(observation_id: str, session: requests.Session) -> dict[str, Any]:
    response = session.get(f"{INAT_API}/observations/{observation_id}", timeout=60)
    response.raise_for_status()
    observation = response.json().get("results", [{}])[0]
    photos = []
    for photo in observation.get("photos", []):
        url = photo.get("url", "")
        photos.append(urljoin(url, "") .replace("/square.", "/original.") if url else "")
    return {
        "id": observation.get("id"),
        "observed_at": observation.get("observed_on_string") or observation.get("observed_on"),
        "location": {"latitude": observation.get("lat"), "longitude": observation.get("lng")},
        "taxon": observation.get("taxon", {}).get("name"),
        "photos": [photo for photo in photos if photo],
    }


def download_photos(observation: dict[str, Any], directory: Path,
                    session: requests.Session) -> list[dict[str, Any]]:
    directory.mkdir(parents=True, exist_ok=True)
    photos = []
    for index, url in enumerate(observation.get("photos", []), start=1):
        target = directory / f"inat_{observation.get('id', 'observation')}_{index}.jpg"
        if not target.exists():
            response = session.get(url, timeout=120)
            response.raise_for_status()
            target.write_bytes(response.content)
        photos.append(image_observation(target))
    return photos


def rank_candidates(candidates: Iterable[Candidate], observation: dict[str, Any]) -> list[Candidate]:
    context = " ".join(str(value) for value in observation.values())
    context_words = words(context)
    for candidate in candidates:
        matches_for_candidate: list[str] = []
        for evidence in list(candidate.evidence):
            evidence_words = words(evidence)
            matches = context_words & evidence_words
            if matches:
                candidate.score += min(3.0, len(matches) * 0.5)
                matches_for_candidate.append(
                    f"Context matches key evidence: {', '.join(sorted(matches))}."
                )
        candidate.evidence.extend(matches_for_candidate)
        if not observation.get("observed_at"):
            candidate.missing.append("date/time of observation")
        if not observation.get("location"):
            candidate.missing.append("location/GPS")
        if not observation.get("photos"):
            candidate.missing.append("a photograph")
        candidate.missing = list(dict.fromkeys(candidate.missing))
    return sorted(candidates, key=lambda item: item.score, reverse=True)


def render_markdown(observation: dict[str, Any], ranked: list[Candidate]) -> str:
    lines = ["# Explainable identification", "", f"Observation: `{observation.get('id', 'local')}`", ""]
    if observation.get("taxon_group"):
        lines.append(f"Taxonomic group: {observation['taxon_group']}")
    if observation.get("observed_at"):
        lines.append(f"Observed at: {observation['observed_at']}")
    if observation.get("location"):
        lines.append(f"Location: {observation['location']}")
    if observation.get("features"):
        lines.append(f"Observed characters: {', '.join(observation['features'])}")
    lines.append(f"Photos: {len(observation.get('photos', []))}")
    lines.append("")
    if not ranked:
        lines.extend(["No candidates were parsed from the supplied key/article.", ""])
        return "\n".join(lines)
    best = ranked[0]
    if best.score <= 0:
        lines.extend(["## No evidence-supported identification", "",
                      "The supplied observation did not match any key evidence.",
                      "Add observed characters with `--feature`, or provide a more structured key.", ""])
        lines.append("### Missing or useful next evidence")
        lines.extend(f"- {item}" for item in best.missing or ["observable diagnostic characters"])
        lines.append("\n### Candidates considered")
        lines.extend(f"- {item.name}" for item in ranked)
        return "\n".join(lines) + "\n"
    lines.extend([f"## Suggested identification: {best.name}",
                  f"Score: {best.score:.1f} (heuristic, not a confirmed identification)", ""])
    lines.append("### Why")
    lines.extend(f"- {item}" for item in best.evidence)
    lines.append("### Missing or useful next evidence")
    lines.extend(f"- {item}" for item in best.missing or ["No additional metadata was identified as missing."])
    lines.append("\n### Other candidates")
    lines.extend(f"- {item.name}: {item.score:.1f}" for item in ranked[1:])
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--article", "--key", dest="key_files", action="append", type=Path,
                        required=True, help="Text, Markdown, HTML, or JSON article/key (repeatable).")
    parser.add_argument("--image", type=Path, action="append", help="Observation image (repeatable).")
    parser.add_argument("--image-dir", type=Path, help="Directory containing observation images.")
    parser.add_argument("--observation-json", type=Path, help="Observation metadata JSON.")
    parser.add_argument("--observation-id", help="Fetch an iNaturalist observation by ID.")
    parser.add_argument("--download-dir", type=Path, default=Path("inat_identification_photos"))
    parser.add_argument("--taxon-group", help="Restrict the report context to a group, e.g. Lepidoptera.")
    parser.add_argument("--feature", action="append", default=[],
                        help="Observed character from the photograph/key (repeatable).")
    parser.add_argument("--location", help="Override location as latitude,longitude.")
    parser.add_argument("--observed-at", help="Override observation date/time.")
    parser.add_argument("--output", type=Path, default=Path("identification_report.md"))
    parser.add_argument("--json-output", type=Path, help="Also write machine-readable JSON.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    session = requests.Session()
    session.headers["User-Agent"] = "iNaturalist-taxonomic-key-identifier/1.0"
    observation: dict[str, Any] = {}
    if args.observation_json:
        observation = json.loads(args.observation_json.read_text(encoding="utf-8"))
    if args.observation_id:
        observation.update(observation_from_inat(args.observation_id, session))
        observation["photos"] = download_photos(observation, args.download_dir, session)
    image_paths = list(args.image or [])
    if args.image_dir:
        image_paths.extend(path for path in args.image_dir.rglob("*")
                           if path.suffix.lower() in IMAGE_EXTENSIONS)
    observation.setdefault("photos", []).extend(image_observation(path) for path in image_paths)
    if args.location:
        latitude, longitude = (float(value) for value in args.location.split(",", 1))
        observation["location"] = {"latitude": latitude, "longitude": longitude}
    if args.observed_at:
        observation["observed_at"] = args.observed_at
    if args.taxon_group:
        observation["taxon_group"] = args.taxon_group
    if args.feature:
        observation.setdefault("features", []).extend(args.feature)
    candidates = [candidate for path in args.key_files for candidate in parse_key_file(path)]
    ranked = rank_candidates(candidates, observation)
    args.output.write_text(render_markdown(observation, ranked), encoding="utf-8")
    if args.json_output:
        args.json_output.write_text(json.dumps({"observation": observation,
                                                 "candidates": [candidate.__dict__ for candidate in ranked]},
                                                indent=2, ensure_ascii=False), encoding="utf-8")
    print(render_markdown(observation, ranked))
    return 0


if __name__ == "__main__":
    sys.exit(main())