#!/usr/bin/env python3
"""Sprawdza pelny eksport MEC i publikuje uporzadkowana wersje JSON.

Gdy dane sa niekompletne, konczy sie bledem. Wtedy workflow nie nadpisuje
poprzedniego poprawnego pliku w GitHub Pages.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from urllib.parse import urlparse, parse_qs, urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from html import unescape
import time
import json
import re
import sys

INPUT = Path('msk_events.raw.json')
OUTPUT = Path('public/msk_events.json')
VALID_HOSTS = {'msk.lodz.pl', 'www.msk.lodz.pl'}
IMAGE_EXTENSION = re.compile(r'\.(?:png|jpe?g|webp|gif|avif)$', re.I)


def valid_image(url: object) -> bool:
    if not isinstance(url, str) or not url:
        return False
    u = urlparse(url)
    return u.scheme == 'https' and u.hostname in VALID_HOSTS and bool(IMAGE_EXTENSION.search(u.path))


def month_index(m: str) -> int:
    year, month = map(int, m.split('-'))
    if not 1 <= month <= 12:
        raise ValueError(f'Nieprawidlowy miesiac: {m}')
    return year * 12 + month - 1


def attach_wordpress_categories(records: list[dict]) -> dict:
    """Publiczne MEC/WP REST; etykiety kategorii z WordPress, nigdy z tytulow.

    Brak danych/awaria REST NIE blokuje publikowania poprawnego kalendarza.
    Jeden slug wydarzenia -> dowolna liczba jego terminow.
    """
    base_to_slug = {}
    for item in records:
        if item.get('categories'):
            continue
        base = item['pageUrl'].split('?', 1)[0]
        slug = urlparse(base).path.strip('/').split('/')[-1]
        if slug:
            base_to_slug[base] = slug

    slugs = sorted(set(base_to_slug.values()))
    by_slug = {}
    failures = 0
    processed = 0
    # Pakiety oszczedzaja ruch do strony MSK; limit dlugosci URL.
    batches = []
    current = []
    char_count = 0
    for slug in slugs:
        if current and (len(current) >= 16 or char_count + len(slug) + 18 > 3800):
            batches.append(current)
            current = []
            char_count = 0
        current.append(slug)
        char_count += len(slug) + 18
    if current:
        batches.append(current)

    # Pętla musi należeć do funkcji, a wszystkie operacje na partii do pętli.
    for batch in batches:
        args = [('per_page', str(len(batch))), ('_embed', 'wp:term'),
                ('_fields', 'slug,_links,_embedded')]
        args.extend(('slug[]', slug) for slug in batch)
        url = 'https://msk.lodz.pl/wp-json/wp/v2/mec-events?' + urlencode(args)
        request = Request(url, headers={
            'Accept': 'application/json',
            'User-Agent': 'MSK-Synchronizator/1.0 (public event categories)',
        })
        try:
            with urlopen(request, timeout=20) as reply:
                if reply.status != 200:
                    raise ValueError('HTTP ' + str(reply.status))
                response = json.load(reply)
            if not isinstance(response, list):
                raise ValueError('WP REST odpowiedzial nieprawidlowa struktura')
            for post in response:
                slug = post.get('slug')
                if not isinstance(slug, str) or slug not in batch:
                    continue
                categories = set()
                embedded = post.get('_embedded') or {}
                for group in embedded.get('wp:term', []):
                    if not isinstance(group, list):
                        continue
                    for term in group:
                        if isinstance(term, dict) and term.get('taxonomy') == 'mec_category':
                            name = unescape(str(term.get('name') or '')).strip()
                            if name:
                                categories.add(name)
                if categories:
                    by_slug[slug] = sorted(categories)
            processed += len(batch)
        except (HTTPError, URLError, OSError, ValueError, json.JSONDecodeError) as exc:
            failures += 1
            print(f'UWAGA: kategorie WP — partia {len(batch)}: {str(exc)[:140]}', file=sys.stderr)
            # Jesli endpoint globalnie niedostepny, nie bombardujemy WordPressa.
            if failures >= 2 and not by_slug:
                print('UWAGA: Publiczne kategorie niedostepne; pozostawiono bez przypisania.', file=sys.stderr)
                break
        time.sleep(0.15)

    filled = 0
    for record in records:
        if record.get('categories'):
            continue
        base = record['pageUrl'].split('?', 1)[0]
        labels = by_slug.get(base_to_slug.get(base, ''), [])
        if labels:
            record['categories'] = labels
            filled += 1

    print(f'Kategorie WP: {len(by_slug)} slugow, {filled} terminow uzupelnionych; '
          f'przetworzono {processed}/{len(slugs)} slugow, nieudane partie: {failures}')
    return {
        'categoryMatchedSlugs': len(by_slug),
        'categoryRequestedSlugs': len(slugs),
        'categoryFetchFailures': failures,
        'categoryEnrichedEvents': filled,
    }


def main() -> int:
    if not INPUT.exists():
        raise ValueError('Brak msk_events.raw.json - synchronizacja nie zakonczyla sie')
    data = json.loads(INPUT.read_text(encoding='utf-8'))
    if data.get('complete') is not True or data.get('schemaVersion') != 1:
        raise ValueError('Eksport niekompletny lub inny format - nie publikujemy')
    coverage = data.get('months')
    if not isinstance(coverage, list) or len(coverage) != 12:
        raise ValueError('Oczekiwano dokladnie 12 miesiecy')
    month_names = [x['month'] for x in coverage]
    indices = [month_index(x) for x in month_names]
    if indices != list(range(indices[0], indices[0] + 12)):
        raise ValueError('Miesiace musza byc kolejne - bez luk')
    if not isinstance(data.get('events'), list):
        raise ValueError('Brak listy events')

    clean = []
    seen = set()
    dropped_date = 0
    dropped_invalid = 0
    duplicate = 0
    for e in data['events']:
        if not isinstance(e, dict):
            dropped_invalid += 1
            continue
        try:
            d = e['date']
            parsed = date.fromisoformat(d)
            if parsed.strftime('%Y-%m') not in month_names:
                dropped_invalid += 1
                continue
            u = urlparse(e['pageUrl'])
            if u.scheme != 'https' or u.hostname not in VALID_HOSTS or not u.path.startswith('/msk/'):
                dropped_invalid += 1
                continue
            occurrence = parse_qs(u.query).get('occurrence', [None])[0]
            if occurrence and occurrence != d:
                dropped_date += 1
                continue
            key = (d, e['pageUrl'], e.get('time'))
            if key in seen:
                duplicate += 1
                continue
            seen.add(key)
            record = {
                'date': d,
                'time': e.get('time'),
                'title': e.get('title') or '',
                'place': e.get('place') or '',
                'categories': e.get('categories') if isinstance(e.get('categories'), list) else [],
                'imageUrl': e.get('imageUrl') if valid_image(e.get('imageUrl')) else None,
                'pageUrl': e['pageUrl'],
            }
            if not record['title']:
                dropped_invalid += 1
                continue
            clean.append(record)
        except (KeyError, ValueError, TypeError):
            dropped_invalid += 1

    # Nie zgadujemy zdjec. Uzupelniamy je wylacznie na podstawie
    # poprawnego URL zdjecia tego samego wpisu WordPress.
    known = defaultdict(Counter)
    for e in clean:
        if e['imageUrl']:
            known[e['pageUrl'].split('?')[0]][e['imageUrl']] += 1
    best = {url: counts.most_common(1)[0][0] for url, counts in known.items()}
    recovered = 0
    for e in clean:
        if e['imageUrl'] is None:
            e['imageUrl'] = best.get(e['pageUrl'].split('?')[0])
            recovered += bool(e['imageUrl'])

    clean.sort(key=lambda e: (e['date'], e['time'] or '', e['title'], e['pageUrl']))
    counts = Counter(e['date'][:7] for e in clean)
    # Bezpiecznik przeciw publikowaniu wyraznie uszkodzonego eksportu.
    if len(clean) < 1000 or any(counts[m] == 0 for m in month_names):
        raise ValueError(f'Za malo danych do publikacji: {len(clean)} terminow, rozklad {dict(counts)}')
    # Kategorie MEC uzupelniane po walidacji i czyszczeniu terminow.
    category_quality = attach_wordpress_categories(clean)
    data['events'] = clean
    data['months'] = [dict(c, events=counts[c['month']]) for c in coverage]
    data['quality'] = {
        'droppedMismatchedDates': dropped_date,
        'droppedInvalid': dropped_invalid,
        'droppedDuplicates': duplicate,
        'recoveredImages': recovered,
        'withoutImage': sum(not e['imageUrl'] for e in clean),
        'withoutCategories': sum(not e['categories'] for e in clean),
        **category_quality,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUTPUT.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    tmp.replace(OUTPUT)
    print('OK: terminow:', len(clean))
    print('Miesiace:', ', '.join(f'{m}={counts[m]}' for m in month_names))
    print('Jakosc danych:', data['quality'])
    print('Zapisano:', OUTPUT)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print(f'BLAD WALIDACJI - NIE PUBLIKUJEMY: {exc}', file=sys.stderr)
        sys.exit(1)
