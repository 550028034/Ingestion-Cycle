#!/usr/bin/env python3
"""Update SOP_JSON tags from SOP_Metadata_All.xlsx.

The Tags worksheet is the authority:
  Column A: Tag
  Column B: SOP Count
  Column C: comma-separated SOP IDs

Matching is by SOP ID. Tags with SOP Count 0 are kept in approved_tags.json
but are not assigned to any SOP record.
"""
from __future__ import annotations
import argparse, json, os, re, tempfile
from collections import defaultdict
from pathlib import Path
from openpyxl import load_workbook

DASH_VALUES = {'', '-', '—', '–', 'none', 'n/a', 'na'}

def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.write('\n')
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)

def clean(value) -> str:
    return str(value or '').strip()

def dedupe(values: list[str]) -> list[str]:
    out, seen = [], set()
    for value in values:
        key = value.casefold()
        if value and key not in seen:
            seen.add(key); out.append(value)
    return out

def parse_tags_sheet(xlsx: Path) -> tuple[dict[str, list[str]], list[str]]:
    wb = load_workbook(xlsx, read_only=True, data_only=True)
    if 'Tags' not in wb.sheetnames:
        raise ValueError(f"Workbook has no 'Tags' worksheet. Found: {wb.sheetnames}")
    ws = wb['Tags']
    headers = [clean(c.value) for c in next(ws.iter_rows(min_row=1, max_row=1))]
    expected = ['Tag (from Tags.xlsx)', 'SOP Count', 'SOP IDs']
    if headers[:3] != expected:
        raise ValueError(f'Unexpected Tags headers: {headers[:3]}')

    by_sop: dict[str, list[str]] = defaultdict(list)
    approved: list[str] = []
    for tag_cell, count_cell, ids_cell in ws.iter_rows(min_row=2, max_col=3, values_only=True):
        tag = clean(tag_cell)
        if not tag or tag.casefold() in DASH_VALUES:
            continue
        approved.append(tag)
        ids_text = clean(ids_cell)
        if not ids_text or ids_text.casefold() in DASH_VALUES:
            continue
        ids = [x.strip().upper() for x in re.split(r'[,;\n]+', ids_text) if x.strip()]
        for sop_id in ids:
            if not re.fullmatch(r'SOP-\d+', sop_id):
                raise ValueError(f'Invalid SOP ID {sop_id!r} for tag {tag!r}')
            by_sop[sop_id].append(tag)
    wb.close()
    return {k: dedupe(v) for k, v in by_sop.items()}, dedupe(approved)

def main() -> None:
    p = argparse.ArgumentParser(description='Merge Tags worksheet into SOP_JSON.json')
    p.add_argument('--workbook', default='SOP_Metadata_All.xlsx')
    p.add_argument('--input-json', default='SOP_JSON.json')
    p.add_argument('--output-json', default='SOP_JSON_with_Tags.json')
    p.add_argument('--approved-tags', default='approved_tags.json')
    p.add_argument('--in-place', action='store_true', help='Safely replace input JSON after creating .bak')
    args = p.parse_args()

    workbook, source = Path(args.workbook), Path(args.input_json)
    catalog = json.loads(source.read_text(encoding='utf-8-sig'))
    if not isinstance(catalog, list):
        raise ValueError('SOP_JSON must be a top-level JSON array.')

    tags_by_sop, approved_tags = parse_tags_sheet(workbook)
    ids_in_catalog = {clean(item.get('sop_id')).upper() for item in catalog}
    ids_in_sheet = set(tags_by_sop)

    updated = 0
    for item in catalog:
        sop_id = clean(item.get('sop_id')).upper()
        new_tags = tags_by_sop.get(sop_id, [])
        if item.get('tags') != new_tags:
            updated += 1
        item['tags'] = new_tags

    missing_in_catalog = sorted(ids_in_sheet - ids_in_catalog)
    catalog_without_mapping = sorted(ids_in_catalog - ids_in_sheet)

    output = source if args.in_place else Path(args.output_json)
    if args.in_place:
        backup = source.with_suffix(source.suffix + '.bak')
        backup.write_bytes(source.read_bytes())
    atomic_json(output, catalog)
    atomic_json(Path(args.approved_tags), approved_tags)

    report = {
        'status': 'complete',
        'catalog_records': len(catalog),
        'records_changed': updated,
        'approved_tag_count': len(approved_tags),
        'assigned_tag_count': sum(len(x.get('tags', [])) for x in catalog),
        'sop_ids_in_tags_sheet_not_in_catalog': missing_in_catalog,
        'catalog_sop_ids_without_assigned_tag': catalog_without_mapping,
        'output_json': str(output.resolve()),
        'approved_tags_json': str(Path(args.approved_tags).resolve())
    }
    print(json.dumps(report, indent=2))

if __name__ == '__main__':
    main()
