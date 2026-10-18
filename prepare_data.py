#!/usr/bin/env python3
"""Sprawdza pelny eksport MEC i publikuje uporzadkowana wersje JSON.

Gdy dane sa niekompletne, konczy sie bledem. Wtedy workflow nie nadpisuje
poprzedniego poprawnego pliku w GitHub Pages.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path
from urllib.parse import urlparse, parse_qs, urlencode, urljoin
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from html import unescape
import time
import json
import re
import sys

from bs4 import BeautifulSoup

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



# Uzywamy obrazow z oryginalnej strony MEC tylko podczas pracy synchronizatora.
# Android otrzymuje gotowy detailImageUrl i nie musi podmieniac miniatury.
PREVIOUS_FEED = 'https://lopezsc.github.io/msk-kalendarz/msk_events.json'
SIZED_IMAGE = re.compile(r'-\d+x\d+\.(?:png|jpe?g|webp|gif|avif)(?:\.webp)?$', re.I)
IMAGE_SELECTORS = (
    '.mec-single-event .mec-event-image img',
    '.mec-event-image img',
    '.mec-events-event-image img',
    '.mec-single-event .wp-post-image',
    '.mec-single-event .mec-event-thumbnail img',
    'article img.wp-post-image',
)


def safe_poster_link(raw: object, page_url: str) -> str | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    candidate = urljoin(page_url, raw.strip())
    parsed = urlparse(candidate)
    if parsed.scheme != 'https' or parsed.hostname not in VALID_HOSTS:
        return None
    if not parsed.path.startswith('/wp-content/') or not valid_image(candidate):
        return None
    return candidate


def source_image_candidates(img, page_url: str) -> list[str]:
    # WordPress umieszcza wieksze warianty w srcset lub data-srcset.
    responsive = []
    for attribute in ('data-srcset', 'srcset'):
        for entry in (img.get(attribute) or '').split(','):
            pieces = entry.strip().split()
            if not pieces:
                continue
            link = safe_poster_link(pieces[0], page_url)
            if link:
                width = int(pieces[1][:-1]) if len(pieces) > 1 and re.fullmatch(r'\d+w', pieces[1]) else 0
                responsive.append((width, link))
    responsive.sort(reverse=True)
    direct = [safe_poster_link(img.get(attr), page_url) for attr in
              ('data-lazy-src', 'data-src', 'data-original', 'src')]
    return list(dict.fromkeys([url for _, url in responsive] + [url for url in direct if url]))


def select_full_poster(html: str, page_url: str) -> str | None:
    soup = BeautifulSoup(html, 'html.parser')
    hero_candidates = []
    for selector in IMAGE_SELECTORS:
        for img in soup.select(selector):
            hero_candidates.extend(source_image_candidates(img, page_url))
    hero_candidates = list(dict.fromkeys(hero_candidates))
    for url in hero_candidates:
        if not SIZED_IMAGE.search(urlparse(url).path):
            return url

    # Gdy hero jest w miniaturze, og:image czesto wskazuje na oryginalny plakat.
    social = []
    for selector in (
        'meta[property="og:image:secure_url"]',
        'meta[property="og:image"]',
        'meta[name="twitter:image"]',
    ):
        tag = soup.select_one(selector)
        if tag:
            link = safe_poster_link(tag.get('content'), page_url)
            if link:
                social.append(link)
    for url in social:
        if not SIZED_IMAGE.search(urlparse(url).path):
            return url

    # Powiekszone, ale nienaruszajace proporcji warianty srcset.
    # Nie zgadujemy adresow na podstawie nazwy miniatury.
    for url in hero_candidates:
        match = re.search(r'-(\d+)x(\d+)\.', urlparse(url).path)
        if match and int(match.group(1)) >= 700 and int(match.group(1)) != int(match.group(2)):
            return url
    return None


def previous_posters() -> dict[tuple[str, str], str]:
    """Cache z ostatniej publikacji; nie pobieramy tych samych stron co 30 minut."""
    try:
        request = Request(PREVIOUS_FEED, headers={
            'User-Agent': 'MSK-Synchronizator/1.0 (poster cache)',
            'Accept': 'application/json',
        })
        with urlopen(request, timeout=12) as reply:
            if reply.status != 200:
                return {}
            data = json.load(reply)
        if data.get('schemaVersion') != 1 or not data.get('complete'):
            return {}
        out = {}
        for record in data.get('events', []):
            if not isinstance(record, dict):
                continue
            page = record.get('pageUrl')
            image = record.get('imageUrl')
            detail = record.get('detailImageUrl')
            if not isinstance(page, str) or not isinstance(image, str):
                continue
            base = page.split('?', 1)[0]
            if detail and safe_poster_link(detail, base):
                out[(base, image)] = detail
        return out
    except (HTTPError, URLError, OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(f'UWAGA: bez cache plakatow: {str(exc)[:100]}', file=sys.stderr)
        return {}


def fetch_full_poster(page_url: str) -> str | None:
    request = Request(page_url, headers={
        'Accept': 'text/html',
        'User-Agent': 'Mozilla/5.0 (compatible; MSK-CalendarSync/1.0)',
    })
    with urlopen(request, timeout=9) as reply:
        if reply.status != 200:
            return None
        # Ograniczenie wielkosci dokumentu chroni synchronizator.
        payload = reply.read(2_500_001)
        if len(payload) > 2_500_000:
            return None
        encoding = reply.headers.get_content_charset() or 'utf-8'
        html = payload.decode(encoding, errors='replace')
    return select_full_poster(html, page_url)


def attach_detail_images(records: list[dict]) -> dict:
    """Pobiera pelne plakaty raz dla unikalnej strony, z cache poprzedniej publikacji."""
    originals = previous_posters()
    # Jeden wpis MEC ma nieraz kilkadziesiat lub kilkaset terminow.
    pages = {}
    for item in records:
        page = item['pageUrl'].split('?', 1)[0]
        thumb = item.get('imageUrl')
        if thumb:
            pages[(page, thumb)] = thumb
    found = {}
    pending = []
    reused = 0
    for (page, thumb) in pages:
        if not SIZED_IMAGE.search(urlparse(thumb).path):
            found[(page, thumb)] = thumb
        elif (page, thumb) in originals:
            found[(page, thumb)] = originals[(page, thumb)]
            reused += 1
        else:
            pending.append((page, thumb))

    failed = 0
    if pending:
        print(f'Pelne plakaty: {len(pending)} stron do pobrania z MSK, {reused} z cache')
        # Maksymalnie trzy jednoczesne zapytania; nie odpytujemy osobno wystapien.
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {pool.submit(fetch_full_poster, page): (page, thumb) for page, thumb in pending}
            for future in as_completed(futures):
                key = futures[future]
                try:
                    full = future.result()
                    if full:
                        found[key] = full
                except (HTTPError, URLError, OSError, ValueError, UnicodeError) as exc:
                    failed += 1
                    if failed <= 5:
                        print(f'UWAGA: nie pobrano plakatu {key[0]}: {str(exc)[:100]}', file=sys.stderr)

    full_count = 0
    for item in records:
        page = item['pageUrl'].split('?', 1)[0]
        thumb = item.get('imageUrl')
        # Brak detailImageUrl jest kompatybilny z dotychczasowa aplikacja.
        full = found.get((page, thumb)) if thumb else None
        item['detailImageUrl'] = full
        full_count += bool(full)
    print(f'Pelne plakaty: {full_count}/{len(records)} terminow; '
          f'z cache {reused}, sprawdzono {len(pending)} stron, bledy HTTP: {failed}')
    return {
        'detailImages': full_count,
        'detailImagePagesFromCache': reused,
        'detailImagePagesChecked': len(pending),
        'detailImageFetchFailures': failed,
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
    image_quality = attach_detail_images(clean)
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
        **image_quality,
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
