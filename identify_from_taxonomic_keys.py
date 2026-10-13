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
from collections import Counter
from functools import lru_cache
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
ARTHROPOD_PHYLUM_ID = "47120"
ORDER_CLASS_IDS = {
    "47158": "Insecta",
    "47119": "Arachnida",
    "243773": "Collembola",
}
MISSING_LOCAL_ORDER_NAMES = {"61525": "Dermaptera"}
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
    "Insecta": """\
Scope: broad visual key to insect orders represented in local observations; adult characters are not reliable for every nymph or larva.
Archaeognatha
Traits: wingless, elongate bristletails with a strongly arched thorax, long antennae, three terminal filaments, and jumping ability.
Blattodea
Traits: oval, flattened body; shield-like pronotum behind the head; long antennae; running legs; short segmented cerci.
Coleoptera
Traits: adults have hardened forewing covers (elytra) meeting along the back; larvae usually have three pairs of thoracic legs and no abdominal prolegs.
Dermaptera
Traits: paired forceps-like cerci at the abdominal tip; adults have short forewing covers; nymphs resemble adults but lack wings.
Diptera
Traits: adults have one functional pair of wings; hind wings are reduced to halteres; larvae are legless.
Embioptera
Traits: small, slender insects; enlarged basal segment of the front tarsus is a distinctive silk-gland structure; only adult males are winged.
Ephemeroptera
Traits: adults have delicate wings and two or three long abdominal filaments; aquatic nymphs have abdominal gills and usually three tail filaments.
Hymenoptera
Traits: adults usually have two pairs of membranous wings linked by small hooks; many ants, bees, and wasps have a narrow waist; sawflies have a broad waist.
Lepidoptera
Traits: adults have wings covered in overlapping scales and usually a coiled proboscis; caterpillar larvae may have abdominal prolegs in addition to thoracic legs.
Odonata
Traits: large compound eyes and two pairs of net-veined wings that do not fold over the abdomen; damselfly wings are narrow-based; dragonfly hind wings broaden at the base.
Orthoptera
Traits: enlarged hind femora adapted for jumping; adults often have narrow leathery forewings (tegmina); nymphs may have wing pads.
Psocodea
Traits: barklice have a prominent head, narrow neck, long antennae, and often tent-like wings; booklice and parasitic lice may be wingless.
Thysanoptera
Traits: minute, tapered insects; when present, narrow wings have a dense fringe of long hairs; many species are wingless.
Trichoptera
Traits: adults have hairy wings held tent-like over the body; larvae are aquatic, often case-building, with a pair of hooked terminal prolegs.
Zygentoma
Traits: wingless, flattened, tapered insects with long antennae and three terminal filaments.
""",
    "Arachnida": """\
Scope: broad visual key to arachnid orders represented in local observations; minute mites may require magnification and Acari rank varies among classifications.
Araneae
Traits: eight walking legs; body divided into cephalothorax and abdomen joined by a narrow pedicel; chelicerae bear fangs.
Opiliones
Traits: cephalothorax and abdomen broadly fused without a narrow waist; usually one pair of eyes near the middle of the body.
Pseudoscorpiones
Traits: small arachnids with prominent pincer-like pedipalps, but no long tail or sting.
Solifugae
Traits: large two-part chelicerae and long, leg-like sensory pedipalps held in front; body often appears hairy.
Trombidiformes
Traits: mites are very small arachnids; adult stages usually have four pairs of legs, while larvae have three pairs; order-level diagnosis often needs magnification.
""",
    "Collembola": """\
Scope: experimental broad habitus key to springtail groups represented in local observations; specialist taxonomy may use different ranks.
Poduromorpha
Traits: elongate body with distinct abdominal tergal segmentation; prothoracic tergum distinct.
Entomobryomorpha
Traits: elongate body with distinct abdominal tergal segmentation; prothoracic tergum indistinct.
Symphypleona
Traits: body subglobular; antennae as long as or longer than the head.
""",
    "Chilopoda": """\
Scope: broad centipede characters for distinguishing the class in local observations.
Chilopoda
Traits: elongate, usually flattened trunk with one pair of legs per leg-bearing segment; first trunk appendages are modified into poison claws.
""",
    "Diplopoda": """\
Scope: broad millipede characters for distinguishing the class in local observations; order-level determinations may require a specimen key.
Diplopoda
Traits: cylindrical or flattened trunk; most apparent trunk rings bear two pairs of legs because adjacent segments are fused.
""",
}

FALLBACK_KEY_SOURCE_URLS = {
    "Insecta": "https://genent.cals.ncsu.edu/insect-identification/",
    "Arachnida": "https://www.americanarachnology.org/about-arachnids/arachnid-orders/",
    "Chilopoda": "https://ucmp.berkeley.edu/arthropoda/uniramia/myriapoda.html",
    "Diplopoda": "https://www.fieldmuseum.org/page/milli-peet-key",
    "Archaeognatha": "https://genent.cals.ncsu.edu/insect-identification/class-archeognatha/",
    "Blattodea": "https://genent.cals.ncsu.edu/insect-identification/order-blattodea/",
    "Coleoptera": "https://genent.cals.ncsu.edu/insect-identification/order-coleoptera/",
    "Dermaptera": "https://genent.cals.ncsu.edu/insect-identification/order-dermaptera/",
    "Diptera": "https://genent.cals.ncsu.edu/insect-identification/order-diptera/",
    "Embioptera": "https://genent.cals.ncsu.edu/insect-identification/order-embioptera/",
    "Ephemeroptera": "https://genent.cals.ncsu.edu/insect-identification/order-ephemeroptera/",
    "Hymenoptera": "https://genent.cals.ncsu.edu/insect-identification/order-hymenoptera/",
    "Lepidoptera": "https://genent.cals.ncsu.edu/insect-identification/order-lepidoptera/",
    "Odonata": "https://genent.cals.ncsu.edu/insect-identification/order-odonata/",
    "Orthoptera": "https://genent.cals.ncsu.edu/insect-identification/order-orthoptera/",
    "Psocodea": "https://genent.cals.ncsu.edu/insect-identification/order-psocodea/",
    "Thysanoptera": "https://genent.cals.ncsu.edu/insect-identification/order-thysanoptera/",
    "Trichoptera": "https://genent.cals.ncsu.edu/insect-identification/order-trichoptera/",
    "Zygentoma": "https://genent.cals.ncsu.edu/insect-identification/order-zygentoma/",
    "Araneae": "https://www.americanarachnology.org/about-arachnids/arachnid-orders/araneae/",
    "Opiliones": "https://www.americanarachnology.org/about-arachnids/arachnid-orders/opiliones/",
    "Pseudoscorpiones": "https://www.americanarachnology.org/about-arachnids/arachnid-orders/pseudoscorpiones/",
    "Solifugae": "https://www.americanarachnology.org/about-arachnids/arachnid-orders/solifugae/",
    "Trombidiformes": "https://www.americanarachnology.org/about-arachnids/arachnid-orders/acari/",
    "Entomobryomorpha": "https://www.collembola.org/key/collbola.htm",
    "Poduromorpha": "https://www.collembola.org/key/collbola.htm",
    "Symphypleona": "https://www.collembola.org/key/collbola.htm",
    "Chilopoda": "https://ucmp.berkeley.edu/arthropoda/uniramia/myriapoda.html",
    "Diplopoda": "https://www.fieldmuseum.org/page/milli-peet-key",
}
FALLBACK_KEY_SCOPE_NOTES = {
    "Arthropoda": "Minimal major-class fallback only; it is not an exhaustive or region-specific key.",
    "Insecta": "Broad teaching characters, not an Israel-specific or exhaustive fauna key; some couplets are adult-only.",
    "Arachnida": "The American Arachnological Society pages are order overviews, not a single dichotomous key.",
    "Collembola": "The cited pictorial key is experimental and notes that taxonomic ranks vary among treatments.",
    "Chilopoda": "This source supports class-level centipede characters, not a complete order key.",
    "Diplopoda": "The Field Museum source is a specimen-oriented key to millipede orders; photo-only results remain tentative.",
}
MUTUALLY_EXCLUSIVE_KEY_TRAITS = [
    (("Insecta", "antennae present"), ("Arachnida", "no antennae")),
    (("Insecta", "Three pairs of legs"), ("Arachnida", "Four pairs of legs")),
    (("Chilopoda", "one pair of legs per segment"),
     ("Diplopoda", "two pairs of legs per segment")),
]


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
        observation.setdefault("key_trait_assessments", []).extend(
            step_observation.get("key_trait_assessments", [])
        )
        observation.setdefault("key_trait_conflicts", []).extend(
            step_observation.get("key_trait_conflicts", [])
        )
        observation["key_trait_retries"] = (
            observation.get("key_trait_retries", 0)
            + step_observation.get("key_trait_retries", 0)
        )
        fallback_taxa = {
            match.group(1)
            for candidate in ranked
            for evidence in candidate.evidence
            if (match := re.match(r"Fallback broad taxonomic key for ([^:]+):", evidence))
        }
        known_fallback_taxa = observation.setdefault("fallback_key_taxa", [])
        known_fallback_taxa.extend(name for name in sorted(fallback_taxa) if name not in known_fallback_taxa)
        source_references = {
            evidence.removeprefix("Source reference: ")
            for candidate in ranked
            for evidence in candidate.evidence
            if evidence.startswith("Source reference: ")
        }
        known_source_references = observation.setdefault("key_source_references", [])
        known_source_references.extend(
            url for url in sorted(source_references) if url not in known_source_references
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
            candidate.evidence.insert(0,
                f"Fallback broad taxonomic key for {taxon_name}: major group-level diagnostic characters.")
            source_url = FALLBACK_KEY_SOURCE_URLS.get(candidate.name)
            if source_url:
                candidate.evidence.insert(0, f"Source reference: {source_url}")
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


def local_arthropod_order_candidates(observations: list[dict[str, Any]],
                                     taxa: dict[str, Any]) -> list[dict[str, Any]]:
    descendant_counts: Counter[str] = Counter()
    for observation in observations:
        taxon_id = str((observation.get("taxon") or {}).get("id", ""))
        taxon = taxa.get(taxon_id, {})
        for ancestor_id in {taxon_id, *(str(item) for item in taxon.get("ancestor_ids", []))}:
            descendant_counts[ancestor_id] += 1

    candidates: dict[str, dict[str, Any]] = {}
    for taxon_id, taxon in taxa.items():
        if taxon.get("rank") != "order" or not taxon.get("name"):
            continue
        ancestor_ids = {str(item) for item in taxon.get("ancestor_ids", [])}
        if ARTHROPOD_PHYLUM_ID not in ancestor_ids:
            continue
        order_id = str(taxon_id)
        class_name = next(
            (name for class_id, name in ORDER_CLASS_IDS.items() if class_id in ancestor_ids),
            "Unknown arthropod class",
        )
        candidates[order_id] = {
            "name": taxon["name"],
            "taxon_id": order_id,
            "class": class_name,
            "observation_count": descendant_counts[order_id],
        }

    for taxon in taxa.values():
        name = str(taxon.get("name", ""))
        order_id = str(taxon.get("parent_id", ""))
        if not name.startswith("Forficula ") or order_id not in MISSING_LOCAL_ORDER_NAMES:
            continue
        candidates[order_id] = {
            "name": MISSING_LOCAL_ORDER_NAMES[order_id],
            "taxon_id": order_id,
            "class": "Insecta",
            "observation_count": descendant_counts[order_id],
        }

    return sorted(candidates.values(), key=lambda item: item["name"].casefold())


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
    if not result.get("location"):
        coordinates = observation.get("geojson", {}).get("coordinates", [])
        if len(coordinates) >= 2:
            result["location"] = {
                "longitude": coordinates[0],
                "latitude": coordinates[1],
            }
        elif result.get("latitude") is not None and result.get("longitude") is not None:
            result["location"] = {
                "latitude": result["latitude"],
                "longitude": result["longitude"],
            }
    if observation.get("place_guess"):
        result["place_guess"] = observation["place_guess"]
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
            result["local_arthropod_orders"] = local_arthropod_order_candidates(
                observations, taxa
            )
    return result


def resolve_observation_photo_paths(observation: dict[str, Any], report_path: Path) -> list[Path]:
    paths: list[Path] = []
    for photo in observation.get("photos", []):
        if not isinstance(photo, dict):
            continue
        for key in ("path", "local_path", "file_path"):
            value = photo.get(key)
            if value and Path(value).is_file():
                paths.append(Path(value))
                break
    if not observation.get("id"):
        return paths

    manifest_path = report_path.parent / "observation_photo_paths.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        manifest = {}
    manifest_paths = manifest.get(str(observation["id"]), [])
    if isinstance(manifest_paths, list):
        for value in manifest_paths:
            path = Path(value)
            if path.is_file() and path not in paths:
                paths.append(path)
    if paths:
        return paths

    try:
        from identify_with_local_clip import load_report_images
        paths.extend(load_report_images(report_path, [str(observation["id"])])
                     .get(str(observation["id"]), []))
    except (FileNotFoundError, ValueError):
        pass
    return paths


@lru_cache(maxsize=1)
def load_key_trait_vqa_model() -> tuple[Any, Any, Any, Any]:
    import torch
    from transformers import BlipForQuestionAnswering, BlipProcessor

    model_name = "Salesforce/blip-vqa-base"
    processor = BlipProcessor.from_pretrained(model_name)
    model = BlipForQuestionAnswering.from_pretrained(model_name).to("cpu")
    model.eval()
    return torch, processor, model, torch.device("cpu")


def key_trait_phrases(candidates: Iterable[Candidate]) -> list[tuple[Candidate, str]]:
    phrases: list[tuple[Candidate, str]] = []
    nonvisual_terms = ("aquatic", "marine", "predator", "detritivore", "fast-running")
    for candidate in candidates:
        for evidence in candidate.evidence:
            if evidence.startswith(("Fallback broad taxonomic key", "Source reference:")):
                continue
            for raw_phrase in re.split(r";|[.!?]\s*|,\s*(?=(?:often|usually|frequently)\b)", evidence):
                phrase = normalise(raw_phrase).strip(" ,:")
                if phrase.casefold().startswith("traits:"):
                    phrase = phrase.partition(":")[2].strip()
                if not phrase or len(phrase) > 160:
                    continue
                if any(term in phrase.lower() for term in nonvisual_terms):
                    candidate.missing.append(f"Not directly verifiable from the image: {phrase}")
                    continue
                phrases.append((candidate, phrase))
    return phrases


def parse_crop_box(value: str) -> tuple[str, tuple[float, float, float, float]]:
    filename, separator, coordinates = value.partition("=")
    if not separator or not filename.strip():
        raise argparse.ArgumentTypeError("Use FILENAME=x,y,width,height")
    try:
        x, y, width, height = (float(part) for part in coordinates.split(","))
    except ValueError as error:
        raise argparse.ArgumentTypeError("Crop coordinates must be four numbers") from error
    if (min(x, y, width, height) < 0 or width <= 0 or height <= 0
            or x + width > 1 or y + height > 1):
        raise argparse.ArgumentTypeError("Crop box must fit in the image using normalized 0-1 values")
    return filename.strip(), (x, y, width, height)


def parse_confirmed_trait(value: str) -> tuple[str, str]:
    candidate, separator, trait = value.partition("=")
    if not separator or not candidate.strip() or not trait.strip():
        raise argparse.ArgumentTypeError("Use CANDIDATE=KEY PHRASE")
    return candidate.strip(), normalise(trait)


def answer_polarity(answer: str) -> str | None:
    normalized = normalise(answer).casefold().strip(" .,!?")
    if normalized in {"yes", "yeah", "yep", "present", "visible", "true"}:
        return "yes"
    if normalized in {"no", "nope", "absent", "not visible", "false"}:
        return "no"
    return None


def detect_key_trait_conflicts(assessments: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], set[tuple[str, str]]]:
    by_trait = {
        (item["candidate"].casefold(), item["trait"].casefold()): item
        for item in assessments
    }
    conflicts: list[dict[str, Any]] = []
    retry_keys: set[tuple[str, str]] = set()

    for key, assessment in by_trait.items():
        answers_by_photo: dict[str, list[str]] = {}
        for answer in assessment.get("model_answers", []):
            polarity = answer_polarity(str(answer.get("answer", "")))
            if polarity:
                answers_by_photo.setdefault(answer["photo"], []).append(polarity)
        for photo, polarities in answers_by_photo.items():
            if len(set(polarities)) > 1:
                retry_keys.add(key)
                conflicts.append({
                    "kind": "same-photo-answer-disagreement",
                    "traits": [{"candidate": assessment["candidate"], "trait": assessment["trait"]}],
                    "photo": photo,
                    "resolved": False,
                })
                break
        all_polarities = {value for values in answers_by_photo.values() for value in values}
        if len(answers_by_photo) > 1 and all_polarities == {"yes", "no"}:
            retry_keys.add(key)

    for left, right in MUTUALLY_EXCLUSIVE_KEY_TRAITS:
        left_key = (left[0].casefold(), left[1].casefold())
        right_key = (right[0].casefold(), right[1].casefold())
        left_item = by_trait.get(left_key)
        right_item = by_trait.get(right_key)
        if not left_item or not right_item:
            continue
        left_yes = {entry["photo"] for entry in left_item.get("model_answers", [])
                    if answer_polarity(str(entry.get("answer", ""))) == "yes"}
        right_yes = {entry["photo"] for entry in right_item.get("model_answers", [])
                     if answer_polarity(str(entry.get("answer", ""))) == "yes"}
        overlap = sorted(left_yes & right_yes)
        if overlap:
            retry_keys.update((left_key, right_key))
            conflicts.append({
                "kind": "mutually-exclusive-traits-both-yes",
                "traits": [
                    {"candidate": left_item["candidate"], "trait": left_item["trait"]},
                    {"candidate": right_item["candidate"], "trait": right_item["trait"]},
                ],
                "photos": overlap,
                "resolved": False,
            })

    return conflicts, retry_keys


def unresolved_key_trait_conflicts(assessments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_trait = {
        (item["candidate"].casefold(), item["trait"].casefold()): item
        for item in assessments
    }
    conflicts: list[dict[str, Any]] = []

    for item in assessments:
        answer_sets: dict[str, set[str]] = {}
        source_answers = item.get("retry_answers") or item.get("model_answers", [])
        for answer in source_answers:
            polarity = answer_polarity(str(answer.get("answer", "")))
            if polarity:
                answer_sets.setdefault(answer["photo"], set()).add(polarity)
        if any(len(values) > 1 for values in answer_sets.values()):
            conflicts.append({
                "kind": "same-photo-answer-disagreement",
                "traits": [{"candidate": item["candidate"], "trait": item["trait"]}],
                "photos": sorted(photo for photo, values in answer_sets.items() if len(values) > 1),
                "resolved": False,
            })
        elif len(answer_sets) > 1 and {answer for values in answer_sets.values() for answer in values} == {"yes", "no"}:
            conflicts.append({
                "kind": "cross-photo-answer-disagreement",
                "traits": [{"candidate": item["candidate"], "trait": item["trait"]}],
                "photos": sorted(answer_sets),
                "resolved": False,
            })

    for left, right in MUTUALLY_EXCLUSIVE_KEY_TRAITS:
        left_item = by_trait.get((left[0].casefold(), left[1].casefold()))
        right_item = by_trait.get((right[0].casefold(), right[1].casefold()))
        if not left_item or not right_item:
            continue

        def yes_photos(item: dict[str, Any]) -> set[str]:
            source_answers = item.get("retry_answers") or item.get("model_answers", [])
            return {entry["photo"] for entry in source_answers
                    if answer_polarity(str(entry.get("answer", ""))) == "yes"}

        overlap = sorted(yes_photos(left_item) & yes_photos(right_item))
        if overlap:
            conflicts.append({
                "kind": "mutually-exclusive-traits-both-yes",
                "traits": [
                    {"candidate": left_item["candidate"], "trait": left_item["trait"]},
                    {"candidate": right_item["candidate"], "trait": right_item["trait"]},
                ],
                "photos": overlap,
                "resolved": False,
            })
    return conflicts


def assess_key_traits(candidates: list[Candidate], observation: dict[str, Any]) -> list[dict[str, Any]]:
    phrases = key_trait_phrases(candidates)
    image_paths = [Path(value) for value in observation.get("photo_paths", []) if value]
    if Image is None or not image_paths or not phrases:
        return []

    images_by_path: dict[Path, Any] = {}
    for path in dict.fromkeys(image_paths):
        if not path.is_file():
            continue
        try:
            with Image.open(path) as source_image:
                crop_box = observation.get("crop_boxes", {}).get(path.name)
                if crop_box:
                    x, y, width, height = crop_box
                    left = round(x * source_image.width)
                    top = round(y * source_image.height)
                    right = round((x + width) * source_image.width)
                    bottom = round((y + height) * source_image.height)
                    images_by_path[path] = source_image.crop((left, top, right, bottom)).convert("RGB")
                else:
                    images_by_path[path] = source_image.convert("RGB")
        except Exception:
            continue
    if not images_by_path:
        return []

    try:
        torch, processor, model, device = load_key_trait_vqa_model()
    except ImportError:
        for image in images_by_path.values():
            image.close()
        observation["photo_trait_note"] = "Photo questions require torch, transformers, and Pillow."
        return []
    except Exception as error:
        for image in images_by_path.values():
            image.close()
        observation["photo_trait_note"] = f"Could not load the local visual question model: {error}"
        return []

    requests = [
        (path, candidate, phrase,
         f"Is this key character visible in the photo: {phrase}? Answer yes, no, or unclear.")
        for path in images_by_path
        for candidate, phrase in phrases
    ]

    def ask(batch_requests: list[tuple[Path, Candidate, str, str]]) -> list[tuple[Path, Candidate, str, str]]:
        results: list[tuple[Path, Candidate, str, str]] = []
        for start in range(0, len(batch_requests), 4):
            batch = batch_requests[start:start + 4]
            batch_images = [images_by_path[path] for path, _, _, _ in batch]
            questions = [question for _, _, _, question in batch]
            inputs = processor(images=batch_images, text=questions, return_tensors="pt", padding=True)
            inputs = inputs.to(device)
            with torch.no_grad():
                generated = model.generate(**inputs, max_new_tokens=8)
            decoded = processor.batch_decode(generated, skip_special_tokens=True)
            results.extend(
                (path, candidate, phrase, answer.strip())
                for (path, candidate, phrase, _), answer in zip(batch, decoded)
            )
        return results

    try:
        answers = ask(requests)
        assessments_by_trait: dict[tuple[str, str], dict[str, Any]] = {}
        for path, candidate, phrase, answer in answers:
            key = (candidate.name, phrase)
            assessment = assessments_by_trait.setdefault(key, {
                "candidate": candidate.name,
                "trait": phrase,
                "model_answers": [],
                "retry_answers": [],
                "verification": "unverified; inspect the photo directly",
            })
            assessment["model_answers"].append({"photo": path.name, "answer": answer})

        initial_assessments = list(assessments_by_trait.values())
        initial_conflicts, retry_keys = detect_key_trait_conflicts(initial_assessments)
        retry_requests: list[tuple[Path, Candidate, str, str]] = []
        for path, candidate, phrase, _ in requests:
            key = (candidate.name.casefold(), phrase.casefold())
            if key not in retry_keys:
                continue
            retry_requests.extend([
                (path, candidate, phrase,
                 f"Look closely at this animal. Can you clearly see {phrase}? Answer only yes, no, or unclear."),
                (path, candidate, phrase,
                 f"Is {phrase} visible on this animal? If the photo does not show it clearly, answer unclear."),
            ])
        retry_answers = ask(retry_requests) if retry_requests else []
        attempt_counts: Counter[tuple[str, str, str]] = Counter()
        for path, candidate, phrase, answer in retry_answers:
            assessment = assessments_by_trait[(candidate.name, phrase)]
            attempt_key = (candidate.name, phrase, path.name)
            attempt_counts[attempt_key] += 1
            assessment["retry_answers"].append({
                "photo": path.name,
                "attempt": attempt_counts[attempt_key],
                "answer": answer,
            })

        assessments = list(assessments_by_trait.values())
        unresolved = unresolved_key_trait_conflicts(assessments)
        observation["key_trait_retries"] = len(retry_answers)
        observation["key_trait_conflicts"] = unresolved
    except Exception as error:
        observation["photo_trait_note"] = f"Could not answer key-trait questions about the images: {error}"
        return []
    finally:
        for image in images_by_path.values():
            image.close()

    for candidate, phrase in phrases:
        assessment = assessments_by_trait.get((candidate.name, phrase))
        if assessment is None:
            continue
        candidate.missing.append(f"Confirm visually from the key: {phrase}")
    return assessments


def rank_candidates(candidates: Iterable[Candidate], observation: dict[str, Any]) -> list[Candidate]:
    candidates = list(candidates)
    observation["key_trait_assessments"] = assess_key_traits(candidates, observation)
    for candidate_name, trait in observation.get("confirmed_key_traits", []):
        candidate = next((item for item in candidates
                          if item.name.casefold() == candidate_name.casefold()), None)
        if candidate and trait.casefold() in " ".join(candidate.evidence).casefold():
            candidate.score += 1.0
            candidate.evidence.append(f"Reviewer-confirmed key character: {trait}.")
    for candidate in candidates:
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
    lines.append(f"Photos: {len(observation.get('photo_paths') or observation.get('photos', []))}")
    if observation.get("crop_boxes"):
        lines.append("Cropped photos: " + ", ".join(sorted(observation["crop_boxes"])))
    if observation.get("local_arthropod_orders"):
        lines.extend(["", "## Arthropod orders represented in local observations", ""])
        lines.extend(
            f"- {item['name']} ({item['class']}): {item['observation_count']} descendant observations"
            for item in observation["local_arthropod_orders"]
        )
    if observation.get("key_trait_assessments"):
        lines.extend(["", "## Key-trait prompt comparison", "",
                      "Model answers are unverified; alternate phrasings are not independent models or calibrated confidence.", ""])
        lines.append(f"Alternate-prompt retry answers: {observation.get('key_trait_retries', 0)}")
        for assessment in observation["key_trait_assessments"]:
            answers = "; ".join(
                f"{item['photo']}: {item['answer']}"
                for item in assessment.get("model_answers", [])
            )
            retries = "; ".join(
                f"retry {item['attempt']} {item['photo']}: {item['answer']}"
                for item in assessment.get("retry_answers", [])
            )
            line = f"- `{assessment['candidate']}`: {assessment['trait']}. Initial: {answers or 'none'}"
            if retries:
                line += f". Retry: {retries}"
            lines.append(line)
    if observation.get("key_trait_conflicts"):
        lines.extend(["", "## Trait conflicts", ""])
        for conflict in observation["key_trait_conflicts"]:
            traits = " versus ".join(
                f"{item['candidate']}: {item['trait']}"
                for item in conflict["traits"]
            )
            photos = ", ".join(conflict.get("photos", [conflict.get("photo", "unknown photo")]))
            lines.append(f"- {conflict['kind']}: {traits} ({photos}); unresolved and not used to select a taxon.")
    if observation.get("key_trait_conflicts"):
        lines.extend(["", "## Unresolved key-trait conflicts", "",
                      "Alternate prompts did not resolve these contradictions; the conflicting evidence remains uncertain."])
        for conflict in observation["key_trait_conflicts"]:
            traits = " versus ".join(
                f"{item['candidate']}: {item['trait']}"
                for item in conflict["traits"]
            )
            photos = ", ".join(conflict.get("photos", [conflict.get("photo", "unknown photo")]))
            lines.append(f"- {conflict['kind']}: {traits} ({photos}).")
    source_urls = sorted(set(observation.get("key_source_references", [])) | {
        evidence.removeprefix("Source reference: ")
        for candidate in ranked
        for evidence in candidate.evidence
        if evidence.startswith("Source reference: ")
    })
    if source_urls:
        lines.extend(["", "## Key sources", ""])
        lines.extend(f"- {url}" for url in source_urls)
    fallback_taxa = sorted(set(observation.get("fallback_key_taxa", [])) | {
        match.group(1)
        for candidate in ranked
        for evidence in candidate.evidence
        if (match := re.match(r"Fallback broad taxonomic key for ([^:]+):", evidence))
    })
    for taxon_name in fallback_taxa:
        scope_note = FALLBACK_KEY_SCOPE_NOTES.get(taxon_name)
        if scope_note:
            lines.append(f"Key scope ({taxon_name}): {scope_note}")
    if observation.get("photo_trait_note"):
        lines.append(f"Image pipeline note: {observation['photo_trait_note']}")
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
        lines.extend(["## No key decision", "",
                      "The available automation did not confirm a key character strongly enough to select a narrower taxon.", ""])
        lines.append("### Missing or useful next evidence")
        lines.extend(f"- {item}" for item in best.missing or ["observable diagnostic characters"])
        lines.append("\n### Candidates considered")
        lines.extend(f"- {item.name}" for item in ranked)
        return "\n".join(lines) + "\n"
    lines.extend([f"## Suggested identification: {best.name}",
                  f"Score: {best.score:.1f} (heuristic, not a confirmed identification)", ""])
    lines.append("### Why")
    lines.extend(f"- {item}" for item in best.evidence
                 if not item.startswith("Source reference: "))
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
    parser.add_argument("--crop-box", type=parse_crop_box, action="append",
                        help="Crop a photo using normalized coordinates: FILENAME=x,y,width,height (repeatable).")
    parser.add_argument("--confirm-trait", type=parse_confirmed_trait, action="append",
                        help="Reviewer-confirmed key character: CANDIDATE=KEY PHRASE (repeatable).")
    parser.add_argument("--observations-file", type=Path, default=Path("observations.json"),
                        help="Local iNaturalist observation cache (default: observations.json).")
    parser.add_argument("--taxa-file", type=Path, default=Path("taxa.json"),
                        help="Local taxon cache used to resolve the observation taxon.")
    parser.add_argument("--report-html", type=Path, default=Path("inat_comparison/report.html"),
                        help="Comparison report used to map observation IDs to local photo paths.")
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
    if args.observation_id:
        image_paths.extend(resolve_observation_photo_paths(observation, args.report_html))
    if args.image_dir:
        image_paths.extend(path for path in args.image_dir.rglob("*")
                           if path.suffix.lower() in IMAGE_EXTENSIONS)
    image_paths = list(dict.fromkeys(image_paths))
    observation["photo_paths"] = [str(path) for path in image_paths]
    observation["crop_boxes"] = dict(args.crop_box or [])
    observation["confirmed_key_traits"] = args.confirm_trait or []
    image_metadata = [image_observation(path) for path in image_paths]
    observation.setdefault("photos", []).extend(image_metadata)
    if not observation.get("observed_at"):
        observation["observed_at"] = next(
            (item["observed_at"] for item in image_metadata if item.get("observed_at")),
            None,
        )
    if not observation.get("location"):
        observation["location"] = next(
            (item["location"] for item in image_metadata if item.get("location")),
            None,
        )

    if not image_paths and observation.get("photos"):
        observation["photo_trait_note"] = (
            "The observation contains photo records, but no local image path was resolved. "
            "Use --image or update the local comparison report before identification."
        )

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