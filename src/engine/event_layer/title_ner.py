"""
event_layer/title_ner.py — Fast deterministic NER for article titles.

Extracts three entity signals from an English title string:
  countries:  canonical lowercase country names matched from COUNTRY_SET,
              with demonyms normalized via _DEMONYM_MAP
              (so "Iranian" and "Iran" both resolve to "iran")
  orgs:       ALL-CAPS acronyms (≥ 2 chars) not in COUNTRY_SET
              (NATO, IMF, WHO, ECB, OPEC, WTO, IAEA, FBI, CIA)
  event_type: POLICY_ACTION | INCIDENT | ANALYSIS | UNKNOWN
              based on first-matching keyword set

Design decisions:
  - Pure Python, zero dependencies, zero I/O — runs inline in Stage 1 normalize
  - Title-Case phrase extraction (e.g. "Federal Reserve") is intentionally absent:
    Title-Case words are indistinguishable from person names and location adjectives
    without a trained NER model. Org detection focuses on unambiguous ALL-CAPS acronyms.
  - 'persons' extraction is deferred — requires trained NER model.
  - CJK titles: caller should pass canonical_title (English-normalized) not title_original.

Public API
----------
extract_title_entities(title: str) -> dict
    Returns {'countries': list[str], 'orgs': list[str], 'event_type': str}
    Never raises — returns empty/UNKNOWN on any failure.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Country set — names and demonyms used for country detection
# ---------------------------------------------------------------------------
# Two purposes:
#   (a) positive: detect country tokens in a title (Rules 1 and 4 in _entity_gate)
#   (b) negative: exclude country abbreviations from ALL-CAPS org detection
#       so that "US", "UK", "EU" are not returned as org acronyms

COUNTRY_SET: frozenset[str] = frozenset({
    # United States
    'United States', 'US', 'USA', 'America',
    # China
    'China', 'Chinese',
    # Russia
    'Russia', 'Russian', 'Russians',
    # Iran
    'Iran', 'Iranian', 'Iranians',
    # North Korea
    'North Korea', 'DPRK',
    # South Korea
    'South Korea', 'Korea', 'Korean',
    # Japan
    'Japan', 'Japanese',
    # Germany
    'Germany', 'German', 'Germans',
    # France
    'France', 'French',
    # United Kingdom
    'UK', 'Britain', 'British', 'England', 'English',
    # Israel
    'Israel', 'Israeli', 'Israelis',
    # Ukraine
    'Ukraine', 'Ukrainian', 'Ukrainians',
    # India
    'India', 'Indian', 'Indians',
    # Brazil
    'Brazil', 'Brazilian', 'Brazilians',
    # Turkey
    'Turkey', 'Turkish',
    # Saudi Arabia
    'Saudi Arabia', 'Saudi', 'Saudis',
    # Taiwan
    'Taiwan', 'Taiwanese',
    # Pakistan
    'Pakistan', 'Pakistani', 'Pakistanis',
    # Afghanistan
    'Afghanistan', 'Afghan', 'Afghans',
    # Syria
    'Syria', 'Syrian', 'Syrians',
    # Venezuela
    'Venezuela', 'Venezuelan', 'Venezuelans',
    # Cuba
    'Cuba', 'Cuban', 'Cubans',
    # Mexico
    'Mexico', 'Mexican', 'Mexicans',
    # Canada
    'Canada', 'Canadian', 'Canadians',
    # Australia
    'Australia', 'Australian', 'Australians',
    # Italy
    'Italy', 'Italian', 'Italians',
    # Spain
    'Spain', 'Spanish',
    # Poland
    'Poland', 'Polish',
    # Netherlands
    'Netherlands', 'Dutch',
    # Sweden
    'Sweden', 'Swedish', 'Swedes',
    # Norway
    'Norway', 'Norwegian', 'Norwegians',
    # Denmark
    'Denmark', 'Danish',
    # Finland
    'Finland', 'Finnish',
    # Nigeria
    'Nigeria', 'Nigerian', 'Nigerians',
    # Egypt
    'Egypt', 'Egyptian', 'Egyptians',
    # Indonesia
    'Indonesia', 'Indonesian', 'Indonesians',
    # Philippines
    'Philippines', 'Filipino', 'Filipinos',
    # Vietnam
    'Vietnam', 'Vietnamese',
    # Thailand
    'Thailand', 'Thai',
    # Malaysia
    'Malaysia', 'Malaysian', 'Malaysians',
    # Argentina
    'Argentina', 'Argentine', 'Argentines',
    # Colombia
    'Colombia', 'Colombian', 'Colombians',
    # Ethiopia
    'Ethiopia', 'Ethiopian', 'Ethiopians',
    # Myanmar
    'Myanmar', 'Burma', 'Burmese',
    # Greece
    'Greece', 'Greek', 'Greeks',
    # Portugal
    'Portugal', 'Portuguese',
    # Czech Republic
    'Czech', 'Czechia',
    # Romania
    'Romania', 'Romanian', 'Romanians',
    # Hungary
    'Hungary', 'Hungarian', 'Hungarians',
    # Belgium
    'Belgium', 'Belgian', 'Belgians',
    # Switzerland
    'Switzerland', 'Swiss',
    # Austria
    'Austria', 'Austrian', 'Austrians',
    # Iraq
    'Iraq', 'Iraqi', 'Iraqis',
    # Lebanon
    'Lebanon', 'Lebanese',
    # Jordan
    'Jordan', 'Jordanian', 'Jordanians',
    # Qatar
    'Qatar', 'Qatari', 'Qataris',
    # UAE
    'UAE', 'Emirati', 'Emiratis',
    # South Africa
    'South Africa',
    # Kenya
    'Kenya', 'Kenyan', 'Kenyans',
    # Ghana
    'Ghana', 'Ghanaian', 'Ghanaians',
    # Morocco
    'Morocco', 'Moroccan', 'Moroccans',
    # Algeria
    'Algeria', 'Algerian', 'Algerians',
    # Chile
    'Chile', 'Chilean', 'Chileans',
    # Peru
    'Peru', 'Peruvian', 'Peruvians',
    # Bangladesh
    'Bangladesh', 'Bangladeshi', 'Bangladeshis',
    # Sri Lanka
    'Sri Lanka',
    # Kazakhstan
    'Kazakhstan', 'Kazakh',
    # New Zealand
    'New Zealand',
    # Singapore
    'Singapore', 'Singaporean', 'Singaporeans',
    # Hong Kong (SAR — listed for news relevance)
    'Hong Kong',
    # Europe / EU (continent/bloc — included so 'EU' is not picked up as an org)
    'Europe', 'European', 'EU',
})

# Precomputed lowercase set for fast membership checks
_COUNTRY_SET_LOWER: frozenset[str] = frozenset(c.lower() for c in COUNTRY_SET)

# ---------------------------------------------------------------------------
# Demonym normalization map
# ---------------------------------------------------------------------------
# Maps lowercase matched token → canonical lowercase country name.
# REQUIRED: without this, "Iran" and "Iranian" produce different strings and
# their set intersection in _entity_gate() is empty (false block or false pass).
# With this map, both resolve to 'iran' → intersection succeeds.

_DEMONYM_MAP: dict[str, str] = {
    # United States — abbreviations and demonyms all → 'united states'
    'us': 'united states', 'usa': 'united states', 'america': 'united states',
    'american': 'united states', 'americans': 'united states',
    # United Kingdom — abbreviations and demonyms all → 'uk'
    'britain': 'uk', 'england': 'uk',
    'british': 'uk', 'english': 'uk',
    # EU / Europe — 'eu' → 'europe' so EU and European both resolve identically
    'eu': 'europe', 'european': 'europe',
    'chinese': 'china',
    'russian': 'russia', 'russians': 'russia',
    'iranian': 'iran', 'iranians': 'iran',
    'ukrainian': 'ukraine', 'ukrainians': 'ukraine',
    'japanese': 'japan',
    'korean': 'korea',
    'german': 'germany', 'germans': 'germany',
    'french': 'france',
    'indian': 'india', 'indians': 'india',
    'israeli': 'israel', 'israelis': 'israel',
    'turkish': 'turkey',
    'saudi': 'saudi arabia', 'saudis': 'saudi arabia',
    'taiwanese': 'taiwan',
    'pakistani': 'pakistan', 'pakistanis': 'pakistan',
    'afghan': 'afghanistan', 'afghans': 'afghanistan',
    'syrian': 'syria', 'syrians': 'syria',
    'venezuelan': 'venezuela', 'venezuelans': 'venezuela',
    'cuban': 'cuba', 'cubans': 'cuba',
    'mexican': 'mexico', 'mexicans': 'mexico',
    'canadian': 'canada', 'canadians': 'canada',
    'australian': 'australia', 'australians': 'australia',
    'italian': 'italy', 'italians': 'italy',
    'spanish': 'spain',
    'polish': 'poland',
    'dutch': 'netherlands',
    'swedish': 'sweden', 'swedes': 'sweden',
    'norwegian': 'norway', 'norwegians': 'norway',
    'danish': 'denmark',
    'finnish': 'finland',
    'nigerian': 'nigeria', 'nigerians': 'nigeria',
    'egyptian': 'egypt', 'egyptians': 'egypt',
    'indonesian': 'indonesia', 'indonesians': 'indonesia',
    'filipino': 'philippines', 'filipinos': 'philippines',
    'vietnamese': 'vietnam',
    'thai': 'thailand',
    'malaysian': 'malaysia', 'malaysians': 'malaysia',
    'argentine': 'argentina', 'argentines': 'argentina',
    'colombian': 'colombia', 'colombians': 'colombia',
    'ethiopian': 'ethiopia', 'ethiopians': 'ethiopia',
    'burmese': 'myanmar',
    'greek': 'greece', 'greeks': 'greece',
    'portuguese': 'portugal',
    'romanian': 'romania', 'romanians': 'romania',
    'hungarian': 'hungary', 'hungarians': 'hungary',
    'belgian': 'belgium', 'belgians': 'belgium',
    'swiss': 'switzerland',
    'austrian': 'austria', 'austrians': 'austria',
    'iraqi': 'iraq', 'iraqis': 'iraq',
    'lebanese': 'lebanon',
    'jordanian': 'jordan', 'jordanians': 'jordan',
    'qatari': 'qatar', 'qataris': 'qatar',
    'emirati': 'uae', 'emiratis': 'uae',
    'kenyan': 'kenya', 'kenyans': 'kenya',
    'ghanaian': 'ghana', 'ghanaians': 'ghana',
    'moroccan': 'morocco', 'moroccans': 'morocco',
    'algerian': 'algeria', 'algerians': 'algeria',
    'chilean': 'chile', 'chileans': 'chile',
    'peruvian': 'peru', 'peruvians': 'peru',
    'bangladeshi': 'bangladesh', 'bangladeshis': 'bangladesh',
    'kazakh': 'kazakhstan',
    'singaporean': 'singapore', 'singaporeans': 'singapore',
}

# ---------------------------------------------------------------------------
# Event type keyword sets
# ---------------------------------------------------------------------------

POLICY_KEYWORDS: frozenset[str] = frozenset({
    'sanction', 'sanctions', 'tariff', 'tariffs', 'ban', 'banned', 'banning',
    'law', 'bill', 'treaty', 'agreement', 'repeal', 'approve', 'approved',
    'veto', 'vetoed', 'announce', 'announced', 'sign', 'signed', 'impose',
    'imposed', 'lift', 'lifted', 'enact', 'enacted', 'policy', 'regulation',
    'regulations', 'embargo', 'deal', 'accord',
})

INCIDENT_KEYWORDS: frozenset[str] = frozenset({
    'resign', 'resignation', 'resigns', 'resigned', 'protest', 'protests',
    'protesting', 'attack', 'attacked', 'attacking', 'kill', 'killed', 'killing',
    'kills', 'arrest', 'arrested', 'arresting', 'crash', 'crashed', 'fire',
    'fired', 'strike', 'strikes', 'coup', 'conflict', 'war', 'bomb', 'bombing',
    'bombed', 'shooting', 'shot', 'explosion', 'disaster', 'earthquake',
    'collapse', 'flood', 'crisis',
})

ANALYSIS_KEYWORDS: frozenset[str] = frozenset({
    'outlook', 'forecast', 'analysis', 'market', 'reaction', 'impact',
    'implications', 'commentary', 'opinion', 'review', 'assessment', 'report',
    'survey', 'study', 'poll', 'data', 'statistics', 'trend', 'prediction',
    'projection', 'estimate', 'index',
})


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _extract_caps_sequences(title: str) -> list[str]:
    """
    Extract ALL-CAPS acronyms (2+ chars) as candidate org identifiers.
    Examples: NATO, IMF, WHO, ECB, OPEC, WTO, IAEA, FBI, CIA, UN (but UN is 2 chars ✓)

    Title-Case phrase extraction (e.g. "Federal Reserve", "European Central Bank")
    is intentionally NOT implemented here. Title-Case words are indistinguishable
    from person names and location adjectives without a trained NER model.
    Rule 5 (org_overlap) in _entity_gate() operates on acronyms only.
    Named-org phrase matching is deferred to a future sprint.
    """
    return [
        m.group(1)
        for m in re.finditer(r'\b([A-Z]{2,})\b', title)
        if m.group(1).lower() not in _COUNTRY_SET_LOWER
    ]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_title_entities(title: str) -> dict:
    """
    Extract entity signals from an English article title.

    Args:
        title: English article title (use canonical_title for CJK items).

    Returns:
        {
          'countries':  list[str],  # canonical lowercase country names
          'orgs':       list[str],  # ALL-CAPS acronyms not in COUNTRY_SET
          'event_type': str,        # POLICY_ACTION | INCIDENT | ANALYSIS | UNKNOWN
        }
        Never raises — returns {'countries': [], 'orgs': [], 'event_type': 'UNKNOWN'}
        on any failure or empty input.
    """
    if not title or not title.strip():
        return {'countries': [], 'orgs': [], 'event_type': 'UNKNOWN'}

    try:
        title_lower = title.lower()

        # Country detection: match COUNTRY_SET members against the title, then
        # normalize via _DEMONYM_MAP to canonical name.
        # Normalization is critical: "Iran" and "Iranian" must both → "iran"
        # so set intersection in _entity_gate() succeeds.
        #
        # Word-boundary matching for single-word entries: substring matching
        # produces many false positives — "us" appears inside "russia" and
        # "discuss"; "uk" appears inside "ukraine". We use a token set for
        # single-word entries so only whole-word matches count.
        # Multi-word entries ("North Korea", "Saudi Arabia") still use substring
        # matching, which is safe because long phrases don't accidentally appear
        # inside unrelated words.
        _title_tokens: frozenset[str] = frozenset(re.findall(r'\b[a-z0-9]+\b', title_lower))
        raw_countries: list[str] = []
        for _c in COUNTRY_SET:
            _c_lower = _c.lower()
            if ' ' in _c_lower:
                # Multi-word phrase — substring match is safe
                if _c_lower in title_lower:
                    raw_countries.append(_c)
            else:
                # Single-word token — whole-word match only
                if _c_lower in _title_tokens:
                    raw_countries.append(_c)
        countries: list[str] = list({
            _DEMONYM_MAP.get(c.lower(), c.lower())
            for c in raw_countries
        })

        orgs: list[str] = _extract_caps_sequences(title)

        # Event type: first matching keyword set wins (POLICY > INCIDENT > ANALYSIS)
        if any(kw in title_lower for kw in POLICY_KEYWORDS):
            event_type = 'POLICY_ACTION'
        elif any(kw in title_lower for kw in INCIDENT_KEYWORDS):
            event_type = 'INCIDENT'
        elif any(kw in title_lower for kw in ANALYSIS_KEYWORDS):
            event_type = 'ANALYSIS'
        else:
            event_type = 'UNKNOWN'

        return {
            'countries':  countries,
            'orgs':       orgs,
            'event_type': event_type,
        }

    except Exception:
        return {'countries': [], 'orgs': [], 'event_type': 'UNKNOWN'}
