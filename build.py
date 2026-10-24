#!/usr/bin/env python3
"""Génère films.json : les films diffusés aujourd'hui à la TV (FR/francophone), avec genres et résumés."""
from collections import Counter
import gzip, io, json, os, re, unicodedata, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Paris")
XMLTV_URL = os.getenv("XMLTV_URL") or "https://xmltvfr.fr/xmltv/xmltv.xml.gz"
TMDB = os.getenv("TMDB_KEY")
DAYS = int(os.getenv("DAYS") or 3)  # aujourd'hui + les jours suivants
NTFY = os.getenv("NTFY_TOPIC")
CLAUDE = os.getenv("ANTHROPIC_API_KEY")
NOTIFY = [g.strip().lower() for g in (os.getenv("NOTIFY_GENRES") or "").split(",") if g.strip()]


def get(url, headers=None, data=None):
    req = urllib.request.Request(url, data=data, headers=headers or {"User-Agent": "films-du-jour"})
    return urllib.request.urlopen(req, timeout=180).read()


def parse_time(s):
    s = s.replace(" ", "")
    if len(s) > 14:
        d = datetime.strptime(s, "%Y%m%d%H%M%S%z")
    else:
        d = datetime.strptime(s, "%Y%m%d%H%M%S").replace(tzinfo=TZ)
    return d.astimezone(TZ)


def tmdb(path, **p):
    p.update(api_key=TMDB, language="fr-FR")
    return json.loads(get(f"https://api.themoviedb.org/3/{path}?{urllib.parse.urlencode(p)}"))


def norm(s):
    s = unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode()
    return " ".join(s.lower().replace("-", " ").split())


def tidy(genres, known):
    """Nettoie les genres : retire « Film d'… », fusionne accents/casse/tirets, supprime les doublons."""
    out, seen = [], set()
    for g in genres:
        g = re.sub(r"^(télé)?films?\s*(d'|de la |de l'|de |du |des )?", "", g.strip(), flags=re.I).strip()
        k = norm(g)
        if k and k not in seen:
            seen.add(k)
            out.append(known.get(k, g[:1].upper() + g[1:]))
    return out


def shorten(title, text):
    body = json.dumps({
        "model": "claude-haiku-5-5", "max_tokens": 120,
        "messages": [{"role": "user", "content":
            f"Résume en 2 phrases courtes, sans spoiler, le film « {title} » d'après ce texte :\n{text}"}],
    }).encode()
    hdr = {"x-api-key": CLAUDE, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    return json.loads(get("https://api.anthropic.com/v1/messages", hdr, body))["content"][0]["text"].strip()


# 1) Guide(s) TV (lecture en flux pour limiter la mémoire : les fichiers sont gros)
today = datetime.now(TZ).date()
chans, films, seen = {}, [], set()
days_seen = Counter()
for url in [u for u in re.split(r"[,\s]+", XMLTV_URL) if u]:
    raw = get(url)
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    for _, e in ET.iterparse(io.BytesIO(raw)):
        if e.tag == "channel":
            chans[e.get("id")] = e.findtext("display-name")
        elif e.tag == "programme":
            start = parse_time(e.get("start"))
            days_seen[start.date().isoformat()] += 1
            cats = [(c.text or "").lower() for c in e.findall("category")]
            is_film = any("film" in c or "cinéma" in c for c in cats)
            not_film = any("série" in c or "documentaire" in c for c in cats)
            key = (e.get("channel"), e.get("start"))
            if 0 <= (start.date() - today).days < DAYS and is_film and not not_film and key not in seen:
                seen.add(key)
                year = (e.findtext("date") or "")[:4]
                films.append({
                    "title": e.findtext("title") or "?", "channel": e.get("channel"),
                    "start": start.isoformat(), "stop": parse_time(e.get("stop")).isoformat(),
                    "year": year, "desc": e.findtext("desc") or "",
                    "genres": [c.capitalize() for c in cats if c not in ("film", "téléfilm")],
                    "rating": None, "poster": None,
                })
            e.clear()
    del raw

# Pays des chaînes : d'abord d'après le nom, sinon d'après la fin de l'identifiant (.ch, .be, .ca…)
QC = r"\b(tva|ici|radio-canada|télé-québec|tele-quebec|noovo|canal d|canal vie|séries\+|series\+|vrak|zeste|unis|ami-tv|super écran|télétoon)\b"
BE = r"\b(rtbf|la une|la deux|la trois|rtl-tvi|rtl tvi|club rtl|plug rtl|tipik|ln24|bx1|ab3|ab 3|notélé|télésambre|tvcom)\b"
CH = r"\b(rts|léman bleu|canal 9)\b"


def country(cid, name):
    n = (name or "").lower()
    for pat, pays in ((QC, "Québec"), (BE, "Belgique"), (CH, "Suisse")):
        if re.search(pat, n):
            return pays
    tld = (cid or "").rsplit(".", 1)[-1].lower()
    return {"ch": "Suisse", "be": "Belgique", "ca": "Canada", "lu": "Luxembourg", "mc": "Monaco"}.get(tld, "France")


print("Jours présents dans le guide :", ", ".join(f"{d} ({n} programmes)" for d, n in sorted(days_seen.items())))
by = {}
for cid, name in chans.items():
    by.setdefault(country(cid, name), set()).add(name or cid)
for pays, names in sorted(by.items()):
    print(f"{pays} : {len(names)} chaînes" + ("" if pays == "France" else " -> " + ", ".join(sorted(names))))

# 2) Genres, note et synopsis via TMDB
genre_names = {g["id"]: g["name"] for g in tmdb("genre/movie/list")["genres"]} if TMDB else {}
cache = {}
for f in films:
    f["country"] = country(f["channel"], chans.get(f["channel"]))
    f["channel"] = chans.get(f["channel"], f["channel"])
    f["summary"] = f.pop("desc")
    if TMDB:
        k = (f["title"], f["year"])
        if k not in cache:
            try:
                args = {"query": f["title"], **({"year": f["year"]} if f["year"] else {})}
                res = tmdb("search/movie", **args)["results"]
                cache[k] = res[0] if res else None
            except Exception as ex:
                print("TMDB:", f["title"], ex)
                cache[k] = None
        m = cache[k]
        if m:
            f["genres"] = [genre_names[i] for i in m.get("genre_ids", []) if i in genre_names] or f["genres"]
            f["rating"] = round(m["vote_average"], 1) if m.get("vote_count", 0) >= 20 else None
            f["poster"] = m.get("poster_path") and "https://image.tmdb.org/t/p/w185" + m["poster_path"]
            f["summary"] = m.get("overview") or f["summary"]

# 3) Résumés raccourcis par IA (optionnel)
if CLAUDE:
    for f in films:
        if len(f["summary"]) > 220:
            try:
                f["summary"] = shorten(f["title"], f["summary"])
            except Exception as ex:
                print("IA:", f["title"], ex)

known = {norm(n): n for n in genre_names.values()}
for f in films:
    f["genres"] = tidy(f["genres"], known)
films.sort(key=lambda f: f["start"])
with open("films.json", "w", encoding="utf-8") as fh:
    json.dump({"date": today.isoformat(), "films": films}, fh, ensure_ascii=False, indent=1)
print(len(films), "films")
print("Films par jour :", dict(sorted(Counter(f["start"][:10] for f in films).items())))

# 4) Notification push via ntfy.sh (optionnel)
if NTFY:
    hits = [f for f in films if f["start"][:10] == today.isoformat()
            and (not NOTIFY or any(g.lower() in NOTIFY for g in f["genres"]))]
    if hits:
        lines = [f'{f["start"][11:16]} {f["channel"]} : {f["title"]}' for f in hits[:10]]
        get(f"https://ntfy.sh/{NTFY}", {"Title": "Films du jour", "User-Agent": "films-du-jour"},
            "\n".join(lines).encode("utf-8"))
