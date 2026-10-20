# -*- coding: utf-8 -*-
"""Hand corrections to the city list build/build_cities.py compiles.

Every entry is keyed by Wikidata id (the Q-number in a city's Wikidata URL,
also the `qid` column of build/cities_review.csv).
"""

# Always in the game, whatever the score - and never dropped as a piece of
# another city or as not a living one. A set of ids: {"Q90", ...}
ADD = {
    "Q3274",       # Managua: Wikidata's capital of Nicaragua is a newer
                   # municipality item with no Wikipedia article
}

# Never in the game.
DROP = set()

# What the game calls a city, when Wikidata's English label is not it.
RENAME = {
}

# More names a city is accepted under, on top of its Wikidata aliases.
EXTRA_ANSWERS = {
    "Q60": ["New York"],          # New York City
}

# The state, province or region shown with a city once it is found, when
# Wikidata's located-in chain gives the wrong one or none.
DIVISION = {
    "Q25287": "Västra Götaland County",   # Gothenburg: ties with Halland
    "Q5196": "Chechnya",                  # Grozny: ties with Stavropol Krai
    "Q55630": "Vojvodina",                # Novi Sad: a district comes first
    "Q183113": "French Polynesia",        # Bora Bora: its commune, not a region
    "Q130800": "French Polynesia",        # Papeete: the Windward Islands
}
