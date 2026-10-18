#!/usr/bin/env python3
"""MSK: publiczne wydarzenia MEC -> jeden JSON. Bez WordPress admin i API.

Wymagania: pip install playwright beautifulsoup4; python -m playwright install chromium.
Skrypt jest narzedziem diagnostycznym do uruchomienia na komputerze autora aplikacji.
"""
from __future__ import annotations

import argparse
from collections import Counter
import calendar
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import time
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout, Error as PlaywrightError

BASE = "https://msk.lodz.pl/kalendarz-msk/"
VALID_HOSTS = {"msk.lodz.pl", "www.msk.lodz.pl"}
DAY = re.compile(r"^\d{8}$")
CLOCK = re.compile(r"(?<!\d)(?:[01]?\d|2[0-3]):[0-5]\d(?!\d)")


def valid_link(link: str) -> bool:
    u = urlparse(link)
    return u.scheme == "https" and u.hostname in VALID_HOSTS


def month_key(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def month_add(year: int, month: int, delta: int) -> tuple[int, int]:
    absolute = year * 12 + month - 1 + delta
    return absolute // 12, absolute % 12 + 1


def html_summary(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    blocks = soup.select(".mec-calendar-events-sec[data-mec-cell]")
    counts = Counter()
    for block in blocks:
        d = block.get("data-mec-cell", "")
        if DAY.fullmatch(d):
            counts[d[:4] + "-" + d[4:6]] += 1
    center = max(counts.items(), key=lambda entry: entry[1])[0] if counts else None
    return {"center": center, "blocks": sum(counts.values()), "byMonth": dict(counts)}


def parse_month(html: str, year: int, month: int) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    wanted = month_key(year, month).replace("-", "")
    events, seen = [], set()
    for block in soup.select(".mec-calendar-events-sec[data-mec-cell]"):
        compact = (block.get("data-mec-cell") or "").strip()
        if not DAY.fullmatch(compact) or not compact.startswith(wanted):
            continue
        iso = f"{compact[:4]}-{compact[4:6]}-{compact[6:]}"
        try:
            datetime.strptime(iso, "%Y-%m-%d")
        except ValueError:
            continue
        for article in block.select("article.mec-event-article"):
            title_anchor = article.select_one(".mec-event-title a[href], h4 a[href]")
            if title_anchor is None:
                continue
            title = title_anchor.get_text(" ", strip=True)
            link = urljoin(BASE, title_anchor.get("href", ""))
            if not title or not valid_link(link):
                continue
            time_node = article.select_one(".mec-event-time")
            tm = CLOCK.search(time_node.get_text(" ", strip=True)) if time_node else None
            start_time = tm.group(0) if tm else None
            # Identyfikator wystapienia, a nie tytul ani zdjecie plakatu.
            key = (compact, link, start_time)
            if key in seen:
                continue
            seen.add(key)
            venue_node = article.select_one(".mec-event-loc-place")
            place = venue_node.get_text(" ", strip=True) if venue_node else ""
            categories = list(dict.fromkeys(
                link_node.get_text(" ", strip=True)
                for link_node in article.select("a[href*='/mec-category/']")
                if link_node.get_text(" ", strip=True)
            ))
            img = article.select_one(".mec-event-image img, img.mec-event-image")
            image = None
            if img:
                for attr in ("data-src", "data-lazy-src", "src"):
                    candidate = urljoin(BASE, (img.get(attr) or "").strip())
                    if valid_link(candidate):
                        image = candidate
                        break
            events.append({
                "date": iso,
                "time": start_time,
                "title": title,
                "place": place,
                "categories": categories,
                "imageUrl": image,
                "pageUrl": link,
            })
    return sorted(events, key=lambda item: (item["date"], item["time"] or "", item["title"]))


def current_center(page) -> str | None:
    return page.evaluate("""() => {
      const counts = {};
      document.querySelectorAll('.mec-calendar-events-sec[data-mec-cell]').forEach(el => {
        const d = el.getAttribute('data-mec-cell') || '';
        if (/^\\d{8}$/.test(d)) {
          const key = d.slice(0,4) + '-' + d.slice(4,6);
          counts[key] = (counts[key] || 0) + 1;
        }
      });
      return Object.keys(counts).sort((a,b) => counts[b] - counts[a])[0] || null;
    }""")


def click_next(page) -> None:
    outcome = page.evaluate("""() => {
      const block = document.querySelector('.mec-calendar-events-sec[data-mec-cell]');
      const root = block?.closest('.mec-full-calendar') ||
         block?.closest('.mec-calendar') || document.querySelector('.mec-full-calendar') || document;
      let elems = [...root.querySelectorAll('.mec-next-month a, a.mec-next-month, .mec-next-month')];
      if (!elems.length) elems = [...document.querySelectorAll('.mec-next-month a, a.mec-next-month, .mec-next-month')];
      const visible = elems.find(el => {
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
      });
      if (!visible) return 'no-visible-next-button';
      visible.click();
      return 'clicked';
    }""")
    if outcome != "clicked":
        raise RuntimeError(f"Nie znaleziono widocznej strzalki 'nastepny miesiac': {outcome}")


def wait_month_ready(page, expected: str, timeout_sec: int, browser_events: list[str]) -> dict:
    """MEC moze zachowywac stare miesiace w ukrytym DOM.

    Dlatego NIE wybieramy miesiaca, ktory ma najwiecej blokow w calym
    dokumencie. Sprawdzamy, czy WSZYSTKIE dni miesiaca docelowego sa juz
    obecne, czy liczba jego wydarzen jest stabilna i czy jQuery zakonczylo
    pobieranie. Dni z miesiecy sasiednich nie zaliczaja sie do warunku.
    """
    y, m = map(int, expected.split("-"))
    expected_days = calendar.monthrange(y, m)[1]
    deadline = time.monotonic() + timeout_sec
    stable = 0
    previous_signature = None
    last = None

    while time.monotonic() < deadline:
        if page.is_closed():
            raise RuntimeError(f"Karta Chromium zamknela sie podczas odczytu {expected}")
        try:
            info = page.evaluate("""target => {
              const counts = {};
              const targetDates = new Set();
              let targetArticles = 0;
              document.querySelectorAll('.mec-calendar-events-sec[data-mec-cell]').forEach(el => {
                const d = el.getAttribute('data-mec-cell') || '';
                if (!/^\\d{8}$/.test(d)) return;
                const key = d.slice(0,4) + '-' + d.slice(4,6);
                counts[key] = (counts[key] || 0) + 1;
                if (key === target) {
                  targetDates.add(d);
                  targetArticles += el.querySelectorAll('article.mec-event-article').length;
                }
              });
              return {
                targetDays: targetDates.size,
                targetBlocks: counts[target] || 0,
                targetArticles,
                monthsInDom: counts,
                ajaxActive: (window.jQuery && window.jQuery.active) || 0
              };
            }""", expected)
        except PlaywrightError as exc:
            raise RuntimeError(
                f"Chromium nie pozwala odczytac {expected}: {type(exc).__name__}: "
                f"{str(exc)[:150]}"
            ) from exc

        last = info
        signature = (info["targetDays"], info["targetBlocks"], info["targetArticles"])
        ready = info["targetDays"] == expected_days and info["ajaxActive"] == 0
        if ready and signature == previous_signature:
            stable += 1
        else:
            stable = 0
        if ready and stable >= 4:
            return info
        previous_signature = signature
        time.sleep(0.5)

    raise TimeoutError(
        f"Brak kompletnej siatki {expected} po {timeout_sec}s. "
        f"Oczekiwano {expected_days} unikalnych dni; stan: {last}; "
        f"odpowiedzi MEC: {browser_events[-5:]}"
    )


def extract_target_month_html(page, target: str) -> str:
    """Eksportuj tylko bloki dni miesiaca docelowego, a nie caly DOM.

    Przy wielu miesiacach MEC pozostawia poprzednie miesiace w dokumencie;
    pobieranie pelnego page.content() niepotrzebnie obciaza pamiec.
    """
    prefix = target.replace("-", "")
    return page.evaluate("""prefix =>
      [...document.querySelectorAll('.mec-calendar-events-sec[data-mec-cell]')]
        .filter(el => (el.getAttribute('data-mec-cell') || '').startsWith(prefix))
        .map(el => el.outerHTML)
        .join('\\n')
    """, prefix)


def save_json(path: Path, collected: list[dict], coverage: list[dict], complete: bool) -> None:
    """Zapis atomowy. Czesciowy eksport nigdy nie zastapi pelnej bazy."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schemaVersion": 1,
        "complete": complete,
        "source": BASE,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "months": coverage,
        "events": collected,
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Zbiorczy eksport publicznego kalendarza MSK")
    parser.add_argument("--months", type=int, default=12, help="Liczba kolejnych miesiecy (domyslnie 12)")
    parser.add_argument("--output", default="msk_events.json", help="Plik wynikowy JSON")
    parser.add_argument("--visible", action="store_true", help="Pokaz przegladarke (diagnostyka)")
    args = parser.parse_args()
    if not 1 <= args.months <= 24:
        parser.error("Parametr --months musi miec wartosc 1..24")

    output = Path(args.output)
    partial = output.with_name(output.stem + ".partial" + output.suffix)
    collected, coverage = [], []
    browser_events: list[str] = []

    with sync_playwright() as p:
        # --visible pozostaje opcja diagnostyczna; do codziennego eksportu
        # uruchamiaj bez niej, aby zuzywac mniej pamieci.
        browser = p.chromium.launch(headless=not args.visible)
        try:
            context = browser.new_context(
                viewport={"width": 1280, "height": 800},
                locale="pl-PL",
                reduced_motion="reduce",
                service_workers="block",
            )
            # Importer potrzebuje tekstu, linkow i dat, nie obrazow.
            # Blokowanie pobierania obrazow/czcionek ogranicza zuzycie RAM
            # na stronie z ponad tysiacem elementow wydarzen.
            def route_lightweight(route):
                if route.request.resource_type in ("image", "font", "media"):
                    route.abort()
                else:
                    route.continue_()

            context.route("**/*", route_lightweight)
            page = context.new_page()
            page.set_default_timeout(15000)
            page.on("crash", lambda: browser_events.append("CRASH: karta Chromium"))
            page.on("close", lambda: browser_events.append("CLOSE: karta zamknieta"))
            browser.on("disconnected", lambda: browser_events.append("DISCONNECT: Chromium"))

            def record_ajax(resp):
                try:
                    if "/wp-admin/admin-ajax.php" not in resp.url:
                        return
                    body = resp.request.post_data or ""
                    for action in ("mec_refine_search_items", "mec_full_calendar_switch_skin"):
                        if action in body:
                            browser_events.append(f"{action}: HTTP {resp.status}")
                            if len(browser_events) > 25:
                                del browser_events[:-25]
                            break
                except PlaywrightError:
                    pass

            page.on("response", record_ajax)
            print("Otwieram kalendarz MSK (bez pobierania zdjec)...", flush=True)
            page.goto(BASE, wait_until="domcontentloaded", timeout=70000)
            page.wait_for_selector(
                ".mec-calendar-events-sec[data-mec-cell]",
                state="attached", timeout=60000
            )
            first = current_center(page)
            if not first or not re.fullmatch(r"\d{4}-\d{2}", first):
                raise RuntimeError("Nie odnaleziono miesiaca w publicznym kalendarzu")
            year, month = map(int, first.split("-"))

            for offset in range(args.months):
                target_year, target_month = month_add(year, month, offset)
                target = month_key(target_year, target_month)
                if offset:
                    print(f"Przelaczam na {target}...", flush=True)
                    click_next(page)
                status = wait_month_ready(page, target, 50, browser_events)
                html = extract_target_month_html(page, target)
                summary = html_summary(html)
                items = parse_month(html, target_year, target_month)
                print(f"{target}: {len(items)} terminow, "
                      f"{status['targetDays']} dni ({status['targetBlocks']} blokow HTML), "
                      f"{status['targetArticles']} artykulow", flush=True)
                collected.extend(items)
                coverage.append({"month": target, "daysInGrid": summary["blocks"], "events": len(items)})
                save_json(partial, collected, coverage, complete=False)
                # Umiarkowane tempo, by ograniczyc obciazenie serwera MSK.
                time.sleep(1.2)
        except (PlaywrightError, TimeoutError, RuntimeError) as exc:
            print(f"BLAD: {exc}", file=sys.stderr)
            print(f"Szczegoly przegladarki: {browser_events[-8:]}", file=sys.stderr)
            if coverage:
                print(f"Zachowano {len(collected)} terminow w pliku "
                      f"{partial.resolve()} (NIEPELNE DANE).", file=sys.stderr)
            print("Sprawdz komunikat powyzej. Dane zebrane przed bledem pozostaja w pliku partial.",
                  file=sys.stderr)
            return 1
        finally:
            try:
                browser.close()
            except PlaywrightError:
                pass

    save_json(output, collected, coverage, complete=True)
    if partial.exists():
        partial.unlink()
    print(f"OK. Zapisano {len(collected)} terminow w {output.resolve()}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (PlaywrightTimeout, TimeoutError, RuntimeError) as exc:
        print(f"BLAD SYNCHRONIZACJI: {exc}", file=sys.stderr)
        print("Nie nadpisano istniejacej bazy. Mozesz uruchomic --visible i przeslac log.", file=sys.stderr)
        sys.exit(1)
