#!/usr/bin/env python3

"""Explainable, evidence-based identification from taxonomic articles/keys.

This tool is intentionally conservative: it ranks candidates from evidence
written in the supplied key/article and observation metadata, but it does not
claim that a photograph alone proves an identification.
"""

import argparse
import html
import json
import os
import re
import ssl
import sys
from urllib.parse import quote_plus, urlencode, urlparse
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

FALLBACK_TAXON_KEYS = {
    "Arthropoda": """\
Broad arthropod key: use the major classes below when no more specific source is available.
Insecta
Three pairs of legs; often one or two pairs of wings; body divided into head, thorax and abdomen; antennae present.
Arachnida
Four pairs of legs; cephalothorax and abdomen; no antennae, often eight eyes or simple eyes.
Crustacea
Mostly aquatic; two pairs of antennae; jointed appendages and gills; often biramous limbs.
Chilopoda
Flattened body with many segments; one pair of legs per segment; fast-running predators.
Diplopoda
Cylindrical body with many segments; two pairs of legs per segment; usually detritivores.
Merostomata
Marine chelicerates with a horseshoe-like carapace; book gills; large abdominal appendages.
Pycnogonida
Small marine sea spiders with very long legs relative to the body; proboscis-like mouthparts.
""",
}


@dataclass
class Candidate:
    name: str
    rank: str = "unknown"
    evidence: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    score: float = 0.0


TAXONOMIC_RANKS = [
    "domain", "kingdom", "phylum", "class", "order", "family",
    "genus", "species", "subspecies",
]


@dataclass
class TaxonomicStep:
    current_name: str
    current_rank: str
    target_rank: str | None
    queries: list[str] = field(default_factory=list)
    selected_name: str | None = None
    selected_score: float = 0.0
    candidates: list[str] = field(default_factory=list)
    stop_reason: str | None = None


def next_taxonomic_rank(rank: str | None) -> str | None:
    if not rank:
        return None
    try:
        index = TAXONOMIC_RANKS.index(rank.lower())
    except ValueError:
        return None
    return TAXONOMIC_RANKS[index + 1] if index + 1 < len(TAXONOMIC_RANKS) else None


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


def fetch_url_text(url: str, timeout: int = 60, headers: dict[str, str] | None = None) -> str:
    request = Request(url, headers=headers or {"User-Agent": "iNaturalist-taxonomic-key-reader/1.0"})
    contexts: list[ssl.SSLContext | None] = []
    try:
        import certifi
        contexts.append(ssl.create_default_context(cafile=certifi.where()))
    except Exception:
        contexts.append(ssl.create_default_context())
    contexts.append(ssl._create_unverified_context())

    errors: list[Exception] = []
    for context in contexts:
        try:
            with urlopen(request, timeout=timeout, context=context) as response:
                payload = response.read()
            return payload.decode("utf-8", errors="replace") if payload else ""
        except Exception as exc:  # pragma: no cover - depends on outside network state
            errors.append(exc)

    if errors:
        raise RuntimeError(f"Request to {url} failed: {errors[-1]}")
    return ""


def clean_html(text: str) -> str:
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", text,
                  flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"</(?:p|h[1-6]|li|br|div|tr)>", "\n", text, flags=re.IGNORECASE)
    return re.sub(r"<[^>]+>", " ", html.unescape(text))


def read_article(source: str) -> str:
    parsed = urlparse(source)
    if parsed.scheme in {"http", "https"}:
        try:
            payload = fetch_url_text(source)
        except Exception:
            return ""
        if "biodiversitylibrary.org" in source.lower() or "api3" in source.lower():
            return extract_bhl_text(payload)
        return clean_html(payload)
    path = Path(source)
    if path.suffix.lower() not in {".pdf", ".html", ".htm"}:
        raise ValueError("Article input must be a PDF file or an HTML file/URL")
    return read_text(path)


def extract_bhl_text(payload: str) -> str:
    try:
        data = json.loads(payload)
    except Exception:
        return clean_html(payload)

    fragments: list[str] = []

    def add(value: Any) -> None:
        if isinstance(value, str):
            text = normalise(value)
            if text:
                fragments.append(text)
        elif isinstance(value, (list, tuple)):
            for item in value:
                add(item)
        elif isinstance(value, dict):
            for key, item in value.items():
                lowered = str(key).lower()
                if lowered in {"ocr", "ocrtext", "text", "title", "description", "notes", "result", "results", "items", "item"}:
                    add(item)
                elif isinstance(item, (dict, list, tuple)):
                    add(item)

    add(data)
    if not fragments:
        return clean_html(payload)
    return "\n".join(fragments[:50])


def load_bhl_token(paths: Iterable[Path]) -> str | None:
    for path in paths:
        try:
            token = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if token:
            return token
    for name in ("BHL_TOKEN", "BHL_API_KEY"):
        token = os.environ.get(name)
        if token and token.strip():
            return token.strip()
    return None


def bhl_search_queries(observation: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for value in [
        observation.get("taxon"),
        observation.get("taxon_lineage", [])
    ]:
        if isinstance(value, str):
            names.append(value)
        elif isinstance(value, list):
            names.extend(str(item) for item in value)
    if not names:
        return []
    seen: set[str] = set()
    queries: list[str] = []
    for name in names:
        cleaned = re.sub(r"\s+", " ", str(name)).strip()
        if not cleaned:
            continue
        for query in [cleaned]:
            query = query.strip()
            if query and query.lower() not in seen:
                seen.add(query.lower())
                queries.append(query)
    return queries[:8]


def bhl_search_articles(query: str, token: str | None, max_results: int = 5) -> list[str]:
    params = {
        "op": "Search",
        "searchterm": query,
        "format": "json",
        "page": "1",
    }
    if token:
        params["apikey"] = token
    url = f"https://www.biodiversitylibrary.org/api3?{urlencode(params)}"
    try:
        payload = fetch_url_text(url)
    except Exception:
        return []

    if not payload.strip():
        return []

    try:
        data = json.loads(payload)
    except Exception:
        return []

    items: list[str] = []
    for key in ("Result", "result", "results", "Items", "items"):
        values = data.get(key) if isinstance(data, dict) else None
        if values is None:
            continue
        if isinstance(values, list):
            raw_items = values
        elif isinstance(values, dict):
            raw_items = list(values.values())
        else:
            raw_items = [values]
        for item in raw_items[:max_results]:
            if not isinstance(item, dict):
                continue
            page_id = item.get("PageID") or item.get("pageid") or item.get("page_id")
            item_id = item.get("ItemID") or item.get("itemid") or item.get("item_id")
            title = item.get("Title") or item.get("title") or item.get("Name") or item.get("name")
            if page_id:
                metadata = {"op": "GetPageMetadata", "pageid": page_id,
                            "format": "json", "ocr": "t"}
                if token:
                    metadata["apikey"] = token
                items.append(f"https://www.biodiversitylibrary.org/api3?{urlencode(metadata)}")
            elif item_id:
                metadata = {"op": "GetItemMetadata", "id": item_id, "format": "json"}
                if token:
                    metadata["apikey"] = token
                items.append(f"https://www.biodiversitylibrary.org/api3?{urlencode(metadata)}")
            elif title:
                items.append(f"https://www.biodiversitylibrary.org/api3?op=Search&searchterm={quote_plus(str(title))}&format=json")
    return items[:max_results]


def sources_for_step(observation: dict[str, Any], source_mode: str,
                     local_sources: list[str], token: str | None) -> tuple[list[str], list[str]]:
    if source_mode == "local":
        return local_sources, []
    queries = bhl_search_queries(observation)
    sources: list[str] = []
    for query in queries:
        sources.extend(bhl_search_articles(query, token, max_results=3))
    if sources:
        return list(dict.fromkeys(sources)), queries
    fallback_source = fallback_source_for_taxon(observation.get("taxon"))
    if fallback_source:
        return [fallback_source], queries
    return [], queries


def recursive_identification(observation: dict[str, Any], source_mode: str,
                             local_sources: list[str], token: str | None,
                             max_levels: int) -> tuple[list[Candidate], list[TaxonomicStep]]:
    current_name = observation.get("taxon")
    current_rank = observation.get("taxon_rank")
    steps: list[TaxonomicStep] = []
    final_ranked: list[Candidate] = []
    if not current_name or not current_rank:
        return [], [TaxonomicStep(
            current_name=str(current_name or "unknown"),
            current_rank=str(current_rank or "unknown"),
            target_rank=None,
            stop_reason="The cached observation has no usable starting taxon and rank.",
        )]

    for _ in range(max_levels):
        target_rank = next_taxonomic_rank(str(current_rank))
        step = TaxonomicStep(str(current_name), str(current_rank), target_rank)
        if target_rank is None:
            step.stop_reason = "Already at the finest supported rank."
            steps.append(step)
            break
        step_observation = dict(observation)
        step_observation["taxon"] = current_name
        step_observation["taxon_rank"] = current_rank
        step_observation["taxon_lineage"] = list(observation.get("taxon_lineage", []))
        sources, queries = sources_for_step(step_observation, source_mode, local_sources, token)
        step.queries = queries
        if not sources:
            step.stop_reason = "No usable source was found for this taxon."
            steps.append(step)
            break
        ranked = rank_candidates(
            [candidate for source in sources for candidate in parse_key_source(source)],
            step_observation,
        )
        final_ranked = ranked
        step.candidates = [candidate.name for candidate in ranked[:10]]
        if not ranked or ranked[0].score <= 0:
            step.stop_reason = "The available key evidence did not support a narrower taxon."
            steps.append(step)
            break
        selected = ranked[0]
        if selected.name.strip().lower() == str(current_name).strip().lower():
            step.stop_reason = "The key returned the current taxon instead of a narrower candidate."
            steps.append(step)
            break
        step.selected_name = selected.name
        step.selected_score = selected.score
        steps.append(step)
        current_name = selected.name
        current_rank = target_rank
        observation["taxon"] = current_name
        observation["taxon_rank"] = current_rank
        observation["taxon_lineage"] = list(observation.get("taxon_lineage", [])) + [current_name]
    return final_ranked, steps


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


def fallback_key_text_for_taxon(taxon_name: str | None) -> str | None:
    if not taxon_name:
        return None
    cleaned = normalise(str(taxon_name))
    for key, text in FALLBACK_TAXON_KEYS.items():
        if cleaned.lower() == key.lower():
            return text
    for key, text in FALLBACK_TAXON_KEYS.items():
        if cleaned.lower() in key.lower() or key.lower() in cleaned.lower():
            return text
    return None


def fallback_source_for_taxon(taxon_name: str | None) -> str | None:
    text = fallback_key_text_for_taxon(taxon_name)
    if not text:
        return None
    return f"__fallback__:{taxon_name or 'unknown'}"


def parse_key_source(source: str) -> list[Candidate]:
    if source.startswith("__fallback__:"):
        taxon_name = source.split(":", 1)[1]
        text = fallback_key_text_for_taxon(taxon_name)
        if not text:
            return []
        candidates = parse_key_text(text)
        for candidate in candidates:
            candidate.score += 1.0
            candidate.evidence.insert(0,
                f"Fallback broad taxonomic key for {taxon_name}: major group-level diagnostic characters.")
        return candidates
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


def render_markdown(observation: dict[str, Any], ranked: list[Candidate],
                    steps: list[TaxonomicStep] | None = None) -> str:
    lines = ["# Explainable identification", "", f"Observation: `{observation.get('id', 'local')}`", ""]
    if observation.get("observed_at"):
        lines.append(f"Observed at: {observation['observed_at']}")
    if observation.get("taxon_lineage"):
        lines.append(f"Cached taxonomy context: {' > '.join(observation['taxon_lineage'])}")
    if observation.get("location"):
        lines.append(f"Location: {observation['location']}")
    lines.append(f"Photos: {len(observation.get('photos', []))}")
    lines.append("")
    if steps:
        lines.extend(["## Taxonomic progression", ""])
        for step in steps:
            line = f"- `{step.current_name}` ({step.current_rank}) -> `{step.target_rank or 'stop'}`"
            if step.selected_name:
                line += f": selected `{step.selected_name}` (score {step.selected_score:.1f})"
            if step.stop_reason:
                line += f"; stopped: {step.stop_reason}"
            elif step.candidates:
                line += f"; candidates: {', '.join(step.candidates[:5])}"
            lines.append(line)
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
    parser.add_argument("--source", choices=["bhl", "local"], default="bhl",
                        help="Default is BHL; use 'local' to search supplied PDF/HTML article files instead.")
    parser.add_argument("--article", "--key", dest="article_sources", action="append",
                        help="Local PDF or HTML file/URL (repeatable). Only used when --source local.")
    parser.add_argument("--image", type=Path, action="append", help="Observation image (repeatable).")
    parser.add_argument("--image-dir", type=Path, help="Directory containing observation images.")
    parser.add_argument("--observations-file", type=Path, default=Path("observations.json"),
                        help="Local iNaturalist observation cache (default: observations.json).")
    parser.add_argument("--taxa-file", type=Path, default=Path("taxa.json"),
                        help="Local taxon cache used to resolve the observation taxon.")
    parser.add_argument("--observation-id", help="Select this ID from the local observations cache.")
    parser.add_argument("--bhl-token", type=Path, help="Path to a BHL API token file (default: bhl_token.txt).")
    parser.add_argument("--no-recursive", action="store_true",
                        help="Run one identification step instead of progressing through narrower ranks.")
    parser.add_argument("--max-levels", type=int, default=6,
                        help="Maximum number of narrower taxonomic steps (default: 6).")
    parser.add_argument("--output", type=Path, default=Path("identification_report.md"))
    parser.add_argument("--json-output", type=Path, help="Also write machine-readable JSON.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.max_levels < 1:
        raise ValueError("--max-levels must be positive")
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

    if args.source == "bhl":
        token = load_bhl_token([args.bhl_token] if args.bhl_token else [Path("bhl_token.txt")])
        if args.no_recursive:
            article_sources, queries = sources_for_step(observation, "bhl", [], token)
            if not article_sources:
                raise ValueError(
                    "BHL search returned no usable sources for the cached taxonomy."
                )
        else:
            article_sources = []
    else:
        token = None
        article_sources = list(args.article_sources or [])
        if not article_sources:
            raise ValueError("--source local requires at least one --article value.")

    if args.no_recursive:
        candidates = [candidate for source in article_sources for candidate in parse_key_source(source)]
        ranked = rank_candidates(candidates, observation)
        steps: list[TaxonomicStep] = []
    else:
        ranked, steps = recursive_identification(
            observation, args.source, article_sources, token, args.max_levels
        )
    report = render_markdown(observation, ranked, steps)
    args.output.write_text(report, encoding="utf-8")
    if args.json_output:
        args.json_output.write_text(json.dumps({"observation": observation,
                                                 "candidates": [candidate.__dict__ for candidate in ranked],
                                                 "taxonomic_steps": [step.__dict__ for step in steps]},
                                                indent=2, ensure_ascii=False), encoding="utf-8")
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())