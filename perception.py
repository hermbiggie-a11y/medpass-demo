"""MEDPASS perception - models propose, classical CV disposes, rules decide.

The load-bearing safety property: the VLM is NEVER the sole reader of a value.
  * Wristband: qwen3-vl locates the band and reads it; Tesseract independently
    reads the full-res crop. The two extracted MRNs must agree or the contract
    carries a read conflict and the rules HOLD - we never guess an identity.
  * Pills: the VLM proposes color/shape; OpenCV independently measures the crop's
    median color. Disagreement caps confidence at 0.5, which the rules escalate.

All model calls: temperature 0, top_k 1, seed 0. Emits the contract JSON only -
verdicts are the rules engine's job.
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
from pathlib import Path

import cv2
import numpy as np
import requests as http

OLLAMA = os.environ.get("OLLAMA_URL", "http://localhost:11434")
VLM = "qwen3-vl:30b-a3b-instruct-q4_K_M"

# Tesseract: an unpacked copy under MEDPASS_TESS_ROOT is used if present;
# otherwise the system tesseract on PATH (for example from apt) is used.
TESS_ROOT = Path(os.environ.get("MEDPASS_TESS_ROOT", str(Path.home() / ".local/opt/tess")))
_LOCAL_TESS = (TESS_ROOT / "usr/bin/tesseract").exists()
if _LOCAL_TESS:
    os.environ.setdefault("TESSDATA_PREFIX", str(TESS_ROOT / "usr/share/tesseract-ocr/5/tessdata"))
    os.environ["LD_LIBRARY_PATH"] = f"{TESS_ROOT}/usr/lib/aarch64-linux-gnu:" + os.environ.get("LD_LIBRARY_PATH", "")
import pytesseract  # noqa: E402  (env must be set first)

if _LOCAL_TESS:
    pytesseract.pytesseract.tesseract_cmd = str(TESS_ROOT / "usr/bin/tesseract")

MRN_RE = re.compile(r"MRN[\s\-:#]*([0-9]{6})", re.IGNORECASE)

# The candies are chosen to be maximally separable in HSV:
# white = achromatic (low saturation, any brightness); everything else by hue band.
SAT_WHITE_MAX = 70        # below this saturation the object is "white" regardless of V
HUE_BANDS = {             # OpenCV H in 0..180 - calibrated on the ACTUAL demo props
    "red": [(0, 6), (165, 180)],
    "orange": [(7, 24)],   # the amber bottle body / white pills seen through it
    "green": [(25, 95)],   # the chartreuse capsules measured at hue 25-45 live
    "blue": [(100, 140)],
}


def _b64(img_bgr, max_side=896) -> tuple[str, int, int]:
    """Returns (b64_jpeg, sent_width, sent_height) - the model sees THIS image, and
    Qwen-VL grounds boxes in the input image's own pixel space, so we must know it."""
    h, w = img_bgr.shape[:2]
    scale = max_side / max(h, w)
    if scale < 1:
        img_bgr = cv2.resize(img_bgr, (int(w * scale), int(h * scale)))
    sh, sw = img_bgr.shape[:2]
    _, jpg = cv2.imencode(".jpg", img_bgr, [cv2.IMWRITE_JPEG_QUALITY, 88])
    return base64.b64encode(jpg.tobytes()).decode(), sw, sh


def _vlm_locate(frame) -> tuple[dict, int, int]:
    """One composited call: find the wristband and the medication, read the band text.
    Returns (result, sent_w, sent_h) so box coords can be normalized correctly."""
    b64, sw, sh = _b64(frame)
    prompt = (
        "You are looking at a medication-pass scene: possibly a patient wristband or "
        "printed patient label (name, DOB, MRN) and medication - pills in a cup OR a "
        "prescription bottle containing pills. Locate them. Bounding boxes use "
        "coordinates on a 0-1000 grid relative to the image, as [x1, y1, x2, y2].\n"
        "For medication: box the pills themselves (the group of pills is fine - if they "
        "are inside a bottle, box the cluster of pills visible through it, not the cap "
        "or the whole bottle). Report the color of the PILLS.\n"
        'Reply ONLY with JSON: {"wristband": {"box": [x1,y1,x2,y2], "text": "<the exact '
        'printed text you can read, or empty string>"} | null, '
        '"pills": [{"box": [x1,y1,x2,y2], "color": "white|green|orange|red|blue|other", '
        '"shape": "oval|round|oblong|other"}]}. '
        "If there is no wristband use null; if no medication is visible use []."
    )
    r = http.post(f"{OLLAMA}/api/generate", json={
        "model": VLM, "stream": False, "format": "json",
        "images": [b64],
        "options": {"temperature": 0, "top_k": 1, "seed": 0, "num_predict": 500},
        "prompt": prompt,
    }, timeout=60)
    r.raise_for_status()
    body = r.json()
    raw = body.get("response") or body.get("thinking") or "{}"
    return json.loads(raw[raw.index("{"):raw.rindex("}") + 1]), sw, sh


def _crop(frame, box, sent_w, sent_h, pad=0.06):
    """box comes back in the SENT image's pixel space (Qwen-VL grounding); convert
    to fractions of the frame. Guards: 0-1 fractions and 0-1000 grids also accepted."""
    h, w = frame.shape[:2]
    if not (isinstance(box, (list, tuple)) and len(box) == 4):
        return None, None
    try:
        x1, y1, x2, y2 = (float(v) for v in box)
    except (TypeError, ValueError):
        return None, None
    mx = max(x1, y1, x2, y2)
    if mx <= 1.0:                      # 0-1 fractions
        fx1, fy1, fx2, fy2 = x1, y1, x2, y2
    else:
        # Qwen-VL grounds on its 0-1000 grid regardless of what the prompt asks for
        # (verified live: y=775 on a 504px-tall image). Always divide by 1000.
        fx1, fy1, fx2, fy2 = x1 / 1000, y1 / 1000, x2 / 1000, y2 / 1000
    fx1, fx2 = sorted((max(0.0, min(1.0, fx1)), max(0.0, min(1.0, fx2))))
    fy1, fy2 = sorted((max(0.0, min(1.0, fy1)), max(0.0, min(1.0, fy2))))
    if (fx2 - fx1) * w < 8 or (fy2 - fy1) * h < 8:
        return None, None
    px, py = (fx2 - fx1) * pad, (fy2 - fy1) * pad
    a, b = int(max(0, (fx1 - px) * w)), int(min(w, (fx2 + px) * w))
    c, d = int(max(0, (fy1 - py) * h)), int(min(h, (fy2 + py) * h))
    # The UI box is the TIGHT box (no OCR padding) so it hugs the actual object.
    norm = {"x": fx1, "y": fy1, "w": fx2 - fx1, "h": fy2 - fy1}
    return frame[c:d, a:b], norm


def _patient_from_text(text: str, patients: dict) -> dict | None:
    """Identify WHICH patient a read names. First names are the distinguishing
    tokens (MARIA vs MARTIN - the look-alike-surname trap is deliberate).
    Ambiguity (both names present, or neither) returns None - never a guess."""
    up = re.sub(r"[^A-Z]", " ", (text or "").upper())
    hits = []
    for p in patients.values():
        first = p["name"].split()[0].upper()
        if re.search(rf"\b{re.escape(first)}\b", up):
            hits.append(p)
    return hits[0] if len(hits) == 1 else None


def _deskew(gray):
    """Wristbands rarely lie perfectly straight; Tesseract hates rotation."""
    inv = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    coords = cv2.findNonZero(inv)
    if coords is None or len(coords) < 50:
        return gray
    angle = cv2.minAreaRect(coords)[-1]
    if angle > 45:
        angle -= 90
    if abs(angle) < 1.5 or abs(angle) > 30:
        return gray
    h, w = gray.shape
    M = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    return cv2.warpAffine(gray, M, (w, h), flags=cv2.INTER_CUBIC,
                          borderMode=cv2.BORDER_REPLICATE)


def _ocr_read(band_crop, patients: dict) -> tuple[dict | None, str]:
    """Tesseract over a few deterministic preprocessing variants; the first one
    that names exactly one patient wins. Returns (patient|None, raw_text)."""
    g = cv2.cvtColor(band_crop, cv2.COLOR_BGR2GRAY)
    g = cv2.resize(g, None, fx=2.6, fy=2.6, interpolation=cv2.INTER_CUBIC)
    g = _deskew(g)
    variants = [
        ("gray-psm6", g, "--psm 6"),
        ("otsu-psm6", cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1], "--psm 6"),
        ("gray-psm11", g, "--psm 11"),
    ]
    best_raw = ""
    for _, img, cfg in variants:
        try:
            text = pytesseract.image_to_string(img, config=cfg)
        except Exception:
            continue
        if len(text.strip()) > len(best_raw):
            best_raw = text.strip()
        p = _patient_from_text(text, patients)
        if p:
            return p, text.strip()
    return None, best_raw


def _segment_object(crop):
    """The VLM box may include cup plastic and mat. Estimate the background from the
    crop's border ring, then keep the pixels most unlike it - that is the pill."""
    h, w = crop.shape[:2]
    px = crop.reshape(-1, 3).astype(float)
    ring = np.concatenate([crop[0:2].reshape(-1, 3), crop[-2:].reshape(-1, 3),
                           crop[:, 0:2].reshape(-1, 3), crop[:, -2:].reshape(-1, 3)]).astype(float)
    bg = np.median(ring, axis=0)
    dist = np.linalg.norm(px - bg, axis=1)
    cut = np.percentile(dist, 70)  # keep the 30% of pixels least like the background
    mask = dist >= max(cut, 30.0)
    if mask.sum() < 40:  # nearly uniform crop - fall back to the central region
        cy, cx = h // 2, w // 2
        return crop[max(0, cy - h // 4): cy + h // 4, max(0, cx - w // 4): cx + w // 4].reshape(-1, 3)
    return px[mask].astype(np.uint8).reshape(-1, 1, 3)


def _hsv_color(pixels) -> tuple[str, float]:
    """(color_family, decisiveness 0..1) by PRESENCE voting, not median.

    A pill box often includes the bottle: a median says 'orange bottle' even when a
    third of the pixels are clearly green capsules. Instead, each family gets the
    fraction of object pixels in its band, with precedence for the distinctive pill
    colors over bottle-colored orange and background-ish white."""
    hsv = cv2.cvtColor(np.asarray(pixels, dtype=np.uint8).reshape(-1, 1, 3),
                       cv2.COLOR_BGR2HSV).reshape(-1, 3).astype(int)
    h, s, v = hsv[:, 0], hsv[:, 1], hsv[:, 2]
    n = max(1, len(h))
    sat = s >= 60

    def frac(family):
        m = np.zeros(n, dtype=bool)
        for lo, hi in HUE_BANDS[family]:
            m |= (h >= lo) & (h <= hi)
        return float((m & sat).sum()) / n

    fr = {f: frac(f) for f in HUE_BANDS}
    fr_white = float(((s < SAT_WHITE_MAX) & (v > 130)).sum()) / n
    # Only the families the ACTIVE props actually use get a vote: green pills,
    # amber-bottle orange, plain white. (The blue mat under the props would
    # otherwise win crops it bleeds into.)
    if fr["green"] >= 0.10:
        return "green", min(1.0, fr["green"] * 2.8)
    if fr["orange"] >= 0.22:
        return "orange", min(1.0, fr["orange"] * 1.8)
    if fr_white >= 0.20:
        return "white", min(1.0, fr_white * 1.6)
    best = max([("green", fr["green"]), ("orange", fr["orange"]), ("white", fr_white)],
               key=lambda kv: kv[1])
    return best[0], 0.2


def _classify_pill(crop, vlm_color: str, vlm_shape: str, library: list[dict]) -> dict:
    """OpenCV measures; the VLM only proposed. Hue-presence vote over the whole crop
    (saturation gating removes the pale background; segmentation would throw away
    light-colored pills) -> attr_match; VLM words against the library -> vlm_match."""
    cv_color, margin = _hsv_color(crop.reshape(-1, 3))

    active = [p for p in library if p.get("active", True)]
    # a pill may present as more than one family (white pills in an amber bottle)
    attr = next((p for p in active
                 if cv_color in (p.get("families") or [p.get("color_family")])), None)
    vlm = next((p for p in active
                if vlm_color in (p.get("vlm_aliases") or [p.get("color_family")])), None)
    if vlm is None:  # fall back to shape wording
        vlm = next((p for p in active if p.get("visual", {}).get("shape") == vlm_shape), None)

    attr_id = attr["pill_id"] if attr else None
    vlm_id = vlm["pill_id"] if vlm else None
    if attr_id and vlm_id and attr_id == vlm_id:
        confidence = round(min(0.97, 0.72 + 0.25 * margin), 2)
        pill_id = attr_id
    else:
        # disagreement between the two readers caps confidence - rules escalate it
        confidence = 0.5
        pill_id = attr_id or vlm_id
    return {"pill_id": pill_id, "vlm_match": vlm_id, "attr_match": attr_id,
            "cv_color": cv_color, "vlm_color": vlm_color, "confidence": confidence}


def analyze(frame, records) -> tuple[dict, list, dict]:
    """frame (BGR) -> (contract, ui_boxes, timings). Never raises on model failure:
    a failed read becomes an honest read-conflict, which the rules HOLD."""
    timings = {}
    boxes = []
    library = list(records.pills.values())

    t0 = time.perf_counter()
    try:
        loc, sent_w, sent_h = _vlm_locate(frame)
    except Exception as e:
        return ({"wristband": {"text_vlm": "", "text_ocr": "", "read_conflict": True,
                               "error": f"vlm: {e}"[:120]}, "cup": []},
                [], {"vlm_ms": round((time.perf_counter() - t0) * 1000)})
    timings["vlm_ms"] = round((time.perf_counter() - t0) * 1000)

    # --- wristband dual-read ---
    wristband = {"text_vlm": "", "text_ocr": "", "mrn": None, "read_conflict": False}
    wb = loc.get("wristband")
    if not isinstance(wb, dict):
        wb = None
    if wb and wb.get("box"):
        crop, norm = _crop(frame, wb["box"], sent_w, sent_h, pad=0.10)
        if crop is not None:
            t1 = time.perf_counter()
            ocr_p, ocr_raw = _ocr_read(crop, records.patients)
            timings["ocr_ms"] = round((time.perf_counter() - t1) * 1000)
            vlm_text = (wb.get("text") or "").strip()
            vlm_p = _patient_from_text(vlm_text, records.patients)
            # The value that matters is WHICH patient; two independent readers must
            # name the same one (the names, not the barcode - print too small).
            agree = vlm_p and ocr_p and vlm_p["mrn"] == ocr_p["mrn"]
            wristband = {
                "text_vlm": vlm_p["name"] if vlm_p else "",
                "text_ocr": ocr_p["name"] if ocr_p else "",
                "vlm_raw": vlm_text[:160], "ocr_raw": ocr_raw[:160],
                "mrn": vlm_p["mrn"] if agree else None,
                "read_conflict": False,  # rules detect empty/mismatch themselves
            }
            label = vlm_p["name"].title() if agree else "?"
            boxes.append({**norm, "label": f"wristband {label}", "kind": "wristband"})

    # --- pills: VLM proposes, OpenCV disposes ---
    cup = []
    t2 = time.perf_counter()
    pills = loc.get("pills") or []
    if not isinstance(pills, list):
        pills = []
    for p in pills[:6]:
        try:
            if not isinstance(p, dict) or not p.get("box"):
                continue
            crop, norm = _crop(frame, p["box"], sent_w, sent_h)
            if crop is None:
                continue
            item = _classify_pill(crop, p.get("color", "other"), p.get("shape", "other"), library)
            if item["pill_id"] is None:
                continue
            cup.append(item)
            drug = records.pills.get(item["pill_id"], {}).get("drug", "?")
            boxes.append({**norm, "label": f"{drug} {item['confidence']:.2f}", "kind": "pill"})
        except Exception:
            continue  # one malformed pill entry must never kill the verification
    timings["classify_ms"] = round((time.perf_counter() - t2) * 1000)

    return {"wristband": wristband, "cup": cup}, boxes, timings
