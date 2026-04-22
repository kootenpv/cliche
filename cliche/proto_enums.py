"""
Parse protobuf enum definitions from _pb2.py files.
Extracts enum values by parsing the serialized descriptor.
"""
import re
from pathlib import Path


def parse_pb2_enums(pb2_path: Path) -> dict[str, list[str]]:
    """
    Parse enum definitions from a _pb2.py file.

    Returns:
        Dict mapping enum name to list of value names.
        e.g., {'Exchange': ['NULL_EXCHANGE', 'bitmex', 'deribit', ...]}
    """
    if not pb2_path.exists():
        return {}

    content = pb2_path.read_text()

    # Get the binary descriptor blob
    match = re.search(r"AddSerializedFile\(b'(.+?)'\)", content, re.DOTALL)
    if not match:
        return {}

    blob_str = match.group(1)
    try:
        blob = bytes(blob_str, 'utf-8').decode('unicode_escape').encode('latin-1')
    except Exception:
        return {}

    # Get enum byte ranges from the markers at the bottom of the file
    # e.g., _EXCHANGE._serialized_start=1338
    ranges = {}
    for m in re.finditer(r'_([A-Z][A-Z0-9_]+)\._serialized_start=(\d+)', content):
        name = m.group(1)
        start = int(m.group(2))
        ranges[name] = {'start': start}

    for m in re.finditer(r'_([A-Z][A-Z0-9_]+)\._serialized_end=(\d+)', content):
        name = m.group(1)
        if name in ranges:
            ranges[name]['end'] = int(m.group(2))

    # Extract values for each enum
    enums = {}
    for enum_name_upper, r in ranges.items():
        if 'end' not in r:
            continue

        chunk = blob[r['start']:r['end']]

        # Extract readable strings from this chunk
        values = [s.decode('latin-1') for s in re.findall(rb'[A-Za-z_][A-Za-z0-9_]+', chunk)]

        if not values:
            continue

        # First string is the enum name itself (e.g., 'Exchange'), rest are values
        enum_name = values[0]
        enum_values = values[1:]

        enums[enum_name] = enum_values

    return enums


def build_enum_cache_from_dir(base_dir: Path) -> dict[str, list[str]]:
    """
    Build a complete enum cache for all _pb2.py files in a directory.

    Returns:
        Dict mapping enum name to list of values.
    """
    all_enums = {}

    for pb2_file in base_dir.rglob('*_pb2.py'):
        enums = parse_pb2_enums(pb2_file)
        all_enums.update(enums)

    return all_enums


if __name__ == '__main__':
    from pathlib import Path

    pb2_path = Path('/home/pascal/egoroot/caps/samrugman/solidsnake/protobuf/enums_pb2.py')
    enums = parse_pb2_enums(pb2_path)

    for name, values in enums.items():
        print(f"{name}: {len(values)} values")
        if len(values) <= 6:
            print(f"  {values}")
        else:
            print(f"  {values[:3]} ... {values[-3:]}")
