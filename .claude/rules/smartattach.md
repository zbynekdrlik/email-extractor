---
paths:
  - "email-extractor/app/smartattach.py"
  - "email-extractor/app/process.py"
  - "email-extractor/app/reextract.py"
  - "email-extractor/tests/test_smartattach.py"
---

# Prílohy cez odkaz — IceWarp SmartAttach (#506)

- Mail bez MIME prílohy s textom „IceWarp SmartAttach“ a riadkom `1/1: <súbor> (271 kB): https://icw.li/<kód>`. `process.link_attachments` súbor stiahne pri príjme mailu a ďalej ide ako bežná príloha: uloží sa a extrahuje rovnako.
- **IceWarp podľa User-Agenta**: `curl/*` dostane súbor priamo, každý iný klient HTML stránku „IceWarp Collaboration“ (`/collaboration/?ticket=T&url=https://<srv>/teamchatapi/`). `smartattach.download_url` z nej odvodí `<url>files.download?ticket=T`, len na tom istom hoste a porte. Nefalšuj UA na `curl`.
- `Content-Disposition` `filename=` má surové UTF-8 bajty, ktoré `http.client` dekóduje ako latin-1 („PekarovÃ¡“). Prednosť má meno z tela mailu.
- **Bezpečnosť** (nemeniť bez nového review):
  - prvý hop len `https://icw.li`;
  - každý hop https a verejná IP, vrátane IPv4 skrytej v IPv6 (mapped, 6to4, Teredo, `::a.b.c.d`, NAT64);
  - žiadny env proxy;
  - `read1` + 120 s deadline na telo, 25 MB;
  - magické bajty podľa prípony a skutočne rozbalená veľkosť zipu ≤ 200 MB;
  - max 5 odkazov na mail.
- Každé zlyhanie (aj neočakávaná výnimka) = textová príloha `<meno>.odkaz.txt`, flag `link_failed`. Je v `combined_text`, ingest mailu nikdy nespadne. DL engine ju nečíta (berie len PDF/obrázky).
- Už prijatý mail: `python -m app.reextract <messages.id>` v kontajneri. Doplní prílohy z odkazu (idempotentne podľa sha256) a prepíše `combined_text`. Starú poznámku o zlyhaní prepne na `skipped_link_superseded`. Spracovanie objednávky je samostatný krok: reprocess cez aplikáciu.
- **Pred reprocessom** pozri odpoveď skladu na `mail` otázku. `manual` („vybav ručne v ORIONe“) znamená, že sklad objednávku zadáva sám a reprocess by ju v CODEXe zdvojil (21080/21081, 20.11.). Kontrolu extrakcie bez odoslania urobíš cez `orders.extract.run(llm.from_config(cfg), {...combined_text...})`.
- Zákazník Brezno (2000000000819) píše množstvo do stĺpca F bez hlavičky, vedľa „Trvanlivosť“ (E). Extrakcia to číta správne (overené 20.11.).
