
    
  
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

