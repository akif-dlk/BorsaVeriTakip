"""Ortam değişkenlerinden ayarları okur."""
import os

from dotenv import load_dotenv

load_dotenv()


def pg_dsn() -> str:
    return (
        f"postgresql://{os.getenv('POSTGRES_USER', 'bvt')}:{os.getenv('POSTGRES_PASSWORD', 'bvt')}"
        f"@{os.getenv('POSTGRES_HOST', 'localhost')}:{os.getenv('POSTGRES_PORT', '5432')}"
        f"/{os.getenv('POSTGRES_DB', 'borsa')}"
    )


# İzlenecek enstrümanlar: 8 BIST hissesi + BIST 100 endeksi, döviz pariteleri, ons altın
FX_PAIRS = {"USDTRY": "USDTRY=X", "EURTRY": "EURTRY=X"}
BIST_TICKERS = ["THYAO.IS", "GARAN.IS", "ASELS.IS", "BIMAS.IS", "KCHOL.IS", "SISE.IS", "TUPRS.IS", "AKBNK.IS"]
BIST_INDEX = "XU100.IS"
GOLD_OUNCE = "GC=F"
TROY_OUNCE_GRAMS = 31.1034768
