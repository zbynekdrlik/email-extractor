# Produkt bez EAN v CODEXe — sklad doplní, potvrdí na nástenke, doklad odíde (#502)

## Prečo

Súbor DL (DESADV) aj objednávky (ORDER) označuje produkt EAN kódom a CODEX podľa neho hľadá kartu.
Karta bez EAN sa preto nedá poslať. Dnes aplikácia o takej karte vôbec nevie, lebo
`tools/codex_cards_push.py` posiela len riadky `NEANKOD > 0`. Sklad dostane len všeobecnú otázku
alebo doklad ide bez riadku a zvyšok zadáva ručne.

Meranie (centrála, stredisko 1, sklady 1 a 100): 573 kariet, z toho 57 aktívnych bez EAN.

Požiadavka ownera (4.11.2026): sklad dostane notifikáciu, že má pre produkt v CODEXe vygenerovať
alebo doplniť EAN. Keď to spraví, klikne na nástenke, že je hotovo, a aplikácia pošle správny DL
alebo objednávku do CODEXu.

## Rozhodnuté ownerom

- Pri kliknutí sklad **opíše EAN aj na nástenku**. Doklad odíde hneď, nečaká na prepočet
  codex-bridge (14:15 a 18:00, trvá ~50 min). Ďalší prepočet kód overí.
- Prijaté riziko: preklep CODEX pri importe odmietne a sklad píše EAN dvakrát.
- Po preklepe, keď CODEX doklad odmietne, ho **sklad zadá ručne a súbor zmaže** (dnešný postup,
  `import_status='manual'`, #494). Aplikácia ten doklad znova neposiela (#239). Opravu z nástenky
  a nové odoslanie owner zamietol kvôli riziku duplicity. Kód karty opraví ďalší prepočet podľa
  CODEXu (riadok „iný kód" v časti 6). „Späť" pri tejto odpovedi je odmietnuté s týmto pokynom.

## Ciele

1. Riadok, ktorého produkt je v CODEXe karta bez EAN, skončí otázkou, ktorá to povie priamo:
   ktorá karta, čo spraviť v CODEXe, kam opísať kód.
2. Po potvrdení odíde kompletný doklad bez ďalšieho ručného kroku.
3. Ďalší prepočet opísaný kód overí a nesúlad ohlási.
4. Platí pre DL aj objednávky.

## Mimo rozsahu

- Produkt, ktorý v CODEXe nemá ani kartu. To ostáva dnešná otázka.
- Hromadná notifikácia všetkých 57 kariet. Notifikácia ide len pri skutočnom doklade.
- Strediská iné ako 1.
- Generovanie EAN aplikáciou alebo zápis do CODEXu. CODEX je len na čítanie, kód vytvára sklad.

## Čo sa nesmie stať

- Doklad odíde potichu s kódom, ktorý nikto nepotvrdil, alebo sa riadok potichu stratí.
- Písanie kódu sa stane všeobecnou cestou, ako vytvoriť kartu. Zákaz z #477 ostáva, výnimka platí
  len pre kartu zo zoznamu kariet bez EAN, a to z tejto otázky.
- Hromadný import kariet do katalógu (#337). Zoznam kariet bez EAN do katalógu nevstupuje.
- Opätovné poslanie už prevzatého dokladu (#239).
- Zmena výsledkov korpusov. Všetko je len na živej ceste, shadow ostáva bajtovo rovnaký.
- Sklad dostane o jednej veci viac správ (#495).

## Návrh (voľby builderа/autora, nie ownera)

### 1. Zoznam kariet bez EAN

`codex_cards_push.py` pridá do toho istého POST-u kľúč `no_code`: aktívne riadky `raw.sm002`
strediska 1 s prázdnym `NEANKOD` (`card_code`, `stredisko`, `sklad`, `name`). Rozhoduje KARTA, nie
riadok: karta, ktorá má kód na inom riadku strediska 1, v zozname nie je. Inak by sklad dostal pokyn
vytvoriť EAN karte, ktorá ho už má (#467: kód môže byť len na časti riadkov). Nová tabuľka
`codex_nocode_cards` sa nahrádza v tej istej transakcii ako `codex_stock_cards`. Push bez kľúča
tabuľku nemení, aby staršia verzia toolu na dev2 nič nezmazala. Rozsah per katalóg ako
`card_guard._SCOPES`: objednávky sklad 1, DL všetky sklady strediska 1.

### 2. Rozpoznanie

Beží až pre riadok, ktorý ostal bez karty (`match_failed`, `codex_missing` a podobne) a ide na
otázku. Nikdy nezmení rozhodnutie o karte a nikdy sám nič nepošle.

1. Ak pamäť riadku ukazuje na mŕtvy kód a neaktívna väzba `codex_card_bindings` vedie na kartu zo
   zoznamu bez EAN, je to táto karta. Toto je prípad karty, ktorej EAN v CODEXe zmizol.
2. Inak `match.candidates` nad názvami zoznamu bez EAN s prahom `PLAUSIBLE_CANDIDATE_SCORE`.
   Do otázky ide najviac jedna karta, a to len keď je zhoda jednoznačná.

Výsledok ide do `payload.codex_no_ean = {card_code, name}` otázky `dl_item` alebo `item`.
Ostatné možnosti otázky ostávajú. Na `codex_no_ean` nezávisí, či je otázka položená.
`„Vybrať kartu z CODEXu"` navyše zobrazí karty bez EAN s označením „bez EAN", aby sklad vedel
začať aj vtedy, keď rozpoznanie kartu netrafí.

### 3. Notifikácia

Žiadny nový kanál. Je to existujúca správa k otázke (`dl_events._post_review`,
`question_alerts`), len s konkrétnym textom cez funkciu v textovom module:
„Karta X v CODEXe nemá EAN. Doplň ho v CODEXe a opíš ho na nástenke." a odkaz na otázku.

### 4. Potvrdenie na nástenke

Karta otázky ukáže kartu CODEXu, pokyn a pole na kód s tlačidlom „Hotovo, EAN je v CODEXe".
Kontroly na serveri:

- len číslice, po `codex_cards.normalize_code`;
- dĺžka: DL najviac `desadv_edi.GTIN_FIELD_WIDTH`;
- kód nesmie niesť iná karta (iný `card_code`) na stredisku 1, aktívna ani neaktívna — 409 s názvom
  tej karty. Výskyt na iných strediskách neblokuje, len sa zaloguje (dnes 17 kódov nesú dve a viac
  kariet). Otázka ostáva zodpovedateľná inou kartou alebo „pošli bez";
- 8 alebo 13 číslic so zlou kontrolnou číslicou vyžaduje druhé potvrdenie `window.confirm`,
  nie odmietnutie, lebo interné kódy CODEXu nemusia byť platné EAN.

Výnimka z #477 je v jednej funkcii v `card_guard` (`add_typed_for_nocode`), ktorá vyžaduje otvorenú
otázku a kartu, ktorá je v zozname bez EAN.

### 5. Po kliknutí

V jednej transakcii: riadok `codex_typed_codes` (`card_code`, `code`, `stredisko`, `sklad`, `name`,
`scope`, `question_id`, `created_by`, `created_at`, `confirmed_at`, `closed_reason`) + rovnaký
riadok do `codex_stock_cards`. `live_guard`, `pickable` aj sync tak čítajú jednu pravdu, ako pri
`codex_code_overrides`.

Potom bežná cesta odpovede: karta cez logiku `add_from_codex` (kód už v katalógu sa len vyberie,
kód v Koši sa obnoví), `teach.KINDS[...].apply` a `release_for_question`, takže doklad odíde
rovnako ako po každej odpovedi.

### 6. Overenie ďalším prepočtom

`replace_cards` pred zápisom prejde otvorené riadky `codex_typed_codes`:

| Čo prišlo v zozname pre kartu | Čo sa stane |
|---|---|
| rovnaký kód | `confirmed_at`, záznam sa zavrie, len log |
| karta stále bez kódu, 1. nová generácia | riadok sa znova pridá do zoznamu, raz ohlásené skladu „v CODEXe EAN ešte nie je" |
| karta stále bez kódu, 2. nová generácia | záznam sa zavrie a kód sa z guardu odstráni, jedno ops upozornenie. Karta je potom bežný `codex_missing`, ďalší doklad sa pýta a neodíde s kódom, ktorý CODEX nepozná |
| iný kód | záznam sa zavrie, platí CODEX, sync prečísluje, ops upozornenie, lebo odoslaný doklad mohol niesť zlý kód |
| karta zmizla | záznam sa zavrie, ops upozornenie |

Porovnáva sa len zoznam novšej generácie (`source_as_of` novší ako `created_at` záznamu).
Opätovný push rovnakej generácie nič nerozhoduje.

### 7. Čakanie dokladu

Nemení sa. DL je držaný ako pri #365 do odpovede. Objednávka čaká ako pri `item` otázke: pri
termíne dodania odíde bez riadku a otázka ostane (#479). Horizont 2 pracovné dni platí ďalej.

Dve výnimky z „odíde hneď", obe zámerné:

- **Faktúra ako DL (#485):** `requeue_invoice` čaká na novšie dáta z CODEXu, lebo chráni pred
  duplicitou, ak sklad medzitým zadal dodávku ručne. Toto sa neoslabuje, text otázky to povie.
- **Hmotnosť (#462):** nová karta skladu 100 má prázdnu hmotnosť. Dodávka v kusoch sa preto ešte
  raz spýta `dl_mass`. Alternatívou by bolo poslať zlé množstvo (×N).

## Testy

RED pred implementáciou, proti skutočnému Postgresu:

- push s `no_code` naplní tabuľku, push bez kľúča ju nemení;
- riadok bez karty + zhoda v zozname bez EAN → otázka s `codex_no_ean`, nikdy upload;
- nejednoznačná alebo žiadna zhoda → dnešná otázka bez `codex_no_ean`;
- karta so zmiznutým EAN sa nájde cez neaktívnu väzbu;
- odpoveď: zlý kód, cudzí kód, príliš dlhý kód, karta mimo zoznamu → odmietnutie bez zápisu;
- odpoveď: platný kód → guard ho prijme a DL aj objednávka nesú presne opísaný kód;
- zlá kontrolná číslica bez potvrdenia odmietnutá, s potvrdením prijatá;
- všetky štyri riadky tabuľky z časti 6 vrátane opätovného pushu rovnakej generácie;
- `#477`: písaná karta mimo tejto cesty stále 403;
- shadow bez zmeny (`e2e-orders`, `e2e-dl` byte-identical);
- Playwright: skutočná otázka v nástenke, zadanie kódu, potvrdenie, nula chýb v konzole.

## Nasadenie

Tool `codex_cards_push.py` sa na dev2 inštaluje podľa `.claude/rules/codex-orders.md`, add-on prvý.
Migrácia je nová revízia v `app/migrate.py`. Po nasadení overiť počet riadkov `codex_nocode_cards`
oproti DuckDB (57 aktívnych v skladoch 1 a 100).

## Ponechané builderovi

Mená modulov a funkcií okrem už uvedených, presné znenie textov, poradie krokov v PR.
