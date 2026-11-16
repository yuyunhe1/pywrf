"""Read selected GFS messages from NOAA's official S3 mirror and crop GRIB grids."""

from concurrent.futures import ThreadPoolExecutor
import re
import time
import urllib.request
from urllib.parse import parse_qs, urlsplit

import numpy as np


S3_BASE = "https://noaa-gfs-bdp-pds.s3.amazonaws.com"


def selection_from_nomads_url(url):
    parsed = urlsplit(url)
    if parsed.hostname != "nomads.ncep.noaa.gov" or "filter_gfs_0p25" not in parsed.path:
        raise ValueError("Only NOAA GFS filter URLs have an S3 equivalent")
    query = parse_qs(parsed.query, keep_blank_values=True)
    directory = query["dir"][0].strip("/")
    filename = query["file"][0]
    if not re.fullmatch(r"gfs\.\d{8}/(?:00|06|12|18)/atmos", directory):
        raise ValueError("Invalid GFS cycle directory")
    if not re.fullmatch(r"gfs\.t\d{2}z\.pgrb2\.0p25\.f\d{3}", filename):
        raise ValueError("Invalid GFS file")
    variables = {key[4:] for key in query if key.startswith("var_")}
    levels = {key[4:].replace("_", " ") for key in query if key.startswith("lev_")}
    bounds = None
    if "subregion" in query:
        bounds = tuple(float(query[key][0]) for key in ("leftlon", "bottomlat", "rightlon", "toplat"))
    return f"{S3_BASE}/{directory}/{filename}", variables, levels, bounds


def selected_ranges(index_text, variables, levels):
    entries = []
    for line in index_text.splitlines():
        fields = line.split(":")
        if len(fields) >= 6:
            entries.append((int(fields[1]), fields[3], fields[4]))
    if not entries or any(right[0] <= left[0] for left, right in zip(entries, entries[1:])):
        raise ValueError("Invalid or unordered NOAA GRIB index")
    selected = [(i, entry) for i, entry in enumerate(entries) if entry[1] in variables and entry[2] in levels]
    present = {(entry[1], entry[2]) for _, entry in selected}
    # Pressure winds/heights, native wind layers and surface HGT must not be
    # silently lost. Other requested combinations may be absent in GFS itself.
    required = {
        (var, level) for var in variables for level in levels
        if re.fullmatch(r"\d+ mb", level)
        or (var in {"UGRD", "VGRD"} and re.fullmatch(r"(?:10|20|30|40|50|80|100) m above ground", level))
        or (var == "HGT" and level == "surface")
    }
    if required - present:
        raise ValueError(f"NOAA mirror lacks requested fields: {sorted(required - present)}")
    if not selected:
        raise ValueError("NOAA mirror contains no requested fields")
    ranges = []
    previous_index = None
    for i, (start, _, _) in selected:
        end = entries[i + 1][0] - 1 if i + 1 < len(entries) else None
        if ranges and previous_index == i - 1:
            ranges[-1] = (ranges[-1][0], end)
        else:
            ranges.append((start, end))
        previous_index = i
    return ranges, len(selected)


def fetch_range(url, byte_range, timeout, retries):
    start, end = byte_range
    header = f"bytes={start}-{'' if end is None else end}"
    for attempt in range(retries):
        try:
            # Distinct URLs prevent intermediary caches reusing a different
            # byte range of the same large object.
            request = urllib.request.Request(
                f"{url}?gfs_range={start}-{'' if end is None else end}",
                headers={"Range": header, "User-Agent": "GFSWindDownloader/1.0"},
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                content_range = response.headers.get("Content-Range", "")
                match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range)
                if response.status != 206 or not match or int(match[1]) != start:
                    raise ValueError("NOAA mirror did not honor the requested byte range")
                actual_end = int(match[2])
                if end is not None and actual_end != end:
                    raise ValueError("NOAA mirror returned a different byte range")
                expected = actual_end - start + 1
                payload = response.read(expected + 1)
                if len(payload) != expected:
                    raise ValueError("Truncated NOAA GRIB byte range")
                return payload
        except Exception:
            if attempt + 1 == retries:
                raise
            time.sleep(min(2 ** attempt, 4))


def crop_message(message, bounds):
    if bounds is None:
        return message
    import eccodes as ec

    handle = ec.codes_new_from_message(message)
    try:
        if ec.codes_get(handle, "gridType") != "regular_ll" or ec.codes_get(handle, "scanningMode") != 0:
            raise ValueError("NOAA mirror cropping requires the standard GFS regular_ll grid")
        nx, ny = ec.codes_get(handle, "Ni"), ec.codes_get(handle, "Nj")
        lons = (ec.codes_get(handle, "longitudeOfFirstGridPointInDegrees") + np.arange(nx) * ec.codes_get(handle, "iDirectionIncrementInDegrees")) % 360
        lats = ec.codes_get(handle, "latitudeOfFirstGridPointInDegrees") - np.arange(ny) * ec.codes_get(handle, "jDirectionIncrementInDegrees")
        west, south, east, north = bounds
        west, east = west % 360, east % 360
        if west > east:
            raise ValueError("S3 regional crop cannot cross the longitude seam")
        cols = np.flatnonzero((lons >= west) & (lons <= east))
        rows = np.flatnonzero((lats >= south) & (lats <= north))
        if not len(rows) or not len(cols):
            raise ValueError("Requested region does not intersect GFS")
        values = ec.codes_get_values(handle).reshape(ny, nx)[np.ix_(rows, cols)]
        for key, value in (
            ("Ni", len(cols)), ("Nj", len(rows)),
            ("latitudeOfFirstGridPointInDegrees", float(lats[rows[0]])),
            ("latitudeOfLastGridPointInDegrees", float(lats[rows[-1]])),
            ("longitudeOfFirstGridPointInDegrees", float(lons[cols[0]])),
            ("longitudeOfLastGridPointInDegrees", float(lons[cols[-1]])),
        ):
            ec.codes_set(handle, key, value)
        ec.codes_set_values(handle, values.ravel())
        return ec.codes_get_message(handle)
    finally:
        ec.codes_release(handle)


def crop_payload(payload, bounds):
    position = 0
    while position < len(payload):
        if payload[position:position + 4] != b"GRIB" or payload[position + 7:position + 8] != b"\x02":
            raise ValueError("NOAA mirror byte range is not GRIB2")
        size = int.from_bytes(payload[position + 8:position + 16], "big")
        if size < 20 or position + size > len(payload) or payload[position + size - 4:position + size] != b"7777":
            raise ValueError("Incomplete NOAA mirror GRIB2 message")
        yield crop_message(payload[position:position + size], bounds)
        position += size


def download_from_s3(nomads_url, temporary_path, timeout=180, retries=3):
    url, variables, levels, bounds = selection_from_nomads_url(nomads_url)
    request = urllib.request.Request(url + ".idx", headers={"User-Agent": "GFSWindDownloader/1.0"})
    with urllib.request.urlopen(request, timeout=min(timeout, 30)) as response:
        index = response.read(1024 * 1024 + 1)
        if len(index) > 1024 * 1024:
            raise ValueError("Oversized NOAA GRIB index")
    ranges, count = selected_ranges(index.decode("utf-8"), variables, levels)
    print(f"[S3] {temporary_path.name}: {count} selected GRIB fields, {len(ranges)} byte ranges", flush=True)
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="gfs-s3") as executor:
        payloads = executor.map(lambda byte_range: fetch_range(url, byte_range, timeout, retries), ranges)
        with temporary_path.open("wb") as output:
            for payload in payloads:
                for message in crop_payload(payload, bounds):
                    output.write(message)
    return url
