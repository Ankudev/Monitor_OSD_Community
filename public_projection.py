"""Publiczna prezentacja danych nie ujawnia wyposażenia ani prywatnych dowodów."""
PRIVATE_FIELDS = frozenset({'power_supply', 'keyword', 'raw', 'pge_event', 'source_hash', 'binding', 'object_fingerprints', 'before_json', 'after_json'})
OBJECT_FIELDS = frozenset({'id','object_id','name','city','city_sym','street','street_sym','house_number','latitude','longitude','location_audit','address_audit','address_verified','active_event_count','attention_event_count','candidate_event_count','confirmed_event_count','events_url','has_coordinates','map_url','monitoring_scope','upcoming_event_count'})
AUDIT_FIELDS = frozenset({'status','requires_review','review_reason','checked_at','invalidated_at','method','radio_id','source_url','gps_region','gps_commune'})
OBJECT_FIELDS = OBJECT_FIELDS | {'matching_pending_sources', 'matching_pending', 'teryt', 'locality', 'provenance'}
_PROJECTED_BODY = object()


def mark_public_response(response):
    """Znacznik serwera dotyczy wyłącznie dokładnie tych przefiltrowanych bajtów."""
    response[_PROJECTED_BODY] = response.body
    return response


def has_public_response(response):
    return response.body is not None and response.get(_PROJECTED_BODY) is response.body


def public_data(value):
    if isinstance(value, list):
        return [public_data(item) for item in value]
    if isinstance(value, dict):
        if 'city_sym' in value and 'name' in value and ('id' in value or 'object_id' in value):
            value = {key:item for key,item in value.items() if key in OBJECT_FIELDS}
        return {key: public_data({k:v for k,v in item.items() if k in AUDIT_FIELDS}) if key == 'location_audit' and isinstance(item, dict) else public_data(item)
                for key,item in value.items() if key not in PRIVATE_FIELDS}
    return value
