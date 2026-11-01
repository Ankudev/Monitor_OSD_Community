"""Opis wykonanej decyzji; nie jest dodatkowym algorytmem dopasowania."""
from copy import deepcopy

VERSION = 1
LIMITATION_LABELS = {
    "location_review_required": "Lokalizacja wymaga przeglądu.",
    "location_not_audited": "Brak aktualnego audytu GPS.",
    "address_not_verified": "Adres obiektu nie jest zweryfikowany.",
    "house_number_missing": "Nie zapisano numeru domu.",
    "numbers_unparsed": "Wykaz adresów lub numerów jest niejednoznaczny.",
    "locality_only": "Zgodność miejscowości nie potwierdza konkretnego punktu.",
    "street_only": "Zgodność ulicy nie potwierdza numeru domu.",
    "proximity_only": "Bliskość punktu nie określa obszaru wyłączenia.",
    "radius_extent_unverified": "Znaczenie promienia jako zasięgu wyłączenia nie jest potwierdzone.",
    "address_geometry_conflict": "Adres i geometria operatora są sprzeczne.",
    "legacy_explanation_missing": "Brak szczegółowego wyjaśnienia starszego wpisu.",
    "details_truncated": "Szczegóły ograniczono; pełny rekord pozostaje w danych operatora.",
    "variant_bases_differ": "Warianty komunikatu mają różne podstawy dopasowania.",
}


def explain(decision, evidence, rule_version):
    method = decision['method']
    details = deepcopy(decision.get('details') or {})
    limitations = list(decision.get('limitations') or [])
    basis = {
        'exact_address':'address', 'exact_teryt':'street' if details.get('street_compared') else 'locality',
        'text_address':'street' if details.get('street_compared') else 'locality',
        'text_keyword':'keyword', 'spatial_polygon':'geometry',
        'spatial_nearby':'proximity', 'spatial_radius':'proximity',
        'county_mass':'county', 'address_geometry_conflict':'conflict',
    }.get(method, 'unknown')
    verification = evidence.get('location_verification')
    if verification == 'review_required': limitations.append('location_review_required')
    if basis == 'geometry' and verification != 'recorded_audit': limitations.append('location_not_audited')
    if basis == 'locality': limitations.append('locality_only')
    if basis == 'street': limitations.append('street_only')
    if basis == 'proximity': limitations.append('proximity_only')
    if method == 'spatial_radius': limitations.append('radius_extent_unverified')
    if basis == 'conflict': limitations.append('address_geometry_conflict')
    if details.get('address', {}).get('unknown_row_count'): limitations.append('numbers_unparsed')
    scope = evidence['scope']
    confidence = ('high' if evidence.get('point_confirmed') else
                  'unknown' if verification in {'review_required','not_audited'} or basis in {'conflict','unknown'} else
                  'medium' if basis == 'street' else 'low')
    # Potwierdzony adres ma własny audyt; nie wymaga audytu geometrii.
    if not evidence.get('point_confirmed') and basis in {'street','locality','keyword','county','proximity'} and verification != 'review_required':
        confidence = 'medium' if basis == 'street' else 'low'
    reason_code = {'address':'verified_address_match', 'street':'street_match_only',
                   'locality':'locality_match_only', 'keyword':'keyword_match_only',
                   'county':'county_match_only', 'conflict':'address_geometry_conflict',
                   'geometry':'geometry_match' if evidence.get('point_confirmed') else 'geometry_unverified',
                   'proximity':'proximity_match_only'}.get(basis,'unknown')
    return {'version':VERSION,'rule_version':rule_version,'method':method,'scope':scope,
            'basis':basis,'confidence':confidence,'reason_code':reason_code,
            'reason':evidence['reason'],'details':details,'limitations':sorted(set(limitations))}


def explanation_details(event, view='full'):
    value = event.get('match_explanation')
    if not isinstance(value, dict) or value.get('version') != VERSION:
        value = {'version':VERSION,'rule_version':None,'method':event.get('match_confidence') or 'unknown',
                 'scope':'unknown','basis':'unknown','confidence':'unknown',
                 'reason_code':'legacy_explanation_missing','reason':LIMITATION_LABELS['legacy_explanation_missing'],
                 'details':{},'limitations':['legacy_explanation_missing']}
    return deepcopy({key:item for key,item in value.items() if view == 'full' or key != 'details'})


def evidence_signature(event):
    """Stabilne znaczenie dowodu, bez tekstów, wersji formatu i aktualnego zegara."""
    evidence = event.get('match_evidence') or {}
    explanation = event.get('match_explanation') or {}
    details = deepcopy(explanation.get('details') or {})
    details.get('address', {}).pop('reason', None)
    for row in details.get('address',{}).get('rows',[]):
        numbers = row.get('numbers') or {}
        numbers.pop('original', None)
    return {'evidence':{key:value for key,value in evidence.items() if key not in {'reason','version'}},
            'method':event.get('match_confidence'),
            'explanation':{key:explanation.get(key) for key in ('method','scope','basis','confidence','limitations')},
            'details':details}


def source_signature(snapshot):
    ignored = {'match_evidence','match_explanation','match_confidence','verification_pending',
               'source_updated_at','time_phase','time_phase_at_recording'}
    return {key:value for key,value in snapshot.items() if key not in ignored}
