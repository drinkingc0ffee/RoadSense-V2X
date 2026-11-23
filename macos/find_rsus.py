#!/usr/bin/env python3
"""
find_rsus.py — Offline-Prep RSU Locator for 5.9 GHz DSRC & C-V2X (FCC ULS 'IQ' + USDOT)
Queries the public FCC ULS License View API for active Intelligent Transportation Service
(Radio Service Code 'IQ', 5850-5925 MHz) licenses by state, city, or keyword, and exports
results to console table, CSV, or Google Maps KML.
"""

import argparse
import csv
import json
import math
import re
import sys
import urllib.parse
import urllib.request

FCC_API = "https://data.fcc.gov/api/license-view/basicSearch/getLicenses"
USDOT_MAP_URL = "https://www.its.dot.gov/deployments/"
FCC_ULS_SEARCH_URL = "https://wireless2.fcc.gov/UlsApp/UlsSearch/searchAdvanced.jsp"

# Shared CSV schema read by both macOS (roadsense_cli/tui) and Android (RoadSense V2X)
RSU_CSV_FIELDS = ["name", "callsign", "lat", "lon", "freq_mhz", "protocol", "range_m", "source", "uls_url"]


def get_ip_location(timeout_s: float = 5.0):
    """Auto-detect approximate (lat, lon, city, region) from IP geolocation on macOS."""
    try:
        req = urllib.request.Request("https://ipinfo.io/json", headers={"User-Agent": "RoadSenseV2X/1.9"})
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            d = json.loads(resp.read().decode("utf-8", errors="ignore"))
        loc = d.get("loc") or ""
        if "," in loc:
            lat_s, lon_s = loc.split(",", 1)
            return float(lat_s), float(lon_s), (d.get("city") or ""), (d.get("region") or "")
    except Exception:
        pass
    try:
        req = urllib.request.Request("http://ip-api.com/json/", headers={"User-Agent": "RoadSenseV2X/1.9"})
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            d = json.loads(resp.read().decode("utf-8", errors="ignore"))
        if d.get("status") == "success":
            return float(d["lat"]), float(d["lon"]), (d.get("city") or ""), (d.get("regionName") or "")
    except Exception:
        pass
    return None, None, "", ""


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2.0 * r * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


def dms_to_dd(deg: float, mins: float, secs: float, hemi: str) -> float:
    dd = abs(deg) + mins / 60.0 + secs / 3600.0
    return -dd if hemi.upper() in ("S", "W") else dd


def try_fetch_uls_coords(uls_url: str):
    """Best-effort parse of site lat/lon from an FCC ULS license detail/location page."""
    if not uls_url:
        return None, None
    try:
        loc_url = uls_url.replace("license.jsp", "licenseLocSum.jsp")
        req = urllib.request.Request(loc_url, headers={"User-Agent": "RoadSenseV2X/1.9"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
        # Match DMS pattern like 42-19-53.4 N, 083-02-45.1 W
        m = re.search(
            r"(\d{2})-(\d{2})-(\d{2}(?:\.\d+)?)\s*([NS])[^0-9]{1,40}(\d{2,3})-(\d{2})-(\d{2}(?:\.\d+)?)\s*([EW])",
            html,
        )
        if m:
            lat = dms_to_dd(float(m.group(1)), float(m.group(2)), float(m.group(3)), m.group(4))
            lon = dms_to_dd(float(m.group(5)), float(m.group(6)), float(m.group(7)), m.group(8))
            return round(lat, 6), round(lon, 6)
    except Exception:
        pass
    return None, None


def fetch_fcc_iq_licenses(search_value: str, max_pages: int = 5):
    """Query FCC License View API and filter to Radio Service Code 'IQ' (5.9 GHz ITS)."""
    results = []
    for page in range(1, max_pages + 1):
        params = urllib.parse.urlencode({
            "searchValue": search_value,
            "format": "json",
            "pageNum": str(page),
        })
        url = f"{FCC_API}?{params}"
        req = urllib.request.Request(url, headers={"User-Agent": "RoadSenseV2X/1.9"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception as e:
            print(f"[FCC API warning on page {page}] {e}", file=sys.stderr)
            break

        licenses = data.get("Licenses", {}).get("License", [])
        if isinstance(licenses, dict):
            licenses = [licenses]
        if not licenses:
            break

        for lic in licenses:
            svc = (lic.get("serviceDesc") or "").upper()
            callsign = lic.get("callsign") or ""
            status = (lic.get("statusDesc") or "").strip()
            # Filter strictly to Intelligent Transportation Service (IQ)
            if "INTELLIGENT TRANSPORTATION" not in svc and lic.get("serviceCode") != "IQ":
                continue
            results.append({
                "callsign": callsign,
                "licensee": (lic.get("licName") or "").strip(),
                "status": status,
                "service": (lic.get("serviceDesc") or "IQ").strip(),
                "frn": (lic.get("frn") or "").strip(),
                "expired_date": (lic.get("expiredDate") or "").strip(),
                "uls_url": (lic.get("licDetailURL") or "").strip(),
            })

        # Stop if last page
        total_rows = int(data.get("Licenses", {}).get("totalRows", 0) or 0)
        row_per_page = int(data.get("Licenses", {}).get("rowPerPage", 100) or 100)
        if page * row_per_page >= total_rows:
            break
    return results


def main():
    ap = argparse.ArgumentParser(
        description="Locate licensed 5.9 GHz DSRC / C-V2X Roadside Units (RSUs) via FCC ULS (Service Code IQ)"
    )
    ap.add_argument("query", nargs="?", default=None,
                    help="Optional search term (state/city/DOT). If omitted, auto-detects your region via IP location.")
    ap.add_argument("--active-only", action="store_true", default=True,
                    help="Only show Active licenses (default: True)")
    ap.add_argument("--include-expired", action="store_true",
                    help="Include expired/cancelled IQ licenses too")
    ap.add_argument("--pages", type=int, default=5,
                    help="Max FCC API pages to scan (100 rows/page, default: 5)")
    ap.add_argument("--no-coords", action="store_true",
                    help="Skip fetching FCC ULS Locations pages for lat/lon (faster)")
    ap.add_argument("--range-m", type=int, default=600,
                    help="Default estimated 5.9 GHz reception radius in meters (default: 600)")
    ap.add_argument("--csv", type=str, default="rsus.csv",
                    help="Output shared RSU CSV for macOS & Android apps (default: rsus.csv)")
    args = ap.parse_args()

    my_lat, my_lon, ip_city, ip_region = get_ip_location()
    if my_lat is not None:
        print(f"IP Location: {ip_city}, {ip_region} ({my_lat:.5f}, {my_lon:.5f})")
    else:
        print("IP Location: unavailable (proceeding without distance sorting)")

    query = args.query or (f"{ip_region} Department of Transportation" if ip_region else "Department of Transportation")
    print(f"Querying FCC ULS (Radio Service IQ — 5.9 GHz ITS) for: {query!r} ...")
    rows = fetch_fcc_iq_licenses(query, max_pages=args.pages)
    if not rows and args.query is None and ip_region:
        print(f"Broadening search to {ip_region!r} ...")
        rows = fetch_fcc_iq_licenses(ip_region, max_pages=args.pages)
    if not args.include_expired:
        rows = [r for r in rows if r["status"].lower() == "active"]

    # Deduplicate by callsign
    seen = set()
    uniq = []
    for r in rows:
        if r["callsign"] not in seen:
            seen.add(r["callsign"])
            uniq.append(r)

    if not uniq:
        print("\nNo active FCC 'IQ' (5.9 GHz ITS) licenses matched that query.")
        print("Tips:")
        print("  - Try broader state/DOT names (e.g. 'Department of Transportation', 'City of ...', 'County').")
        print(f"  - For exact lat/lon & channel maps, check USDOT Connected Vehicle Map: {USDOT_MAP_URL}")
        print(f"  - Or FCC ULS Advanced Search (select Radio Service = IQ): {FCC_ULS_SEARCH_URL}")
        return

    out_records = []
    for r in uniq:
        lat, lon = (None, None)
        if not args.no_coords:
            lat, lon = try_fetch_uls_coords(r["uls_url"])
        dist_m = haversine_m(my_lat, my_lon, lat, lon) if (my_lat is not None and lat is not None) else None
        out_records.append((dist_m, {
            "name": r["licensee"],
            "callsign": r["callsign"],
            "lat": f"{lat:.6f}" if lat is not None else "",
            "lon": f"{lon:.6f}" if lon is not None else "",
            "freq_mhz": "5905.0",
            "protocol": "ITS-5.9G",
            "range_m": str(args.range_m),
            "source": "FCC_ULS_IQ",
            "uls_url": r["uls_url"],
        }))

    # Sort closest first when coordinates are available
    out_records.sort(key=lambda item: item[0] if item[0] is not None else 1e12)

    print(f"\nFound {len(out_records)} FCC 5.9 GHz ITS ('IQ') license(s):\n")
    print(f"{'CALLSIGN':<10} {'DIST':<10} {'LAT, LON':<23} {'LICENSEE':<38} {'ULS DETAIL URL'}")
    print("-" * 124)
    for dist_m, rec in out_records:
        ll = f"{rec['lat']}, {rec['lon']}" if rec["lat"] else "(no site coords)"
        if dist_m is None:
            d_str = "--"
        elif dist_m <= args.range_m:
            d_str = f"*{dist_m:.0f}m*"
        else:
            d_str = f"{dist_m/1000.0:.1f}km"
        print(f"{rec['callsign']:<10} {d_str:<10} {ll:<23} {rec['name'][:37]:<38} {rec['uls_url']}")

    if args.csv:
        with open(args.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=RSU_CSV_FIELDS)
            w.writeheader()
            w.writerows(rec for _, rec in out_records)
        print(f"\nExported {len(out_records)} RSU record(s) -> {args.csv}")
        print("Use with macOS (auto IP loc): ./roadsense_tui.py --rsu-db rsus.csv")
        print("Push to Android (live GPS):   adb push rsus.csv /sdcard/Android/data/com.trafficlight.v2x/files/v2x_captures/rsus.csv")

    print("\nField notes:")
    print("  • Click any ULS Detail URL -> 'Locations' & 'Frequencies' tabs to see exact RSU GPS coordinates & 5895-5925 MHz channels.")
    print("  • Upper 30 MHz safety band: Ch 180 (5900), Ch 182 (5910), Ch 184 (5920) [10M DSRC/C-V2X] and Ch 183 (5915) [20M C-V2X].")
    print(f"  • USDOT active deployment tracker: {USDOT_MAP_URL}")


if __name__ == "__main__":
    main()
