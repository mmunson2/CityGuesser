# -*- coding: utf-8 -*-
"""Compile the cities the game asks about: about a thousand places a player
has probably heard of, ranked by how well known they are.

    python3 build/build_cities.py data/cities.js

"Well known" has no list of its own, so it is measured from two signals:

  * English Wikipedia pageviews over the last twelve months - what people
    actually look up. Las Vegas and Marrakesh read far above towns ten
    times their size.
  * Wikidata sitelinks - how many Wikipedias (and Wikivoyage, Commons...)
    carry an article. Keeps the list global rather than English-speaking,
    but on its own it overrates small European towns that bot-written
    Wikipedias give a stub each.

Each is turned into a percentile over all candidates, weighted, and a
national capital or a city holding a UNESCO World Heritage Site gets a
bonus. Every one of the 197 countries is guaranteed at least one city.

Candidates are every human settlement on Wikidata with at least
MIN_SITELINKS sitelinks (a few thousand). Responses are cached under
build/cache/, so a rebuild is quick; --refresh fetches everything again.

Hand corrections live in build/cities_curated.py. The facts a player can
ask for as a hint are written by hand, not fetched: build/cities_facts.json,
keyed by Wikidata id, a few short sentences per city that name neither the
city nor its country. Every candidate, with
its score and why it was kept or dropped, goes to build/cities_review.csv.
"""
import csv
import datetime
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from build_map import normalize_answer, slug  # noqa: E402
from cities_curated import ADD, DIVISION, DROP, EXTRA_ANSWERS, RENAME  # noqa: E402
from countries_table import COUNTRIES, MERGE_INTO  # noqa: E402

TARGET = 1000             # cities in the game
MIN_SITELINKS = 60        # the bar to be considered at all
W_VIEWS = 0.65           # weight of the English pageview percentile
W_LINKS = 0.35           # ... and of the sitelink percentile
CAPITAL_BONUS = 0.05
UNESCO_BONUS = 0.04
PER_COUNTRY = 1           # no country goes without at least this many
BATCH = 150               # Wikidata items per detail query
VIEW_THREADS = 4          # parallel pageview requests

CACHE = os.path.join(HERE, "cache")
REVIEW = os.path.join(HERE, "cities_review.csv")
FACTS = os.path.join(HERE, "cities_facts.json")
TOPO = os.path.join(HERE, "countries-50m.json")
SPARQL = "https://qlever.dev/api/wikidata"
PREFIXES = """
PREFIX wd: <http://www.wikidata.org/entity/>
PREFIX wdt: <http://www.wikidata.org/prop/direct/>
PREFIX wikibase: <http://wikiba.se/ontology#>
PREFIX schema: <http://schema.org/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
PREFIX p: <http://www.wikidata.org/prop/>
PREFIX ps: <http://www.wikidata.org/prop/statement/>
PREFIX pq: <http://www.wikidata.org/prop/qualifier/>
"""
PAGEVIEWS = ("https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/"
             "en.wikipedia/all-access/user/{title}/monthly/{start}/{end}")
AGENT = "CityGuesser-build/1.0 (https://github.com/mmunson2/CountryGuesser)"

# Settlements that are not places a player could visit today, or that are
# only a piece of a bigger city. Anything of these types is dropped.
NOT_A_CITY = {
    "Q839954",     # archaeological site
    "Q15661340",   # ancient city
    "Q19953632",   # former administrative territorial entity
    "Q74047",      # ghost town
    "Q2974842",    # lost city
    "Q123705",     # neighborhood
}

ENTITY = "http://www.wikidata.org/entity/"


# ------------------------------------------------------------------ fetching
def fetch(url, data=None, tries=8):
    request = urllib.request.Request(url, data=data, headers={
        "User-Agent": AGENT,
        "Accept": "application/sparql-results+json, application/json",
    })
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return json.load(response)
        except urllib.error.HTTPError as err:
            if err.code == 404:
                return None
            if err.code not in (429, 500, 502, 503, 504) or attempt == tries - 1:
                raise
            wait = err.headers.get("Retry-After")
            if wait and wait.isdigit():
                time.sleep(int(wait))
                continue
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == tries - 1:
                raise
        time.sleep(min(60, 2 ** attempt))


def sparql(query):
    """Rows of {variable: value}, from QLever's copy of Wikidata.

    query.wikidata.org stops at 60 seconds, and the candidate query walks
    the whole settlement class tree: on a busy day it breaks off mid-answer
    every time. QLever answers the same query in seconds. It wants its
    prefixes spelled out, and compares sitelink counts only once cast.
    """
    body = urllib.parse.urlencode({"query": PREFIXES + query}).encode()
    rows = fetch(SPARQL, data=body)["results"]["bindings"]
    return [{k: v["value"] for k, v in row.items()} for row in rows]


def cached(name, refresh, make):
    path = os.path.join(CACHE, name)
    if not refresh and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    value = make()
    os.makedirs(CACHE, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False)
    return value


def qid(uri):
    return uri[len(ENTITY):] if uri.startswith(ENTITY) else uri


# ----------------------------------------------------------------- wikidata
def candidates(refresh):
    """Every human settlement with MIN_SITELINKS or more: {qid: sitelinks}."""
    def make():
        # The class tree first, then its members: the other way round
        # runs QLever out of memory.
        rows = sparql("""
            SELECT ?item (MAX(?n) AS ?links) WHERE {
              ?type wdt:P279* wd:Q486972 .
              ?item wdt:P31 ?type ; wikibase:sitelinks ?n .
              FILTER(xsd:integer(?n) >= %d)
            }
            GROUP BY ?item""" % MIN_SITELINKS)
        return {qid(r["item"]): int(r["links"]) for r in rows}
    return cached("candidates.json", refresh, make)


def details(ids, refresh):
    """Name, article, place, country and kind of each candidate.

    The cache is topped up rather than rebuilt: an id added to the
    candidates (a capital, a curated ADD) is fetched on its own.
    """
    path = os.path.join(CACHE, "details.json")
    out = {}
    if not refresh and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            out = json.load(f)
    ids_sorted = sorted(set(ids) - set(out))
    if ids_sorted:
        for i in range(0, len(ids_sorted), BATCH):
            chunk = ids_sorted[i:i + BATCH]
            print("  details %d-%d of %d" % (i + 1, i + len(chunk), len(ids_sorted)),
                  file=sys.stderr)
            rows = sparql("""
                SELECT ?item
                  (SAMPLE(?label) AS ?name) (SAMPLE(?title) AS ?article)
                  (SAMPLE(?coord) AS ?where) (MAX(?pop) AS ?people)
                  (SAMPLE(?gone) AS ?dissolved)
                  (GROUP_CONCAT(DISTINCT STR(?country); SEPARATOR=" ") AS ?countries)
                  (GROUP_CONCAT(DISTINCT STR(?type); SEPARATOR=" ") AS ?types)
                  (GROUP_CONCAT(DISTINCT STR(?parent); SEPARATOR=" ") AS ?parents)
                  (GROUP_CONCAT(DISTINCT ?alias; SEPARATOR="|") AS ?aliases)
                WHERE {
                  VALUES ?item { %s }
                  OPTIONAL { ?item rdfs:label ?label . FILTER(LANG(?label) = "en") }
                  OPTIONAL { ?a schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> ;
                                schema:name ?title }
                  OPTIONAL { ?item wdt:P625 ?coord }
                  OPTIONAL { ?item wdt:P1082 ?pop }
                  OPTIONAL { ?item wdt:P576 ?gone }
                  OPTIONAL { ?item wdt:P17 ?country }
                  OPTIONAL { ?item wdt:P31 ?type }
                  OPTIONAL { ?item wdt:P131 ?parent }
                  OPTIONAL { ?item skos:altLabel ?alias . FILTER(LANG(?alias) = "en") }
                }
                GROUP BY ?item""" % " ".join("wd:" + q for q in chunk))
            for r in rows:
                out[qid(r["item"])] = {
                    "name": r.get("name"),
                    "article": r.get("article"),
                    "coord": r.get("where"),
                    "pop": float(r["people"]) if r.get("people") else None,
                    "dissolved": bool(r.get("dissolved")),
                    "countries": [qid(c) for c in r.get("countries", "").split()],
                    "types": [qid(t) for t in r.get("types", "").split()],
                    "parents": [qid(p) for p in r.get("parents", "").split()],
                    "aliases": [a for a in r.get("aliases", "").split("|") if a],
                }
        os.makedirs(CACHE, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)
    return out


def places(ids, refresh):
    """What the game shows once a city is found: the first-level division
    it sits in (state, province, region...) and its population with a date.

    Every first-level division above the city on the located-in chain comes
    back, with its country and whether it is gone, and so do the chain's
    links, to tell the nearest one; every population statement comes back
    with its rank and date. place_of() picks. Topped up like details().
    """
    path = os.path.join(CACHE, "places.json")
    out = {}
    if not refresh and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            out = json.load(f)
    todo = sorted(set(ids) - set(out))
    for i in range(0, len(todo), BATCH):
        chunk = todo[i:i + BATCH]
        print("  places %d-%d of %d" % (i + 1, i + len(chunk), len(todo)), file=sys.stderr)
        values = " ".join("wd:" + q for q in chunk)
        for q in chunk:
            out[q] = {"admin": [], "links": [], "pop": []}
        for r in sparql("""
            SELECT ?item ?adm ?label ?country ?gone WHERE {
              VALUES ?item { %s }
              ?item wdt:P131+ ?adm .
              ?adm wdt:P31/wdt:P279* wd:Q10864048 .
              OPTIONAL { ?adm rdfs:label ?label . FILTER(LANG(?label) = "en") }
              OPTIONAL { ?adm wdt:P17 ?country }
              OPTIONAL { ?adm wdt:P576 ?gone }
            }""" % values):
            entry = [qid(r["adm"]), r.get("label"),
                     qid(r["country"]) if r.get("country") else None, bool(r.get("gone"))]
            if entry not in out[qid(r["item"])]["admin"]:
                out[qid(r["item"])]["admin"].append(entry)
        for r in sparql("""
            SELECT DISTINCT ?item ?x ?y WHERE {
              VALUES ?item { %s }
              ?item wdt:P131* ?x . ?x wdt:P131 ?y .
            }""" % values):
            out[qid(r["item"])]["links"].append([qid(r["x"]), qid(r["y"])])
        for r in sparql("""
            SELECT ?item ?pop ?when ?rank WHERE {
              VALUES ?item { %s }
              ?item p:P1082 ?st . ?st ps:P1082 ?pop ; wikibase:rank ?rank .
              OPTIONAL { ?st pq:P585 ?when }
            }""" % values):
            out[qid(r["item"])]["pop"].append(
                [float(r["pop"]), (r.get("when") or "")[:4], r["rank"].rsplit("#", 1)[-1]])
    if todo:
        os.makedirs(CACHE, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)
    return out


RANKS = {"PreferredRank": 2, "NormalRank": 1, "DeprecatedRank": 0}


def place_of(q, name, place, countries):
    """(division name or None, population or None, year or None) for a city.

    The division is the nearest one up the located-in chain that still
    exists and is in the city's own country (Greece's regions, not the
    decentralized administrations above them). None if the city is a
    division itself (Berlin, Tokyo), if two tie (Jerusalem), or if it only
    restates the city's name (Dublin City). The population is the latest
    dated figure of the best rank Wikidata has.
    """
    up = {}
    for x, y in place["links"]:
        up.setdefault(x, []).append(y)
    depth, frontier = {q: 0}, [q]
    while frontier:
        nxt = []
        for x in frontier:
            for y in up.get(x, []):
                if y not in depth:
                    depth[y] = depth[x] + 1
                    nxt.append(y)
        frontier = nxt
    admin = [(depth.get(adm, 99), label) for adm, label, country, gone in place["admin"]
             if label and adm != q and not gone and (country is None or country in countries)]
    division = None
    if admin:
        near = min(d for d, _ in admin)
        labels = {label for d, label in admin if d == near}
        if len(labels) == 1:
            division = labels.pop()
    if division and division.lower() in (name.lower(), name.lower() + " city"):
        division = None
    pops = [p for p in place["pop"] if RANKS.get(p[2], 0) > 0]
    if not pops:
        return division, None, None
    best = max(RANKS[p[2]] for p in pops)
    value, year, _ = max((p for p in pops if RANKS[p[2]] == best), key=lambda p: (p[1], p[0]))
    return division, int(value), int(year) if year else None


def countries_of_world(refresh):
    """Every country Wikidata knows: {qid: {numeric, names, capitals}}."""
    def make():
        rows = sparql("""
            SELECT ?c ?num ?label ?alias ?cap WHERE {
              { ?c wdt:P31 wd:Q3624078 } UNION { ?c wdt:P31 wd:Q6256 }
              OPTIONAL { ?c wdt:P299 ?num }
              OPTIONAL { ?c rdfs:label ?label . FILTER(LANG(?label) = "en") }
              OPTIONAL { ?c skos:altLabel ?alias . FILTER(LANG(?alias) = "en") }
              OPTIONAL { ?c wdt:P36 ?cap }
            }""")
        out = {}
        for r in rows:
            entry = out.setdefault(qid(r["c"]), {"numeric": None, "names": [], "capitals": []})
            if r.get("num"):
                entry["numeric"] = r["num"]
            for key in ("label", "alias"):
                if r.get(key) and r[key] not in entry["names"]:
                    entry["names"].append(r[key])
            if r.get("cap") and qid(r["cap"]) not in entry["capitals"]:
                entry["capitals"].append(qid(r["cap"]))
        return out
    return cached("countries.json", refresh, make)


def unesco_places(refresh):
    """Settlements that are, or hold, a UNESCO World Heritage Site."""
    def make():
        rows = sparql("""
            SELECT DISTINCT ?site ?place WHERE {
              ?site wdt:P1435 wd:Q9259 .
              OPTIONAL { ?site wdt:P131 ?place }
            }""")
        return sorted({qid(r[key]) for r in rows for key in ("site", "place") if key in r})
    return set(cached("unesco.json", refresh, make))


# ---------------------------------------------------------------- pageviews
def view_window(today=None):
    """The last twelve whole months, as the API wants them."""
    today = today or datetime.date.today()
    end = today.replace(day=1) - datetime.timedelta(days=1)
    start = datetime.date(end.year - 1, end.month, 1) + datetime.timedelta(days=32)
    start = start.replace(day=1)
    return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")


def pageviews(titles, refresh):
    start, end = view_window()
    name = "pageviews-%s-%s.json" % (start, end)
    path = os.path.join(CACHE, name)
    have = {}
    if not refresh and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            have = json.load(f)
    missing = sorted(set(titles) - set(have))

    def one(title):
        url = PAGEVIEWS.format(title=urllib.parse.quote(title.replace(" ", "_"), safe=""),
                               start=start, end=end)
        data = fetch(url)
        return title, sum(i["views"] for i in data["items"]) if data else 0

    if missing:
        print("  pageviews for %d articles (%s-%s)" % (len(missing), start, end),
              file=sys.stderr)
        os.makedirs(CACHE, exist_ok=True)

        def keep():
            with open(path, "w", encoding="utf-8") as f:
                json.dump(have, f, ensure_ascii=False)

        # Saved as it goes, so a run cut short by rate limits picks up here.
        try:
            with ThreadPoolExecutor(VIEW_THREADS) as pool:
                for done, (title, views) in enumerate(pool.map(one, missing), 1):
                    have[title] = views
                    if done % 250 == 0:
                        print("    %d / %d" % (done, len(missing)), file=sys.stderr)
                        keep()
        finally:
            keep()
    return have


# --------------------------------------------------------------- countries
def country_matcher(world):
    """Wikidata country qid -> the game's country slug.

    By ISO 3166 numeric code through the map's own TopoJSON ids first, then
    by English name or alias for the countries that have no code there
    (Kosovo) or no polygon at all (Tuvalu).
    """
    with open(TOPO, encoding="utf-8") as f:
        topo = json.load(f)
    by_ne = {}
    for ne_name, display, region, aliases in COUNTRIES:
        info = {"id": slug(display), "region": region, "name": display}
        if ne_name:
            by_ne[ne_name] = info
        for extra, parent in MERGE_INTO.items():
            if parent == ne_name:
                by_ne[extra] = info
    by_numeric = {}
    for g in topo["objects"]["countries"]["geometries"]:
        if g.get("id") and g["properties"]["name"] in by_ne:
            by_numeric[g["id"]] = by_ne[g["properties"]["name"]]

    by_name = {}
    for ne_name, display, region, aliases in COUNTRIES:
        info = {"id": slug(display), "region": region, "name": display}
        for answer in [display] + list(aliases):
            by_name.setdefault(normalize_answer(answer), info)

    table = {}
    for q, c in world.items():
        hit = by_numeric.get(c["numeric"]) if c["numeric"] else None
        if not hit:
            for name in c["names"]:
                hit = by_name.get(normalize_answer(name))
                if hit:
                    break
        if hit:
            table[q] = hit
    return table


# ------------------------------------------------------------------- scoring
def percentiles(values):
    """{key: value} -> {key: 0..1}, ties sharing the same rank."""
    order = sorted(set(values.values()))
    if len(order) < 2:
        return {k: 1.0 for k in values}
    place = {v: i / (len(order) - 1) for i, v in enumerate(order)}
    return {k: place[v] for k, v in values.items()}


def parse_point(text):
    # "Point(lon lat)"
    lon, lat = text[text.index("(") + 1:text.index(")")].split()
    return float(lon), float(lat)


def answers_for(name, info, extra):
    out = {normalize_answer(name)}
    if info.get("name"):
        out.add(normalize_answer(info["name"]))
    if info.get("article"):
        out.add(normalize_answer(info["article"].split(",")[0].split(" (")[0]))
    for alias in info.get("aliases", []):
        a = normalize_answer(alias)
        if a and len(a) <= 40 and not any(ch.isdigit() for ch in a):
            out.add(a)
    out.update(normalize_answer(a) for a in extra)
    # The page folds what is typed down to a-z, 0-9 and spaces, so an alias
    # with anything else left in it (đ, Cyrillic і) could never be matched.
    return sorted(a for a in out if a and all(ch.isascii() for ch in a))


def main(dst, refresh=False):
    print("candidates", file=sys.stderr)
    links = candidates(refresh)
    world = countries_of_world(refresh)
    capitals = {cap for c in world.values() for cap in c["capitals"]}
    # Every capital is a candidate, sitelinks or not: Wikidata files some
    # (Managua) under classes that sit outside the settlement tree.
    links.update({q: links.get(q, 0) for q in ADD | capitals})
    info = details(list(links), refresh)
    unesco = unesco_places(refresh)
    match = country_matcher(world)

    # Who is what. Anything that falls out is kept in the review with why.
    review = {}
    pool = {}
    for q, n in links.items():
        d = info.get(q)
        row = {"qid": q, "sitelinks": n, "name": (d or {}).get("name") or q}
        review[q] = row
        if not d:
            row["status"] = "no details"
            continue
        country = next((match[c] for c in d["countries"] if c in match), None)
        row["country"] = country["name"] if country else ""
        # Some items carry only the language-neutral "mul" label; their
        # English article title, less any ", Country" or "(city)", stands in.
        if not d["name"] and d["article"]:
            d["name"] = d["article"].split(",")[0].split(" (")[0]
            row["name"] = d["name"]
        reason = None
        if q in DROP:
            reason = "curated drop"
        elif not d["name"]:
            reason = "no English name"
        elif not d["article"]:
            reason = "no English Wikipedia article"
        elif not d["coord"]:
            reason = "no coordinates"
        elif not country:
            reason = "no country in the game"
        elif q not in ADD and d["dissolved"]:
            reason = "dissolved"
        elif q not in ADD and NOT_A_CITY & set(d["types"]):
            reason = "not a living city"
        elif q not in ADD and q not in capitals and not d["pop"]:
            # A place nobody is counted as living in is a castle, a park, a
            # trail or a ruin that Wikidata files under settlements.
            reason = "no population"
        if reason:
            row["status"] = reason
            continue
        pool[q] = {"info": d, "country": country}

    views = pageviews([p["info"]["article"] for p in pool.values()], refresh)
    view_pct = percentiles({q: views.get(p["info"]["article"], 0) for q, p in pool.items()})
    link_pct = percentiles({q: links[q] for q in pool})
    for q, p in pool.items():
        p["views"] = views.get(p["info"]["article"], 0)
        p["capital"] = q in capitals
        p["unesco"] = q in unesco
        p["score"] = (W_VIEWS * view_pct[q] + W_LINKS * link_pct[q] +
                      (CAPITAL_BONUS if p["capital"] else 0) +
                      (UNESCO_BONUS if p["unesco"] else 0))
        review[q].update({"views": p["views"], "capital": int(p["capital"]),
                          "unesco": int(p["unesco"]), "score": round(p["score"], 4)})

    # A piece of a bigger city - Brooklyn in New York, Shinjuku in Tokyo -
    # gives way to it, and so does an administrative shell that merely
    # contains its better-known city. Whichever of the two scores lower goes.
    for q in sorted(pool, key=lambda q: pool[q]["score"]):
        if q not in pool or q in ADD:
            continue
        for parent in pool[q]["info"]["parents"]:
            if parent in pool and parent != q:
                lose = q if pool[q]["score"] <= pool[parent]["score"] else parent
                if lose in ADD:
                    continue
                keep = parent if lose == q else q
                review[lose]["status"] = "part of %s" % pool[keep]["info"]["name"]
                del pool[lose]
                break

    ranked = sorted(pool, key=lambda q: -pool[q]["score"])
    forced = [q for q in ranked if q in ADD]
    chosen = forced + [q for q in ranked if q not in ADD][:max(0, TARGET - len(forced))]

    # Every country gets a city: its best, and failing that nothing - which
    # is reported, not hidden.
    counts = {}
    for q in chosen:
        counts[pool[q]["country"]["id"]] = counts.get(pool[q]["country"]["id"], 0) + 1
    games_countries = {slug(display): display for _, display, _, _ in COUNTRIES}
    for cid in games_countries:
        while counts.get(cid, 0) < PER_COUNTRY:
            spare = [q for q in ranked if q not in chosen and pool[q]["country"]["id"] == cid]
            if not spare:
                break
            # Room is made by the weakest city of a country that can spare one.
            for out in reversed(chosen):
                oc = pool[out]["country"]["id"]
                if out not in ADD and counts[oc] > PER_COUNTRY:
                    chosen.remove(out)
                    counts[oc] -= 1
                    review[out]["status"] = "made room for %s" % games_countries[cid]
                    break
            chosen.append(spare[0])
            counts[cid] = counts.get(cid, 0) + 1
            review[spare[0]]["status"] = "kept: only city of %s" % games_countries[cid]
    bare = sorted(name for cid, name in games_countries.items() if not counts.get(cid))

    chosen.sort(key=lambda q: -pool[q]["score"])
    with open(FACTS, encoding="utf-8") as f:
        facts = json.load(f)
    found = places(chosen, refresh)
    cities = []
    for rank, q in enumerate(chosen, 1):
        p = pool[q]
        d = p["info"]
        name = RENAME.get(q, d["name"])
        lon, lat = parse_point(d["coord"])
        division, people, year = place_of(q, name, found[q], d["countries"])
        cities.append({
            "id": q,
            "name": name,
            "country": p["country"]["id"],
            "region": p["country"]["region"],
            "lon": round(lon, 4),
            "lat": round(lat, 4),
            "division": DIVISION.get(q, division),
            "pop": people,
            "popYear": year,
            "rank": rank,
            "score": round(p["score"], 4),
            "capital": p["capital"],
            "answers": answers_for(name, d, EXTRA_ANSWERS.get(q, [])),
            "facts": facts.get(q, {}).get("facts", []),
        })
        review[q].setdefault("status", "kept")
        review[q]["rank"] = rank
    for q, row in review.items():
        row.setdefault("status", "below the cut")

    with open(REVIEW, "w", encoding="utf-8", newline="") as f:
        fields = ["rank", "qid", "name", "country", "score", "views", "sitelinks",
                  "capital", "unesco", "status"]
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(sorted(review.values(),
                                key=lambda r: (r.get("rank") or 10 ** 6, -r.get("score", -1))))

    start, end = view_window()
    payload = {
        "built": datetime.date.today().isoformat(),
        "pageviews": [start, end],
        "cities": cities,
    }
    with open(dst, "w", encoding="utf-8") as f:
        f.write("/* Generated by build/build_cities.py - do not edit by hand. */\n")
        f.write("window.CITY_DATA = ")
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        f.write(";\n")

    print("%d cities -> %s (review: %s)" % (len(cities), dst, os.path.relpath(REVIEW)),
          file=sys.stderr)
    if bare:
        print("no city for: %s" % ", ".join(bare), file=sys.stderr)
    factless = sorted(c["name"] for c in cities if not c["facts"])
    if factless:
        print("no facts for %d (add them to %s): %s" % (
            len(factless), os.path.relpath(FACTS), ", ".join(factless)), file=sys.stderr)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) != 1:
        raise SystemExit("usage: build_cities.py [--refresh] OUT.js")
    main(args[0], refresh="--refresh" in sys.argv)
