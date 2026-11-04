"""Configuration observations derived from official, versioned Registries."""
from __future__ import annotations

import hashlib
import math
import re
import shlex
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET


def validate_fortran_namelist(content: str) -> str | None:
    """Reject wrappers/trailing prose that permissive namelist readers ignore.

    Tokenize quoted literals and comments before group markers: paths containing
    '/', doubled quotes and inline groups must keep their Fortran meaning.
    Field syntax/types are subsequently checked by the native reader/schema.
    """
    tokens = re.finditer(r"'(?:(?:'')|[^'])*'|\"(?:(?:\"\")|[^\"])*\"|![^\n]*|&[A-Za-z][A-Za-z0-9_]*|/|\s+|.", content)
    active, seen, line = None, False, 1
    for match in tokens:
        token = match[0]
        token_line = line
        line += token.count("\n")
        if token.isspace() or token.startswith("!"):
            continue
        if token == "/" or token.lower() == "&end":
            if active is None:
                return f"line {token_line}: namelist terminator has no open group"
            active = None
        elif token.startswith("&"):
            if active is not None:
                return f"line {token_line}: group {active} is not closed before {token[1:]}"
            active, seen = token[1:], True
        elif active is None:
            offending = content[match.start():].splitlines()[0][:120]
            return (f"line {token_line}: unexpected {offending!r} outside a namelist group. "
                    "Fortran comments start with !; convert the entire foreign comment block to ! lines "
                    "and remove XML/Markdown wrappers, preserving all namelist assignments.")
    if active is not None:
        return f"namelist group {active} is not terminated"
    return None if seen else "no namelist group beginning with '&' was found"


async def mpas_atmosphere_schema(state, software: dict, filenames) -> dict:
    """Share the same versioned definitions between the writer and reviewer.

    This inspects the Registry, not a compiled MPAS executable. Preprocessor
    alternatives remain a union of available definitions, not proof that a
    particular build enables all packages.
    """
    if (not isinstance(software, dict) or "mpas" not in str(software.get("name") or "").lower()
            or not any(Path(name).name in {"namelist.atmosphere", "streams.atmosphere"} for name in filenames)):
        return {}
    import asyncio
    import httpx
    from shared.tools.web import _http_get

    version = str(software.get("version") or "").strip().removeprefix("v")
    assumed = not version or version.lower() in {"unknown", "unspecified"}
    if assumed:
        version = "8.0.0"
    # Major/minor versions select the corresponding .0 release, not master.
    release = re.fullmatch(r"(\d+(?:\.\d+){0,2})(?:\s+\([^)]*\))?", version)
    if not release:
        raise ValueError(f"MPAS version must identify a release, observed {version!r}")
    version = ".".join((release[1].split(".") + ["0", "0"])[:3])
    base = f"https://raw.githubusercontent.com/MPAS-Dev/MPAS-Model/v{version}/src/"
    cache = state.root / ".data_node_work" / "review_sources"
    async def reference(relative):
        url = base + relative
        path = cache / f"MPAS-Atmosphere-{version}-{Path(relative).name}"
        if path.is_file():
            return path.read_bytes()
        try:
            async with asyncio.timeout(30):
                response = await _http_get(url)
            response.raise_for_status()
            raw = response.content
        except httpx.HTTPError as exc:
            raise OSError(f"Cannot retrieve {url}: {exc}") from exc
        if len(raw) > 16 * 1024 * 1024:
            raise ValueError("MPAS reference exceeds the bounded reference size")
        return raw
    url = base + "core_atmosphere/Registry.xml"
    raw = await reference("core_atmosphere/Registry.xml")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError(f"Invalid MPAS Registry XML: {exc}") from exc
    if root.tag != "registry" or root.get("core") != "atmosphere" or root.get("version") != version:
        raise ValueError("MPAS Registry core/version does not match the selected release")
    groups = {record.get("name").casefold(): {option.get("name").casefold(): {
        "type": option.get("type"), "default": option.get("default_value"),
        "units": option.get("units"), "possible_values": option.get("possible_values")}
        for option in record.findall("nml_option") if option.get("name")}
        for record in root.findall("nml_record") if record.get("name")}
    streams = {item.get("name"): {
        **{key: value for key, value in item.attrib.items() if key not in {"immutable", "runtime_format"}},
        "element": "immutable_stream" if item.get("immutable") == "true" else "stream",
        "members": [] if item.get("immutable") == "true" else [dict(child.attrib, kind=child.tag) for child in item]}
               for item in root.findall("streams/stream") if item.get("name")}
    if not groups or not streams:
        raise ValueError("MPAS Registry has no namelist or stream definitions")
    # Registry metadata alone does not specify read-error behavior. Supply the
    # actual release's reader generator, not a model's inferred required groups.
    reader = await reference("tools/registry/gen_inc.c")
    source = reader.decode("utf-8")
    start = source.find("int parse_namelist_records_from_registry(")
    end = source.find("\nint ", start + 1)
    reader_lines = source[start:end if end != -1 else None].splitlines() if start >= 0 else []
    evidence = "\n".join(line.strip() for line in reader_lines if re.search(
        r'default_value|:: %s =|read\(unitNumber|ierr [<>]=? 0|following values|Error while reading', line))[:6000]
    if not evidence:
        raise ValueError("MPAS namelist reader definition could not be located in the selected release")
    stream_reader = await reference("framework/xml_stream_parser.c")
    stream_excerpt = "\n".join(dict.fromkeys(line.strip() for line in stream_reader.decode("utf-8").splitlines()
        if re.search(r'handle changes to immutable|ezxml_child\(stream(?:s|_xml),|interval_(?:in|out) = ezxml_attr|stream_mgr_add_(?:field|pool|alarm)_c\(', line)))[:6000]
    if not stream_excerpt:
        raise ValueError("MPAS stream reader definition could not be located in the selected release")
    cache.mkdir(parents=True, exist_ok=True)
    for name, content in (("Registry.xml", raw), ("gen_inc.c", reader), ("xml_stream_parser.c", stream_reader)):
        path = cache / f"MPAS-Atmosphere-{version}-{name}"
        if not path.exists():
            path.write_bytes(content)
    reader_evidence = {"reference_url": base + "tools/registry/gen_inc.c",
        "reference_sha256": hashlib.sha256(reader).hexdigest(), "excerpt": evidence,
        "interpretation_rule": "Fortran group/key names are case-insensitive. in_defaults is not proof of a required runtime group. "
            "Assess omitted groups/options using the reader's default initializers and iostat branches; do not infer a fatal error from a missing group alone."}
    return {"reference_url": url, "reference_sha256": hashlib.sha256(raw).hexdigest(),
            "reference_version": version, "version_assumed": assumed, "groups": groups, "streams": streams,
            "namelist_reader": reader_evidence,
            "stream_reader": {"reference_url": base + "framework/xml_stream_parser.c",
                "reference_sha256": hashlib.sha256(stream_reader).hexdigest(), "excerpt": stream_excerpt},
            "stream_serialization": "element names the XML tag, not an attribute. members are child elements. "
                "Immutable fixes field membership, not filename/output_interval; runtime overrides are supported. "
                "Mutable streams may select declared fields or structures instead of copying the default member list; active output needs members.",
            "variables": {kind: sorted({item.get("name") for item in root.iter()
                if item.tag in tags and item.get("name")}) for kind, tags in
                (("fields", {"var", "var_array"}), ("structures", {"var_struct"}))},
            "scope": "Registry group/key/type and stream definitions; build-specific packages and runtime inputs are not verified."}


def inspect_mpas_configuration(text: str, filename: str, schema: dict) -> dict:
    """Check actual configuration bytes, never model-declared PASS claims."""
    issues, observations = [], {}
    def issue(message):
        issues.append({"code": "mpas_configuration_schema", "file": filename, "message": message,
            "recommendation": "Correct the identified configuration against " + schema["reference_url"]
                + "; preserve caller values and unrelated valid settings. Do not copy Registry defaults over requested values."})
    try:
        if Path(filename).name == "namelist.atmosphere":
            import f90nml

            syntax_error = validate_fortran_namelist(text)
            if syntax_error:
                issue(syntax_error)
            parsed = f90nml.reads(text)
            owners = {key: group for group, fields in schema["groups"].items() for key in fields}
            for group, fields in parsed.items():
                if group not in schema["groups"]:
                    issue(f"Unknown namelist group &{group}.")
                for key, value in fields.items():
                    owner = owners.get(key)
                    if owner != group:
                        issue(f"{group}.{key}: " + (f"declared in &{owner}." if owner else "not declared in this Registry."))
                        continue
                    kind = schema["groups"][group][key]["type"]
                    valid = {"integer": type(value) is int, "real": type(value) in (int, float),
                             "logical": type(value) is bool, "character": isinstance(value, str)}
                    if kind in valid and not valid[kind]:
                        issue(f"{group}.{key} requires a scalar {kind}, observed {value!r}.")
            observations["namelist"] = dict(parsed)
            observations["registry_field_definitions"] = {
                group: {key: schema["groups"][group][key] for key in fields if key in schema["groups"][group]}
                for group, fields in parsed.items() if group in schema["groups"]}
            observations["omitted_groups"] = sorted(schema["groups"].keys() - parsed.keys())
            observations["namelist_reader"] = schema.get("namelist_reader", {})
        elif Path(filename).name == "streams.atmosphere":
            root = ET.fromstring(text)
            if root.tag != "streams":
                issue("The streams document must have a <streams> root.")
            names = [item.get("name") for item in root]
            observations["streams"] = []
            observations["stream_reader"] = schema.get("stream_reader", {})
            observations["stream_serialization"] = schema.get("stream_serialization", "")
            observations["registry_stream_definitions"] = {
                name: {key: value for key, value in definition.items() if key != "members"}
                for name, definition in schema["streams"].items()}
            used_members = {member.get("name") for stream in root for member in stream}
            observations["registry_member_definitions"] = {
                kind: sorted(used_members.intersection(names)) for kind, names in schema["variables"].items()}
            member_tags = {"var", "var_array", "var_struct", "stream", "file"}
            for stream in root:
                name = stream.get("name")
                definition = schema["streams"].get(name, {})
                if not name or names.count(name) > 1:
                    issue(f"Missing or duplicate stream name: {name!r}.")
                if stream.tag not in {"stream", "immutable_stream"}:
                    issue(f"Unsupported stream element <{stream.tag}>.")
                if stream.tag == "immutable_stream" and definition.get("element") != "immutable_stream":
                    issue(f"{name!r} is not a Registry-defined immutable stream; use a mutable stream with explicit contents.")
                if definition.get("element") == "immutable_stream":
                    if stream.tag != "immutable_stream" or len(stream):
                        issue(f"{name!r} must use <immutable_stream> without field children; its fields are fixed by the Registry.")
                    if (stream.get("type") != "none" and not set((stream.get("type") or "").split(";"))
                            <= set(definition.get("type", "").split(";"))):
                        issue(f"Immutable stream {name!r} enables a direction absent from Registry type {definition.get('type')!r}.")
                for key in ("type", "filename_template"):
                    if not stream.get(key):
                        issue(f"Stream {name!r} is missing required attribute {key}.")
                if stream.get("type") and stream.get("type") not in {"input", "output", "input;output", "none"}:
                    issue(f"Stream {name!r} has unsupported type {stream.get('type')!r}.")
                for direction in ("input", "output"):
                    if direction in (stream.get("type") or "").split(";") and not stream.get(direction + "_interval"):
                        issue(f"Stream {name!r} is missing {direction}_interval.")
                if member_tags & stream.attrib.keys():
                    issue(f"Stream {name!r} declares field members as attributes. Use named child elements from the Registry; "
                          "attributes such as var_struct='all' do not select output fields.")
                if (stream.tag == "stream" and "output" in (stream.get("type") or "").split(";")
                        and stream.get("output_interval") not in {None, "none"} and not len(stream)):
                    issue(f"Mutable output stream {name!r} has no field members. Select explicit Registry members or a supplied field-list file; an empty active stream cannot deliver history fields.")
                for member in stream:
                    if member.tag not in member_tags:
                        issue(f"Stream {name!r} has unsupported member element <{member.tag}>.")
                    if member.tag in {"var", "var_array", "var_struct"}:
                        # MPAS sends var and var_array to the same add_field
                        # reader; var_struct uses the separate pool reader.
                        kind = "structures" if member.tag == "var_struct" else "fields"
                        if member.get("name") not in schema["variables"][kind]:
                            issue(f"Stream {name!r} references undeclared {member.tag} {member.get('name')!r}.")
                    if member.tag == "stream" and member.get("name") not in set(names) | schema["streams"].keys():
                        issue(f"Stream {name!r} references unknown stream {member.get('name')!r}.")
                observations["streams"].append(dict(stream.attrib, element=stream.tag,
                    members=[dict(item.attrib, kind=item.tag) for item in stream]))
    except (ET.ParseError, ValueError, TypeError, KeyError) as exc:
        issue(f"Configuration parsing failed: {exc}")
    return {"status": "fail" if issues else "pass", "issues": issues, "observations": observations,
            **{key: schema[key] for key in ("reference_url", "reference_sha256", "reference_version", "version_assumed", "scope")}}


def inspect_wrf_pair(input_text: str, wps_text: str, registry: str) -> dict:
    import f90nml

    issues = []
    def issue(message, file="namelist.input"):
        issues.append({"code": "wrf_namelist_schema", "file": file, "message": message,
                       "recommendation": "Correct the named field using the cited Registry and WPS dates; preserve frozen domain parameters."})
    parsed = []
    for name, text in (("namelist.input", input_text), ("namelist.wps", wps_text)):
        try:
            parsed.append(f90nml.reads(text))
        except (ValueError, TypeError, KeyError) as exc:
            issue(f"Native namelist parsing failed: {exc}", name)
    if issues:
        return {"status": "fail", "issues": issues}
    config, wps = parsed
    schema = {}
    for line in registry.splitlines():
        if not line.lstrip().startswith("rconfig "):
            continue
        fields = shlex.split(line, comments=True)
        if len(fields) >= 6:
            schema[fields[2].lower()] = dict(zip(("type", "group", "count", "default"),
                (fields[1], fields[3], fields[4], fields[5])))
    if len(schema) < 100:
        raise ValueError("Reference is not a complete WRF Registry.EM_COMMON file")
    unknown, effective = [], {}
    domains = int(config.get("domains", {}).get("max_dom", 1))
    if not 1 <= domains <= 100:
        raise ValueError("max_dom must be between 1 and 100 for this inspector")
    def values(value):
        return value if isinstance(value, list) else [value]
    wps_domains = wps.get("share", {}).get("max_dom")
    if type(wps_domains) is not int or not 1 <= wps_domains <= 100:
        issue("WPS max_dom must be an integer between 1 and 100.", "namelist.wps")
    elif wps_domains != domains:
        issue(f"WRF max_dom={domains} differs from WPS max_dom={wps.get('share', {}).get('max_dom')}.")
    for group, fields in config.items():
        for key, value in fields.items():
            rule = schema.get(key)
            if not rule:
                unknown.append(f"{group}.{key}")
                continue  # Other Registry includes may declare this key.
            if rule["group"] != "namelist," + group:
                issue(f"{group}.{key} is declared as {rule['group']}, not in &{group}.")
                continue
            items = values(value)
            if rule["count"].isdigit() and len(items) > int(rule["count"]):
                issue(f"{group}.{key} accepts {rule['count']} value(s), observed {len(items)}.")
            kind = rule["type"].lower()
            valid = {"integer": lambda x: type(x) is int, "real": lambda x: type(x) in (int, float),
                     "logical": lambda x: type(x) is bool, "character": lambda x: isinstance(x, str)}.get(kind)
            if valid and any(x is not None and not valid(x) for x in items):
                issue(f"{group}.{key} must contain {kind} values, observed {items!r}.")
    # Missing indexed values retain Registry defaults; Fortran does not repeat
    # the first value. Report effective dates, including explicitly indexed input.
    times = config.get("time_control", {})
    for prefix in ("start", "end"):
        fields = []
        for part in ("year", "month", "day", "hour", "minute", "second"):
            key = prefix + "_" + part
            rule = schema[key]
            actual = values(times.get(key, []))
            start = (times.start_index.get(key) or [1])[0] if hasattr(times, "start_index") else 1
            expanded = [int(rule["default"])] * domains
            for i, value in enumerate(actual, start=start - 1):
                if 0 <= i < domains and value is not None:
                    expanded[i] = value
            fields.append(expanded)
        dates = ["%04d-%02d-%02d_%02d:%02d:%02d" % tuple(field[i] for field in fields) for i in range(domains)]
        effective[prefix + "_date"] = dates
    # WPS may cover a wider window; nests normally need only their initial
    # instant in WPS. Comparing all end dates for equality rejects legal nests.
    wps_dates = {key: values(wps.get("share", {}).get(key, [])) for key in effective}
    if all(len(items) >= domains for items in wps_dates.values()):
        for domain in range(domains):
            dates = []
            for owner, fields in (("namelist.input", effective), ("namelist.wps", wps_dates)):
                try:
                    dates.extend(datetime.strptime(fields[key][domain], "%Y-%m-%d_%H:%M:%S")
                                 for key in ("start_date", "end_date"))
                except (ValueError, TypeError) as exc:
                    issue(f"d{domain+1:02d} invalid date: {exc}", owner)
            if len(dates) != 4:
                continue
            start, end, wps_start, wps_end = dates
            if wps_end < wps_start:
                issue(f"WPS d{domain+1:02d} end_date precedes start_date.", "namelist.wps")
                continue
            if end < start or not wps_start <= start <= wps_end or (domain == 0 and end > wps_end):
                issue(f"Effective WRF d{domain+1:02d} dates {start}..{end} are invalid or outside WPS coverage "
                      f"{wps_start}..{wps_end}; omitted domain entries retain Registry defaults.")
    else:
        unknown.append("WPS dates require resolving component/indexed namelist fields")
    grid, wrf_grid, placement = wps.get("geogrid", {}), config.get("domains", {}), []
    for label, owner, topology in (("WPS", "namelist.wps", grid), ("WRF", "namelist.input", wrf_grid)):
        for child in range(1, domains):
            needed = ("parent_id", "parent_grid_ratio", "e_we", "e_sn", "i_parent_start", "j_parent_start")
            if any(len(values(topology.get(key, []))) <= child for key in needed):
                issue(f"{label} d{child+1:02d} omits a parent/extent/placement entry.", owner)
                continue
            if any(type(value) is not int for key in needed for value in values(topology[key])):
                issue(f"{label} parent/extent/placement entries must be integers.", owner)
                continue
            parent, ratio = values(topology["parent_id"])[child] - 1, values(topology["parent_grid_ratio"])[child]
            if not isinstance(ratio, int) or ratio <= 0 or not 0 <= parent < child:
                issue(f"{label} d{child+1:02d} has invalid parent_id or parent_grid_ratio.", owner)
                continue
            for extent, start in (("e_we", "i_parent_start"), ("e_sn", "j_parent_start")):
                sizes, starts = values(topology[extent]), values(topology[start])
                span = (sizes[child] - 1) / ratio
                if (sizes[child] - 1) % ratio or starts[child] < 1 or starts[child] + span > sizes[parent]:
                    issue(f"{label} d{child+1:02d} {extent} spans parent indices {starts[child]}..{starts[child]+span}; "
                          f"must fit 1..{sizes[parent]} with (child_extent-1) divisible by ratio {ratio}.", owner)
                if label != "WPS":
                    continue
                centered_start = (sizes[parent] + 1 - span) / 2
                nearest_starts = sorted({math.floor(centered_start), math.ceil(centered_start)})
                minimum_offset = min(abs(value-centered_start) for value in nearest_starts)
                placement.append({"domain": child+1, "axis": extent, "parent_start": starts[child],
                    "parent_end": starts[child]+span,
                    "center_offset_parent_cells": starts[child]-centered_start,
                    "exact_centering_parent_start": centered_start,
                    "nearest_integer_parent_starts": [value for value in nearest_starts if abs(value-centered_start) == minimum_offset],
                    "minimum_center_offset_parent_cells": minimum_offset,
                    "achieves_nearest_center": abs(starts[child]-centered_start) == minimum_offset})
    # Repair an invalid configuration at its owner before reconciling peers.
    # Otherwise a valid input is repeatedly changed to match an invalid WPS.
    if not issues:
        for key in ("e_we", "e_sn", "parent_id", "parent_grid_ratio", "i_parent_start", "j_parent_start"):
            first = 1 if key == "parent_id" else 0  # Root conventions differ.
            if key in grid and key in wrf_grid and values(grid[key])[first:domains] != values(wrf_grid[key])[first:domains]:
                issue(f"Cross-file {key} differs: WPS={grid[key]!r}, WRF={wrf_grid[key]!r}; "
                      "reconcile with the frozen request, not by copying an unverified peer value.")
    return {"status": "fail" if issues else "measured", "issues": issues,
            "effective_dates": effective, "wps_dates": wps_dates, "max_dom": domains, "nest_placement": placement, "unresolved_keys": unknown,
            "index_convention": "Child span = (child_extent-1)/ratio in parent cells; exact centered start = "
                "(parent_extent+1-span)/2. Starts must be integers. A half-integer exact start makes zero offset "
                "impossible with the given extents and ratio; the two nearest starts give equal opposite offsets.",
            "time_scope": "Parent WPS coverage contains WRF interval; child WPS coverage contains initialization. Nudging/SST update may require further coverage.",
            "scope": "Registry-declared groups, types, cardinality and effective domain dates; unknown keys need other source definitions. No WRF simulation was run."}


async def inspect_wrf_files(state, files: dict[str, str], software: dict) -> dict:
    """Cache the primary reference once per run; never guess a versioned schema."""
    import asyncio
    import httpx
    from shared.tools.web import _http_get

    version = str(software.get("version") or "").strip()
    assumed = not version or version.lower() in {"unknown", "unspecified", "4.x"}
    if assumed:
        version = "4.6.1"
    version = version.removeprefix("v")
    if not re.fullmatch(r"\d+\.\d+(?:\.\d+)?", version):
        raise ValueError(f"Cannot select a versioned WRF Registry for {version!r}")
    url = f"https://raw.githubusercontent.com/wrf-model/WRF/v{version}/Registry/Registry.EM_COMMON"
    path = state.root / ".data_node_work" / "review_sources" / f"WRF-{version}-Registry.EM_COMMON"
    if not path.is_file():
        try:
            async with asyncio.timeout(30):
                response = await _http_get(url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise OSError(f"Cannot retrieve {url}: {exc}") from exc
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(response.content)
    result = {"reference_url": url, "reference_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
              "reference_version": version, "version_assumed": assumed, "files": []}
    for name, text in files.items():
        if Path(name).name.lower() != "namelist.input":
            continue
        sibling = str(Path(name).with_name("namelist.wps"))
        if sibling not in files:
            continue
        try:
            measurement = inspect_wrf_pair(text, files[sibling], path.read_text())
        except (ValueError, TypeError, KeyError) as exc:
            if "complete WRF Registry" in str(exc) or "for this inspector" in str(exc):
                raise
            measurement = {"status": "fail", "issues": [{"code": "wrf_namelist_parse",
                "message": f"Native namelist parsing/field evaluation failed: {exc}",
                "recommendation": "Correct the invalid namelist syntax or field value without changing the caller's frozen parameters."}]}
        for item in measurement["issues"]:
            item["file"] = sibling if item.get("file") == "namelist.wps" else name
        result["files"].append({"file": name, "compared_wps": sibling, **measurement})
    return result
