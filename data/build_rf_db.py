#!/usr/bin/env python3
"""
build_rf_db.py — Unified RoadSense V2X RF Database Builder (SQLite3)
Consolidates ALL collected FCC ULS licenses, geolocated RSU sites, USDOT/NJIT
corridors, 5.9 GHz frequency allocations, V2X protocol definitions, local .iq
capture metadata, and live RF observations into one unified SQLite database:
  data/roadsense_rf.db
"""

import argparse
import csv
import datetime
import io
import json
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.request
import zipfile

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT_DIR, "data")
DEFAULT_DB = os.path.join(DATA_DIR, "roadsense_rf.db")
DEFAULT_SEED_CSV = os.path.join(ROOT_DIR, "macos", "nyc_nj_rsus.csv")
ANTENNA_CHECK_DIR = os.path.join(ROOT_DIR, "antenna_check")
FCC_BULK_LMPRIV_URL = "https://data.fcc.gov/download/pub/uls/complete/l_LMpriv.zip"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS fcc_licenses (
    callsign        TEXT PRIMARY KEY,
    licensee        TEXT NOT NULL,
    frn             TEXT,
    radio_service   TEXT NOT NULL,
    status          TEXT NOT NULL,
    state           TEXT,
    mailing_address TEXT,
    area_of_op      TEXT,
    freq_band_mhz   TEXT,
    total_locations INTEGER DEFAULT 0,
    grant_date      TEXT,
    effective_date  TEXT,
    expiration_date TEXT,
    uls_url         TEXT
);

CREATE TABLE IF NOT EXISTS rsu_sites (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    callsign        TEXT,
    fcc_key_loc     TEXT,
    state           TEXT,
    county          TEXT,
    city            TEXT,
    lat             REAL NOT NULL,
    lon             REAL NOT NULL,
    freq_mhz        REAL NOT NULL,
    channels        TEXT,
    protocol        TEXT NOT NULL,
    eirp_dbm        REAL,
    range_m         REAL NOT NULL DEFAULT 600.0,
    source          TEXT NOT NULL,
    uls_url         TEXT,
    UNIQUE(name, lat, lon)
);

CREATE TABLE IF NOT EXISTS corridors (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    region          TEXT NOT NULL,
    agency          TEXT NOT NULL,
    technology      TEXT NOT NULL,
    waypoints_json  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS frequency_allocations (
    channel_num     INTEGER PRIMARY KEY,
    center_mhz      REAL NOT NULL,
    bandwidth_mhz   REAL NOT NULL,
    band_role       TEXT NOT NULL,
    standard        TEXT NOT NULL,
    fcc_status      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS protocol_registry (
    protocol        TEXT PRIMARY KEY,
    standard        TEXT NOT NULL,
    ethertype_hex   TEXT,
    msg_id          INTEGER,
    btp_port        INTEGER,
    category        TEXT NOT NULL,
    description     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS iq_captures (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path       TEXT NOT NULL UNIQUE,
    freq_mhz        REAL,
    size_bytes      INTEGER NOT NULL,
    captured_at     TEXT NOT NULL,
    source_dir      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rf_observations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL,
    lat             REAL,
    lon             REAL,
    freq_mhz        REAL NOT NULL,
    protocol        TEXT NOT NULL,
    rssi_dbfs       REAL,
    summary         TEXT,
    iq_file         TEXT,
    device          TEXT DEFAULT 'unknown'
);

CREATE INDEX IF NOT EXISTS idx_rsu_lat_lon ON rsu_sites(lat, lon);
CREATE INDEX IF NOT EXISTS idx_rsu_callsign ON rsu_sites(callsign);
CREATE INDEX IF NOT EXISTS idx_obs_proto ON rf_observations(protocol);
CREATE INDEX IF NOT EXISTS idx_obs_freq ON rf_observations(freq_mhz);
"""

# Live FCC ULS records gathered from wireless2.fcc.gov
FCC_LICENSES_SEED = [
    (
        "WRDK393",
        "New Jersey Turnpike Authority",
        "0009431263",
        "IQ - Intelligent Transportation Service (Public Safety)",
        "Active",
        "NJ",
        "P.O. Box 5042, 1 Turnpike Plaza, Woodbridge, NJ 07095",
        "S - Operating Statewide: NJ",
        "5895.0-5925.0",
        0,
        "2019-05-17",
        "2026-05-16",
        "2029-05-17",
        "https://wireless2.fcc.gov/UlsApp/UlsSearch/license.jsp?licKey=4161524",
    ),
    (
        "WRFR820",
        "New Jersey Department of Transportation (NJDOT)",
        "0029339686",
        "IQ - Intelligent Transportation Service (Public Safety)",
        "Active",
        "NJ",
        "1035 Parkway Avenue, Ewing Township, NJ 08618",
        "S - Operating Statewide: NJ",
        "5895.0-5925.0",
        309,
        "2020-04-20",
        "2026-10-06",
        "2029-04-30",
        "https://wireless2.fcc.gov/UlsApp/UlsSearch/license.jsp?licKey=4272888",
    ),
]

# Exact FCC ULS keyLoc / county / EIRP enrichment for the 6 Essex County WRFR820 sites
FCC_ESSEX_ENRICHMENT = {
    "NJDOT RT280 MP 13 RSU 30 (Newark)":   ("16935909", "Essex", "Newark",    "180,181,182,184", 23.0),
    "NJDOT RT280 MP 14 RSU 31 (Newark)":   ("16935910", "Essex", "Newark",    "180,181,182,184", 23.0),
    "NJDOT RT46 MP 52 RSU 18 (Fairfield)": ("16872063", "Essex", "Fairfield", "180,181,182,184", 23.0),
    "NJDOT RT46 MP 52 RSU 19 (Fairfield)": ("16871840", "Essex", "Fairfield", "180,181,182,184", 23.0),
    "NJDOT RT46 MP 53 RSU 21 (Fairfield)": ("16871842", "Essex", "Fairfield", "180,181,182,184", 23.0),
    "NJDOT RT46 MP 55 RSU 22 (Fairfield)": ("16872064", "Essex", "Fairfield", "180,181,182,184", 21.1),
}

FREQ_ALLOCATIONS = [
    (172, 5860.0, 10.0, "Legacy DSRC Service",       "IEEE 802.11p",     "Reallocated UNII-4 (legacy RSUs remain)"),
    (174, 5870.0, 10.0, "Legacy DSRC Service",       "IEEE 802.11p",     "Reallocated UNII-4"),
    (176, 5880.0, 10.0, "Legacy DSRC Service",       "IEEE 802.11p",     "Reallocated UNII-4"),
    (178, 5890.0, 10.0, "Legacy DSRC Control",       "IEEE 802.11p",     "Reallocated UNII-4 (legacy RSUs remain)"),
    (180, 5900.0, 10.0, "US Safety Band (Lower 10M)","DSRC / C-V2X PC5", "Active FCC 5.9 GHz ITS Safety Band"),
    (181, 5905.0, 20.0, "US Safety Band (20M Agg)",  "C-V2X / Dual 10M", "Active FCC 5.9 GHz ITS Safety Band"),
    (182, 5910.0, 10.0, "US Safety Band (Mid 10M)",  "DSRC / C-V2X PC5", "Active FCC 5.9 GHz ITS Safety Band"),
    (183, 5915.0, 20.0, "US Safety Band (Primary)",  "C-V2X PC5 (20M)",  "Active FCC 5.9 GHz ITS Safety Band"),
    (184, 5920.0, 10.0, "US Safety Band (Upper 10M)","DSRC / C-V2X PC5", "Active FCC 5.9 GHz ITS Safety Band"),
    (149, 5745.0, 20.0, "Desk Sanity Test (Wi-Fi)",  "IEEE 802.11a/n/ac","UNII-3 ISM (5 GHz Wi-Fi)"),
]

PROTOCOLS = [
    ("SPaT",   "US SAE J2735 (V2I)", "0x88DC", 19, None, "Traffic Signals",    "Signal Phase and Timing (Red/Yellow/Green/Arrow + countdown)"),
    ("MAP",    "US SAE J2735 (V2I)", "0x88DC", 18, None, "Intersection Info",  "Intersection lane geometry & reference GPS"),
    ("BSM",    "US SAE J2735 (V2V)", "0x88DC", 20, None, "Vehicles",           "Basic Safety Message (temp ID, GPS, speed, heading, brakes)"),
    ("TIM",    "US SAE J2735 (V2I)", "0x88DC", 31, None, "Hazards",            "Traveler Information Message (work zones, WWD, speed advisories)"),
    ("SRM",    "US SAE J2735 (V2I)", "0x88DC", 29, None, "Emergency/Priority", "Signal Request Message (transit/emergency green-light priority)"),
    ("SSM",    "US SAE J2735 (V2I)", "0x88DC", 30, None, "Emergency/Priority", "Signal Status Message (priority grant status)"),
    ("RTCM",   "US SAE J2735 (V2I)", "0x88DC", 28, None, "Infrastructure",     "Differential GPS corrections from RSUs"),
    ("PSM",    "US SAE J2735 (V2P)", "0x88DC", 32, None, "Pedestrians",        "Personal Safety Message (pedestrian/cyclist V2P)"),
    ("EVA",    "US SAE J2735 (V2V)", "0x88DC", 22, None, "Emergency/Priority", "Emergency Vehicle Alert (police/fire/EMS)"),
    ("RSA",    "US SAE J2735 (V2I)", "0x88DC", 27, None, "Hazards",            "Road Side Alert (roadway hazards)"),
    ("CAM",    "EU ETSI ITS-G5",     "0x8947",  2, 2001, "Vehicles",           "Cooperative Awareness Message"),
    ("DENM",   "EU ETSI ITS-G5",     "0x8947",  1, 2002, "Hazards",            "Decentralized Environmental Notification Message"),
    ("SPATEM", "EU ETSI ITS-G5",     "0x8947",  4, 2004, "Traffic Signals",    "European SPaT over GeoNetworking + BTP"),
    ("MAPEM",  "EU ETSI ITS-G5",     "0x8947",  5, 2003, "Intersection Info",  "European MAP over GeoNetworking + BTP"),
    ("C-V2X",  "3GPP LTE-V2X PC5",   None,   None, None, "RF Burst",           "20 MHz / 10 MHz PC5 Sidelink SC-FDMA (Ch 183 / 5915 MHz)"),
    ("802.11", "IEEE 802.11a/p",     None,   None, None, "General Wi-Fi",      "General 802.11 beacons, management, control & data frames"),
]

CORRIDORS = [
    ("NYC 1st Ave (CVPD)",             "NY", "NYCDOT",      "DSRC/C-V2X", [[40.7313, -73.9818], [40.7440, -73.9726], [40.7596, -73.9614]]),
    ("NYC 2nd Ave (CVPD)",             "NY", "NYCDOT",      "DSRC/C-V2X", [[40.7324, -73.9845], [40.7450, -73.9754], [40.7606, -73.9642]]),
    ("NYC 5th Ave (CVPD)",             "NY", "NYCDOT",      "DSRC/C-V2X", [[40.7359, -73.9935], [40.7484, -73.9845], [40.7536, -73.9809], [40.7644, -73.9730]]),
    ("NYC 6th Ave (CVPD)",             "NY", "NYCDOT",      "DSRC/C-V2X", [[40.7372, -73.9966], [40.7498, -73.9878], [40.7549, -73.9840], [40.7644, -73.9772]]),
    ("NYC FDR Drive (CVPD)",           "NY", "NYCDOT",      "DSRC/C-V2X", [[40.7582, -73.9585], [40.7705, -73.9476]]),
    ("Brooklyn Flatbush Ave (CVPD)",   "NY", "NYCDOT",      "DSRC/C-V2X", [[40.6960, -73.9847], [40.6845, -73.9776], [40.6742, -73.9702]]),
    ("Newark NJIT Warren St (ICUC)",   "NJ", "NEWARK-NJIT", "DSRC/C-V2X", [[40.7432, -74.1798], [40.7421, -74.1783]]),
    ("Newark Raymond Blvd (ICUC)",     "NJ", "NEWARK-NJIT", "DSRC/C-V2X", [[40.7388, -74.1785], [40.7376, -74.1712], [40.7359, -74.1656]]),
    ("NJDOT I-280 Newark (WRFR820)",   "NJ", "NJDOT",       "Ch180-184",  [[40.752139, -74.190278], [40.748056, -74.171889]]),
    ("NJDOT Rt 46 Fairfield (WRFR820)","NJ", "NJDOT",       "Ch180-184",  [[40.867972, -74.311556], [40.870222, -74.307639], [40.881639, -74.292083], [40.891194, -74.269389]]),
    ("NJDOT US-1 Princeton",           "NJ", "NJDOT",       "DSRC/C-V2X", [[40.2923, -74.6828], [40.3266, -74.6407]]),
]


def init_db(db_path: str):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    if os.path.exists(db_path):
        os.remove(db_path)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA_SQL)
    conn.executemany(
        "INSERT OR REPLACE INTO fcc_licenses VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        FCC_LICENSES_SEED,
    )
    conn.executemany(
        "INSERT OR REPLACE INTO frequency_allocations VALUES (?, ?, ?, ?, ?, ?)",
        FREQ_ALLOCATIONS,
    )
    conn.executemany(
        "INSERT OR REPLACE INTO protocol_registry VALUES (?, ?, ?, ?, ?, ?, ?)",
        PROTOCOLS,
    )
    conn.executemany(
        "INSERT OR REPLACE INTO corridors (name, region, agency, technology, waypoints_json) VALUES (?, ?, ?, ?, ?)",
        [(n, r, a, t, json.dumps(pts)) for n, r, a, t, pts in CORRIDORS],
    )
    conn.commit()
    return conn


def import_rsu_csv(conn: sqlite3.Connection, csv_path: str) -> int:
    if not os.path.exists(csv_path):
        return 0
    count = 0
    with open(csv_path, newline="", encoding="utf-8", errors="ignore") as f:
        for row in csv.DictReader(f):
            try:
                lat = float(row["lat"])
                lon = float(row["lon"])
            except (KeyError, ValueError, TypeError):
                continue
            name = (row.get("name") or "RSU").strip()
            callsign = (row.get("callsign") or "").strip()
            freq = float(row.get("freq_mhz") or 5905.0)
            proto = (row.get("protocol") or "ITS-5.9G").strip()
            rng = float(row.get("range_m") or 600.0)
            src = (row.get("source") or "CSV").strip()
            url = (row.get("uls_url") or "").strip()

            key_loc, county, city, channels, eirp = FCC_ESSEX_ENRICHMENT.get(name, ("", "", "", "180,181,182,184", None))
            if "NYC" in name:
                state, county, city = "NY", "New York", "New York"
            elif "Brooklyn" in name:
                state, county, city = "NY", "Kings", "Brooklyn"
            elif "Newark" in name:
                state, county, city = "NJ", "Essex", "Newark"
            elif "Fairfield" in name:
                state, county, city = "NJ", "Essex", "Fairfield"
            elif "NJDOT" in name:
                state, county, city = "NJ", "Mercer", "Princeton"
            else:
                state = ""

            conn.execute(
                """INSERT OR REPLACE INTO rsu_sites
                   (name, callsign, fcc_key_loc, state, county, city, lat, lon, freq_mhz, channels, protocol, eirp_dbm, range_m, source, uls_url)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (name, callsign, key_loc, state, county, city, lat, lon, freq, channels, proto, eirp, rng, src, url),
            )
            count += 1
    conn.commit()
    return count


def index_local_iq_captures(conn: sqlite3.Connection) -> int:
    count = 0
    for folder_name in ("antenna_check", "captures", "scans"):
        folder = os.path.join(ROOT_DIR, folder_name)
        if not os.path.isdir(folder):
            continue
        for fname in sorted(os.listdir(folder)):
            if not fname.endswith(".iq"):
                continue
            full = os.path.join(folder, fname)
            st = os.stat(full)
            m = re.search(r"(\d{9,10})", fname)
            freq_mhz = (int(m.group(1)) / 1e6) if m else None
            ts = datetime.datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")
            conn.execute(
                "INSERT OR REPLACE INTO iq_captures (file_path, freq_mhz, size_bytes, captured_at, source_dir) VALUES (?, ?, ?, ?, ?)",
                (os.path.relpath(full, ROOT_DIR), freq_mhz, st.st_size, ts, folder_name),
            )
            count += 1
    conn.commit()
    return count


def sync_fcc_uls_bulk(conn: sqlite3.Connection, zip_path: str = None) -> int:
    local_zip = zip_path or os.path.join(DATA_DIR, "l_lmpriv.zip")
    if not os.path.exists(local_zip):
        print(f"Downloading FCC ULS bulk archive ({FCC_BULK_LMPRIV_URL}) -> {local_zip} ...")
        req = urllib.request.Request(
            FCC_BULK_LMPRIV_URL,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
                "Accept": "*/*",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp, open(local_zip, "wb") as out:
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
        except urllib.error.HTTPError as e:
            if e.code == 403:
                print("\n[FCC CDN returned 403 Forbidden to Python urllib]", file=sys.stderr)
                print("Download the weekly Land Mobile Private zip in your browser instead:", file=sys.stderr)
                print("  1. Open: https://www.fcc.gov/uls/transactions/daily-weekly", file=sys.stderr)
                print("  2. Click 'Land Mobile - Private' (l_lmpriv.zip) under Weekly Complete -> Licenses", file=sys.stderr)
                print("  3. Run:  ./build_rf_db.py --fcc-zip ~/Downloads/l_lmpriv.zip\n", file=sys.stderr)
                return 0
            raise

    active_iq = {}
    licensees = {}
    added = 0
    with zipfile.ZipFile(local_zip, "r") as zf:
        with zf.open("HD.dat") as f:
            for raw in io.TextIOWrapper(f, encoding="latin-1", errors="ignore"):
                parts = raw.rstrip("\r\n").split("|")
                if len(parts) > 6 and parts[5] == "A" and parts[6] in ("IQ", "QQ"):
                    active_iq[parts[1]] = (parts[4], parts[6])
        with zf.open("EN.dat") as f:
            for raw in io.TextIOWrapper(f, encoding="latin-1", errors="ignore"):
                parts = raw.rstrip("\r\n").split("|")
                if len(parts) > 7 and parts[1] in active_iq:
                    licensees[parts[1]] = parts[7].strip() or active_iq[parts[1]][0]
        with zf.open("LO.dat") as f:
            for raw in io.TextIOWrapper(f, encoding="latin-1", errors="ignore"):
                parts = raw.rstrip("\r\n").split("|")
                if len(parts) < 28 or parts[1] not in active_iq:
                    continue
                try:
                    lat = (abs(float(parts[19])) + float(parts[20]) / 60.0 + float(parts[21]) / 3600.0) * (-1.0 if parts[22] == "S" else 1.0)
                    lon = (abs(float(parts[23])) + float(parts[24]) / 60.0 + float(parts[25]) / 3600.0) * (-1.0 if parts[26] == "W" else 1.0)
                except ValueError:
                    continue
                sys_id = parts[1]
                callsign, svc_code = active_iq[sys_id]
                city, county, state = parts[12].strip(), parts[13].strip(), parts[14].strip()
                loc_desc = parts[11].strip() or f"{city}, {state}".strip(", ")
                name = f"{licensees.get(sys_id, callsign)[:28]} — {loc_desc[:28]}".strip(" —")
                conn.execute(
                    """INSERT OR REPLACE INTO rsu_sites
                       (name, callsign, fcc_key_loc, state, county, city, lat, lon, freq_mhz, channels, protocol, eirp_dbm, range_m, source, uls_url)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (name, callsign, parts[4], state, county, city, round(lat, 6), round(lon, 6),
                     5905.0, "180,181,182,184", f"ITS-5.9G ({svc_code})", None, 700.0, f"FCC_ULS_{svc_code}",
                     f"https://wireless2.fcc.gov/UlsApp/UlsSearch/license.jsp?licKey={sys_id}"),
                )
                added += 1
    conn.commit()
    return added


def main():
    ap = argparse.ArgumentParser(description="Build Unified RoadSense V2X RF SQLite Database")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--sync-fcc-uls", action="store_true")
    ap.add_argument("--fcc-zip", default=None)
    args = ap.parse_args()

    conn = init_db(args.db)
    import_rsu_csv(conn, DEFAULT_SEED_CSV)
    index_local_iq_captures(conn)
    if args.sync_fcc_uls or args.fcc_zip:
        sync_fcc_uls_bulk(conn, args.fcc_zip)

    tables = ["fcc_licenses", "rsu_sites", "corridors", "frequency_allocations", "protocol_registry", "iq_captures", "rf_observations"]
    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    iq_total_bytes = conn.execute("SELECT COALESCE(SUM(size_bytes), 0) FROM iq_captures").fetchone()[0]
    conn.close()

    size_bytes = os.path.getsize(args.db)
    print(f"Unified RF Database: {args.db}")
    print(f"  SQLite DB size: {size_bytes:,} bytes ({size_bytes / 1024:.1f} KB)")
    for t, c in counts.items():
        print(f"  {t:<22}: {c}")
    print(f"  Indexed raw .iq files : {iq_total_bytes / (1024*1024):.1f} MB on disk (metadata indexed in DB)")


if __name__ == "__main__":
    main()
