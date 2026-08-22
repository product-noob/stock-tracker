"""Company registry: one entry per company the pipeline has been run for.
Adding a new company = adding a line here, then running the scripts with its slug.
"""
from dataclasses import dataclass


@dataclass
class Company:
    slug: str            # directory name under data/
    name: str            # legal name, used in LLM prompts
    nse_symbol: str
    bse_scrip: str
    yahoo_symbol: str    # .NS suffix = NSE series
    aliases: list        # for news queries / identity resolution later


COMPANIES = {
    "tataelxsi": Company(
        slug="tataelxsi",
        name="Tata Elxsi Limited",
        nse_symbol="TATAELXSI",
        bse_scrip="500408",
        yahoo_symbol="TATAELXSI.NS",
        aliases=['"Tata Elxsi"', "TATAELXSI"],
    ),
    "hdfcbank": Company(
        slug="hdfcbank",
        name="HDFC Bank Limited",
        nse_symbol="HDFCBANK",
        bse_scrip="500180",
        yahoo_symbol="HDFCBANK.NS",
        aliases=['"HDFC Bank"', "HDFCBANK"],
    ),
}


def get(slug: str) -> Company:
    return COMPANIES[slug]
