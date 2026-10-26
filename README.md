# Generator Zadań (Streamlit)

Generator zadań z algebry liniowej i liczb zespolonych z platformy Ruda Wiedzy, przeniesiony do jednego
pliku w Streamlit. Nie potrzebuje serwera ani innych plików, wystarczy `generator_streamlit.py`.

## Uruchomienie

```
pip install -r requirements.txt
streamlit run generator_streamlit.py
```

Aplikacja otworzy się w przeglądarce (domyślnie http://localhost:8501). Adres `?zadanie=ID`, np.
`http://localhost:8501/?zadanie=9`, od razu wybiera zadanie o danym numerze.

## Możliwości

- 27 typów zadań w działach: liczby zespolone, macierze, układy równań, przestrzenie liniowe,
  wartości własne i diagonalizacja, rozkłady macierzy.
- Wyszukiwarka zadań, liczba przykładów (1–20) i parametry wspólne dla całego zestawu.
- Wzory w LaTeX, rozwiązania do pokazania lub ukrycia, wykresy obszarów na płaszczyźnie zespolonej.
- Przeliczanie pojedynczego przykładu z własnymi ustawieniami albo całego zestawu.
- Pobieranie: TXT, plik `.tex` (zadania z rozwiązaniami, same zadania albo same rozwiązania; trzy układy
  dokumentu) i rysunki w ZIP jako SVG. PNG wymaga dodatkowo `pip install cairosvg`.
