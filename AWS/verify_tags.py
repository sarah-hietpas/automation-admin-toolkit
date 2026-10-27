#!/usr/bin/env python3
"""
verify_tags.py

Read-only check that runs after tagging. It scans the same billable resources as
tag_billable_resources.py and reports every one that isn't tagged the way it
should be. Nothing is changed.

Keep it in the same folder as tag_billable_resources.py; it uses that script's
scanners, so both always cover the same services.

What it checks (defaults shown)
  Environment   present, spelled exactly "Environment", and one of:
                Production, qa, dev, test
  Platform      present, spelled exactly "Platform" (any value)

Each resource gets one of these results:
  OK                  everything above is right
  missing <key>       no Environment (or Platform) tag at all
  wrong key case      only a differently-capitalized key, e.g. environment=qa
  duplicate key       both Environment and environment, for example
  value case          right value, wrong capitalization, e.g. Environment=Dev
  unexpected value    a value outside the list, e.g. Environment=Staging

Examples
  python3 verify_tags.py --profile profileName --all-regions
  python3 verify_tags.py --profile profilename --regions us-east-1 us-east-2 --costs
  python3 verify_tags.py --allowed Production Dev --require Platform Owner
  python3 verify_tags.py --types "EC2 instance" "S3 bucket"

--costs also asks Cost Explorer how much of the account's spend still has no
Environment tag, by service, since --since (default: 7 days ago). That needs the
tag key activated as a cost allocation tag, and each Cost Explorer call costs $0.01.

A CSV with every resource and its result is saved to your home folder. The script
exits with code 1 when anything needs attention, so it can also run on a schedule.
"""

import argparse
import csv
import datetime as dt
import sys
from collections import Counter, defaultdict
from pathlib import Path

try:
    import tag_billable_resources as tbr
except ImportError:
    sys.exit("verify_tags.py needs tag_billable_resources.py in the same folder.")

from tag_billable_resources import BotoCoreError, ClientError, bold, dim, green, yellow, fmt_tags

DEFAULT_ALLOWED = ["Production", "qa", "dev", "test"]
ORDER_TAIL = ["wrong key case", "duplicate key", "value case", "unexpected value"]


def parse_args():
    p = argparse.ArgumentParser(
        description="Report billable AWS resources that aren't tagged correctly (read-only).",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument("--profile", metavar="NAME",
                   help="AWS CLI profile to use on your own computer (not needed in CloudShell)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--regions", nargs="+", metavar="REGION",
                   help="regions to scan (default: the profile's region, or CloudShell's current region)")
    g.add_argument("--all-regions", action="store_true", help="scan every region enabled for the account")
    p.add_argument("--key", default="Environment", help="tag key whose value is checked (default: Environment)")
    p.add_argument("--allowed", nargs="+", default=DEFAULT_ALLOWED, metavar="VALUE",
                   help=f"allowed values for --key, exact spelling (default: {' '.join(DEFAULT_ALLOWED)})")
    p.add_argument("--require", nargs="*", default=["Platform"], metavar="KEY",
                   help="other tag keys that must be present with any value (default: Platform; "
                        "pass --require with nothing after it to skip)")
    p.add_argument("--types", nargs="+", default=[], metavar="TYPE",
                   help="only check these resource types, e.g. \"EC2 instance\" (partial names work)")
    p.add_argument("--show", type=int, default=40, metavar="N",
                   help="how many resources to list per problem on screen (default 40; the CSV has all)")
    p.add_argument("--costs", action="store_true",
                   help="also show spend by --key value and untagged spend by service from Cost Explorer")
    p.add_argument("--since", metavar="YYYY-MM-DD",
                   help="start date for --costs (default: 7 days ago)")
    return p.parse_args()


def check(tags, key, allowed, required):
    """Return a list of (category, explanation) problems for one resource's tags."""
    problems = []
    allowed_lc = {a.lower(): a for a in allowed}

    def key_problems(k, check_value):
        copies = {t: v for t, v in tags.items() if t.lower() == k.lower()}
        if not copies:
            return [(f"missing {k}", f"no {k} tag")]
        out = []
        if k not in copies:
            out.append(("wrong key case", f"has {fmt_tags(copies)} instead of {k}"))
        elif len(copies) > 1:
            others = {t: v for t, v in copies.items() if t != k}
            out.append(("duplicate key", f"also has {fmt_tags(others)}"))
        value = copies.get(k)
        if check_value and value is not None and allowed and value not in allowed:
            if value.lower() in allowed_lc:
                out.append(("value case", f"{k}={value} should be {allowed_lc[value.lower()]}"))
            else:
                out.append(("unexpected value", f"{k}={value} isn't one of {', '.join(allowed)}"))
        return out

    problems += key_problems(key, True)
    for rk in required:
        if rk.lower() != key.lower():
            problems += key_problems(rk, False)
    return problems


def scan(session, acct, part, regions):
    resources, tag_indexes = [], {}
    for region in regions:
        print(f"Scanning {region} ...", flush=True)
        tag_indexes[region] = tbr.attempt("existing tags", region, tbr.build_tag_index, session, region, default={})
        ctx = tbr.Ctx(session, region, acct, part, tag_indexes[region])
        for label, fn in tbr.REGIONAL_COLLECTORS:
            resources += tbr.attempt(label, region, fn, ctx)
    print("Scanning S3" + (", CloudFront, Route 53 and WAF for CloudFront" if "us-east-1" in regions else "")
          + " ...", flush=True)
    resources += tbr.attempt("S3 buckets", "global", tbr.s3_buckets, session, part, regions, tag_indexes)
    if "us-east-1" in regions:
        resources += tbr.attempt("CloudFront distributions", "global", tbr.cloudfront_distributions,
                                 session, tag_indexes["us-east-1"])
        resources += tbr.attempt("Route 53 zones and health checks", "global", tbr.route53_resources,
                                 session, part)
        resources += tbr.attempt("WAF web ACLs for CloudFront", "global", tbr.waf_cloudfront, session)
    resources += tbr.MANUAL  # e.g. CloudSearch domains, which can only be tagged in their console
    rorder = {r: i for i, r in enumerate(regions)}
    resources.sort(key=lambda r: (rorder.get(r.region, len(regions)), tbr.TYPE_ORDER.index(r.rtype), r.rid))
    return resources


def show_costs(session, acct, key, since):
    """Spend by tag value, and untagged spend by service, from Cost Explorer."""
    end = dt.date.today()
    start = since or (end - dt.timedelta(days=7))
    if start >= end:
        print(yellow("\n--since must be before today; skipping the cost check."))
        return
    ce = session.client("ce", region_name="us-east-1", config=tbr.CFG)
    by_value, untagged, unit, calls, token = defaultdict(float), defaultdict(float), "USD", 0, None
    try:
        while True:
            kw = dict(TimePeriod={"Start": str(start), "End": str(end)}, Granularity="MONTHLY",
                      Metrics=["UnblendedCost"],
                      GroupBy=[{"Type": "TAG", "Key": key}, {"Type": "DIMENSION", "Key": "SERVICE"}],
                      Filter={"Dimensions": {"Key": "LINKED_ACCOUNT", "Values": [acct]}})
            if token:
                kw["NextPageToken"] = token
            resp = ce.get_cost_and_usage(**kw)
            calls += 1
            for period in resp.get("ResultsByTime", []):
                for grp in period.get("Groups", []):
                    tag_key, service = grp["Keys"]
                    value = tag_key.split("$", 1)[1] if "$" in tag_key else tag_key
                    m = grp["Metrics"]["UnblendedCost"]
                    amount, unit = float(m["Amount"]), m.get("Unit", unit)
                    by_value[value] += amount
                    if not value:
                        untagged[service] += amount
            token = resp.get("NextPageToken")
            if not token:
                break
    except (ClientError, BotoCoreError) as e:
        print(yellow(f"\nCouldn't read Cost Explorer ({tbr.err_text(e)[:160]}). Skipping the cost check."))
        return

    total = sum(by_value.values())
    print(bold(f"\nSpend by {key}, {start} to {end} (Cost Explorer):"))
    for value, amount in sorted(by_value.items(), key=lambda kv: -kv[1]):
        if amount < 0.01:
            continue
        label = value or f"(no {key} tag)"
        share = f"{amount / total:6.1%}" if total else ""
        print(f"  {amount:>12,.2f} {unit}  {share}  {label}")
    if untagged:
        print(bold(f"\nSpend with no {key} tag, by service:"))
        for svc, amount in sorted(untagged.items(), key=lambda kv: -kv[1]):
            if amount < 0.01:
                continue
            fixed = next((k for k in tbr.NOT_TAGGABLE if k in svc.lower()), None)
            note = dim(f"  {tbr.NOT_TAGGABLE[fixed] or 'not taggable'}") if fixed is not None else ""
            print(f"  {amount:>12,.2f} {unit}  {svc[:50]}{note}")
        print(dim("  Some untagged spend is expected: tax, support, commitments, and charges that aren't tied to "
                  "a resource.\n  Spend from before the tags were added also stays untagged."))
    if total and not any(by_value[v] for v in by_value if v):
        print(yellow(f"  Nothing has a {key} value here. Check that '{key}' is activated under Billing and Cost "
                     "Management > Cost allocation tags,\n  and that the date range starts after the tags were "
                     "added (Cost Explorer can lag by about a day)."))
    print(dim(f"  ({calls} Cost Explorer call(s), $0.01 each.)"))


def main():
    args = parse_args()
    try:
        session = tbr.boto3.Session(profile_name=args.profile) if args.profile else tbr.boto3.Session()
        ident = session.client("sts", config=tbr.CFG).get_caller_identity()
    except (BotoCoreError, ClientError) as e:
        login = f"aws sso login --profile {args.profile}" if args.profile else "aws sso login --profile NAME"
        sys.exit(f"Couldn't sign in to AWS: {tbr.err_text(e)}\nOn your own computer, sign in first with:  {login}")
    acct, part = ident["Account"], ident["Arn"].split(":")[1]
    regions = tbr.resolve_regions(args, session)
    since = None
    if args.since:
        try:
            since = dt.date.fromisoformat(args.since)
        except ValueError:
            sys.exit("--since must look like YYYY-MM-DD")

    print(bold("\nTag verification (read-only)"))
    print(f"  Account : {acct}  ({ident['Arn']})")
    print(f"  Regions : {', '.join(regions)}")
    print(f"  Checks  : {args.key} is one of {', '.join(args.allowed)}"
          + (f"; {', '.join(args.require)} present" if args.require else ""))
    if args.types:
        print(f"  Types   : {', '.join(args.types)}")
    print()

    resources = scan(session, acct, part, regions)
    if args.types:
        wanted = [t.lower() for t in args.types]
        resources = [r for r in resources if any(t in r.rtype.lower() for t in wanted)]

    results = {r: check(r.tags, args.key, args.allowed, args.require) for r in resources}
    bad = {r: p for r, p in results.items() if p}
    ok = len(resources) - len(bad)

    # Summary
    print(bold(f"\nChecked {len(resources)} resource(s): ") + green(f"{ok} OK") +
          (", " + yellow(f"{len(bad)} need attention") if bad else ""))
    values = Counter(r.tags.get(args.key, "") for r in resources if args.key in r.tags)
    if values:
        print(f"  {args.key} values: " + ", ".join(f"{v}: {n}" for v, n in values.most_common()))
    by_cat = defaultdict(list)
    for r, problems in bad.items():
        for cat, why in problems:
            by_cat[cat].append((r, why))
    order = [f"missing {k}" for k in [args.key, *args.require] if k] + ORDER_TAIL
    width = max(len(c) for c in order)
    for cat in order:
        if by_cat.get(cat):
            print(f"  {cat:<{width}} : {len(by_cat[cat])}")

    # Untagged / mis-tagged resources by type
    if bad:
        by_type = Counter(r.rtype for r in bad)
        print(bold("\nNeeds attention, by type:"))
        for t in tbr.TYPE_ORDER:
            if by_type[t]:
                print(f"  {by_type[t]:>5}  {t}")

    # Details per problem
    for cat in order:
        rows = by_cat.get(cat) or []
        if not rows:
            continue
        print(bold(f"\n{cat[0].upper() + cat[1:]} ({len(rows)}):"))
        for r, why in rows[:args.show]:
            hand = dim("  [tag by hand in its console]") if r.via == "manual" else ""
            print(f"  {r.region:<12} {r.rtype:<27} {r.rid[:38]:<38} {r.name[:26]:<26} {why}{hand}")
        if len(rows) > args.show:
            print(dim(f"  ...and {len(rows) - args.show} more (all are in the CSV)."))

    # CSV report
    path = Path.home() / f"tag-verification-{acct}-{dt.datetime.now():%Y%m%d-%H%M%S}.csv"
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["region", "type", "id", "name", "result", "problems", args.key,
                    *[k for k in args.require if k.lower() != args.key.lower()], "all_tags", "arn"])
        for r in resources:
            problems = results[r]
            w.writerow([r.region, r.rtype, r.rid, r.name, "OK" if not problems else problems[0][0],
                        "; ".join(why for _, why in problems), r.tags.get(args.key, ""),
                        *[r.tags.get(k, "") for k in args.require if k.lower() != args.key.lower()],
                        fmt_tags({k: v for k, v in r.tags.items() if not k.startswith("aws:")}, limit=1000),
                        r.arn])
    print(f"\nFull report: {path}")

    if args.costs:
        show_costs(session, acct, args.key, since)

    if bad:
        print(dim("\nTo fix: tag_billable_resources.py with --skip-tagged " + " ".join(
            f"{args.key}={v}" for v in args.allowed) + " finds what's missing; --fix-case cleans up "
            "key capitalization."))
    return 1 if bad else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(130)