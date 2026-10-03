"""Prints the passages of Sisal's football betting rules (public PDF) that mention a word, e.g. the card markets.
Run on GitHub (probe: db / rules) when the local network blocks the download. No API request, no key."""
import io
import re
import sys
import urllib.request

from pypdf import PdfReader

URL = "https://www.sisal.it/content/dam/new-dam/italy/canali/sisal-it/doc-pdf/scommesse/info-scommesse/calcio.pdf"


def main(words: list[str]) -> None:
    req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"})
    data = urllib.request.urlopen(req, timeout=120).read()
    pages = PdfReader(io.BytesIO(data)).pages
    print(f"{URL}: {len(data)} byte, {len(pages)} pagine")
    pat = re.compile("|".join(words), re.I)
    for n, page in enumerate(pages, 1):
        text = re.sub(r"[ \t]+", " ", page.extract_text() or "")
        paras = [p.strip() for p in re.split(r"\n(?=[A-Z0-9•\-])", text) if p.strip()]
        for p in paras:
            if pat.search(p):
                print(f"\n[p. {n}] {p[:1500]}")


if __name__ == "__main__":
    main(sys.argv[1:] or ["cartellin", "ammoni", "espuls", "booking"])
