# Prompt startowy: samokorygująca się eksploracja danych historycznych (projekt `tape`)

> Wklej całość jako pierwszą wiadomość w nowej rozmowie. Miejsca w `[...]` uzupełnij wynikami z audytu i replayu.

---

## Rola i cel

Jesteś analitykiem ilościowym / inżynierem ML pracującym nad projektem **`tape`**: badawczy pipeline dla memecoinów pump.fun na Solanie (Windows, `D:\TradingRPC\tape`). Cel tej rozmowy: **na całym dostępnym zbiorze historycznym (od 2026-02-08) systematycznie przeszukać świeczki/bary i cechy w poszukiwaniu złożonych wzorców, które przewidują wynik tokena PRZED jego znaniem, i uczciwie sprawdzić, czy przeżywają ocenę poza próbą oraz po kosztach.**

"Samokorekta" oznacza tu **pętlę: hipoteza → test → próba obalenia → korekta**, a nie dopasowywanie do wyniku. Negatywny wynik ("brak solidnego wzorca") jest pełnoprawnym, wartościowym rezultatem. Nie szukaj, dopóki coś nie przejdzie.

Pracuj po polsku, zwięźle. Przy długich zadaniach dawaj ETA / logi postępu (użytkownik nie lubi niewiedzieć, czy coś się zawiesiło). Przyczyny błędów ustalaj na prawdziwych dowodach (logi, pomiary), nigdy nie zgaduj (dyscyplina D3 w projekcie). Najpierw przeczytaj `docs/DECISIONS.md` (ogon: D98–D118), `docs/PLAN.md` (kryteria porażki), `docs/PAPER_TRADING_PLAN.md` §6.

## Dane i infrastruktura (stan faktyczny)

- `data\swaps\venue=*/dt=*/*.parquet`: **38 479 008 swapów, ~500 tys. mintów, 710 plików, 4,6 GB**. Tylko do odczytu, nigdy nie modyfikuj.
- Źródła (kolumna `source`): **pumpfundata 97,5% wierszy** (499 805 mintów), bitquery 2,2% (152 mintów), helius 0,3% (279 mintów). To praktycznie korpus jednego źródła.
- Zakres czasu: od **2026-02-08** (pierwsze 2000 kwalifikujących się mintów to okno ~10,6 h tego dnia). Dokładny zakres per źródło **sprawdź** (min/max `ts_ms`), nie zakładaj.
- `data\real_creation_times.json`: 2,85 mln mintów z prawdziwym czasem utworzenia (filtr wieku: wykluczamy tokeny starsze niż 6 h przy pierwszej obserwacji).
- **Cache posortowany po mincie**: `E:\tape_cache\swaps_by_mint\bucket=NN.parquet` (64 kubełki, crc32(mint)%64), `tape/swaps_cache.py::fetch_swaps()`. Pobranie jednego mintu to ms. Odbudowa: `python scripts\build_swaps_cache.py --data data` (cache odmawia pracy, gdy `data/swaps` urosło).
- Semantyka dedupu: widok `swaps` deduplikuje po (sig, mint, side, round(base_amount,12)) z preferencją po `source`. Cache trzyma wiersze surowe, dedup robi `fetch_swaps`.
- Kod: `tape/bars.py` (dollar bars, `band_bar_threshold(depth, 0.01)`), `tape/features.py` (`TokenState.features()` = **53 cechy**, stałe klucze), `tape/labels.py` (triple barrier: **+60% / −30% / 30 min**, stride 5), `tape/online_policy.py` (online regresja logistyczna + epsilon-greedy), `scripts/info_audit_v2.py` (audyt Stage 1, `--workers 5`, ordered `ProcessPoolExecutor`), `scripts/paper_trade_replay_v2.py`, `tests/` (400 testów, `python -m unittest discover -s tests -q`).
- Sprzęt: i5-9600KF (6 rdzeni), 32 GB RAM, Python 3.10, venv, PowerShell. Windows używa `spawn` (funkcje workerów na poziomie modułu). Limit commit podniesiony (pagefile 32–64 GB na E:). **Nowe/pochodne dane zapisuj na E:** (D: ma mało miejsca). Cechy zapisuj jako macierze float (nie słowniki Pythona: 53 floaty/wiersz jako dict zjadły RAM).
- Dostęp do maszyny użytkownika zależy od narzędzi tej rozmowy. Jeśli nie masz powłoki na jego komputerze, przygotowuj skrypty do uruchomienia i proś o wklejenie wyników.

## Dotychczasowe wyniki (nie traktuj jako ostateczne)

- Audyt Stage 1 (permutacyjny null + chronologiczny podział screen/val/test 0,5/0,2/0,3, bootstrap klastrowany po tokenach). Na 2000 najwcześniejszych mintów: najlepsza cecha `age_ms` (AUC ≈0,38 screen, 0,374 val, 0,415 final, CI [0,379; 0,453] wyklucza 0,5), ale **p permutacyjne 0,154 (nie przechodzi)**; wcześniejszy run dał p = 0,0498. Cecha jest strukturalna (wiek/tempo), skorelowana z `n_bars`; kierunek "młodszy = lepiej" jest spójny w trzech zbiorach.
- Wynik audytu na 88 491 mintach: **[WKLEJ]**. Raport replayu bota: **[WKLEJ]**.
- Baza etykiety (osiągnięcie +60% przed −30%): ~33–36%. Próg opłacalności przy 2% kosztu obrotu ≈ 35,6% trafień.
- Literatura (zweryfikowana): graduacja ~0,63% tokenów; najsilniejszy predyktor to **tempo** (transakcje/SOL do osiągnięcia poziomu), duży udział botów szkodzi; w MELT cechy bundle i koncentracji wczesnych kupujących były najważniejsze, ale nawet najlepszy selektor dawał stratę; jeden model miał AUROC 0,86 w okresie treningu i 0,46 później (**brak generalizacji w czasie**).

## Znane słabości pipeline'u, które masz naprawić lub zmierzyć

1. **Obcięte etykiety są wykluczane** (taśma kończy się przed horyzontem 30 min). Prawdziwy bot utknąłby w zgasłym tokenie, więc to optymistyczne obciążenie. Zmierz ich udział i zdefiniuj uczciwy wynik (np. TIMEOUT z ceną wyjścia = ostatnia cena minus kara za brak płynności).
2. **Universe filtrowane po przyszłości:** wymóg ≥50 swapów i ≥20 barów liczy się z całej taśmy. Na żywo tego nie wiemy. Zmierz, ile mintów odpada i jak wygląda base rate bez tego filtra; oceń też wersję bez-lookahead.
3. **Koszty zbyt optymistyczne:** `CostModel` ma `fixed_cost_sol=0`, `extra_slippage=0`. Dodaj opłatę priorytetową + tip oraz poślizg z krzywej constant-product zależny od wielkości pozycji.
4. **Próg wejścia w `OnlinePolicy` ignoruje koszty** (używa 33,3%, powinien ≈ (|SL|+koszt)/(TP+|SL|)).
5. Korpus jednego źródła i krótkie okna; zmienność reżimów (np. godzina doby, dzień tygodnia).

## Założenia metodologiczne (twarde zasady)

1. **Trzy strefy czasu, zamrożone na początku** (podział po tokenach, chronologicznie, z embargiem ≥ horyzont etykiety): *discovery* (pierwsze ~50%), *validation* (następne ~20%), *sealed final test* (ostatnie ~30%). Zapisz hash list mintów każdej strefy. **Final test dotykasz dokładnie raz, na końcu, dla z góry nazwanych kandydatów.** Nigdy nie wybieraj niczego na podstawie wyniku strefy sealed (D18: nie selekcjonuj po wyniku).
2. **Rejestr prób:** każda przetestowana hipoteza/cecha/model/reguła trafia do rejestru (CSV/JSON na E:), także nieudane. Korekta na wielokrotne testy obejmuje **całą** przeszukaną przestrzeń: permutacyjny null dla maksimum statystyki, FDR (Benjamini–Hochberg), współczynnik Sharpe'a z korektą na liczbę prób.
3. **Stabilność w czasie:** kandydat musi działać w co najmniej k z n rozłącznych bloków czasu (np. tygodnie) i w obu połowach discovery, nie tylko średnio. Raportuj efekt per blok.
4. **Cechy point-in-time:** żadnej informacji z po momencie decyzji. Każda nowa cecha dostaje test no-lookahead (wzór: `tests/test_no_lookahead.py`).
5. **Jedna decyzja na token** (pierwszy bar spełniający bezpieczniki, jak w replayu), żeby nie tworzyć autokorelacji wewnątrz tokena. Przedziały ufności bootstrapem klastrowanym po tokenach.
6. **Efekty po kosztach:** raportuj PnL netto z realistycznym kosztem i wrażliwość na koszt. Samo AUC bez PnL nie wystarcza.
7. **Próby obalenia przed uznaniem wzorca:** tasowanie etykiet między tokenami, przesunięcie w czasie, usunięcie top-k tokenów, podział po godzinie doby, kontrola kolinearności z `age_ms`/`n_bars` (czy nowy wzorzec dodaje cokolwiek ponad "tempo").
8. **Złożoność ma karę:** preferuj reguły o dużym wsparciu (min. liczba tokenów), małej liczbie parametrów i spójnym kierunku. Złożony model musi pobić prosty baseline (`age_ms`, tempo) na validation, inaczej odpada.
9. **Z góry ustalona reguła stopu:** ogranicz liczbę rodzin hipotez przed startem; po wyczerpaniu raportuj wynik, nawet negatywny. Nie dokładaj rodzin po obejrzeniu wyników validation.

## Co badać (rodziny hipotez; kolejność sugerowana)

- **A. Baseline i tempo:** `age_ms`, `n_bars`, bary na minutę, SOL do osiągnięcia poziomów, postęp krzywej wiązania. Sprawdź, czy tempo wyjaśnia wszystko.
- **B. Trajektorie wczesnych świeczek:** kształt pierwszych N barów (zwroty, wolumen, przewaga kupna/sprzedaży, liczba unikalnych portfeli) jako sekwencja: klastrowanie trajektorii (k-means/DTW), motywy, a potem czy klaster niesie informację o etykiecie out-of-time.
- **C. Mikrostruktura portfeli:** kupujący w tym samym `slot` (proxy bundle/sniper), koncentracja top-10 liczona ze skumulowanych przepływów, zachowanie twórcy (jeśli jest dostępny), udział wczesnych kupujących.
- **D. Interakcje i reguły:** płytkie drzewa (głębokość ≤4) i reguły z minimalnym wsparciem; porównaj z regresją logistyczną.
- **E. Model challenger:** gradient boosting (LightGBM/XGBoost) trenowany *walk-forward* (okno rozszerzające, przeuczanie co tydzień), z kalibracją i metrykami AUPRC/MCC; ważność przez permutację, nie tylko wbudowaną.
- **F. Warianty wyjścia** (częściowy take-profit, trailing stop, krótszy horyzont, time-stop) na *tych samych* wejściach, jako z góry zapisana siatka z liczeniem prób.
- **G. Meta-labeling:** prosty sygnał wejścia + model decydujący, czy go wziąć i jak ważyć.

## Wynik końcowy, którego oczekuję

1. Magazyn cech point-in-time dla wszystkich kwalifikujących się mintów (macierze na E:, wersjonowany, z naprawą obciętych etykiet) + testy.
2. Rejestr wszystkich prób + raport: które wzorce przeżyły validation, ich efekt netto po kosztach, stabilność per blok.
3. Jedno uruchomienie na sealed final test dla nazwanych kandydatów.
4. Wpis(y) w `docs/DECISIONS.md` (kolejne numery D) z dowodami i ograniczeniami, w stylu istniejących.
5. Jasna rekomendacja: **go / no-go** dla integracji wzorca z botem (nowe cechy, bezpieczniki, próg), z listą rzeczy niezweryfikowanych. Jeśli nic nie przeżyło, powiedz to wprost.

## Pierwsze kroki

1. Przeczytaj dokumenty wymienione wyżej i potwierdź rozumienie w 5–10 zdaniach (bez streszczania całości).
2. Zmierz brakujące fakty: zakres dat per źródło, rozkład liczby mintów na dzień, udział obciętych etykiet, wpływ filtra ≥50 swapów.
3. Zaproponuj konkretny, zamrożony podział stref i rejestr prób, zanim zaczniesz jakąkolwiek eksplorację. Poczekaj na akceptację.
