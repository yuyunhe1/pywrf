"""Regression checks for the realtime GFS downloader used by backend startup."""

import importlib.util
import json
import re
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest

from app import gfs_downloader


SCRIPT_PATH = Path(__file__).resolve().parents[3] / "download_gfs_hourly_70vars_realtime.py"
SPEC = importlib.util.spec_from_file_location("gfs_download_script", SCRIPT_PATH)
downloader = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(downloader)
CYCLE = datetime(2026, 10, 10, tzinfo=timezone.utc)


def make_args(tmp_path):
    return downloader.build_parser().parse_args(
        ["realtime", "--output-dir", str(tmp_path), "--global-region", "--min-bytes", "8"]
    )


def test_backend_defaults_request_hourly_wind_and_height_only(monkeypatch):
    for name in (
        "GFS_REALTIME_GLOBAL_REGION", "GFS_REALTIME_START_FHOUR", "GFS_REALTIME_END_FHOUR",
        "GFS_REALTIME_CYCLE_COUNT", "GFS_REALTIME_WIND_MAP_ONLY",
        "GFS_REALTIME_FALLBACK_PREVIOUS_CYCLE", "GFS_REALTIME_PROBE_F001",
    ):
        monkeypatch.delenv(name, raising=False)
    args = downloader.build_parser().parse_args(gfs_downloader._build_command()[2:])
    monkeypatch.setattr(downloader, "sync_once", lambda args: None)
    downloader.run_realtime(args)
    variables, levels = downloader.requested_variables_and_levels(args)
    url = downloader.build_nomads_url(
        CYCLE, args.start_fhour, global_region=args.global_region,
        top_lat=args.toplat, bottom_lat=args.bottomlat,
        left_lon=args.leftlon, right_lon=args.rightlon, variables=variables, levels=levels,
    )
    query = parse_qs(urlsplit(url).query, keep_blank_values=True)

    assert urlsplit(url).path == "/cgi-bin/filter_gfs_0p25_1hr.pl"
    assert query["file"] == ["gfs.t00z.pgrb2.0p25.f001"]
    assert {key for key in query if key.startswith("var_")} == {"var_UGRD", "var_VGRD", "var_HGT"}
    pressures = {
        1000, 975, 950, 925, 900, 850, 800, 750, 700, 650, 600,
        550, 500, 450, 400, 350, 300, 250, 200, 150, 100, 50,
    }
    assert {int(level[:-3]) for level in levels if level[:-3].isdigit() and level.endswith("_mb")} == pressures
    assert "surface" in levels  # HGT:surface is needed for AGL interpolation.
    assert "100_m_above_ground" in levels
    frontend_api = Path(__file__).resolve().parents[2] / "frontend/src/api.js"
    frontend_bbox = json.loads(re.search(
        r"export const CHINA_BBOX = (\[[^\]]+\])", frontend_api.read_text(encoding="utf-8")
    ).group(1))
    requested_bbox = [float(query[key][0]) for key in ("leftlon", "bottomlat", "rightlon", "toplat")]
    assert requested_bbox == frontend_bbox == [73, 18, 135, 54]
    assert "subregion" in query
    assert args.subregion and not args.global_region
    assert (args.start_fhour, args.end_fhour, args.cycle_count) == (1, 12, 1)
    assert args.fallback_previous_cycle and args.probe_f001


@pytest.mark.parametrize(
    "kind,source_mode,fail_preferred",
    [("preferred", "auto", False), ("fallback_previous_cycle", "auto", True),
     ("fallback_previous_cycle", "fallback", False)],
)
def test_old_multivariable_files_are_replaced_even_during_fallback(
    monkeypatch, tmp_path, kind, source_mode, fail_preferred
):
    args = make_args(tmp_path)
    target = downloader.output_path(tmp_path, CYCLE, 1, True)
    target.write_bytes(b"GRIBold7777")
    downloader.write_meta(target, {
        "kind": kind, "variables": ["UGRD", "VGRD", "HGT", "TMP"],
        "levels": downloader.WIND_CHECK_LEVELS,
        **downloader.requested_bounds(args),
    })
    calls = []

    def fake_download(url, path, **kwargs):
        calls.append(url)
        if fail_preferred and len(calls) == 1:
            return False
        path.write_bytes(b"GRIBnew7777")
        return True

    monkeypatch.setattr(downloader, "download", fake_download)
    result = downloader.sync_target(args, CYCLE, 1, source_mode=source_mode)

    assert result.startswith("downloaded_")
    assert len(calls) == (2 if fail_preferred else 1)
    assert target.read_bytes() == b"GRIBnew7777"
    assert set(downloader.read_meta(target)["variables"]) == {"UGRD", "VGRD", "HGT"}


@pytest.mark.parametrize("kind,source_mode", [("preferred", "auto"), ("fallback_previous_cycle", "fallback")])
def test_current_selection_still_reuses_existing_files(monkeypatch, tmp_path, kind, source_mode):
    args = make_args(tmp_path)
    target = downloader.output_path(tmp_path, CYCLE, 1, True)
    target.write_bytes(b"GRIBcurrent7777")
    downloader.write_meta(target, {
        "kind": kind, "variables": ["HGT", "VGRD", "UGRD"],
        "levels": downloader.WIND_CHECK_LEVELS,
        **downloader.requested_bounds(args),
    })

    def unexpected_download(*args, **kwargs):
        pytest.fail("Matching selection should be reused")

    monkeypatch.setattr(downloader, "download", unexpected_download)
    assert downloader.sync_target(args, CYCLE, 1, source_mode=source_mode).startswith("skipped_")


@pytest.mark.parametrize("payload", [b"<html>error page</html>", b"GRIBtruncated"])
def test_bad_http_200_payload_preserves_existing_grib(monkeypatch, tmp_path, payload):
    target = tmp_path / "wind.grib2"
    target.write_bytes(b"GRIBprevious7777")
    monkeypatch.setattr(downloader.urllib.request, "urlopen", lambda *args, **kwargs: BytesIO(payload))

    assert not downloader.download("https://example.test/wind", target, retries=1, min_bytes=8)
    assert target.read_bytes() == b"GRIBprevious7777"
    assert not target.with_suffix(".grib2.tmp").exists()


def test_valid_grib_response_replaces_target(monkeypatch, tmp_path):
    target = tmp_path / "wind.grib2"
    payload = b"GRIBdata7777"
    monkeypatch.setattr(downloader.urllib.request, "urlopen", lambda *args, **kwargs: BytesIO(payload))

    assert downloader.download("https://example.test/wind", target, retries=1, min_bytes=8)
    assert target.read_bytes() == payload


@pytest.mark.parametrize("status", [404, 410])
def test_unpublished_forecast_falls_back_without_retry_delay(monkeypatch, tmp_path, status):
    args = make_args(tmp_path)
    urls = []

    def fake_urlopen(request, **kwargs):
        urls.append(request.full_url)
        if len(urls) == 1:
            raise HTTPError(request.full_url, status, "not available", {}, None)
        return BytesIO(b"GRIBfallback7777")

    def unexpected_sleep(*args):
        pytest.fail("Unavailable forecasts should immediately use the previous cycle")

    monkeypatch.setattr(downloader.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(downloader.time, "sleep", unexpected_sleep)

    assert downloader.sync_target(args, CYCLE, 1) == "downloaded_fallback"
    assert len(urls) == 2
    assert parse_qs(urlsplit(urls[1]).query)["file"] == ["gfs.t18z.pgrb2.0p25.f007"]
    target = downloader.output_path(tmp_path, CYCLE, 1, True)
    meta = downloader.read_meta(target)
    assert meta["source_cycle_utc"] == "2026100918"
    assert meta["valid_time_utc"] == "2026101001"


def test_published_first_hour_keeps_fallback_for_individually_missing_later_hours(monkeypatch, tmp_path):
    args = make_args(tmp_path)
    urls = []
    def download_hour(url, target, **kwargs):
        urls.append(url)
        if len(urls) == 1:
            return False
        target.write_bytes(b"GRIBfallback7777")
        return True
    monkeypatch.setattr(downloader, "download", download_hour)
    assert downloader.sync_target(args, CYCLE, 6, source_mode="preferred") == "downloaded_fallback"
    assert parse_qs(urlsplit(urls[0]).query)["file"] == ["gfs.t00z.pgrb2.0p25.f006"]
    assert parse_qs(urlsplit(urls[1]).query)["file"] == ["gfs.t18z.pgrb2.0p25.f012"]
    target = downloader.output_path(tmp_path, CYCLE, 6, True)
    meta = downloader.read_meta(target)
    assert meta["kind"] == "fallback_previous_cycle"
    assert meta["valid_time_utc"] == "2026101006"
    assert len(downloader.make_source_plan(CYCLE, 6, use_fallback=False, source_mode="preferred")) == 1


def test_global_override_remains_available(monkeypatch):
    monkeypatch.setenv("GFS_REALTIME_GLOBAL_REGION", "1")
    args = downloader.build_parser().parse_args(gfs_downloader._build_command()[2:])
    monkeypatch.setattr(downloader, "sync_once", lambda args: None)
    downloader.run_realtime(args)
    assert args.global_region and not args.subregion
    assert downloader.requested_bounds(args) == {
        "leftlon": -180, "bottomlat": -90, "rightlon": 180, "toplat": 90,
    }


def test_changed_region_does_not_reuse_same_variable_cache(tmp_path):
    args = make_args(tmp_path)
    meta = {
        "variables": downloader.WIND_CHECK_VARS, "levels": downloader.WIND_CHECK_LEVELS,
        **downloader.requested_bounds(args),
    }
    assert downloader.has_matching_selection(meta, args)
    meta.update(downloader.CHINA_BOUNDS)
    assert not downloader.has_matching_selection(meta, args)


def test_china_download_writes_matching_bounds_and_regional_filename(monkeypatch, tmp_path):
    args = downloader.build_parser().parse_args([
        "realtime", "--output-dir", str(tmp_path), "--min-bytes", "8",
        "--subregion", "--leftlon", "73", "--bottomlat", "18", "--rightlon", "135", "--toplat", "54",
        "--start-fhour", "1", "--end-fhour", "1", "--cycle-count", "1", "--now", "2026101000",
    ])
    urls = []

    def fake_download(url, target, **kwargs):
        urls.append(url)
        target.write_bytes(b"GRIBregional7777")
        return True

    monkeypatch.setattr(downloader, "download", fake_download)
    downloader.run_realtime(args)
    assert len(urls) == 1
    query = parse_qs(urlsplit(urls[0]).query, keep_blank_values=True)
    assert [float(query[key][0]) for key in ("leftlon", "bottomlat", "rightlon", "toplat")] == [73, 18, 135, 54]
    target = downloader.output_path(tmp_path, CYCLE, 1, region="china")
    assert target.exists()
    meta = downloader.read_meta(target)
    assert all(meta[key] == value for key, value in downloader.CHINA_BOUNDS.items())


def test_provider_prefers_china_file_over_existing_global_and_point(monkeypatch, tmp_path):
    from app import gfs_provider

    for region in ("point", "global", "china"):
        (tmp_path / f"1010_gfs_20261010_00z_f001_{region}_windcheck.grib2").touch()
    monkeypatch.setattr(gfs_provider, "configured_data_dirs", lambda: (tmp_path,))
    gfs_provider.refresh_file_index()
    try:
        files = gfs_provider.discover_files()
        assert len(files) == 1
        assert "_china_" in files[0].path.name
    finally:
        gfs_provider.refresh_file_index()
