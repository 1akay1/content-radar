#!/usr/bin/env python3
"""
AYBORN Content-Radar
====================
Zieht Reels von beobachteten Instagram-Accounts, rechnet Outlier-Scores,
destilliert Muster und Hook-Ideen und schreibt alles als JSON raus, das
das Dashboard direkt frisst.

Modi
----
  python3 radar.py --demo              -> Demo-Datensatz (kein Token noetig)
  python3 radar.py                     -> echter Lauf ueber Apify (APIFY_TOKEN noetig)
  python3 radar.py --discover          -> zusaetzlich neue Accounts vorschlagen
  python3 radar.py --input rohdaten.json  -> eigene/manuell gesammelte Rohdaten verarbeiten

Ausgabe: radar_output.json
"""

import argparse
import json
import math
import os
import random
import re
import statistics
import sys
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
OUT_PATH = os.path.join(HERE, "radar_output.json")

APIFY_RUN_URL = "https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items?token={token}"

# ---------------------------------------------------------------- Hilfsmittel

STOPWORDS = set("""
und oder aber der die das den dem des ein eine einer eines einem einen ist sind war waren
sein seine seinem ich du er sie es wir ihr mich dich sich uns euch mein dein für fuer mit
von zu zum zur im in am an auf aus bei bis durch gegen ohne um über ueber unter vor nach
seit während waehrend wie was wer wo wann warum wenn dann noch nur auch schon mehr sehr
mal so als dass daß man kann können koennen hat haben habe wird werden wurde nicht kein
keine keinen dieser diese dieses jeder jede jedes alle allen the and for you your with
this that from have has are was were will can just how what why when who not but our out
about into over more most all any get got make made now new one two three like some
""".split())

HOOK_MUSTER = [
    ("frage", r"^(wie|warum|was|wieso|wer|wo|wann|how|why|what|when|who)\b|\?$"),
    ("zahl", r"^\D{0,12}\d+\s"),
    ("fehler", r"\b(fehler|falsch|nie wieder|hoer auf|hör auf|stop|niemals|schlecht)\b"),
    ("ergebnis", r"\b(so habe ich|so hab ich|deshalb|ergebnis|resultat|vorher|nachher|von \d)\b"),
    ("behauptung", r"\b(niemand|keiner|jeder|die meisten|90%|die wahrheit|ehrlich)\b"),
    ("pov", r"^\s*(pov|povs?:)"),
    ("anleitung", r"\b(so gehts|so geht's|schritt|tutorial|anleitung|in \d+ (sekunden|minuten|schritten))\b"),
    ("behind", r"\b(behind the scenes|bts|dreh|setup|making of|wie wir)\b"),
]

FORMAT_MUSTER = [
    ("talking head", r"\b(erklaer|erklär|ich zeige|ich erkläre|talking)\b"),
    ("b-roll montage", r"\b(cinematic|montage|b-?roll|edit|farbe|color|grading|look)\b"),
    ("case study", r"\b(kunde|projekt|case|auftrag|imagefilm für|für die|zusammenarbeit)\b"),
    ("tutorial", r"\b(tutorial|so gehts|so geht's|tipps?|trick|preset|lut|einstellung)\b"),
    ("gear", r"\b(fx30|sony|dji|drohne|drone|objektiv|gimbal|licht|kamera|setup)\b"),
    ("bts", r"\b(bts|behind the scenes|dreh|set|making of)\b"),
]


def lade_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def iso_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def kw_label(dt=None):
    dt = dt or datetime.now(timezone.utc)
    jahr, woche, _ = dt.isocalendar()
    return f"{jahr}-KW{woche:02d}"


def zahl(x, default=0):
    try:
        if x is None:
            return default
        return int(x)
    except (TypeError, ValueError):
        return default


def parse_datum(wert):
    if not wert:
        return None
    if isinstance(wert, (int, float)):
        return datetime.fromtimestamp(wert, tz=timezone.utc)
    try:
        return datetime.fromisoformat(str(wert).replace("Z", "+00:00"))
    except ValueError:
        return None


def erste_zeile(text, maxlen=110):
    if not text:
        return ""
    zeile = text.strip().split("\n")[0].strip()
    zeile = re.sub(r"#\w+", "", zeile).strip()
    zeile = re.sub(r"\s+", " ", zeile)
    return zeile[:maxlen]


def klassifiziere(text, muster, fallback):
    t = (text or "").lower()
    treffer = [name for name, rx in muster if re.search(rx, t, re.IGNORECASE)]
    return treffer[0] if treffer else fallback


# ---------------------------------------------------------------- Datenabruf

def apify_call(actor, token, payload, timeout=600):
    url = APIFY_RUN_URL.format(actor=actor, token=token)
    daten = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=daten, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def hole_reels(cfg, token):
    """Holt Reels aller Watchlist-Accounts in EINEM Apify-Lauf."""
    accounts = []
    for gruppe in cfg["watchlist"].values():
        if isinstance(gruppe, list):
            accounts.extend(gruppe)
    accounts = [a.lstrip("@").strip() for a in accounts if a and not a.startswith("_")]
    if not accounts:
        print("! Watchlist ist leer. Erst --discover laufen lassen oder Handles in config.json eintragen.")
        return [], []

    payload = {
        "directUrls": [f"https://www.instagram.com/{a}/" for a in accounts],
        "resultsType": "posts",
        "resultsLimit": cfg["scan"]["reels_pro_account"],
        "onlyPostsNewerThan": (
            datetime.now(timezone.utc) - timedelta(days=cfg["scan"]["zeitfenster_tage"])
        ).strftime("%Y-%m-%d"),
        "addParentData": True,
    }
    print(f"-> Apify: {len(accounts)} Accounts x {payload['resultsLimit']} Beitraege")
    items = apify_call(cfg["apify"]["actor"], token, payload)
    return items, accounts


def speichere_config(cfg):
    sauber = {k: v for k, v in cfg.items()}
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(sauber, f, ensure_ascii=False, indent=2)


def uebernehme_accounts(cfg, account_stats, neue_accounts, max_gesamt=16):
    """Traegt die staerksten neu entdeckten Accounts dauerhaft in die Watchlist ein."""
    d = cfg["discovery"]
    bekannt = set()
    for gruppe in cfg["watchlist"].values():
        if isinstance(gruppe, list):
            bekannt.update(a.lstrip("@").lower() for a in gruppe)

    kandidaten = [
        a for a in account_stats
        if a["account"].lower() in neue_accounts
        and a["account"].lower() not in bekannt
        and a["account"] != "?"
        and d["min_follower"] <= a["follower"] <= d["max_follower"]
        and a["median_views"] > 0
    ]
    kandidaten.sort(key=lambda a: -a["median_views"])
    frei = max(max_gesamt - len(bekannt), 0)
    aufgenommen = [a["account"] for a in kandidaten[:frei]]
    if aufgenommen:
        cfg["watchlist"].setdefault("regional", [])
        cfg["watchlist"]["regional"].extend(aufgenommen)
        speichere_config(cfg)
        print("-> neu in die Watchlist: " + ", ".join("@" + a for a in aufgenommen))
    return aufgenommen


def hole_discovery(cfg, token):
    """Sucht neue Accounts ueber Hashtags und Orte."""
    d = cfg["discovery"]
    if not d.get("aktiv"):
        return []
    payload = {
        "directUrls": [f"https://www.instagram.com/explore/tags/{h}/" for h in d["hashtags"]],
        "resultsType": "posts",
        "resultsLimit": d["max_pro_hashtag"],
        "addParentData": True,
    }
    print(f"-> Apify Discovery: {len(d['hashtags'])} Hashtags")
    try:
        return apify_call(cfg["apify"]["actor"], token, payload)
    except urllib.error.URLError as e:
        print(f"! Discovery fehlgeschlagen: {e}")
        return []


# ---------------------------------------------------------------- Normalisierung

def normalisiere(items):
    """Apify-Rohdaten -> einheitliche Reel-Struktur."""
    reels = []
    for it in items:
        typ = (it.get("type") or "").lower()
        produkt = (it.get("productType") or "").lower()
        if typ not in ("video", "sidecar", "image") and produkt != "clips":
            continue
        ist_reel = produkt == "clips" or typ == "video"

        besitzer = it.get("ownerUsername") or (it.get("owner") or {}).get("username") or "?"
        caption = it.get("caption") or ""
        views = zahl(it.get("videoPlayCount")) or zahl(it.get("videoViewCount")) or zahl(it.get("playsCount"))
        likes = zahl(it.get("likesCount"))
        kommentare = zahl(it.get("commentsCount"))
        dauer = it.get("videoDuration")
        gepostet = parse_datum(it.get("timestamp"))
        follower = zahl((it.get("latestComments") and None) or it.get("ownerFollowersCount")) or zahl(
            (it.get("owner") or {}).get("followersCount")
        )

        reels.append({
            "id": it.get("shortCode") or it.get("id") or "",
            "account": besitzer,
            "url": it.get("url") or (f"https://www.instagram.com/p/{it.get('shortCode')}/" if it.get("shortCode") else ""),
            "thumbnail": it.get("displayUrl") or "",
            "caption": caption[:600],
            "hook": erste_zeile(caption),
            "views": views,
            "likes": likes,
            "kommentare": kommentare,
            "dauer": round(float(dauer), 1) if dauer else None,
            "gepostet": gepostet.isoformat() if gepostet else None,
            "follower": follower,
            "ist_reel": ist_reel,
            "hashtags": re.findall(r"#(\w+)", caption)[:12],
        })
    return reels


# ---------------------------------------------------------------- Analyse

def berechne_scores(reels, cfg):
    """Outlier-Score je Reel: geschlagener Account-Median + Reichweite + Aktualitaet."""
    s = cfg["scoring"]
    jetzt = datetime.now(timezone.utc)

    nach_account = {}
    for r in reels:
        nach_account.setdefault(r["account"], []).append(r)

    account_stats = {}
    for acc, liste in nach_account.items():
        views = [r["views"] for r in liste if r["views"] > 0]
        median = statistics.median(views) if views else 0
        follower = max([r["follower"] for r in liste] or [0])
        account_stats[acc] = {
            "account": acc,
            "median_views": int(median),
            "beste_views": max(views) if views else 0,
            "reels": len(liste),
            "follower": follower,
            "engagement": round(
                statistics.mean(
                    [(r["likes"] + r["kommentare"]) / follower * 100 for r in liste if follower]
                ), 2
            ) if follower else 0.0,
        }

    for r in reels:
        st = account_stats[r["account"]]
        median = st["median_views"] or 1
        r["outlier"] = round(r["views"] / median, 2) if r["views"] else 0.0
        r["reichweite_quote"] = round(r["views"] / st["follower"], 2) if st["follower"] else 0.0

        gepostet = parse_datum(r["gepostet"])
        alter = (jetzt - gepostet).days if gepostet else 90
        aktualitaet = 0.5 ** (max(alter, 0) / s["halbwertszeit_tage"])
        r["alter_tage"] = alter

        r["score"] = round(
            min(r["outlier"] / 5, 1.0) * s["gewicht_outlier"]
            + min(r["reichweite_quote"] / 3, 1.0) * s["gewicht_reichweite"]
            + aktualitaet * s["gewicht_aktualitaet"],
            4,
        ) * 100
        r["score"] = round(r["score"], 1)
        r["ist_outlier"] = r["outlier"] >= s["outlier_schwelle"]
        r["hook_typ"] = klassifiziere(r["hook"], HOOK_MUSTER, "aussage")
        r["format"] = klassifiziere(f"{r['hook']} {r['caption']}", FORMAT_MUSTER, "sonstiges")

    reels.sort(key=lambda x: x["score"], reverse=True)
    return reels, list(account_stats.values())


def finde_muster(reels, cfg):
    """Destilliert Themen, Hooks, Formate, Laengen aus den Outliern."""
    outlier = [r for r in reels if r["ist_outlier"]] or reels[:20]
    rest = [r for r in reels if not r["ist_outlier"]]

    def anteile(liste, key):
        z = {}
        for r in liste:
            z[r[key]] = z.get(r[key], 0) + 1
        gesamt = len(liste) or 1
        return sorted(
            ({"name": k, "anzahl": v, "anteil": round(v / gesamt * 100)} for k, v in z.items()),
            key=lambda x: -x["anzahl"],
        )

    woerter = {}
    for r in outlier:
        for w in re.findall(r"[a-zA-ZäöüÄÖÜß]{4,}", (r["caption"] or "").lower()):
            if w not in STOPWORDS:
                woerter[w] = woerter.get(w, 0) + 1
    themen = sorted(woerter.items(), key=lambda x: -x[1])[:14]

    laengen = [r["dauer"] for r in outlier if r["dauer"]]
    laengen_rest = [r["dauer"] for r in rest if r["dauer"]]

    hashtags = {}
    for r in outlier:
        for h in r["hashtags"]:
            hashtags[h.lower()] = hashtags.get(h.lower(), 0) + 1

    return {
        "hook_typen": anteile(outlier, "hook_typ"),
        "formate": anteile(outlier, "format"),
        "themen": [{"wort": w, "anzahl": n} for w, n in themen],
        "hashtags": sorted(
            ({"tag": k, "anzahl": v} for k, v in hashtags.items()), key=lambda x: -x["anzahl"]
        )[:12],
        "laenge_median_outlier": round(statistics.median(laengen), 1) if laengen else None,
        "laenge_median_rest": round(statistics.median(laengen_rest), 1) if laengen_rest else None,
        "outlier_anzahl": len([r for r in reels if r["ist_outlier"]]),
        "basis": len(reels),
    }


def baue_ideen(reels, muster, cfg):
    """Uebersetzt die Muster in konkrete, drehbare Ideen fuer AYBORN MEDIA."""
    top = [r for r in reels if r["ist_outlier"]][:12] or reels[:8]
    ideen = []
    for i, r in enumerate(top[:8]):
        ideen.append({
            "id": f"idee-{kw_label()}-{i+1}",
            "titel": r["hook"][:80] or f"Format von @{r['account']}",
            "quelle_url": r["url"],
            "quelle_account": r["account"],
            "quelle_views": r["views"],
            "outlier": r["outlier"],
            "hook_typ": r["hook_typ"],
            "format": r["format"],
            "warum": (
                f"Schlug den eigenen Account-Median um Faktor {r['outlier']} "
                f"({r['views']:,} Views bei {r['follower']:,} Followern).".replace(",", ".")
            ),
            "uebertrag": "",
            "aufwand": "mittel",
            "status": "idee",
            "woche": kw_label(),
        })
    return ideen


# ---------------------------------------------------------------- Demo-Daten

def demo_daten(cfg):
    """Realistisch geformter, aber klar gekennzeichneter Demo-Datensatz."""
    random.seed(7)
    accounts = [
        ("demo.filmstudio_nord", 12400),
        ("demo.videograf_mv", 3100),
        ("demo.imagefilm_hamburg", 28700),
        ("demo.dronekollektiv", 54300),
        ("demo.hochzeitsfilm_ostsee", 8900),
        ("demo.creative_agency_hh", 19600),
    ]
    hooks = [
        ("Wie ich einen Imagefilm in 6 Stunden drehe", "anleitung", "tutorial"),
        ("3 Fehler, die 90% der Videografen beim Interview machen", "zahl", "talking head"),
        ("POV: Der Kunde sagt 'mach mal was Kreatives'", "pov", "bts"),
        ("Warum dein B-Roll langweilig aussieht", "frage", "b-roll montage"),
        ("Von 0 auf 12 Anfragen im Monat - so lief es wirklich", "ergebnis", "case study"),
        ("Niemand redet über diesen Teil vom Drehtag", "behauptung", "bts"),
        ("Das Licht-Setup, das ich in jedem Firmenvideo nutze", "aussage", "gear"),
        ("Hör auf, Drohnenaufnahmen so zu schneiden", "fehler", "b-roll montage"),
        ("So sieht ein Drehtag für einen Handwerksbetrieb aus", "anleitung", "case study"),
        ("Der Unterschied zwischen 500 EUR und 5000 EUR Video", "aussage", "case study"),
        ("Behind the Scenes: Imagefilm für eine Tischlerei", "behind", "bts"),
        ("Was ich nach 100 Drehtagen anders mache", "zahl", "talking head"),
    ]
    jetzt = datetime.now(timezone.utc)
    items = []
    for acc, follower in accounts:
        basis = max(int(follower * random.uniform(0.35, 0.9)), 400)
        for j in range(10):
            hook, _, _ = hooks[(hash(acc) + j) % len(hooks)]
            faktor = random.choice([0.4, 0.6, 0.8, 1.0, 1.0, 1.2, 1.5, 2.6, 4.1, 7.3])
            items.append({
                "shortCode": f"DEMO{abs(hash(acc+str(j))) % 100000}",
                "type": "video",
                "productType": "clips",
                "ownerUsername": acc,
                "owner": {"followersCount": follower},
                "ownerFollowersCount": follower,
                "caption": f"{hook}\n\nGedreht in einem halben Drehtag, geschnitten am Abend. #videoproduktion #imagefilm #schwerin",
                "videoPlayCount": int(basis * faktor),
                "likesCount": int(basis * faktor * random.uniform(0.02, 0.06)),
                "commentsCount": int(basis * faktor * random.uniform(0.001, 0.006)),
                "videoDuration": random.choice([11.0, 14.5, 18.0, 22.5, 27.0, 34.0, 41.0, 58.0]),
                "timestamp": (jetzt - timedelta(days=random.randint(1, 55))).isoformat(),
                "url": "https://www.instagram.com/",
                "displayUrl": "",
            })
    return items


# ---------------------------------------------------------------- Ausgabe

def baue_output(reels, account_stats, muster, ideen, cfg, quelle):
    eigener = cfg.get("eigener_account", "")
    eigene = [r for r in reels if r["account"].lower() == eigener.lower()]
    fremde = [r for r in reels if r["account"].lower() != eigener.lower()]
    alle_views = [r["views"] for r in fremde if r["views"] > 0]

    return {
        "meta": {
            "woche": kw_label(),
            "erstellt": iso_now(),
            "quelle": quelle,
            "eigener_account": eigener,
            "accounts_gescannt": len(account_stats),
            "reels_gescannt": len(reels),
            "demo": quelle == "demo",
        },
        "kennzahlen": {
            "outlier": len([r for r in reels if r["ist_outlier"]]),
            "median_views_nische": int(statistics.median(alle_views)) if alle_views else 0,
            "top_views": max(alle_views) if alle_views else 0,
            "eigene_reels": len(eigene),
            "eigene_median_views": int(statistics.median([r["views"] for r in eigene])) if eigene else 0,
            "kapazitaet": cfg["kapazitaet"]["posts_pro_woche"],
        },
        "reels": reels[:120],
        "accounts": sorted(account_stats, key=lambda a: -a["median_views"]),
        "muster": muster,
        "ideen": ideen,
        "kapazitaet": cfg["kapazitaet"],
    }


def main():
    p = argparse.ArgumentParser(description="AYBORN Content-Radar")
    p.add_argument("--demo", action="store_true", help="Demo-Datensatz statt echtem Abruf")
    p.add_argument("--discover", action="store_true", help="Neue Accounts ueber Hashtags suchen")
    p.add_argument("--input", help="JSON mit Apify-Rohdaten statt Live-Abruf")
    p.add_argument("--out", default=OUT_PATH)
    args = p.parse_args()

    cfg = lade_config()
    quelle = "apify"
    neue_accounts = set()

    if args.demo:
        items = demo_daten(cfg)
        quelle = "demo"
    elif args.input:
        with open(args.input, "r", encoding="utf-8") as f:
            items = json.load(f)
        quelle = "import"
    else:
        token = os.environ.get(cfg["apify"]["token_env"])
        if not token:
            print(f"! {cfg['apify']['token_env']} nicht gesetzt. Mit --demo starten oder Token setzen.")
            sys.exit(1)
        items, _ = hole_reels(cfg, token)
        if args.discover:
            disco = hole_discovery(cfg, token)
            neue_accounts = {
                (it.get("ownerUsername") or (it.get("owner") or {}).get("username") or "").lower()
                for it in disco
            }
            neue_accounts.discard("")
            items += disco

    reels = normalisiere(items)
    if not reels:
        print("! Keine Reels gefunden.")
        sys.exit(1)

    reels, account_stats = berechne_scores(reels, cfg)
    if neue_accounts:
        uebernehme_accounts(cfg, account_stats, neue_accounts)
    muster = finde_muster(reels, cfg)
    ideen = baue_ideen(reels, muster, cfg)
    out = baue_output(reels, account_stats, muster, ideen, cfg, quelle)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"OK  {len(reels)} Reels, {len(account_stats)} Accounts, "
          f"{out['kennzahlen']['outlier']} Outlier -> {args.out}")


if __name__ == "__main__":
    main()
