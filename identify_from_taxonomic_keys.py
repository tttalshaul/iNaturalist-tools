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
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

try:
    from PIL import ExifTags, Image
except ImportError:  # pragma: no cover - Pillow is optional
    ExifTags = None
    Image = None


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
    if path.suffix.lower() == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as error:
            raise RuntimeError(
                "PDF input requires pypdf. Install it with: python3 -m pip install pypdf"
            ) from error
        reader = PdfReader(str(path))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    if path.suffix.lower() in {".html", ".htm"}:
        return clean_html(path.read_text(encoding="utf-8", errors="replace"))
    return path.read_text(encoding="utf-8", errors="replace")


def clean_html(text: str) -> str:
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", text,
                  flags=re.IGNORECASE | re.DOTALL)
    return re.sub(r"<[^>]+>", " ", html.unescape(text))


def read_article(source: str) -> str:
    parsed = urlparse(source)
    if parsed.scheme in {"http", "https"}:
        request = Request(source, headers={"User-Agent": "iNaturalist-taxonomic-key-reader/1.0"})
        with urlopen(request, timeout=60) as response:
            return clean_html(response.read().decode("utf-8", errors="replace"))
    path = Path(source)
    if path.suffix.lower() not in {".pdf", ".html", ".htm"}:
        raise ValueError("Article input must be a PDF file or an HTML file/URL")
    return read_text(path)


def normalise(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def words(value: str) -> set[str]:
    return {word for word in re.findall(r"[a-z][a-z-]{2,}", value.lower())
            if word not in STOP_WORDS}


def parse_key_text(text: str) -> list[Candidate]:
    candidates: list[Candidate] = []
    current: Candidate | None = None
    for raw_line in text.splitlines():
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


def parse_key_source(source: str) -> list[Candidate]:
    return parse_key_text(read_article(source))


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


def load_local_observation(observations_file: Path, observation_id: str,
                           taxa_file: Path | None = None) -> dict[str, Any]:
    observations = json.loads(observations_file.read_text(encoding="utf-8"))
    observation = next(
        (item for item in observations if str(item.get("id")) == str(observation_id)),
        None,
    )
    if observation is None:
        raise ValueError(f"Observation {observation_id} was not found in {observations_file}")

    result: dict[str, Any] = {
        "id": observation.get("id"),
        "observed_at": observation.get("time_observed_at"),
        "photos": observation.get("photos", []),
        "taxon_id": observation.get("taxon", {}).get("id"),
    }
    for key in ("location", "latitude", "longitude", "lat", "lng"):
        if key in observation:
            result[key] = observation[key]
    if taxa_file and taxa_file.exists() and result.get("taxon_id") is not None:
        taxa = json.loads(taxa_file.read_text(encoding="utf-8"))
        taxon = taxa.get(str(result["taxon_id"]), {})
        if taxon:
            result["taxon"] = taxon.get("name")
            result["taxon_rank"] = taxon.get("rank")
            result["taxon_ancestors"] = taxon.get("ancestor_ids", [])
            result["taxon_lineage"] = [
                taxa[str(ancestor_id)]["name"]
                for ancestor_id in taxon.get("ancestor_ids", [])
                if str(ancestor_id) in taxa and taxa[str(ancestor_id)].get("name")
            ] + [taxon["name"]]
    return result


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
    if observation.get("observed_at"):
        lines.append(f"Observed at: {observation['observed_at']}")
    if observation.get("taxon_lineage"):
        lines.append(f"Cached taxonomy context: {' > '.join(observation['taxon_lineage'])}")
    if observation.get("location"):
        lines.append(f"Location: {observation['location']}")
    lines.append(f"Photos: {len(observation.get('photos', []))}")
    lines.append("")
    if not ranked:
        lines.extend(["No candidates were parsed from the supplied key/article.", ""])
        return "\n".join(lines)
    best = ranked[0]
    if best.score <= 0:
        lines.extend(["## No evidence-supported identification", "",
                      "The supplied observation did not match any key evidence.",
                      "The available date, location, and photo metadata did not match key evidence.", ""])
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
    parser.add_argument("--article", "--key", dest="article_sources", action="append",
                        required=True, help="PDF file or HTML file/URL (repeatable).")
    parser.add_argument("--image", type=Path, action="append", help="Observation image (repeatable).")
    parser.add_argument("--image-dir", type=Path, help="Directory containing observation images.")
    parser.add_argument("--observations-file", type=Path, default=Path("observations.json"),
                        help="Local iNaturalist observation cache (default: observations.json).")
    parser.add_argument("--taxa-file", type=Path, default=Path("taxa.json"),
                        help="Local taxon cache used to resolve the observation taxon.")
    parser.add_argument("--observation-id", help="Select this ID from the local observations cache.")
    parser.add_argument("--output", type=Path, default=Path("identification_report.md"))
    parser.add_argument("--json-output", type=Path, help="Also write machine-readable JSON.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    observation: dict[str, Any] = {}
    if args.observation_id:
        observation.update(load_local_observation(args.observations_file,
                                                  args.observation_id,
                                                  args.taxa_file))
    image_paths = list(args.image or [])
    if args.image_dir:
        image_paths.extend(path for path in args.image_dir.rglob("*")
                           if path.suffix.lower() in IMAGE_EXTENSIONS)
    observation.setdefault("photos", []).extend(image_observation(path) for path in image_paths)
    candidates = [candidate for source in args.article_sources
                  for candidate in parse_key_source(source)]
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