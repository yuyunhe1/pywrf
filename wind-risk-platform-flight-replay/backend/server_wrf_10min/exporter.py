"""Native ten-minute export, derived from the frozen server implementation.

All wind rotation, spatial/vertical mapping and extra-variable derivations below
are preserved from upstream/SOURCE.json. Only lineage, time identity and output
metadata change. The upstream files themselves remain unchanged.
"""
from __future__ import annotations
import hashlib
from pathlib import Path
from upstream.run_realtime_wrf_platform_cache import *

NATIVE_MODEL='NATIVE_WRF_TEN_MINUTE_HOLD_V1'


def file_digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


def export_wrf_file(path: Path, cycle: datetime, args) -> list[CacheRecord]:
    records: list[CacheRecord] = []
    raw_sha256 = file_digest(path)
    heights = np.asarray(args.heights, dtype=np.float32)
    with open_wrf_dataset(path) as dataset:
        times = read_times(dataset)
        lineage = getattr(dataset, "SIMULATION_START_DATE", None)
        if lineage != cycle.strftime("%Y-%m-%d_%H:%M:%S"):
            raise ValueError("原始 WRF 起报来源与批次不一致")
        xlat = read_time_slice(dataset.variables["XLAT"], 0)
        xlon = read_time_slice(dataset.variables["XLONG"], 0)
        lons, lats, mapper = regular_grid_mapper(xlat, xlon, args.output_spacing_deg)
        target_shape = (len(lats), len(lons))
        cosalpha = read_time_slice(dataset.variables["COSALPHA"], 0) if "COSALPHA" in dataset.variables else np.ones_like(xlat)
        sinalpha = read_time_slice(dataset.variables["SINALPHA"], 0) if "SINALPHA" in dataset.variables else np.zeros_like(xlat)

        for time_index, valid_time in enumerate(times):
            seconds = (valid_time - cycle).total_seconds()
            minute = int(round(seconds / 60))
            if abs(seconds-minute*60) > 1e-6 or minute % 10:
                raise ValueError("原始 WRF 时间不是整 10 分钟")
            forecast_hour = minute / 60
            if minute < 0 or minute > args.forecast_hours*60:
                continue
            print(f"[EXPORT] {path.name} time={valid_time:%Y-%m-%d_%H:%M:%S} m{minute:05d}")
            u_stag = as_array(dataset.variables["U"][time_index])
            v_stag = as_array(dataset.variables["V"][time_index])
            grid_u = 0.5 * (u_stag[:, :, :-1] + u_stag[:, :, 1:])
            grid_v = 0.5 * (v_stag[:, :-1, :] + v_stag[:, 1:, :])
            earth_u, earth_v = rotate_to_earth(grid_u, grid_v, cosalpha[np.newaxis, :, :], sinalpha[np.newaxis, :, :])

            ph = as_array(dataset.variables["PH"][time_index])
            phb = as_array(dataset.variables["PHB"][time_index])
            hgt = read_time_slice(dataset.variables["HGT"], time_index)
            z_interfaces = (ph + phb) / GRAVITY
            heights_agl = 0.5 * (z_interfaces[:-1] + z_interfaces[1:]) - hgt[np.newaxis, :, :]

            u10 = v10 = None
            if "U10" in dataset.variables and "V10" in dataset.variables:
                u10_grid = read_time_slice(dataset.variables["U10"], time_index)
                v10_grid = read_time_slice(dataset.variables["V10"], time_index)
                u10, v10 = rotate_to_earth(u10_grid, v10_grid, cosalpha, sinalpha)

            level_u, level_v = interpolate_levels(heights_agl, earth_u, earth_v, u10, v10, heights)
            regular_u = apply_mapper(level_u, target_shape, mapper)
            regular_v = apply_mapper(level_v, target_shape, mapper)

            z_msl = 0.5 * (z_interfaces[:-1] + z_interfaces[1:])
            temperature_k = wrf_temperature_k(dataset, time_index)
            specific_humidity = wrf_specific_humidity(dataset, time_index)
            relative_humidity = wrf_relative_humidity(dataset, time_index, temperature_k, specific_humidity)
            vertical_velocity = wrf_vertical_velocity(dataset, time_index)

            extra_payload: dict[str, np.ndarray] = {}
            extra_sources: dict[str, str] = {}

            for out_name, candidates in {
                "gust_surface": ("GUST", "WSPD10MAX", "AFWA_GUST"),
                "pblh": ("PBLH", "HPBL"),
                "cape": ("CAPE", "MCAPE"),
                "cin": ("CIN", "MCIN"),
                "prate": ("PRATE",),
            }.items():
                source_name, values = read_optional_2d(dataset, time_index, candidates, target_shape, mapper)
                if values is not None:
                    extra_payload[out_name] = values
                    extra_sources[out_name] = source_name

            apcp_values = accumulated_precip(dataset, time_index)
            if apcp_values is not None:
                if apcp_values.shape != target_shape:
                    apcp_values = apply_mapper_2d(apcp_values, target_shape, mapper)
                extra_payload["apcp"] = apcp_values.astype(np.float32)
                extra_sources["apcp"] = "RAINC+RAINNC+RAINSH" if "RAINC" in dataset.variables or "RAINNC" in dataset.variables else "APCP"

            for out_name, values, source_name in (
                ("vvel", vertical_velocity, "VVEL/W"),
                ("rh", relative_humidity, "RH/QVAPOR+T+P+PB"),
                ("spfh", specific_humidity, "QVAPOR"),
                ("tmp", temperature_k, "T+P+PB"),
                ("hgt", z_msl, "PH+PHB"),
            ):
                if values is None:
                    continue
                extra_payload[out_name] = apply_mapper(
                    interpolate_scalar_levels(heights_agl, values, heights),
                    target_shape,
                    mapper,
                )
                extra_sources[out_name] = source_name

            extra_payload["hgt_surface"] = apply_mapper_2d(hgt, target_shape, mapper)
            extra_sources["hgt_surface"] = "HGT"

            rel = Path(cycle.strftime("%Y%m%d%H")) / f"wrf_d{args.domain:02d}_{cycle:%Y%m%d%H}_m{minute:05d}.npz"
            out_path = args.cache_dir / rel
            out_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "lons": lons,
                "lats": lats,
                "levels_m": heights,
                "u": regular_u,
                "v": regular_v,
                "cycle_utc": np.asarray(cycle.strftime("%Y-%m-%d %H:%M UTC")),
                "valid_time_utc": np.asarray(valid_time.strftime("%Y-%m-%d %H:%M UTC")),
                "forecast_hour": np.asarray(forecast_hour, dtype=np.float64),
                "forecast_minutes": np.asarray(minute,dtype=np.int32),
                "output_interval_seconds": np.asarray(600,dtype=np.int32),
                "wind_time_model": np.asarray(NATIVE_MODEL),
                "source_wrf_sha256": np.asarray(raw_sha256),
                "source_wrf_time_index": np.asarray(time_index,dtype=np.int32),
                "source_wrf_cycle_utc": np.asarray(cycle.strftime("%Y-%m-%d %H:%M UTC")),
                "cache_variables": np.asarray(json.dumps(sorted(extra_payload), ensure_ascii=False)),
                "cache_variable_sources": np.asarray(json.dumps(extra_sources, ensure_ascii=False, sort_keys=True)),
                **extra_payload,
            }
            np.savez_compressed(out_path, **payload)
            records.append(
                CacheRecord(
                    cycle=cycle,
                    forecast_hour=forecast_hour,
                    valid_time=valid_time,
                    relative_path=rel.as_posix(),
                    bbox=[float(lons[0]), float(lats[-1]), float(lons[-1]), float(lats[0])],
                )
            )
    return records
