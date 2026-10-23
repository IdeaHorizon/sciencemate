"""Scholar identities: names are labels, never publication admission evidence."""
from __future__ import annotations
import os
import re
import unicodedata
import httpx


def openalex_id(value):
    value = str(value or '').strip().removeprefix('openalex:').removeprefix('https://openalex.org/').removeprefix('http://openalex.org/').upper()
    return value if re.fullmatch(r'A\d+', value) else ''


def identity(entry):
    return openalex_id(entry.get('author_id') or entry.get('key'))


def params(values):
    if os.environ.get('OPENALEX_API_KEY'):
        values = {**values, 'api_key': os.environ['OPENALEX_API_KEY']}
    return values


def authorships(work):
    return [{'author_id': openalex_id((row.get('author') or {}).get('id')),
             'orcid': str((row.get('author') or {}).get('orcid') or ''),
             'name': str((row.get('author') or {}).get('display_name') or ''),
             'institutions': [str(i.get('display_name') or '') for i in row.get('institutions') or []]}
            for row in work.get('authorships') or [] if openalex_id((row.get('author') or {}).get('id'))]



def _creator_tokens(value):
    text = unicodedata.normalize('NFKD', str(value or '').casefold())
    text = ''.join(char for char in text if not unicodedata.combining(char))
    # Zenodo commonly uses "family, given" while OpenAlex uses "given family".
    return tuple(re.findall(r'[a-z]+', text))


def _same_creator(openalex_name, source_name):
    wanted, actual = _creator_tokens(openalex_name), _creator_tokens(source_name)
    if not wanted or not actual:
        return False
    if len(wanted) >= 2 and len(actual) >= 2:
        family = wanted[-1]
        given = wanted[0]
        if ',' in str(source_name):
            head, tail = str(source_name).split(',', 1)
            family_tokens, given_tokens = _creator_tokens(head), _creator_tokens(tail)
            return family in family_tokens and bool(given_tokens) and given[0] == given_tokens[0][0]
        return family in actual and given[0] == actual[0][0]
    return wanted == actual


async def _zenodo_verified_ids(client, work, identities, cache):
    doi = str(work.get('doi') or '').casefold().removeprefix('https://doi.org/')
    match = re.fullmatch(r'10\.5281/zenodo\.(\d+)', doi)
    if not match:
        return None  # Not a Zenodo DOI; existing OpenAlex rules apply.
    record_id = match.group(1)
    if record_id not in cache:
        try:
            response = await client.get(f'https://zenodo.org/api/records/{record_id}')
            response.raise_for_status()
            payload = response.json()
            official_dois = {
                str(payload.get('doi') or '').casefold(),
                str(payload.get('conceptdoi') or '').casefold(),
                str((payload.get('metadata') or {}).get('doi') or '').casefold(),
            }
            if doi not in official_dois:
                cache[record_id] = ()
            else:
                cache[record_id] = tuple(
                    str(row.get('name') or (row.get('person_or_org') or {}).get('name') or '')
                    for row in (payload.get('metadata') or {}).get('creators') or []
                )
        except (httpx.HTTPError, ValueError, TypeError):
            cache[record_id] = ()  # Fail closed when source authors cannot be checked.
    creators = cache[record_id]
    return [row['author_id'] for row in identities
            if any(_same_creator(row['name'], creator) for creator in creators)]


def matches(item, entries):
    wanted = {identity(entry) for entry in entries if identity(entry)}
    extra = item.extra or {}
    doi = str(extra.get('doi') or '').casefold().removeprefix('https://doi.org/')
    if re.fullmatch(r'10\.5281/zenodo\.\d+', doi):
        # Old OpenAlex-only rows are untrusted too.  A corrupted OpenAlex author
        # entity can carry the depositor's ORCID yet another person's name.
        present = {openalex_id(value) for value in extra.get('verified_author_ids') or []}
    else:
        present = {openalex_id(row.get('author_id')) for row in extra.get('author_identities') or [] if isinstance(row, dict)}
    return bool(wanted & present)


async def search(name, *, limit=20):
    async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
        response = await client.get('https://api.openalex.org/authors',
            params=params({'search': name, 'per-page': min(limit, 50)}))
        response.raise_for_status()
        payload = response.json()
        rows = payload.get('results') or []
        complete = int((payload.get('meta') or {}).get('count') or len(rows)) <= len(rows)
    result = []
    for row in rows:
        aid = openalex_id(row.get('id'))
        if not aid:
            continue
        institutions = [str(i.get('display_name') or '') for i in row.get('last_known_institutions') or []]
        if not institutions and row.get('last_known_institution'):
            institutions = [str(row['last_known_institution'].get('display_name') or '')]
        topics = [str(t.get('display_name') or '') for t in (row.get('topics') or row.get('x_concepts') or [])[:3]]
        result.append({'key': 'openalex:' + aid, 'name': str(row.get('display_name') or ''),
            'source': 'openalex', 'author_id': aid, 'orcid': str(row.get('orcid') or ''),
            'institution': ' / '.join(filter(None, institutions)), 'topics': topics,
            'works_count': int(row.get('works_count') or 0), 'identity_status': 'resolved', 'search_complete': complete})
    return result


async def papers(entry, *, since):
    """Retrieve and independently verify works against the chosen author entity."""
    from nodes.literature.tools.search_engines import Paper
    aid = identity(entry)
    if not aid:
        return []
    output, cursor = [], '*'
    zenodo_cache = {}
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        # A bounded round; subsequent daily windows continue acquisition.
        for _ in range(5):
            response = await client.get('https://api.openalex.org/works', params=params({
                'filter': f'authorships.author.id:{aid},from_publication_date:{since}',
                'per-page': 200, 'sort': 'publication_date:desc', 'cursor': cursor}))
            response.raise_for_status()
            payload = response.json()
            for work in payload.get('results') or []:
                identities = authorships(work)
                if not any(row['author_id'] == aid for row in identities):
                    continue
                verified_ids = await _zenodo_verified_ids(client, work, identities, zenodo_cache)
                if verified_ids is not None and aid not in verified_ids:
                    continue
                title = str(work.get('title') or '').strip()
                if not title:
                    continue
                from app.services.feed.discovery import _openalex_abstract
                doi = str(work.get('doi') or '').casefold().removeprefix('https://doi.org/')
                match = re.fullmatch(r'10\.5281/zenodo\.(\d+)', doi)
                creators = [
                    ' '.join(part.strip() for part in name.split(',', 1)[::-1]) if ',' in name else name
                    for name in zenodo_cache.get(match.group(1), ())
                ] if match else []
                output.append(Paper(title=title, authors=creators or [row['name'] for row in identities],
                    author_identities=identities, verified_author_ids=verified_ids or [],
                    zenodo_creators=creators,
                    year=work.get('publication_year'),
                    abstract=_openalex_abstract(work.get('abstract_inverted_index')),
                    venue=str(((work.get('primary_location') or {}).get('source') or {}).get('display_name') or ''),
                    doi=str(work.get('doi') or '').removeprefix('https://doi.org/'),
                    source='openalex', url=str(work.get('doi') or work.get('id') or ''),
                    citations=int(work.get('cited_by_count') or 0), pub_date=str(work.get('publication_date') or '')))
            following = (payload.get('meta') or {}).get('next_cursor')
            if not payload.get('results') or not following or following == cursor:
                break
            cursor = following
    return output
