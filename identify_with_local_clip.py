#!/usr/bin/env python3
"""Identify observation photos using a local CLIP model and a PDF field guide.

Results are visual/textual retrieval suggestions, not taxonomic determinations.
The first run downloads the configured Hugging Face model and builds a guide
image index. Later runs reuse the cache unless the PDF or model changes.
"""

import argparse
import base64
import hashlib
import html
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"}
DEFAULT_MODEL = "openai/clip-vit-large-patch14"
CACHE_VERSION = 2
DEFAULT_WORK_DIR = Path("clip_work")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_guide_name(pdf_path: Path) -> str:
    name = re.sub(r"[^\w.-]+", "_", pdf_path.stem, flags=re.UNICODE).strip("._")
    return name or "guide"


def clean_html(text: str) -> str:
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", text,
                  flags=re.IGNORECASE | re.DOTALL)
    return re.sub(r"<[^>]+>", " ", html.unescape(text))


def guide_page_texts(pdf_path: Path) -> list[str]:
    try:
        from pypdf import PdfReader
    except ImportError as error:
        raise RuntimeError("Install PDF support with: python3 -m pip install pypdf PyMuPDF") from error
    return [(page.extract_text() or "").strip() for page in PdfReader(str(pdf_path)).pages]


def scientific_labels(text: str) -> list[str]:
    labels = re.findall(r"\b[A-Z][a-z]{2,}(?:\s+(?:\([^)]+\)\s*)?[A-Z][a-z-]{2,})?", text)
    rejected = {"Photo", "Israel", "March", "Copyright", "Wikipedia"}
    return list(dict.fromkeys(label for label in labels if label.split()[0] not in rejected))


def extract_guide_assets(pdf_path: Path, asset_dir: Path) -> list[dict[str, Any]]:
    try:
        import fitz
    except ImportError as error:
        raise RuntimeError("Install PDF support with: python3 -m pip install pypdf PyMuPDF") from error
    asset_dir.mkdir(parents=True, exist_ok=True)
    page_text = guide_page_texts(pdf_path)
    document = fitz.open(str(pdf_path))
    assets: list[dict[str, Any]] = []
    seen: dict[str, str] = {}
    for page_index in range(document.page_count):
        page = document.load_page(page_index)
        labels = scientific_labels(page_text[page_index])
        for image_index, image_info in enumerate(page.get_images(full=True)):
            xref = image_info[0]
            try:
                image_data = document.extract_image(xref)
                raw = image_data["image"]
                image_hash = sha256_bytes(raw)
                if image_hash in seen:
                    assets.append({"path": seen[image_hash], "page": page_index + 1,
                                   "image_index": image_index, "duplicate": True,
                                   "labels": labels, "text": page_text[page_index]})
                    continue
                suffix = image_data.get("ext", "jpg")
                target = asset_dir / f"page_{page_index + 1:02d}_image_{image_index:02d}_{image_hash[:12]}.{suffix}"
                target.write_bytes(raw)
                seen[image_hash] = str(target)
                assets.append({"path": str(target), "page": page_index + 1,
                               "image_index": image_index, "duplicate": False,
                               "labels": labels, "text": page_text[page_index],
                               "sha256": image_hash})
            except Exception as error:
                assets.append({"page": page_index + 1, "image_index": image_index,
                               "error": str(error), "labels": labels,
                               "text": page_text[page_index]})
    document.close()
    return assets


def load_local_observation(observations_file: Path, observation_id: str) -> dict[str, Any]:
    observations = json.loads(observations_file.read_text(encoding="utf-8"))
    observation = next((item for item in observations
                        if str(item.get("id")) == str(observation_id)), None)
    if observation is None:
        raise ValueError(f"Observation {observation_id} was not found in {observations_file}")
    return {"id": observation.get("id"),
            "observed_at": observation.get("time_observed_at"),
            "photos": observation.get("photos", []),
            "taxon": observation.get("taxon", {})}


class ReportParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows: list[dict[str, Any]] = []
        self.row: dict[str, Any] | None = None
        self.cell_text: list[str] = []
        self.in_cell = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "tr":
            self.row = {"text": [], "data_path": attributes.get("data-path")}
        elif self.row is not None and tag == "td":
            self.in_cell = True
            self.cell_text = []

    def handle_data(self, data: str) -> None:
        if self.row is not None and self.in_cell:
            self.cell_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self.row is not None and tag == "td":
            self.row["text"].append(" ".join(self.cell_text))
            self.in_cell = False
        elif tag == "tr" and self.row is not None:
            self.row["all_text"] = " ".join(self.row.pop("text"))
            self.rows.append(self.row)
            self.row = None


def load_report_images(report_path: Path, observation_ids: list[str]) -> dict[str, list[Path]]:
    if not report_path.exists():
        raise FileNotFoundError(f"Report HTML was not found: {report_path}")
    parser = ReportParser()
    parser.feed(report_path.read_text(encoding="utf-8", errors="replace"))
    requested = {str(observation_id) for observation_id in observation_ids}
    found: dict[str, list[Path]] = {}
    for row in parser.rows:
        match = re.search(r"Obs ID:\s*(\d+)", row.get("all_text", ""))
        if not match or match.group(1) not in requested:
            continue
        paths: list[Path] = []
        if row.get("data_path"):
            original = Path(row["data_path"])
            if original.exists():
                paths.append(original)
        if paths:
            found[match.group(1)] = paths
    missing = requested - set(found)
    if missing:
        raise ValueError(f"No local report images found for observation ID(s): {', '.join(sorted(missing))}")
    return found


def load_clip(model_name: str, device_name: str):
    try:
        import torch
        from transformers import CLIPModel, CLIPProcessor
    except ImportError as error:
        raise RuntimeError(
            "Install the local CLIP dependencies with: "
            "python3 -m pip install torch transformers Pillow pypdf PyMuPDF"
        ) from error
    if device_name == "auto":
        device_name = "mps" if torch.backends.mps.is_available() else "cpu"
    device = torch.device(device_name)
    processor = CLIPProcessor.from_pretrained(model_name)
    model = CLIPModel.from_pretrained(model_name).to(device)
    model.eval()
    return torch, processor, model, device


def feature_tensor(values: Any) -> Any:
    """Support both tensor and BaseModelOutput CLIP return formats."""
    if hasattr(values, "pooler_output"):
        return values.pooler_output
    if hasattr(values, "image_embeds"):
        return values.image_embeds
    if hasattr(values, "text_embeds"):
        return values.text_embeds
    return values


def encode_images(paths: list[Path], torch, processor, model, device, batch_size: int) -> Any:
    from PIL import Image
    embeddings = []
    for start in range(0, len(paths), batch_size):
        images = [Image.open(path).convert("RGB") for path in paths[start:start + batch_size]]
        with torch.no_grad():
            inputs = processor(images=images, return_tensors="pt", padding=True).to(device)
            values = feature_tensor(model.get_image_features(**inputs))
            values = values / values.norm(dim=-1, keepdim=True)
            embeddings.append(values.cpu())
        for image in images:
            image.close()
    return torch.cat(embeddings) if embeddings else torch.empty((0, model.config.projection_dim))


def encode_texts(texts: list[str], torch, processor, model, device, batch_size: int) -> Any:
    embeddings = []
    for start in range(0, len(texts), batch_size):
        with torch.no_grad():
            inputs = processor(text=texts[start:start + batch_size], return_tensors="pt",
                               padding=True, truncation=True).to(device)
            values = feature_tensor(model.get_text_features(**inputs))
            values = values / values.norm(dim=-1, keepdim=True)
            embeddings.append(values.cpu())
    return torch.cat(embeddings) if embeddings else torch.empty((0, model.config.projection_dim))


def build_or_load_index(pdf_path: Path, cache_dir: Path, guide_photos_dir: Path,
                        model_name: str,
                        torch, processor, model, device, batch_size: int):
    guide_name = safe_guide_name(pdf_path)
    guide_cache_dir = cache_dir / guide_name
    guide_cache_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = guide_cache_dir / "manifest.json"
    embedding_path = guide_cache_dir / "guide_image_embeddings.pt"
    source_hash = sha256_file(pdf_path)
    if manifest_path.exists() and embedding_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (manifest.get("version") == CACHE_VERSION and
                manifest.get("source_sha256") == source_hash and
                manifest.get("model") == model_name):
            return manifest["assets"], torch.load(embedding_path, map_location="cpu", weights_only=True)
    assets = extract_guide_assets(pdf_path, guide_photos_dir / safe_guide_name(pdf_path))
    usable = [asset for asset in assets if asset.get("path") and not asset.get("duplicate")]
    paths = [Path(asset["path"]) for asset in usable]
    embeddings = encode_images(paths, torch, processor, model, device, batch_size)
    manifest = {"version": CACHE_VERSION, "source": str(pdf_path),
                "source_sha256": source_hash, "model": model_name,
                "assets": usable, "embedding_shape": list(embeddings.shape)}
    temp_manifest = manifest_path.with_suffix(".tmp")
    temp_embeddings = embedding_path.with_suffix(".tmp")
    temp_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    torch.save(embeddings, temp_embeddings)
    temp_manifest.replace(manifest_path)
    temp_embeddings.replace(embedding_path)
    return usable, embeddings


def image_data_uri(path: Path, max_size: int = 500) -> str:
    from PIL import Image
    from io import BytesIO
    image = Image.open(path).convert("RGB")
    image.thumbnail((max_size, max_size))
    stream = BytesIO()
    image.save(stream, format="JPEG", quality=82)
    image.close()
    return "data:image/jpeg;base64," + base64.b64encode(stream.getvalue()).decode("ascii")


def esc(value: Any) -> str:
    return html.escape(str(value))


def render_report(output: Path, model_name: str, observations: list[dict[str, Any]],
                  results: list[dict[str, Any]]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    sections = []
    for result in results:
        observation = result["observation"]
        cards = []
        for match in result["matches"]:
            asset = match["asset"]
            image = image_data_uri(Path(asset["path"]))
            labels = ", ".join(asset.get("labels", [])) or "No scientific label extracted"
            cards.append(f"""<article class=match><img src='{image}'><div><h3>Page {asset['page']} · {esc(labels)}</h3>
<p><b>Visual similarity:</b> {match['image_score']:.3f}<br><b>Guide text similarity:</b> {match['text_score']:.3f}<br><b>Combined retrieval score:</b> {match['score']:.3f}</p>
<p>{esc(' '.join(asset.get('text', '').split())[:700])}</p></div></article>""")
        query_images = "".join(f"<img class=query src='{image_data_uri(path, 360)}'>" for path in result["image_paths"])
        sections.append(f"""<section><h2>Observation {esc(observation.get('id'))}</h2>
<p><b>Observed:</b> {esc(observation.get('observed_at', 'unknown'))} · <b>Place:</b> {esc(observation.get('place_guess', 'unknown'))}</p>
<p><b>Existing iNaturalist taxon:</b> {esc((observation.get('taxon') or {}).get('name', 'none'))}</p>
<div class=queries>{query_images}</div><h3>Closest guide examples</h3>{''.join(cards)}
<p class=notice>These are local CLIP retrieval scores, not species confidence. A close image match can reflect pose, background, color, or page layout.</p></section>""")
    output.write_text(f"""<!doctype html><html><head><meta charset=utf-8><title>Local CLIP field-guide report</title>
<style>body{{font:16px system-ui,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem;color:#20252b;background:#f5f3ee}}h1{{margin-bottom:.2rem}}section{{background:white;padding:1.2rem;margin:1.5rem 0;border:1px solid #d9d5cc;border-radius:8px}}.queries{{display:flex;gap:1rem;flex-wrap:wrap;margin:1rem 0}}.query{{max-width:360px;max-height:280px;object-fit:contain}}.match{{display:flex;gap:1rem;border-top:1px solid #ddd;padding:1rem 0}}.match img{{width:220px;height:170px;object-fit:contain;background:#eee}}.match h3{{margin-top:0}}.notice{{background:#fff4cf;padding:.8rem;border-left:4px solid #d29325}}</style></head>
<body><h1>Local CLIP field-guide identification</h1><p>Model: <code>{esc(model_name)}</code>. Guide images and text were indexed locally.</p>{''.join(sections)}</body></html>""", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--guide-pdf", type=Path, required=True)
    parser.add_argument("--image", type=Path, action="append", default=[])
    parser.add_argument("--image-dir", type=Path)
    parser.add_argument("--observation-id", action="append", default=[])
    parser.add_argument("--observations-file", type=Path, default=Path("observations.json"))
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--device", choices=["auto", "cpu", "mps"], default="auto")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_WORK_DIR / "cache")
    parser.add_argument("--guide-photos-dir", type=Path,
                        default=DEFAULT_WORK_DIR / "guide_photos",
                        help="Shared directory containing one extracted-photo folder per guide.")
    parser.add_argument("--report-html", type=Path, default=Path("inat_comparison/report.html"),
                        help="Local comparison report containing observation images.")
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--output", type=Path, default=Path("clip_identification_report.html"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.top_k < 1 or args.batch_size < 1:
        raise ValueError("--top-k and --batch-size must be positive")
    observations: list[dict[str, Any]] = []
    image_groups: list[tuple[dict[str, Any], list[Path]]] = []
    image_paths = list(args.image)
    for observation_id in args.observation_id:
        report_images = load_report_images(args.report_html, [observation_id])[str(observation_id)]
        metadata = load_local_observation(args.observations_file, observation_id)
        observations.append(metadata)
        image_groups.append((metadata, report_images))
        image_paths.extend(report_images)
    if args.image_dir:
        directory_images = [path for path in args.image_dir.rglob("*")
                            if path.suffix.lower() in IMAGE_EXTENSIONS]
        image_paths.extend(directory_images)
        image_groups.append(({"id": "local"}, directory_images))
    if args.image:
        image_groups.append(({"id": "local"}, list(args.image)))
    if not image_paths:
        raise ValueError("Provide --image, --image-dir, or --observation-id")
    torch, processor, model, device = load_clip(args.model, args.device)
    assets, guide_embeddings = build_or_load_index(args.guide_pdf, args.cache_dir,
                                                    args.guide_photos_dir, args.model,
                                                    torch, processor, model, device, args.batch_size)
    query_embeddings = encode_images(image_paths, torch, processor, model, device, args.batch_size)
    labels = []
    for asset in assets:
        name = ", ".join(asset.get("labels", [])) or "wild bee"
        labels.append(f"a field-guide photograph of {name}. {asset.get('text', '')[:300]}")
    text_embeddings = encode_texts(labels, torch, processor, model, device, args.batch_size)
    results = []
    image_offset = 0
    for observation, group_paths in image_groups:
        group_results = []
        for query_embedding in query_embeddings[image_offset:image_offset + len(group_paths)]:
            image_scores = guide_embeddings @ query_embedding
            text_scores = text_embeddings @ query_embedding
            combined = image_scores * 0.75 + text_scores * 0.25
            selected = torch.topk(combined, min(args.top_k, len(assets))).indices.tolist()
            matches = [{"asset": assets[item], "image_score": float(image_scores[item]),
                        "text_score": float(text_scores[item]), "score": float(combined[item])}
                       for item in selected]
            group_results.append({"observation": observation, "image_paths": [group_paths[len(group_results)]],
                                  "matches": matches})
        results.extend(group_results)
        image_offset += len(group_paths)
    render_report(args.output, args.model, observations, results)
    print(f"Wrote {args.output}")
    print(f"Device: {device}; guide assets indexed: {len(assets)}; query images: {len(image_paths)}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
