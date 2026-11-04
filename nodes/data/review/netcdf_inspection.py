"""Bounded native NetCDF observations; CF metadata rules are not LLM guesses."""
from __future__ import annotations

import re


def measure_conservation(source_path, target_path, variable: str) -> dict:
    """Independent spherical integrals for a rectilinear spatial regrid.

    Compare the same fully covered, non-missing target cells. Report excluded
    cells explicitly; this is not a proof about an unobserved/missing region.
    """
    import netCDF4
    import numpy as np

    with netCDF4.Dataset(source_path) as source, netCDF4.Dataset(target_path) as target:
        def grid(dataset):
            field = dataset[variable]
            if not np.issubdtype(field.dtype, np.number):
                raise ValueError("Conservation measurements require a numeric field")
            coords = []
            for standard, unit in (("latitude", "degrees_north"), ("longitude", "degrees_east")):
                names = [name for name in field.dimensions if name in dataset.variables
                         and (getattr(dataset[name], "standard_name", "") == standard
                              or getattr(dataset[name], "units", "") == unit)]
                if len(names) != 1:
                    raise ValueError("Requires one-dimensional latitude/longitude coordinate variables in degrees")
                coord = dataset[names[0]]
                allowed_units = ({"degrees_north", "degree_north", "degrees_n", "degree_n", "degreen", "degreesn"}
                                 if standard == "latitude" else {"degrees_east", "degree_east", "degrees_e", "degree_e", "degreee", "degreese"})
                if str(getattr(coord, "units", "")).lower() not in allowed_units:
                    raise ValueError("Native conservation currently requires geographic angular coordinates in degrees")
                centers = np.asarray(coord[:], dtype=float)
                if centers.ndim != 1 or len(centers) < 2:
                    raise ValueError("Requires nontrivial rectilinear coordinate axes")
                bound_name = getattr(coord, "bounds", "")
                if bound_name:
                    bounds = np.asarray(dataset[bound_name][:], dtype=float)
                else:
                    differences = np.diff(centers)
                    if not np.allclose(differences, differences[0], rtol=1e-6, atol=1e-10):
                        raise ValueError("Irregular axes need explicit cell bounds")
                    bounds = centers[:, None] + np.array([-.5, .5]) * abs(differences[0])
                if bounds.shape != (len(centers), 2):
                    raise ValueError("Expected two bounds per rectilinear cell")
                if not np.isfinite(bounds).all() or (standard == "latitude" and np.max(np.abs(bounds)) > 90):
                    raise ValueError("Invalid geographic cell bounds")
                if standard == "longitude" and np.max(np.abs(np.diff(bounds))) > 180:
                    raise ValueError("Wrapped longitude bounds need explicit unwrapping before this measurement")
                coords.append((names[0], np.sort(bounds, axis=1)))
            other = [dim for dim in field.dimensions if dim not in {c[0] for c in coords}]
            if len(other) > 1 or any(dim not in dataset.variables for dim in other):
                raise ValueError("Only a spatial field with an optional time axis is supported")
            return field, coords, other
        src, sg, st = grid(source)
        dst, tg, tt = grid(target)
        if str(getattr(src, "units", "")) != str(getattr(dst, "units", "")) or bool(st) != bool(tt):
            raise ValueError("Source/target must use the same units and compatible axes")
        sy, sx, ty, tx = len(sg[0][1]), len(sg[1][1]), len(tg[0][1]), len(tg[1][1])
        steps = len(target[tt[0]]) if tt else 1
        if (max(sy*sx, ty*tx, sy*ty, sx*tx) > 5_000_000
                or steps * (ty*sy*sx + ty*sx*tx) > 50_000_000):
            raise ValueError("Grid/time extent exceeds the bounded native measurement capacity")
        if st:
            a, b = source[st[0]], target[tt[0]]
            if any(getattr(a, key, "standard" if key == "calendar" else "") != getattr(b, key, "standard" if key == "calendar" else "") for key in ("units", "calendar")):
                raise ValueError("Time units/calendar differ; establish their mapping before this measurement")
            indices = []
            for value in b[:]:
                matched = np.flatnonzero(np.asarray(a[:]) == value)
                if len(matched) != 1:
                    raise ValueError("Target time is not uniquely represented in source")
                indices.append(int(matched[0]))
        else:
            indices = [None]
        # Shift longitude intervals by whole turns to the target's branch.
        sb, tb = sg[1][1], tg[1][1]
        center = float(tb.mean())
        sb = sb + (360*np.round((center-sb.mean(axis=1))/360))[:, None]
        def overlap(s, t):
            return np.maximum(0., np.minimum(t[:, None, 1], s[None, :, 1]) - np.maximum(t[:, None, 0], s[None, :, 0]))
        slat, tlat = np.sin(np.deg2rad(sg[0][1])), np.sin(np.deg2rad(tg[0][1]))
        wlat, wlon = overlap(slat, tlat), overlap(np.deg2rad(sb), np.deg2rad(tb))
        areas = np.outer(np.diff(tlat).ravel(), np.diff(np.deg2rad(tb)).ravel())
        if np.any(areas <= 0):
            raise ValueError("Target cell areas must be positive")
        def slab(field, coordinate_grid, time_axis, index):
            selection = [slice(None)] * field.ndim
            if time_axis:
                selection[field.dimensions.index(time_axis[0])] = index
            remaining_dims = [d for d in field.dimensions if d not in time_axis]
            return np.ma.asarray(field[tuple(selection)], dtype=float).transpose(
                remaining_dims.index(coordinate_grid[0][0]), remaining_dims.index(coordinate_grid[1][0]))
        samples = []
        for target_index, source_index in enumerate(indices):
            a, b = slab(src, sg, st, source_index), slab(dst, tg, tt, target_index)
            valid = ~np.ma.getmaskarray(a) & np.isfinite(a.filled(np.nan))
            coverage = wlat @ valid.astype(float) @ wlon.T
            common = np.isclose(coverage, areas, rtol=1e-10, atol=1e-16) & ~np.ma.getmaskarray(b) & np.isfinite(b.filled(np.nan))
            contribution = wlat @ np.where(valid, a.filled(0.), 0.) @ wlon.T
            before = float(contribution[common].sum())
            after = float((b.filled(0.)*areas)[common].sum())
            samples.append({"target_time_index": target_index, "source_time_index": source_index,
                "source_integral": before, "target_integral": after, "absolute_error": abs(after-before),
                "relative_error": abs(after-before)/abs(before) if before else None,
                "compared_cells": int(common.sum()), "excluded_cells": int(common.size-common.sum())})
        return {"method": "Independent overlaps in sin(latitude) and longitude radians; integrals on the unit sphere.",
                "integral_units": str(getattr(src, "units", ""))+" sr", "samples": samples,
                "scope": "Fully source-covered, valid target cells; excluded cells require separate missing/coverage checks. No tolerance is silently imposed."}


def inspect_netcdf(path) -> dict:
    import netCDF4
    import numpy as np

    issues, variables = [], {}
    with netCDF4.Dataset(path) as dataset:
        conventions = str(getattr(dataset, "Conventions", ""))
        cf = bool(re.search(r"\bCF-1\.(?:[7-9]|1[0-9])\b", conventions))
        remaining = 1_000_000  # Metadata checks must not load an unbounded field.
        for name, variable in sorted(dataset.variables.items(), key=lambda item: item[1].size):
            attrs = {key: getattr(variable, key) for key in variable.ncattrs()}
            report = {"dimensions": list(variable.dimensions), "shape": list(variable.shape),
                      "dtype": str(variable.dtype), "attributes": {key: np.asarray(attrs[key]).tolist()
                          for key in ("units", "calendar", "standard_name", "bounds", "cell_methods", "actual_range",
                                      "_FillValue", "missing_value", "coordinate_defines", "delta_t", "avg_period") if key in attrs}}
            variables[name] = report
            def issue(code, message, recommendation):
                issues.append({"code": code, "message": f"{name}: {message}", "recommendation": recommendation})
            if np.issubdtype(variable.dtype, np.number) and variable.size <= remaining:
                remaining -= variable.size
                data = np.ma.asarray(variable[:])
                count = int(data.count())
                report.update(valid_count=count, missing_count=int(data.size-count))
                if variable.dimensions == (name,) and data.size <= 128:
                    report["coordinate_values"] = data.tolist()
                if count:
                    observed = np.asarray([data.min(), data.max()])
                    report["measured_range"] = observed.tolist()
                if cf and "actual_range" in attrs and (not count or not np.array_equal(np.asarray(attrs["actual_range"]), observed)):
                    issue("cf_actual_range", f"actual_range={np.asarray(attrs['actual_range']).tolist()} differs from actual unpacked values {report.get('measured_range')}; CF 1.8 §2.5.1 requires equality.",
                          "Recompute actual_range from the final stored/unpacked values at their precision, or remove this optional attribute. Update dependent reports/hashes.")
                if cf and variable.dimensions == (name,) and count != data.size:
                    issue("cf_coordinate_missing", "coordinate variable contains missing values.", "Repair the coordinate values from the requested grid; do not fill missing scientific data with invented values.")
            if cf and "bounds" in attrs:
                bounds = dataset.variables.get(str(attrs["bounds"]))
                if bounds is None or bounds.dimensions[:-1] != variable.dimensions or len(bounds.dimensions) != len(variable.dimensions)+1:
                    issue("cf_bounds_reference", "bounds variable is absent or has incompatible dimensions.", "Write the referenced boundary variable with coordinate dimensions followed by a vertex dimension.")
                elif any(key in bounds.ncattrs() and key in attrs and str(getattr(bounds, key)) != str(attrs[key]) for key in ("units", "calendar", "standard_name", "axis", "positive")):
                    issue("cf_bounds_metadata", "bounds metadata conflicts with its coordinate.", "Remove redundant bounds attributes or make them agree with the coordinate (CF §7.1); absent duplicate units are legal.")
            if cf:
                for coordinate, method in re.findall(r"(\w+)\s*:\s*(\w+)", str(attrs.get("cell_methods", ""))):
                    axis = dataset.variables.get(coordinate)
                    if (axis is not None and method == "sum" and " since " in str(getattr(axis, "units", ""))
                            and not getattr(axis, "bounds", None) and not getattr(axis, "climatology", None)):
                        issue("cf_accumulation_bounds", f"{coordinate}: sum has no accumulation interval bounds (CF §7.3).",
                              "Derive time bounds from the source's documented accumulation period and coordinate definition, preserving original time values/units/calendar. Do not guess an undocumented period.")
        return {"status": "fail" if issues else "measured", "issues": issues, "Conventions": conventions,
                "dimensions": {name: len(value) for name, value in dataset.dimensions.items()}, "variables": variables,
                "scope": "Native NetCDF read-back, bounded ranges and selected CF metadata invariants; not a full CF checker or conservation proof.",
                "reference": "https://cfconventions.org/Data/cf-conventions/cf-conventions-1.8/cf-conventions.html"}
