# Jednotná nástenka (board redesign) — design spec

Dátum: 2026-09-15 · Epic: #441 · Stav: schválené ownerom (časti 1–5, prístup A)

## 1. Cieľ

Jedna hlavná webstránka pre sklad aj objednávky, so záložkami, kde skladníčka
všetko vidí, vyhľadá, upraví, pridá a — keď niečo zmaže — vie to vrátiť.
Dnešný stav: tri oddelené vstupy (`/otazky`, `/otazky-dl`, `/znalosti` + admin
dashboard), UI ako HTML/JS reťazce v jednom 1434-riadkovom Python súbore,
naučené pravidlá bez zoznamu, mazanie bez cesty späť, „vrátiť" len pre
posledných 20 odpovedí, expirované otázky neviditeľné.

Tvrdá požiadavka ownera: poriadna kódová architektúra — žiadny 1000-riadkový
Python so šablónami ako reťazce.

## 2. Rozhodnutia ownera (#441)

1. Jedna webka pre sklad aj objednávky; oddelené záložky.
2. Prihlásenie: jeden odkaz pre sklad (bez hesla, HMAC kľúč ako dnes); admin
   po prihlásení heslom vidí to isté + technické záložky.
3. Záložky: Otázky sklad · Otázky objednávky · Produkty sklad · Produkty
   objednávky · Naučené sklad · Naučené objednávky · Zákazníci (spoločné) ·
   Dodávatelia (sklad) · História objednávok · História dodacích listov ·
   Kôš / História zmien. Admin navyše: Maily (dnešný dashboard).
4. História: dohľadať doklad, pozrieť položky a párovanie, otvoriť originál,
   akcie „spustiť znova" / „zadané ručne" / „doučiť: toto má byť iné".
5. Technický prístup **A**: Flask + Jinja šablóny v súboroch + vanilla JS
   ES moduly, bez build kroku, bez CDN, bez novej runtime závislosti.
6. Poradie prác: Otázky + Kôš → Produkty / Zákazníci / Dodávatelia /
   Naučené → História; nová nástenka beží popri starej, staré stránky sa
   vypnú až po overení so skladom.

## 3. Architektúra kódu

```
email-extractor/app/
  board/                      # Flask blueprinty po záložkách, ≤200 r. každý
    __init__.py               # register_board(app): blueprint `board`, /nastenka
    auth.py                   # rola z session/kľúča: sklad | admin; jeden gate
    questions_orders.py       # /nastenka/otazky-objednavky + /api/board/...
    questions_dl.py
    products_orders.py
    products_dl.py
    rules_orders.py           # naučené pravidlá objednávok
    rules_dl.py
    customers.py
    suppliers.py
    history_orders.py
    history_dl.py
    trash.py                  # kôš + história zmien + vrátiť
    services/                 # logika, žiadne Flask objekty, žiadne SQL v routách
      questions.py            # list/filter/reopen/undo nad orders.teach + hold
      catalog.py              # produkty (orders + DL) nad snapshot / dl_snapshot
      rules.py                # mail_rules, item_memory, global_item_memory,
                              # dl_item_memory, dl_supplier_memory
      partners.py             # customer_overrides, dl_supplier_overrides
      history.py              # messages + order_runs + email_events + desadv/edi
      teachback.py            # „doučiť z histórie" → rovnaké zápisy ako answer
      audit.py                # audit_log zápis + vrátenie
  templates/board/
    layout.html               # hlavička, verzia, záložky, toasty
    _table.html _card_form.html _confirm.html _pager.html   # partialy
    questions.html products.html rules.html partners.html
    history.html trash.html
  static/board/
    board.css
    api.js                    # fetch wrapper, chyby → toast
    ui.js                     # tabuľka, hľadanie s debounce, formuláre,
                              # potvrdenia, refresh-safety (data-open, focus)
    tab-questions.js tab-products.js tab-rules.js tab-partners.js
    tab-history.js tab-trash.js
```

Pravidlá:

- Routa = parsovanie vstupu + volanie service + odpoveď. Service = logika a
  DB (cez `db.connect()`, autocommit ako dnes). Existujúce moduly
  (`orders.teach`, `orders.hold`, `orders.snapshot`, `orders.dl_snapshot`,
  `orders.dl_questions`, `httpapi_znalosti` funkcie) sa **volajú**, nekopírujú.
- Jedna kópia každého JS helpera (`api.js`, `ui.js`); žiadne HTML v Pythone.
- Šablóny sa renderujú `render_template`; verzia v `[data-testid="version"]`.
- Statické súbory servíruje Flask z `app/static/` (žiadne CDN).

## 4. Záložky — obsah a akcie

| Záložka | Zobrazuje | Akcie |
|---|---|---|
| Otázky sklad / Otázky objednávky | karty otázok (dnešné druhy: item, customer, mail, date, line / dl_item, dl_supplier), filter otvorené · expirované · zodpovedané, náhľad originálu (mail/sken) vedľa otázky | odpovedať (dnešné voľby vrátane „➕ Nová karta", „Vyriešené ručne", „Netýka sa skladu"), **Vrátiť odpoveď** pri každej zodpovedanej, **Znovu otvoriť** expirovanú |
| Produkty sklad / Produkty objednávky | tabuľka kariet (číslo položky, názov, doplnok/aliasy, jednotka), hľadanie | pridať, upraviť, zmazať (kôš), spravovať aliasy položky na karte |
| Naučené sklad / Naučené objednávky | ignorované maily (odosielateľ + predmet, pôvod), aliasy položiek (per zákazník + globálne), pamäť dodávateľov, s odkazom na otázku/doklad, z ktorého vznikli | upraviť, zmazať (kôš), vrátiť |
| Zákazníci | karty zákazníkov vrátane prevádzok (rodina), e-maily, adresy | pridať, upraviť, zmazať (kôš), vrátiť |
| Dodávatelia | karty dodávateľov DL, e-maily, mesto | pridať, upraviť, zmazať (kôš), vrátiť |
| História objednávok / História dodacích listov | doklady: dátum, partner, stav (odišlo do ORIONu · čaká na sklad · na kontrole · zlyhalo · zadané ručne), detail = položky + ako sa spárovali (karta, pravidlo, istota) + originál | spustiť znova, zadané ručne, **doučiť: toto má byť iné** |
| Kôš / História zmien | každá zmena (kto, kedy, tabuľka, akcia, pred/po) a všetko zmazané | **Vrátiť** |
| Maily (admin) | dnešný dashboard (maily, fix fronta, neprijaté, zahodené AI) | dnešné akcie |

## 5. Dáta

- **Mäkké mazanie:** stĺpec `deleted_at timestamptz` na `catalog_overrides`,
  `dl_catalog_overrides`, `customer_overrides`, `dl_supplier_overrides`,
  `mail_rules`, `item_memory`, `global_item_memory`, `dl_item_memory`,
  `dl_supplier_memory` (kde je dnes `retired`, migrácia ho zjednotí na
  `deleted_at`). Zmazaný riadok sa pri párovaní nepoužije (snapshot rebuild
  ho vynechá). Fyzické DELETE z UI zmizne.
- **`audit_log`** (nová tabuľka, verzovaná migrácia): `id, ts, actor
  (sklad|admin|auto:<modul>), table_name, row_id, action
  (create|update|delete|restore|answer|undo|reopen|teach), before jsonb,
  after jsonb, note, question_id, message_id`. Píše ho `services/audit.py`
  pri každej zmene cez nástenku a existujúce `teach.apply/undo` cesty.
  „Vrátiť" = obnoviť `before` (alebo `deleted_at=NULL`) + nový audit riadok
  `restore`.
- **Otázky:** expirované (`status='expired'`) sú viditeľné; „Znovu otvoriť"
  = `status='open'` + hold späť do `held` (cez `hold`), audit `reopen`.
- **Doučenie z histórie:** `services/teachback.py` zapíše tú istú pamäť ako
  odpoveď na otázku (`item_memory`/`global_item_memory`/`dl_item_memory`,
  `source='teachback'`, odkaz na `message_id`); odoslaný doklad v ORIONe sa
  nemení. „Spustiť znova" a „zadané ručne" volajú dnešné sankcionované cesty
  (`hold`/release, sanctioned reset s kontrolou `edi_sent`/`desadv_sent`
  a ORIONu — nikdy neposlať dvakrát).

## 6. Role a prepnutie

- `/sklad/<kľúč>` a `/sklad-dl/<kľúč>` → `/nastenka` (jedna rola `sklad` so
  všetkými záložkami; oba kľúče ostávajú platné). Admin (`session.auth`) →
  `/nastenka` + záložka Maily; `/` ostáva dashboard do vypnutia.
- Gate: `board/auth.py` — jeden allowlist pre `/nastenka*` a `/api/board/*`
  namiesto dnešných troch regex sád; DL/orders rozlíšenie robí záložka, nie
  kľúč.
- Rollout po záložkách; každá lane pridá záložku + testy; po dokončení a
  potvrdení skladom sa staré stránky (`/otazky`, `/otazky-dl`, `/znalosti`)
  presmerujú a ich šablóny + hash-testy zmažú.

## 7. Testovanie

- Service + endpoint testy (pytest, test-Postgres) na každú záložku vrátane
  koša (zmazať → vrátiť), audit_log, znovuotvorenie, doučenie.
- Playwright E2E cez skutočný prehliadač na každú záložku: hľadať → upraviť →
  zmazať → vrátiť; otázka → odpoveď → vrátiť; história → doučiť; refresh
  nezničí rozpísaný formulár; 0 chýb v konzole; verzia v DOM = `/version`.
- Korpusy e2e-orders / e2e-dl nedotknuté (mení sa UI + audit, nie párovanie).
- Charakterizačný test routovej mapy sa rozšíri o `board` routy.

## 8. Rozdelenie na tikety (lane)

1. Kostra: `app/board/` + `templates/board/layout.html` + `static/board/` +
   `/nastenka` s prázdnymi záložkami + `audit_log` migrácia + `deleted_at`
   migrácia + `services/audit.py` + presmerovanie kľúčov (bez vypnutia starých
   stránok). Playwright: prihlásenie kľúčom, záložky, verzia.
2. Otázky sklad + Otázky objednávky (filter, vrátiť odpoveď, znovu otvoriť,
   náhľad originálu).
3. Kôš / História zmien (vrátiť čokoľvek).
4. Produkty sklad + Produkty objednávky (+ aliasy).
5. Zákazníci + Dodávatelia.
6. Naučené sklad + Naučené objednávky.
7. História objednávok + História dodacích listov (+ doučenie, spustiť znova,
   zadané ručne).
8. Vypnutie starých stránok + zmazanie šablón/hash-testov + návod skladu.

Každá lane: bump verzie, RED→GREEN, review, PR dev→main, CI, nasadenie,
overenie na živom add-one (DOM verzia + Playwright na novú záložku).
