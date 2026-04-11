#!/usr/bin/env python3
"""
Filter, classify, and incrementally update the Medicaid data catalog.

Usage:
  First run (no existing catalog):
    python catalog_clean.py mbes-catalog.json -o medicaid-catalog/catalog_classified.json

  Incremental update (preserves notes, relationships, category overrides):
    python catalog_clean.py mbes-catalog.json --existing medicaid-catalog/catalog_classified.json -o medicaid-catalog/catalog_classified.json

  Dry run (show what would change without writing):
    python catalog_clean.py mbes-catalog.json --existing medicaid-catalog/catalog_classified.json --dry-run
"""

import json
import re
import sys
import argparse
from collections import Counter

# --- Periodicity mapping ---
PERIODICITY_MAP = {
    "R/P1M": "Monthly",
    "R/P3M": "Quarterly",
    "R/P1Y": "Annual",
    "R/P10Y": "Decennial",
    "R/PT1S": "Irregular",
    "": "Unknown",
}

# --- Noise filter ---
NOISE_PATTERNS = [
    r'^(prodAuto_|devAuto_|featAuto_|implAuto_)',
    r'^[Ss]corecard',
    r'^CoreS[Ee]t\s',
    r'^Product Data for Newly Reported Drugs.*\d{4}',
    r'^(Monthly Enrollment - Test|Test UTF)',
]
NOISE_EXACT = {'category_tiles'}

def is_noise(title):
    t = title.strip()
    if t in NOISE_EXACT:
        return True
    return any(re.match(p, t) for p in NOISE_PATTERNS)

# --- Classification rules ---
RULES = [
    (lambda t, d, kw, th: 'Enrollment' in th and 'Unwinding' not in th and 'Quality' not in th, "Enrollment"),
    (lambda t, d, kw, th: t == 'PI dataset', "Enrollment"),
    (lambda t, d, kw, th: 'CHIP Enrollment' in t or 'CHIP Applications' in t or 'Eligibility Determinations' in t, "Enrollment"),
    (lambda t, d, kw, th: 'Medicaid Enrollment' in t and 'New Adult' in t, "Enrollment"),
    (lambda t, d, kw, th: t.startswith('Share of Medicaid Enrollees'), "Enrollment"),
    (lambda t, d, kw, th: 'Dual Status' in t, "Enrollment"),
    (lambda t, d, kw, th: 'Program Information for Medicaid' in t, "Enrollment"),
    (lambda t, d, kw, th: 'Separate CHIP Enrollment' in t, "Enrollment"),
    (lambda t, d, kw, th: 'State Medicaid and CHIP Applications' in t, "Enrollment"),

    (lambda t, d, kw, th: any(x in t for x in ['Eligibility Levels', 'Presumptive Eligibility', 'Continuous Eligibility', 'Express Lane', '1915(c) waiver']), "Eligibility & Enrollment Policy"),
    (lambda t, d, kw, th: 'Eligibility Processing' in t, "Eligibility & Enrollment Policy"),
    (lambda t, d, kw, th: 'Major Eligibility Group' in t, "Eligibility & Enrollment Policy"),
    (lambda t, d, kw, th: 'Benefit Package for Medicaid' in t, "Eligibility & Enrollment Policy"),
    (lambda t, d, kw, th: 'Eligibility' in th and 'Enrollment' not in th, "Eligibility & Enrollment Policy"),

    (lambda t, d, kw, th: 'CMS-64' in t or 'Financial Management' in t, "Expenditures"),
    (lambda t, d, kw, th: 'DSH' in t or 'Disproportionate Share' in t, "Expenditures"),
    (lambda t, d, kw, th: 'MLR Summary' in t, "Expenditures"),

    (lambda t, d, kw, th: 'State Drug Utilization' in th or 'Drug Pricing' in th or 'National Average Drug Acquisition' in th, "Drug Pricing & Utilization"),
    (lambda t, d, kw, th: any(x in t for x in ['NADAC', 'SDUD', 'State Drug Utilization', 'ACA Federal Upper Limits', 'Drug AMP', 'Clotting Factor', 'Exclusive Pediatric', 'Drug Manufacturer Contact', 'Drug Rebate Program State Contact', 'Drug Products in the Medicaid', 'Blood Disorder Treatment', 'Division of Pharmacy', 'First Time NADAC', 'NADAC Comparison']), "Drug Pricing & Utilization"),
    (lambda t, d, kw, th: 'Product Data for Newly Reported Drugs' in t, "Drug Pricing & Utilization"),

    (lambda t, d, kw, th: 'Quality' in th, "Quality Measures"),
    (lambda t, d, kw, th: 'Child and Adult Health Care Quality' in t, "Quality Measures"),
    (lambda t, d, kw, th: 'NAM CAHPS' in t, "Quality Measures"),
    (lambda t, d, kw, th: 'well-child visit' in t.lower(), "Quality Measures"),

    (lambda t, d, kw, th: 'Managed Care Programs' in t or 'Managed Care Features' in t, "Managed Care Programs"),
    (lambda t, d, kw, th: 'Managed Care Enrollment' in t, "Managed Care Programs"),
    (lambda t, d, kw, th: 'Managed Care Information' in t, "Managed Care Programs"),
    (lambda t, d, kw, th: 'MLTSS' in t or 'Managed Long Term' in t, "Managed Care Programs"),
    (lambda t, d, kw, th: 'Share of Medicaid Enrollees in Managed Care' in t, "Managed Care Programs"),

    (lambda t, d, kw, th: 'Unwinding' in th or 'Unwinding' in t, "Unwinding"),
    (lambda t, d, kw, th: any(x in t for x in ['CAA Reporting', 'Renewal Outcomes', 'Marketplace Medicaid Unwinding']), "Unwinding"),

    (lambda t, d, kw, th: any(x in t for x in ['Acute Care Services', 'Behavioral Health Services', 'Dental Services', 'Telehealth Services', 'Vaccination', 'Contraceptive Care', 'Health Screenings', 'COVID Testing', 'Respiratory Conditions', 'Blood Lead Screening']), "Service Utilization"),
    (lambda t, d, kw, th: 'mental health or SUD services' in t.lower(), "Service Utilization"),
    (lambda t, d, kw, th: t.startswith('Beneficiaries') and ('behavioral health' in t.lower() or 'physical health' in t.lower() or 'integrated care' in t.lower()), "Service Utilization"),

    (lambda t, d, kw, th: any(x in t for x in ['Pregnancy Outcomes', 'Perinatal Care', 'NAS per 1,000', 'SMM among', 'pregnant and postpartum', 'Prematurity and severe maternal']), "Maternal & Child Health"),
]

def classify(entry):
    title = entry['title']
    desc = entry.get('description', '')
    keywords = entry.get('keyword', [])
    themes = entry.get('theme', [])
    for test_fn, category in RULES:
        try:
            if test_fn(title, desc, keywords, themes):
                return category
        except:
            continue
    return "Other"


# --- Fields that are machine-derived and safe to overwrite ---
MACHINE_FIELDS = {'description', 'cadence', 'modified', 'download_url', 'format', 'keywords', 'theme'}

# --- Fields that may contain human edits and should be preserved ---
HUMAN_FIELDS = {'notes', 'relationships', 'category'}


def build_entry(raw):
    """Build a classified entry from a raw catalog record."""
    dists = raw.get('distribution', [])
    download_url = dists[0].get('downloadURL', '') if dists else ''
    fmt = dists[0].get('format', 'unknown') if dists else 'unknown'
    return {
        "title": raw['title'],
        "identifier": raw.get('identifier', ''),
        "description": raw.get('description', ''),
        "category": classify(raw),
        "cadence": PERIODICITY_MAP.get(raw.get('accrualPeriodicity', ''), 'Unknown'),
        "modified": raw.get('modified', ''),
        "download_url": download_url,
        "format": fmt,
        "keywords": raw.get('keyword', []),
        "theme": raw.get('theme', []),
        "notes": "",
        "relationships": [],
    }


def merge(existing, fresh):
    """
    Merge a fresh entry into an existing one.
    Preserves: notes, relationships, category (if manually changed).
    Updates: description, cadence, modified, download_url, format, keywords, theme.
    """
    merged = dict(existing)
    for field in MACHINE_FIELDS:
        merged[field] = fresh[field]
    # Preserve category only if the human changed it from what the classifier would assign.
    # If existing category matches what the classifier *previously* assigned, update it.
    # Heuristic: if fresh classifier agrees with existing, no conflict. If they differ,
    # keep existing (assume human override).
    if existing['category'] != fresh['category']:
        merged['_category_conflict'] = {
            'existing': existing['category'],
            'classifier': fresh['category'],
        }
        # Keep existing (human override wins)
    return merged


def main():
    parser = argparse.ArgumentParser(description='Filter, classify, and update Medicaid data catalog.')
    parser.add_argument('raw_catalog', help='Path to raw mbes-catalog.json from data.medicaid.gov')
    parser.add_argument('--existing', '-e', help='Path to existing catalog_classified.json (for incremental update)')
    parser.add_argument('-o', '--output', default='catalog_classified.json', help='Output path')
    parser.add_argument('--dry-run', action='store_true', help='Show changes without writing')
    args = parser.parse_args()

    # Load raw catalog
    with open(args.raw_catalog) as f:
        raw_data = json.load(f)
    if isinstance(raw_data, dict) and 'dataset' in raw_data:
        raw_data = raw_data['dataset']

    # Load existing catalog if provided
    existing_by_id = {}
    if args.existing:
        try:
            with open(args.existing) as f:
                existing = json.load(f)
            existing_by_id = {e['identifier']: e for e in existing}
            print(f"Loaded existing catalog: {len(existing_by_id)} entries", file=sys.stderr)
        except FileNotFoundError:
            print(f"No existing catalog at {args.existing}, starting fresh", file=sys.stderr)

    # Process raw entries
    results = []
    stats = Counter()

    for raw in raw_data:
        title = raw.get('title', '')
        uid = raw.get('identifier', '')

        if is_noise(title):
            stats['excluded_noise'] += 1
            continue

        fresh = build_entry(raw)

        if uid in existing_by_id:
            merged = merge(existing_by_id[uid], fresh)
            if '_category_conflict' in merged:
                stats['category_conflicts'] += 1
            stats['updated'] += 1
            results.append(merged)
            del existing_by_id[uid]  # mark as seen
        else:
            stats['new'] += 1
            results.append(fresh)

    # Anything left in existing_by_id was not in the fresh dump
    for uid, entry in existing_by_id.items():
        entry['_removed_from_source'] = True
        results.append(entry)
        stats['removed'] += 1

    results.sort(key=lambda x: (x.get('_removed_from_source', False), x['category'], x['title']))

    # Report
    cats = Counter(r['category'] for r in results if not r.get('_removed_from_source'))
    print(f"\n{'='*50}", file=sys.stderr)
    print(f"Results:", file=sys.stderr)
    print(f"  Noise excluded: {stats['excluded_noise']}", file=sys.stderr)
    print(f"  Updated (existing): {stats['updated']}", file=sys.stderr)
    print(f"  New datasets: {stats['new']}", file=sys.stderr)
    print(f"  Removed from source: {stats['removed']}", file=sys.stderr)
    print(f"  Category conflicts: {stats['category_conflicts']}", file=sys.stderr)
    print(f"\nCategories:", file=sys.stderr)
    for cat, count in sorted(cats.items()):
        print(f"  {cat}: {count}", file=sys.stderr)
    print(f"{'='*50}\n", file=sys.stderr)

    if stats['new']:
        print("NEW DATASETS (need notes):", file=sys.stderr)
        for r in results:
            if r['identifier'] not in {e['identifier'] for e in existing_by_id.values()} and not any(e['identifier'] == r['identifier'] for e in (existing if args.existing else [])):
                if not r.get('notes'):
                    print(f"  - {r['title']}", file=sys.stderr)

    if stats['category_conflicts']:
        print("\nCATEGORY CONFLICTS (kept your override, classifier disagrees):", file=sys.stderr)
        for r in results:
            if '_category_conflict' in r:
                c = r['_category_conflict']
                print(f"  - {r['title']}: yours={c['existing']}, classifier={c['classifier']}", file=sys.stderr)

    if stats['removed']:
        print("\nREMOVED FROM SOURCE (kept in catalog, flagged):", file=sys.stderr)
        for r in results:
            if r.get('_removed_from_source'):
                print(f"  - {r['title']}", file=sys.stderr)

    if args.dry_run:
        print("\nDry run — no file written.", file=sys.stderr)
        return

    with open(args.output, 'w') as f:
        json.dump(results, f, indent=2)
    print(f"Wrote {len(results)} entries to {args.output}", file=sys.stderr)


if __name__ == '__main__':
    main()
