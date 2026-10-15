"""Abgleich der Tipprunde mit fussball.de.

Holt die Tabelle der Kreisliga D Lippstadt, rechnet aus der Veränderung der
Tabelle die Ergebnisse aus (fussball.de verschlüsselt die Ergebnisse selbst),
legt die nächsten Spieltage an und schreibt alles in die Supabase-Datenbank.

Umgebungsvariablen: SUPABASE_URL, SUPABASE_SECRET_KEY, optional DRY_RUN=1
"""
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

BERLIN = ZoneInfo("Europe/Berlin")
BASE = "https://www.fussball.de"
STAFFEL = "kreisliga-d-lippstadt-kreis-lippstadt-kreisliga-d-herren-saison2627-westfalen/-/staffel/031H01QQ6C000004VS5489BTVVG7L386-G"
TABLE_URL = f"{BASE}/spieltag/{STAFFEL}"
MD_URL = BASE + "/spieltag/kreisliga-d-lippstadt-kreis-lippstadt-kreisliga-d-herren-saison2627-westfalen/-/spieltag/{md}/staffel/031H01QQ6C000004VS5489BTVVG7L386-G"
TEAM_URL = f"{BASE}/mannschaft/sw-overhagen-ii-sv-sw-overhagen-westfalen/-/saison/2627/team-id/011MIANKAS000000VTVG0001VTR8C1K7"
REF_TEAM = "SW Overhagen II"
MAX_MD = 34
DEFAULT_LOCK = (12, 0)  # Tippschluss, wenn die Anstoßzeit unbekannt ist

SB_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SB_KEY = os.environ.get("SUPABASE_SECRET_KEY", "")
DRY = os.environ.get("DRY_RUN") == "1"

log_lines = []


def log(msg):
    print(msg)
    log_lines.append(msg)


# ---------------- fussball.de ----------------
def fetch(url):
    r = requests.get(url, timeout=30, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
        "Accept-Language": "de-DE,de;q=0.9",
    })
    r.raise_for_status()
    return r.text


def clean(s):
    return re.sub(r"\s+", " ", s or "").strip()


def club_names(tr):
    return [clean(d.get_text()) for d in tr.select(".club-name")]


def parse_table(html):
    """Gibt [{team, gp, gf, ga, pts}] zurück."""
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    seen = set()
    for tr in soup.find_all("tr"):
        names = club_names(tr)
        if len(names) != 1:
            continue
        cells = [clean(td.get_text()) for td in tr.find_all("td")]
        goals_idx = next((i for i, c in enumerate(cells) if re.fullmatch(r"\d+\s*:\s*\d+", c)), None)
        if goals_idx is None:
            continue
        gf, ga = (int(x) for x in re.findall(r"\d+", cells[goals_idx]))
        name_idx = next((i for i, c in enumerate(cells) if names[0] in c), None)
        if name_idx is None:
            continue
        before = [int(c) for c in cells[name_idx + 1:goals_idx] if re.fullmatch(r"\d+", c)]
        after = [c for c in cells[goals_idx + 1:] if re.fullmatch(r"-?\d+", c)]
        if not before or not after:
            continue
        team = names[0]
        if team in seen:  # die Seite kann die Tabelle mehrfach enthalten (Heim/Auswärts)
            continue
        seen.add(team)
        rows.append({"team": team, "gp": before[0], "gf": gf, "ga": ga, "pts": int(after[-1])})
    return rows


def parse_pairings(html):
    """Paarungen eines Spieltags, ohne spielfrei und ohne verlegte Spiele."""
    cut = html.find("Verlegte Spiele")
    if cut > 0:
        html = html[:cut]
    soup = BeautifulSoup(html, "html.parser")
    pairs = []
    for tr in soup.find_all("tr"):
        names = club_names(tr)
        if len(names) == 2 and names[0] != names[1] and (names[0], names[1]) not in pairs:
            pairs.append((names[0], names[1]))
    return pairs


DATE_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})(?:\D{1,25}?(\d{1,2}):(\d{2}))?")


def parse_team_games(html):
    """Spiele der Referenzmannschaft: [(datetime_berlin_or_None, has_time, home, away)]."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    last_date = None
    for tr in soup.find_all("tr"):
        text = clean(tr.get_text(" "))
        m = DATE_RE.search(text)
        if m:
            d, mo, y, hh, mi = m.groups()
            y = int(y)
            y = y + 2000 if y < 100 else y
            last_date = (y, int(mo), int(d), hh, mi)
        names = club_names(tr)
        if len(names) == 2 and last_date:
            y, mo, d, hh, mi = last_date
            has_time = hh is not None
            dt = datetime(y, mo, d, int(hh) if has_time else DEFAULT_LOCK[0], int(mi) if has_time else DEFAULT_LOCK[1], tzinfo=BERLIN)
            out.append((dt, has_time, names[0], names[1]))
    return out


# ---------------- Supabase ----------------
def sb_headers(extra=None):
    h = {"apikey": SB_KEY, "Content-Type": "application/json"}
    if SB_KEY.startswith("eyJ"):
        h["Authorization"] = "Bearer " + SB_KEY
    if extra:
        h.update(extra)
    return h


def sb_get(path):
    r = requests.get(f"{SB_URL}/rest/v1/{path}", headers=sb_headers(), timeout=30)
    r.raise_for_status()
    return r.json()


def sb_upsert(table, rows):
    if not rows:
        return
    if DRY:
        log(f"[DRY] upsert {table}: {json.dumps(rows, ensure_ascii=False)[:600]}")
        return
    r = requests.post(f"{SB_URL}/rest/v1/{table}", headers=sb_headers({"Prefer": "resolution=merge-duplicates,return=minimal"}),
                      data=json.dumps(rows), timeout=30)
    if r.status_code >= 300:
        raise RuntimeError(f"Supabase {table}: {r.status_code} {r.text}")


def sb_patch(table, id_, patch):
    if DRY:
        log(f"[DRY] update {table} {id_}: {patch}")
        return
    r = requests.patch(f"{SB_URL}/rest/v1/{table}?id=eq.{requests.utils.quote(id_)}",
                       headers=sb_headers({"Prefer": "return=minimal"}), data=json.dumps(patch), timeout=30)
    if r.status_code >= 300:
        raise RuntimeError(f"Supabase {table} {id_}: {r.status_code} {r.text}")


# ---------------- Logik ----------------
def derive_results(matches, old_rows, new_rows, now):
    old = {r["team"]: r for r in old_rows}
    new = {r["team"]: r for r in new_rows}
    delta = {}
    for t, n in new.items():
        o = old.get(t)
        if o:
            delta[t] = {k: n[k] - o[k] for k in ("gp", "gf", "ga", "pts")}
    results = {}
    for m in matches:
        if m.get("hg") is not None or m.get("res_manual"):
            continue
        ko = parse_ts(m.get("kickoff"))
        if not ko or ko > now:
            continue
        dh, da = delta.get(m["home"]), delta.get(m["away"])
        cand = None
        if dh and dh["gp"] == 1:
            cand = (dh["gf"], dh["ga"])
        elif da and da["gp"] == 1:
            cand = (da["ga"], da["gf"])
        if cand is None:
            if (dh and dh["gp"] > 1) or (da and da["gp"] > 1):
                log(f"Unklar (Nachholspiel?): {m['home']} – {m['away']}, bitte von Hand eintragen")
            continue
        hg, ag = cand
        exp_h = 3 if hg > ag else 1 if hg == ag else 0
        exp_a = 3 if ag > hg else 1 if hg == ag else 0
        ok = True
        if dh and dh["gp"] == 1 and (dh["pts"] != exp_h or (dh["gf"], dh["ga"]) != (hg, ag)):
            ok = False
        if da and da["gp"] == 1 and (da["pts"] != exp_a or (da["gf"], da["ga"]) != (ag, hg)):
            ok = False
        if not ok:
            log(f"Widerspruch bei {m['home']} – {m['away']}, Ergebnis bleibt offen")
            continue
        results[m["id"]] = (hg, ag)
    return results


def parse_ts(s):
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def sunday_for(md, pairs, team_games, known_sundays):
    for dt, has_time, h, a in team_games:
        if (h, a) in pairs:
            return dt.date(), (h, a, dt, has_time)
    prev = [k for k in known_sundays if k < md]
    nxt = [k for k in known_sundays if k > md]
    if prev:
        p = max(prev)
        return known_sundays[p] + timedelta(days=7 * (md - p)), None
    if nxt:
        n = min(nxt)
        return known_sundays[n] - timedelta(days=7 * (n - md)), None
    return None, None


def main():
    if not SB_URL or not SB_KEY:
        sys.exit("SUPABASE_URL und SUPABASE_SECRET_KEY fehlen")
    now = datetime.now(timezone.utc)

    # 1. Tabelle holen und prüfen
    new_rows = parse_table(fetch(TABLE_URL))
    if len(new_rows) < 10:
        sys.exit(f"Tabelle nicht lesbar: nur {len(new_rows)} Zeilen gefunden. Hat fussball.de die Seite geändert?")
    if sum(r["gf"] for r in new_rows) != sum(r["ga"] for r in new_rows):
        sys.exit("Tabelle unplausibel: Summe Tore ungleich Summe Gegentore")
    log(f"Tabelle gelesen: {len(new_rows)} Teams")

    matches = sb_get("matches?select=*")
    snaps = {s["id"]: s["data"] for s in sb_get("snapshots?select=*")}
    old_rows = (snaps.get("latest") or {}).get("rows") or []

    # 2. Ergebnisse aus der Tabellendifferenz
    results = derive_results(matches, old_rows, new_rows, now)
    for mid, (hg, ag) in results.items():
        sb_patch("matches", mid, {"hg": hg, "ag": ag})
        m = next(x for x in matches if x["id"] == mid)
        m["hg"], m["ag"] = hg, ag
        log(f"Ergebnis {m['home']} – {m['away']}: {hg}:{ag}")

    # 3. Spieltage: erledigt / nächster
    by_md = {}
    for m in matches:
        by_md.setdefault(m["md"], []).append(m)
    done = [md for md, ms in by_md.items() if all(x.get("hg") is not None for x in ms)]
    after_md = max(done) if done else (snaps.get("latest") or {}).get("afterMd", 0)

    latest = {"rows": new_rows, "afterMd": after_md, "updated": now.isoformat()}
    sb_upsert("snapshots", [{"id": "latest", "data": latest}])

    upcoming = sorted(md for md, ms in by_md.items()
                      if any(x.get("hg") is None for x in ms) and not any(parse_ts(x.get("kickoff")) and parse_ts(x["kickoff"]) <= now for x in ms))
    if upcoming and str(upcoming[0]) not in snaps:
        sb_upsert("snapshots", [{"id": str(upcoming[0]), "data": latest}])
        log(f"Quoten-Tabelle für Spieltag {upcoming[0]} festgehalten")

    # 4. Nächste Spieltage anlegen, Anstoß von Overhagen übernehmen
    team_games = parse_team_games(fetch(TEAM_URL))
    log(f"Termine {REF_TEAM}: {len(team_games)} Spiele gefunden")
    known_sundays = {}
    for md, ms in by_md.items():
        kos = [parse_ts(x["kickoff"]) for x in ms if x.get("kickoff")]
        if kos:
            known_sundays[md] = max(kos).astimezone(BERLIN).date()

    # Termin-Änderungen bei bestehenden Overhagen-Spielen
    for m in matches:
        if REF_TEAM not in (m["home"], m["away"]) or m.get("ko_manual") or m.get("hg") is not None:
            continue
        for dt, has_time, h, a in team_games:
            if (h, a) == (m["home"], m["away"]) and has_time:
                cur = parse_ts(m.get("kickoff"))
                if not cur or abs((cur - dt).total_seconds()) > 60:
                    sb_patch("matches", m["id"], {"kickoff": dt.isoformat(), "ko_exact": True})
                    log(f"Neuer Termin {h} – {a}: {dt:%d.%m. %H:%M}")

    start = after_md + 1
    for md in range(start, min(start + 3, MAX_MD + 1)):
        if md in by_md:
            continue
        pairs = parse_pairings(fetch(MD_URL.format(md=md)))
        if len(pairs) < 3:
            log(f"Spieltag {md}: Paarungen nicht lesbar ({len(pairs)} gefunden), übersprungen")
            continue
        sunday, ref = sunday_for(md, pairs, team_games, known_sundays)
        if not sunday:
            log(f"Spieltag {md}: kein Datum ermittelbar, übersprungen")
            continue
        known_sundays[md] = sunday
        rows = []
        for i, (h, a) in enumerate(pairs, 1):
            ko = datetime(sunday.year, sunday.month, sunday.day, *DEFAULT_LOCK, tzinfo=BERLIN)
            exact = False
            if ref and (h, a) == (ref[0], ref[1]):
                ko, exact = ref[2], ref[3]
            rows.append({"id": f"md{md:02d}-{i:02d}", "md": md, "home": h, "away": a,
                         "kickoff": ko.isoformat(), "ko_exact": exact})
        sb_upsert("matches", rows)
        log(f"Spieltag {md} angelegt: {len(rows)} Spiele, Sonntag {sunday:%d.%m.%Y}")

    log("Fertig.")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("### Tipprunde Abgleich\n\n" + "\n".join(f"- {l}" for l in log_lines) + "\n")


if __name__ == "__main__":
    main()
