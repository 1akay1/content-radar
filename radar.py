#!/usr/bin/env python3
"""
AYBORN Content-Radar
====================
Zieht Reels von beobachteten Instagram-Accounts, rechnet Outlier-Scores,
destilliert Muster und Hook-Ideen und schreibt alles als JSON raus, das
das Dashboard direkt frisst.

Modi
----
  python3 radar.py --demo                 -> Demo-Datensatz (kein Token noetig)
  python3 radar.py                        -> echter Lauf ueber Apify (APIFY_TOKEN)
  python3 radar.py --discover             -> zusaetzlich neue Accounts suchen
  python3 radar.py --input rohdaten.json  -> vorhandene Apify-Rohdaten verarbeiten

Wie die Discovery funktioniert (wichtig)
----------------------------------------
Ein Hashtag-Scan bei Instagram liefert nur Likes und Kommentare, KEINE Views
und keine Video-Kennzeichnung. Er taugt deshalb nur zum Finden von Accounts,
nie zum Messen. Der Radar macht daraus zwei Stufen:

  Stufe 1  Hashtags scannen  -> Kandidaten-Handles sammeln, nach Branchen-
                                begriffen im Namen/Text filtern
  Stufe 2  diese Kandidaten als Profile scannen -> echte Reels mit Views,
                                Laenge, Datum; Follower ueber einen
                                separaten, billigen "details"-Aufruf

Ausgabe: radar_output.json
"""

import argparse
import json
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

# ---------------------------------------------------------------- Wortlisten

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

# Begriffe, an denen ein Branchen-Account erkennbar ist
BRANCHE = re.compile(
    r"film|video|media|medien|foto|photo|studio|creativ|kreativ|motion|produkt|"
    r"agentur|visual|drohne|drone|cut|frame|lens|werbe|content|kamera|regie",
    re.IGNORECASE,
)

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


# ---------------------------------------------------------------- Hilfsmittel

def lade_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def speichere_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def iso_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def kw_label(dt=None):
    dt = dt or datetime.now(timezone.utc)
    jahr, woche, _ = dt.isocalendar()
    return f"{jahr}-KW{woche:02d}"


def zahl(x, default=0):
    try:
        return default if x is None else int(x)
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
    return re.sub(r"\s+", " ", zeile)[:maxlen]


def klassifiziere(text, muster, fallback):
    t = (text or "").lower()
    for name, rx in muster:
        if re.search(rx, t, re.IGNORECASE):
            return name
    return fallback


def handle(it):
    return (it.get("ownerUsername") or (it.get("owner") or {}).get("username") or "").lower()


def watchlist_handles(cfg):
    raus = []
    for schluessel, gruppe in cfg["watchlist"].items():
        if schluessel.startswith("_") or not isinstance(gruppe, list):
            continue
        raus.extend(a.lstrip("@").strip().lower() for a in gruppe if a)
    return raus


# ---------------------------------------------------------------- Apify

def apify_call(actor, token, payload, timeout=900):
    url = APIFY_RUN_URL.format(actor=actor, token=token)
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def hole_posts(cfg, token, accounts):
    """Beitraege von Profilen. Nur hier gibt es Views, Laengen und Reel-Flag."""
    if not accounts:
        return []
    payload = {
        "directUrls": [f"https://www.instagram.com/{a}/" for a in accounts],
        "resultsType": "posts",
        "resultsLimit": cfg["scan"]["reels_pro_account"],
        "onlyPostsNewerThan": (
            datetime.now(timezone.utc) - timedelta(days=cfg["scan"]["zeitfenster_tage"])
        ).strftime("%Y-%m-%d"),
        "addParentData": True,
    }
    print(f"-> Profile: {len(accounts)} Accounts x {payload['resultsLimit']} Beitraege")
    try:
        return apify_call(cfg["apify"]["actor"], token, payload)
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        print(f"! Profil-Scan fehlgeschlagen: {e}")
        return []


def hole_follower(cfg, token, accounts):
    """Ein Ergebnis je Account, nur fuer die Follower-Zahl. Billig."""
    if not accounts:
        return {}
    payload = {
        "directUrls": [f"https://www.instagram.com/{a}/" for a in accounts],
        "resultsType": "details",
        "resultsLimit": 1,
    }
    print(f"-> Follower: {len(accounts)} Profile")
    try:
        items = apify_call(cfg["apify"]["actor"], token, payload)
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        print(f"! Follower-Abruf fehlgeschlagen: {e}")
        return {}
    karte = {}
    for it in items:
        name = (it.get("username") or "").lower()
        f = zahl(it.get("followersCount")) or zahl(it.get("followersCount", 0))
        if name and f:
            karte[name] = f
    print(f"   {len(karte)} Follower-Zahlen erhalten")
    return karte


def hole_hashtags(cfg, token):
    """Stufe 1 der Discovery: nur zum Finden von Accounts, nicht zum Messen."""
    d = cfg["discovery"]
    if not d.get("aktiv"):
        return []
    payload = {
        "directUrls": [f"https://www.instagram.com/explore/tags/{h}/" for h in d["hashtags"]],
        "resultsType": "posts",
        "resultsLimit": d["max_pro_hashtag"],
    }
    print(f"-> Hashtags: {len(d['hashtags'])} Tags")
    try:
        return apify_call(cfg["apify"]["actor"], token, payload)
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        print(f"! Hashtag-Scan fehlgeschlagen: {e}")
        return []


def finde_kandidaten(items, bekannt, max_neu):
    """Aus Hashtag-Treffern die Accounts filtern, die nach Branche aussehen."""
    im_namen, im_text = {}, {}
    for it in items:
        name = handle(it)
        if not name or name in bekannt:
            continue
        punkte = zahl(it.get("likesCount")) + 1
        if BRANCHE.search(name):
            im_namen[name] = im_namen.get(name, 0) + punkte
        elif BRANCHE.search((it.get("caption") or "")[:300]):
            im_text[name] = im_text.get(name, 0) + punkte

    def sortiert(d):
        return [n for n, _ in sorted(d.items(), key=lambda x: -x[1])]

    # Ein Branchenbegriff im Handle ist ein starkes Signal, einer im Text ein
    # schwaches - ein Baumarkt, der "Werbefilm" schreibt, ist kein Kandidat.
    beste = sortiert(im_namen)[:max_neu]
    if len(beste) < max_neu:
        beste += sortiert(im_text)[:max_neu - len(beste)]
    print(f"   {len(im_namen)} Treffer im Handle, {len(im_text)} nur im Text "
          f"-> {len(beste)} werden geprueft")
    return beste


# ---------------------------------------------------------------- Aufbereitung

def normalisiere(items, follower_karte=None):
    follower_karte = follower_karte or {}
    reels = []
    for it in items:
        typ = (it.get("type") or "").lower()
        produkt = (it.get("productType") or "").lower()
        name = handle(it) or "?"
        caption = it.get("caption") or ""
        views = (zahl(it.get("videoPlayCount")) or zahl(it.get("videoViewCount"))
                 or zahl(it.get("playsCount")))
        dauer = it.get("videoDuration")
        gepostet = parse_datum(it.get("timestamp"))
        follower = (follower_karte.get(name)
                    or zahl(it.get("ownerFollowersCount"))
                    or zahl((it.get("owner") or {}).get("followersCount")))

        reels.append({
            "id": it.get("shortCode") or it.get("id") or "",
            "account": name,
            "url": it.get("url") or (f"https://www.instagram.com/p/{it.get('shortCode')}/"
                                     if it.get("shortCode") else ""),
            "caption": caption[:600],
            "hook": erste_zeile(caption),
            "views": views,
            "likes": zahl(it.get("likesCount")),
            "kommentare": zahl(it.get("commentsCount")),
            "dauer": round(float(dauer), 1) if dauer else None,
            "gepostet": gepostet.isoformat() if gepostet else None,
            "follower": follower,
            "ist_reel": produkt == "clips" or typ == "video",
            "hashtags": re.findall(r"#(\w+)", caption)[:12],
        })
    return reels


def waehle_metrik(reels):
    """Views wenn vorhanden, sonst Likes. Ein Hashtag-Scan liefert keine Views."""
    mit_views = sum(1 for r in reels if r["views"] > 0)
    if reels and mit_views >= max(len(reels) * 0.3, 5):
        return "views"
    return "likes"


def berechne_scores(reels, cfg, metrik):
    s = cfg["scoring"]
    jetzt = datetime.now(timezone.utc)

    for r in reels:
        r["wert"] = r["views"] if metrik == "views" else r["likes"] + r["kommentare"] * 3

    nach_account = {}
    for r in reels:
        nach_account.setdefault(r["account"], []).append(r)

    stats = {}
    for acc, liste in nach_account.items():
        werte = [r["wert"] for r in liste if r["wert"] > 0]
        median = statistics.median(werte) if werte else 0
        follower = max([r["follower"] for r in liste] or [0])
        stats[acc] = {
            "account": acc,
            "median_views": int(median),
            "beste_views": max(werte) if werte else 0,
            "reels": len(liste),
            "follower": follower,
            "engagement": round(statistics.mean(
                [(r["likes"] + r["kommentare"]) / follower * 100 for r in liste]
            ), 2) if follower else 0.0,
        }

    for r in reels:
        st = stats[r["account"]]
        median = st["median_views"] or 1
        r["outlier"] = round(r["wert"] / median, 2) if r["wert"] else 0.0
        r["reichweite_quote"] = round(r["wert"] / st["follower"], 2) if st["follower"] else 0.0

        gepostet = parse_datum(r["gepostet"])
        alter = (jetzt - gepostet).days if gepostet else 90
        r["alter_tage"] = alter
        aktualitaet = 0.5 ** (max(alter, 0) / s["halbwertszeit_tage"])

        r["score"] = round((
            min(r["outlier"] / 5, 1.0) * s["gewicht_outlier"]
            + min(r["reichweite_quote"] / 3, 1.0) * s["gewicht_reichweite"]
            + aktualitaet * s["gewicht_aktualitaet"]
        ) * 100, 1)
        r["ist_outlier"] = r["outlier"] >= s["outlier_schwelle"] and st["reels"] >= 3
        r["hook_typ"] = klassifiziere(r["hook"], HOOK_MUSTER, "aussage")
        r["format"] = klassifiziere(f"{r['hook']} {r['caption']}", FORMAT_MUSTER, "sonstiges")

    reels.sort(key=lambda x: x["score"], reverse=True)
    return reels, list(stats.values())


def uebernehme_accounts(cfg, stats, kandidaten, max_gesamt=16):
    """Traegt gepruefte Kandidaten dauerhaft in die Watchlist ein."""
    d = cfg["discovery"]
    bekannt = set(watchlist_handles(cfg))
    hat_follower = any(a["follower"] > 0 for a in stats)

    passend = []
    for a in stats:
        if a["account"] not in kandidaten or a["account"] in bekannt:
            continue
        if a["median_views"] <= 0 or a["reels"] < 3:
            continue
        if hat_follower and not (d["min_follower"] <= a["follower"] <= d["max_follower"]):
            continue
        passend.append(a)

    passend.sort(key=lambda a: -a["median_views"])
    frei = max(max_gesamt - len(bekannt), 0)
    neu = [a["account"] for a in passend[:frei]]
    if neu:
        cfg["watchlist"].setdefault("regional", [])
        cfg["watchlist"]["regional"].extend(neu)
        speichere_config(cfg)
        print("-> neu in der Watchlist: " + ", ".join("@" + n for n in neu))
    else:
        print("-> keine neuen Accounts uebernommen")
    return neu


def finde_muster(reels, cfg):
    outlier = [r for r in reels if r["ist_outlier"]] or reels[:20]
    rest = [r for r in reels if not r["ist_outlier"]]

    def anteile(liste, key):
        z = {}
        for r in liste:
            z[r[key]] = z.get(r[key], 0) + 1
        gesamt = len(liste) or 1
        return sorted(({"name": k, "anzahl": v, "anteil": round(v / gesamt * 100)}
                       for k, v in z.items()), key=lambda x: -x["anzahl"])

    woerter = {}
    for r in outlier:
        for w in re.findall(r"[a-zA-ZäöüÄÖÜß]{4,}", (r["caption"] or "").lower()):
            if w not in STOPWORDS:
                woerter[w] = woerter.get(w, 0) + 1

    laengen = [r["dauer"] for r in outlier if r["dauer"]]
    laengen_rest = [r["dauer"] for r in rest if r["dauer"]]

    hashtags = {}
    for r in outlier:
        for h in r["hashtags"]:
            hashtags[h.lower()] = hashtags.get(h.lower(), 0) + 1

    return {
        "hook_typen": anteile(outlier, "hook_typ"),
        "formate": anteile(outlier, "format"),
        "themen": [{"wort": w, "anzahl": n}
                   for w, n in sorted(woerter.items(), key=lambda x: -x[1])[:14]],
        "hashtags": sorted(({"tag": k, "anzahl": v} for k, v in hashtags.items()),
                           key=lambda x: -x["anzahl"])[:12],
        "laenge_median_outlier": round(statistics.median(laengen), 1) if laengen else None,
        "laenge_median_rest": round(statistics.median(laengen_rest), 1) if laengen_rest else None,
        "outlier_anzahl": len([r for r in reels if r["ist_outlier"]]),
        "basis": len(reels),
    }


def baue_ideen(reels):
    top = [r for r in reels if r["ist_outlier"]][:12] or reels[:8]
    ideen = []
    for i, r in enumerate(top[:8]):
        ideen.append({
            "id": f"idee-{kw_label()}-{i+1}",
            "titel": r["hook"][:80] or f"Format von @{r['account']}",
            "quelle_url": r["url"],
            "quelle_account": r["account"],
            "quelle_views": r["wert"],
            "outlier": r["outlier"],
            "hook_typ": r["hook_typ"],
            "format": r["format"],
            "warum": (f"Schlug den eigenen Account-Median um Faktor {r['outlier']} "
                      f"({r['wert']:,} bei {r['follower']:,} Followern).".replace(",", ".")),
            "aufwand": "mittel",
            "status": "idee",
            "woche": kw_label(),
        })
    return ideen


# ---------------------------------------------------------------- Demo

def demo_daten():
    random.seed(7)
    accounts = [("demo.filmstudio_nord", 12400), ("demo.videograf_mv", 3100),
                ("demo.imagefilm_hamburg", 28700), ("demo.dronekollektiv", 54300),
                ("demo.hochzeitsfilm_ostsee", 8900), ("demo.creative_agency_hh", 19600)]
    hooks = ["Wie ich einen Imagefilm in 6 Stunden drehe",
             "3 Fehler, die 90% der Videografen beim Interview machen",
             "POV: Der Kunde sagt 'mach mal was Kreatives'",
             "Warum dein B-Roll langweilig aussieht",
             "Von 0 auf 12 Anfragen im Monat - so lief es wirklich",
             "Niemand redet über diesen Teil vom Drehtag",
             "Das Licht-Setup, das ich in jedem Firmenvideo nutze",
             "Hör auf, Drohnenaufnahmen so zu schneiden",
             "So sieht ein Drehtag für einen Handwerksbetrieb aus",
             "Der Unterschied zwischen 500 EUR und 5000 EUR Video",
             "Behind the Scenes: Imagefilm für eine Tischlerei",
             "Was ich nach 100 Drehtagen anders mache"]
    jetzt = datetime.now(timezone.utc)
    items = []
    for acc, follower in accounts:
        basis = max(int(follower * random.uniform(0.35, 0.9)), 400)
        for j in range(10):
            hook = hooks[(hash(acc) + j) % len(hooks)]
            faktor = random.choice([0.4, 0.6, 0.8, 1.0, 1.0, 1.2, 1.5, 2.6, 4.1, 7.3])
            items.append({
                "shortCode": f"DEMO{abs(hash(acc + str(j))) % 100000}",
                "type": "video", "productType": "clips",
                "ownerUsername": acc, "ownerFollowersCount": follower,
                "caption": f"{hook}\n\nGedreht in einem halben Drehtag. #videoproduktion #imagefilm #schwerin",
                "videoPlayCount": int(basis * faktor),
                "likesCount": int(basis * faktor * random.uniform(0.02, 0.06)),
                "commentsCount": int(basis * faktor * random.uniform(0.001, 0.006)),
                "videoDuration": random.choice([11.0, 14.5, 18.0, 22.5, 27.0, 34.0, 41.0, 58.0]),
                "timestamp": (jetzt - timedelta(days=random.randint(1, 55))).isoformat(),
                "url": "https://www.instagram.com/",
            })
    return items


# ---------------------------------------------------------------- Ausgabe

def baue_output(reels, stats, muster, ideen, cfg, quelle, metrik, neue):
    eigener = (cfg.get("eigener_account") or "").lower()
    eigene = [r for r in reels if r["account"] == eigener]
    fremde = [r for r in reels if r["account"] != eigener]
    werte = [r["wert"] for r in fremde if r["wert"] > 0]

    return {
        "meta": {
            "woche": kw_label(), "erstellt": iso_now(), "quelle": quelle,
            "metrik": metrik, "eigener_account": eigener,
            "accounts_gescannt": len(stats), "reels_gescannt": len(reels),
            "neue_accounts": neue, "demo": quelle == "demo",
        },
        "kennzahlen": {
            "outlier": len([r for r in reels if r["ist_outlier"]]),
            "median_views_nische": int(statistics.median(werte)) if werte else 0,
            "top_views": max(werte) if werte else 0,
            "eigene_reels": len(eigene),
            "eigene_median_views": int(statistics.median([r["wert"] for r in eigene])) if eigene else 0,
            "kapazitaet": cfg["kapazitaet"]["posts_pro_woche"],
        },
        "reels": reels[:120],
        "accounts": sorted(stats, key=lambda a: -a["median_views"]),
        "muster": muster,
        "ideen": ideen,
        "kapazitaet": cfg["kapazitaet"],
    }


def main():
    p = argparse.ArgumentParser(description="AYBORN Content-Radar")
    p.add_argument("--demo", action="store_true")
    p.add_argument("--discover", action="store_true")
    p.add_argument("--input")
    p.add_argument("--out", default=OUT_PATH)
    args = p.parse_args()

    cfg = lade_config()
    quelle, kandidaten, follower_karte = "apify", [], {}

    if args.demo:
        items, quelle = demo_daten(), "demo"
    elif args.input:
        with open(args.input, "r", encoding="utf-8") as f:
            items = json.load(f)
        quelle = "import"
    else:
        token = os.environ.get(cfg["apify"]["token_env"])
        if not token:
            print(f"! {cfg['apify']['token_env']} nicht gesetzt.")
            sys.exit(1)

        beobachtet = watchlist_handles(cfg)
        eigener = (cfg.get("eigener_account") or "").strip().lstrip("@").lower()
        if eigener and eigener not in beobachtet:
            beobachtet.append(eigener)

        if args.discover:
            kandidaten = finde_kandidaten(
                hole_hashtags(cfg, token), set(beobachtet),
                cfg["discovery"].get("max_kandidaten", 12))

        alle = beobachtet + kandidaten
        if not alle:
            print("! Nichts zu scannen. Accounts in config.json eintragen oder --discover nutzen.")
            sys.exit(1)

        follower_karte = hole_follower(cfg, token, alle)
        items = hole_posts(cfg, token, alle)

    reels = normalisiere(items, follower_karte)

    # Bilder rauswerfen, sobald genug echte Videos da sind
    videos = [r for r in reels if r["ist_reel"]]
    if len(videos) >= 10:
        reels = videos

    if not reels:
        print("! Keine Beitraege gefunden.")
        sys.exit(1)

    metrik = waehle_metrik(reels)
    if metrik == "likes":
        print("! Keine View-Zahlen in den Daten - es wird nach Likes bewertet.")

    reels, stats = berechne_scores(reels, cfg, metrik)
    neue = uebernehme_accounts(cfg, stats, set(kandidaten)) if kandidaten else []
    muster = finde_muster(reels, cfg)
    ideen = baue_ideen(reels)
    out = baue_output(reels, stats, muster, ideen, cfg, quelle, metrik, neue)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"OK  {len(reels)} Beitraege, {len(stats)} Accounts, "
          f"{out['kennzahlen']['outlier']} Ausreisser, Metrik {metrik} -> {args.out}")


if __name__ == "__main__":
    main()
