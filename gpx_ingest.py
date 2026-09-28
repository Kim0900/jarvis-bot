"""task#164 — GPX text -> gpx_sessions staging row.

Pure deterministic parser. No DB/network access.
Keeps the same summary semantics as the legacy index.html parseGpx():
- service date = first valid track point in KST
- duration = last timestamp - first timestamp
- distance = point-to-point Haversine sum
- first/last timestamps preserved in UTC ISO text
"""
from __future__ import annotations
import hashlib
import math
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
EARTH_RADIUS_KM = 6371.0088

class GpxParseError(ValueError):
    pass

def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag

def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def _haversine_km(a, b) -> float:
    _, lat1, lon1 = a
    _, lat2, lon2 = b
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    sin_phi = math.sin(dphi / 2.0)
    sin_lam = math.sin(dlambda / 2.0)
    h = sin_phi * sin_phi + math.cos(p1) * math.cos(p2) * sin_lam * sin_lam
    h = min(1.0, max(0.0, h))
    return EARTH_RADIUS_KM * 2.0 * math.atan2(math.sqrt(h), math.sqrt(1.0 - h))

def parse_gpx_text(text: str) -> dict:
    if not isinstance(text, str) or not text.strip():
        raise GpxParseError("gpx_text_empty")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise GpxParseError(f"gpx_xml_invalid:{exc}") from exc

    points = []
    invalid_points = 0
    for elem in root.iter():
        if _local_name(elem.tag) != "trkpt":
            continue
        try:
            lat = float(elem.attrib["lat"])
            lon = float(elem.attrib["lon"])
        except (KeyError, TypeError, ValueError):
            invalid_points += 1
            continue
        if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
            invalid_points += 1
            continue
        time_value = None
        for child in elem:
            if _local_name(child.tag) == "time":
                time_value = child.text
                break
        when = _parse_time(time_value)
        if when is None:
            invalid_points += 1
            continue
        points.append((when, lat, lon))

    if not points:
        raise GpxParseError("gpx_no_valid_trkpt")

    points.sort(key=lambda p: p[0])
    first = points[0][0]
    last = points[-1][0]
    distance_km = sum(_haversine_km(a, b) for a, b in zip(points, points[1:]))
    duration_h = (last - first).total_seconds() / 3600.0

    return {
        "service_date": first.astimezone(KST).date().isoformat(),
        "duration_h": round(duration_h, 2),
        "distance_km": round(distance_km, 2),
        "first_track_at": first.isoformat().replace("+00:00", "Z"),
        "last_track_at": last.isoformat().replace("+00:00", "Z"),
        "point_count": len(points),
        "invalid_point_count": invalid_points,
    }

def build_gpx_session_row(text: str, source_file_id: str | None, source_file_name: str | None):
    if not source_file_id:
        raise GpxParseError("source_file_id_required")
    if not source_file_name:
        raise GpxParseError("source_file_name_required")
    parsed = parse_gpx_text(text)
    sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    row = {
        "날짜": parsed["service_date"],
        "파일명": source_file_name,
        "구속시간_h": parsed["duration_h"],
        "총주행거리_km": parsed["distance_km"],
        "첫트랙시각": parsed["first_track_at"],
        "마지막트랙시각": parsed["last_track_at"],
        "트랙포인트수": parsed["point_count"],
        "source_file_id": source_file_id,
        "source_sha256": sha256,
    }
    return row, parsed
